#!/usr/bin/env python3
"""
sondas.py — Comprobaciones sobre cada equipo.

Cada sonda recibe un equipo y devuelve un Resultado con:
    vivo     True/False/None (None = no aplica o no se pudo comprobar)
    ms       latencia en milisegundos, si la sonda la mide
    detalle  texto corto para mostrar al usuario

Sondas disponibles, tal y como se escriben en reglas.json:

    icmp            ping (en masa con fping, que hace las 2500 en segundos)
    tcp:PUERTO      conexion TCP al puerto indicado
    http            HTTP y, si falla, HTTPS a la raiz
    https           solo HTTPS
    snmp            sysUpTime y sysDescr por SNMP v2c (necesita snmpget)
    tls:PUERTO      certificado: avisa si caduca pronto
    dns:NOMBRE      resuelve un nombre y comprueba que responde

El ICMP es especial: se resuelve para todos los equipos de una vez antes de
lanzar el resto, porque una sola llamada a fping sustituye a miles de procesos.
"""

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone


class Resultado:
    __slots__ = ("vivo", "ms", "detalle")

    def __init__(self, vivo=None, ms=None, detalle=""):
        self.vivo = vivo
        self.ms = ms
        self.detalle = detalle

    def __repr__(self):
        return "Resultado(%r, %r, %r)" % (self.vivo, self.ms, self.detalle)


# ------------------------------------------------------------------ ICMP

def hay_fping():
    return shutil.which("fping") is not None


def ping_masivo(ips, timeout_ms=800, reintentos=1, intervalo_ms=10):
    """Devuelve {ip: ms} de los que responden. Usa fping si esta disponible."""
    if not ips:
        return {}
    if hay_fping():
        return _ping_fping(ips, timeout_ms, reintentos, intervalo_ms)
    return _ping_sistema(ips, timeout_ms)


def _ping_fping(ips, timeout_ms, reintentos, intervalo_ms):
    vivos = {}
    ruta = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("\n".join(ips))
            ruta = f.name
        # -C 1 imprime la latencia por equipo; la salida va a stderr
        cmd = ["fping", "-q", "-C", str(max(1, reintentos)),
               "-t", str(timeout_ms), "-i", str(intervalo_ms), "-f", ruta]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=max(120, len(ips) // 10 + 60))
        salida = (p.stderr or "") + (p.stdout or "")
        for linea in salida.splitlines():
            # formato: "10.0.0.1 : 0.53 1.02" o "10.0.0.2 : - -"
            if " : " not in linea:
                continue
            ip, _, tiempos = linea.partition(" : ")
            ip = ip.strip()
            for t in tiempos.split():
                if t != "-":
                    try:
                        vivos[ip] = int(round(float(t)))
                    except ValueError:
                        vivos[ip] = None
                    break
        # Si fping no pudo ni empezar (permisos ICMP, argumento invalido) no
        # hay ni una linea util: mejor caer al ping del sistema que dar por
        # caidos a los 2500 equipos en silencio.
        if not vivos and p.returncode >= 2:
            raise OSError("fping devolvio %d: %s"
                          % (p.returncode, (p.stderr or "").strip()[:200]))
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return _ping_sistema(ips, timeout_ms)
    finally:
        if ruta:
            try:
                os.unlink(ruta)
            except OSError:
                pass
    return vivos


def _ping_uno(ip, timeout_ms):
    segundos = max(1, int(round(timeout_ms / 1000.0)))
    inicio = time.time()
    try:
        p = subprocess.run(["ping", "-n", "-c", "1", "-W", str(segundos), ip],
                           capture_output=True, timeout=segundos + 2)
        if p.returncode == 0:
            return int((time.time() - inicio) * 1000)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _ping_sistema(ips, timeout_ms, hilos=128):
    vivos = {}
    with ThreadPoolExecutor(max_workers=hilos) as pool:
        for ip, ms in zip(ips, pool.map(lambda i: _ping_uno(i, timeout_ms), ips)):
            if ms is not None:
                vivos[ip] = ms
    return vivos


# ------------------------------------------------------------------- TCP

def sonda_tcp(ip, puerto, timeout=3):
    inicio = time.time()
    try:
        with socket.create_connection((ip, int(puerto)), timeout=timeout):
            ms = int((time.time() - inicio) * 1000)
            return Resultado(True, ms, "puerto %s abierto" % puerto)
    except socket.timeout:
        return Resultado(False, None, "puerto %s sin respuesta" % puerto)
    except OSError as e:
        return Resultado(False, None, "puerto %s: %s" % (puerto, e.strerror or e))


# ------------------------------------------------------------------ HTTP

def _contexto_tls():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def sonda_http(ip, timeout=3, solo_https=False):
    import urllib.error
    import urllib.request

    urls = ["https://%s/" % ip] if solo_https else ["http://%s/" % ip,
                                                    "https://%s/" % ip]
    ultimo = ""
    for url in urls:
        inicio = time.time()
        try:
            peticion = urllib.request.Request(
                url, method="GET", headers={"User-Agent": "network-status-dashboard"})
            ctx = _contexto_tls() if url.startswith("https") else None
            with urllib.request.urlopen(peticion, timeout=timeout, context=ctx):
                ms = int((time.time() - inicio) * 1000)
                return Resultado(True, ms, "web responde")
        except urllib.error.HTTPError as e:
            # 401/403 tambien significa que el servicio esta vivo
            ms = int((time.time() - inicio) * 1000)
            return Resultado(True, ms, "web responde (%s)" % e.code)
        except Exception as e:
            ultimo = type(e).__name__
    return Resultado(False, None, "sin web (%s)" % ultimo)


# ------------------------------------------------------------------ SNMP

OID_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_DESCR = "1.3.6.1.2.1.1.1.0"


def hay_snmp():
    return shutil.which("snmpget") is not None


def sonda_snmp(ip, comunidad="public", timeout=2, reintentos=0):
    if not hay_snmp():
        return Resultado(None, None, "snmpget no instalado")
    cmd = ["snmpget", "-v2c", "-c", comunidad, "-t", str(timeout),
           "-r", str(reintentos), "-Ovq", ip, OID_UPTIME, OID_DESCR]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout * (reintentos + 1) + 3)
    except (subprocess.TimeoutExpired, OSError):
        return Resultado(False, None, "SNMP sin respuesta")
    if p.returncode != 0:
        lineas = (p.stderr or "").strip().splitlines()
        return Resultado(False, None,
                         "SNMP: %s" % (lineas[0][:80] if lineas else "sin respuesta"))
    lineas = [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]
    uptime = lineas[0] if lineas else ""
    descripcion = lineas[1][:60] if len(lineas) > 1 else ""
    return Resultado(True, None, ("%s · %s" % (uptime, descripcion)).strip(" ·"))


