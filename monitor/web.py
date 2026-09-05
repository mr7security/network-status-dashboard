#!/usr/bin/env python3
"""
web.py — Servidor HTTP: API JSON y ficheros estaticos.

API (todo bajo /api/v1/):

    GET  estado                 lo que pinta el panel
    GET  serie?horas=24         serie historica agregada
    GET  equipo/<ip>            ficha e historial de un equipo
    GET  informes/caidas        ranking de los que mas caen
    GET  informes/disponibilidad
    POST mantenimiento          {"ip": "...", "horas": 8}
    POST inventario             multipart con el Devices.csv

Las peticiones POST exigen la cabecera X-Monitor: 1. Es lo que impide que una
web ajena lance la peticion desde el navegador de quien tenga el panel abierto
(un multipart o un JSON simple no dispara preflight; una cabecera propia si).
"""

import json
import math
import mimetypes
import os
import posixpath
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import inventario as mod_inventario
from .estado import log

MAX_SUBIDA = 25 * 1024 * 1024
LOCK_IMPORTACION = threading.Lock()


def extraer_fichero_multipart(cuerpo, content_type):
    """Primer fichero de un multipart/form-data. El modulo cgi ya no existe."""
    marca = "boundary="
    if marca not in content_type:
        return None, None
    frontera = content_type.split(marca, 1)[1].split(";")[0].strip().strip('"')
    if not frontera:
        return None, None
    for parte in cuerpo.split(("--" + frontera).encode()):
        if not parte or parte in (b"--\r\n", b"--"):
            continue
        cabecera, _, datos = parte.partition(b"\r\n\r\n")
        if not datos or "filename=" not in cabecera.decode("utf-8", "replace"):
            continue
        texto = cabecera.decode("utf-8", "replace")
        nombre = texto.split("filename=", 1)[1].split("\r\n")[0].strip().strip('"')
        if datos.endswith(b"\r\n"):
            datos = datos[:-2]
        return (nombre or "Devices.csv"), datos
    return None, None