# -------------------------------------------------------------------- TLS

def sonda_tls(ip, puerto=443, timeout=4, dias_aviso=21):
    """Comprueba el certificado. Ojo: con verify_mode=CERT_NONE Python NO
    devuelve las fechas del certificado, asi que primero se intenta con
    verificacion (que es la unica forma de leer notAfter) y, si el equipo
    lleva un autofirmado, se cae a la comprobacion simple de que hay TLS."""
    cert = None
    verificado = False
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        with socket.create_connection((ip, int(puerto)), timeout=timeout) as s, \
                ctx.wrap_socket(s) as tls:
            cert = tls.getpeercert()
            verificado = True
    except ssl.SSLError:
        pass                       # autofirmado o CA desconocida: reintentamos
    except Exception as e:
        return Resultado(False, None, "sin TLS (%s)" % type(e).__name__)

    if not verificado:
        try:
            with socket.create_connection((ip, int(puerto)),
                                          timeout=timeout) as s, _contexto_tls().wrap_socket(s):
                return Resultado(True, None,
                                 "TLS activo (certificado no verificable)")
        except Exception as e:
            return Resultado(False, None, "sin TLS (%s)" % type(e).__name__)

    caduca = (cert or {}).get("notAfter")
    if not caduca:
        return Resultado(True, None, "TLS activo")
    try:
        fecha = datetime.strptime(caduca, "%b %d %H:%M:%S %Y %Z")
        fecha = fecha.replace(tzinfo=timezone.utc)
    except ValueError:
        return Resultado(True, None, "TLS activo")
    dias = (fecha - datetime.now(timezone.utc)).days
    if dias < 0:
        return Resultado(False, None, "certificado caducado hace %d dias" % -dias)
    if dias <= dias_aviso:
        return Resultado(False, None, "certificado caduca en %d dias" % dias)
    return Resultado(True, None, "certificado valido %d dias" % dias)


# -------------------------------------------------------------------- DNS

def sonda_dns(nombre, timeout=3):
    # No se toca socket.setdefaulttimeout: es global al proceso y pisaria el
    # timeout de los demas hilos. getaddrinfo lo ignora de todas formas.
    inicio = time.time()
    try:
        socket.getaddrinfo(nombre, None)
        return Resultado(True, int((time.time() - inicio) * 1000), "resuelve")
    except socket.gaierror as e:
        return Resultado(False, None, "no resuelve (%s)" % e.strerror)
    except OSError as e:
        return Resultado(False, None, "DNS: %s" % e)


# ---------------------------------------------------------------- despacho

def ejecutar(nombre_sonda, equipo, config):
    """Ejecuta una sonda por su nombre en reglas.json ('tcp:443', 'http'...)."""
    nombre, _, argumento = nombre_sonda.partition(":")
    ip = equipo["ip"]
    tiempos = config.get("timeouts") or {}

    if nombre == "tcp":
        return sonda_tcp(ip, argumento or 80, tiempos.get("tcp", 3))
    if nombre == "http":
        return sonda_http(ip, tiempos.get("http", 3))
    if nombre == "https":
        return sonda_http(ip, tiempos.get("http", 3), solo_https=True)
    if nombre == "snmp":
        return sonda_snmp(ip, config.get("comunidad_snmp", "public"),
                          tiempos.get("snmp", 2))
    if nombre == "tls":
        return sonda_tls(ip, argumento or 443, tiempos.get("tls", 4),
                         config.get("dias_aviso_certificado", 21))
    if nombre == "dns":
        return sonda_dns(argumento or equipo.get("nombre") or ip,
                         tiempos.get("dns", 3))
    if nombre == "icmp":
        return Resultado(None, None, "")     # lo resuelve el ping masivo
    return Resultado(None, None, "sonda desconocida: %s" % nombre_sonda)