class Manejador(BaseHTTPRequestHandler):
    motor = None
    almacen = None
    config = {}
    estaticos = ""
    server_version = "network-status-dashboard"
    # con HTTP/1.1 el navegador reutiliza la conexion: menos churn de hilos y
    # de conexiones sqlite (siempre mandamos Content-Length, asi que es seguro)
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ------------------------------------------------------------ utilidades

    def _responder(self, codigo, tipo, cuerpo, cache=False):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control",
                         "max-age=300" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(cuerpo)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, codigo, contenido):
        self._responder(codigo, "application/json; charset=utf-8",
                        json.dumps(contenido, ensure_ascii=False).encode())

    def _parametros(self):
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(self.path).query)

    def _entero(self, nombre, defecto, minimo, maximo):
        try:
            valor = int(self._parametros().get(nombre, [defecto])[0])
        except (ValueError, TypeError):
            return defecto
        return max(minimo, min(maximo, valor))

    def _cabecera_propia(self):
        if self.headers.get("X-Monitor") != "1":
            self._json(403, {"error": "Peticion no valida."})
            return False
        sitio = (self.headers.get("Sec-Fetch-Site") or "same-origin").lower()
        if sitio not in ("same-origin", "same-site", "none"):
            self._json(403, {"error": "Peticion de origen ajeno."})
            return False
        return True

    # ------------------------------------------------------------------ GET

    def do_GET(self):
        ruta = self.path.split("?")[0]

        if ruta.startswith("/api/"):
            return self._api_get(ruta)
        if ruta == "/":
            ruta = "/index.html"
        return self._estatico(ruta)

    def _api_get(self, ruta):
        if ruta == "/api/v1/estado":
            with self.motor.lock:
                datos = dict(self.motor.estado)
            datos["permisos"] = {
                "subida_csv": bool(self.config.get("permitir_subida_csv", True)),
                "mantenimiento": bool(self.config.get("permitir_mantenimiento",
                                                      True)),
            }
            return self._json(200, datos)

        if ruta == "/api/v1/serie":
            horas = self._entero("horas", 24, 1, 24 * 90)
            grupo = self._parametros().get("grupo", [None])[0]
            desde = time.time() - horas * 3600
            etiqueta = self.motor.config.get("agrupar_por", "sede")
            puntos = self.almacen.serie(desde, None,
                                        etiqueta if grupo else None, grupo)
            return self._json(200, {"horas": horas, "puntos": puntos})

        if ruta.startswith("/api/v1/equipo/"):
            ip = ruta.rsplit("/", 1)[-1]
            equipos = {e["ip"]: e for e in self.almacen.equipos(True)}
            if ip not in equipos:
                return self._json(404, {"error": "No existe ese equipo."})
            cx = self.almacen.conexion()
            eventos = [dict(f) for f in cx.execute(
                "SELECT nivel, desde, hasta, detalle FROM eventos "
                "WHERE ip = ? ORDER BY desde DESC LIMIT 100", (ip,))]
            return self._json(200, {"equipo": equipos[ip], "eventos": eventos})

        if ruta == "/api/v1/informes/caidas":
            dias = self._entero("dias", 30, 1, 400)
            return self._json(200, {
                "dias": dias,
                "equipos": self.almacen.ranking_caidas(
                    time.time() - dias * 86400,
                    self._entero("limite", 20, 1, 200)),
            })

        if ruta == "/api/v1/informes/disponibilidad":
            dias = self._entero("dias", 30, 1, 400)
            return self._json(200, {
                "dias": dias,
                "equipos": self.almacen.disponibilidad(time.time() - dias * 86400),
            })

        if ruta == "/api/v1/salud":
            return self._json(200, {"ok": True,
                                    "sondeando": self.motor.sondeando})

        return self._json(404, {"error": "No existe ese recurso."})

    def _estatico(self, ruta):
        # normalizamos para que nadie se escape del directorio de estaticos
        limpia = posixpath.normpath(ruta).lstrip("/")
        if limpia.startswith("..") or os.path.isabs(limpia):
            return self._responder(403, "text/plain; charset=utf-8", b"No\n")
        destino = os.path.join(self.estaticos, limpia)
        if not os.path.isfile(destino):
            return self._responder(404, "text/plain; charset=utf-8",
                                   b"No existe\n")
        tipo = mimetypes.guess_type(destino)[0] or "application/octet-stream"
        if tipo.startswith("text/") or tipo.endswith("javascript"):
            tipo += "; charset=utf-8"
        try:
            with open(destino, "rb") as f:
                cuerpo = f.read()
        except OSError:
            return self._responder(500, "text/plain; charset=utf-8", b"Error\n")
        return self._responder(200, tipo, cuerpo,
                               cache=not limpia.endswith(".html"))

    # ----------------------------------------------------------------- POST

    def do_POST(self):
        ruta = self.path.split("?")[0]
        if ruta == "/api/v1/mantenimiento":
            return self._mantenimiento()
        if ruta == "/api/v1/inventario":
            return self._importar_csv()
        return self._json(404, {"error": "No existe ese recurso."})

    def _leer_cuerpo(self, maximo):
        try:
            longitud = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if not 0 < longitud <= maximo:
            return None
        try:
            return self.rfile.read(longitud)
        except (BrokenPipeError, ConnectionResetError):
            return None

    def _mantenimiento(self):
        if not self.config.get("permitir_mantenimiento", True):
            return self._json(403, {"error": "Desactivado en config.json."})
        if not self._cabecera_propia():
            return

        cuerpo = self._leer_cuerpo(4096)
        if cuerpo is None:
            return self._json(400, {"error": "Peticion mal formada."})
        try:
            datos = json.loads(cuerpo)
            if not isinstance(datos, dict):
                raise ValueError("se esperaba un objeto")
            ip = str(datos.get("ip", "")).strip()
            horas = float(datos.get("horas", 0))
            if not math.isfinite(horas) or horas > 24 * 90:
                raise ValueError("duracion no valida")
        except (ValueError, TypeError, AttributeError):
            return self._json(400, {"error": "Peticion mal formada."})

        if not any(e["ip"] == ip for e in self.almacen.equipos()):
            return self._json(404, {"error": "Ese equipo no esta en el "
                                             "inventario."})

        marca = self.almacen.marcar_mantenimiento(ip, horas)
        log("Equipo %s %s" % (ip, "liberado" if marca is None
                              else "en mantenimiento"))
        self.motor.recomponer()          # el panel lo ve al instante
        return self._json(200, {"ok": True, "ip": ip, "marca": marca})

    def _importar_csv(self):
        if not self.config.get("permitir_subida_csv", True):
            return self._json(403, {"error": "Desactivado en config.json."})
        if not self._cabecera_propia():
            return

        cuerpo = self._leer_cuerpo(MAX_SUBIDA)
        if cuerpo is None:
            return self._json(400, {"error": "Fichero ausente o demasiado "
                                             "grande."})
        nombre, datos = extraer_fichero_multipart(
            cuerpo, self.headers.get("Content-Type") or "")
        if not datos:
            return self._json(400, {"error": "No se ha recibido ningun fichero."})
        if not nombre.lower().endswith(".csv"):
            return self._json(400, {"error": "El fichero debe ser un .csv."})
        if b"\x00" in datos[:100000]:
            return self._json(400, {"error": "Parece un fichero binario."})

        try:
            texto = datos.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = datos.decode("latin-1")

        if not LOCK_IMPORTACION.acquire(timeout=1):
            return self._json(429, {"error": "Ya se esta procesando otro "
                                             "fichero."})
        try:
            resultado = mod_inventario.desde_csv_dude(texto)
            equipos = self.motor.reglas.clasificar(resultado["equipos"])
            anteriores = len(self.almacen.equipos())
            forzar = "forzar=1" in (self.path.split("?", 1)[1]
                                    if "?" in self.path else "")
            if anteriores and len(equipos) < anteriores * 0.5 and not forzar:
                return self._json(409, {
                    "confirmar": True,
                    "error": "El inventario pasaria de %d a %d equipos."
                             % (anteriores, len(equipos)),
                })
            altas, bajas = self.almacen.guardar_inventario(equipos, "csv_dude")
            log("Inventario importado: %d equipos (%d altas, %d bajas)"
                % (len(equipos), altas, bajas))
            self.motor.recomponer()
            return self._json(200, {
                "ok": True, "equipos": len(equipos), "altas": altas,
                "bajas": bajas, "descartes": resultado["descartes"],
            })
        except mod_inventario.ErrorInventario as e:
            log("CSV rechazado: %s" % e)
            return self._json(400, {"error": "No es un export valido del Dude: "
                                             "%s." % e})
        except Exception as e:
            log("ERROR importando el CSV: %r" % e)
            return self._json(500, {"error": "No se ha podido procesar el "
                                             "fichero. Mira el log."})
        finally:
            LOCK_IMPORTACION.release()


def servir(config, almacen, motor, estaticos):
    Manejador.config = config
    Manejador.almacen = almacen
    Manejador.motor = motor
    Manejador.estaticos = estaticos
    puerto = config.get("puerto", 8082)
    servidor = ThreadingHTTPServer(("0.0.0.0", puerto), Manejador)
    log("Sirviendo en http://0.0.0.0:%d/" % puerto)
    return servidor
