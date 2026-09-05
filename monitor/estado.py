#!/usr/bin/env python3
"""
estado.py — El motor: sondea, decide niveles y agrupa por etiqueta.

Separado a proposito del servidor web y del almacen. Se le puede pedir un
barrido desde la linea de comandos y ver el resultado, sin levantar nada.

Niveles de un equipo:
    ok             todas sus sondas responden
    aviso          responde al ping pero alguna sonda de servicio falla
    caido          no responde al ping (o su unica sonda falla)
    mantenimiento  marcado a mano; no cuenta como caido

Nivel de un grupo (lo que pinta el semaforo de la tarjeta):
    error   cae algun equipo critico, o el % de activos baja del umbral
    aviso   hay al menos N equipos con incidencia
    ok      el resto
"""

import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import sondas as mod_sondas


def log(mensaje):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), mensaje), flush=True)


class Motor:
    def __init__(self, config, almacen, reglas):
        self.config = config
        self.almacen = almacen
        self.reglas = reglas
        self.lock = threading.Lock()          # protege self.estado
        self.lock_barrido = threading.Lock()  # un barrido a la vez
        self.estado = {"generado": None, "grupos": [], "resumen": {}}
        self.ultimo_sondeo = None             # para recomponer sin sondear
        self.sondeando = False

    # ------------------------------------------------------------- sondeo

    def sondear(self):
        """Ejecuta todas las sondas y devuelve {ip: (nivel, ms, detalle)}."""
        equipos = self.almacen.equipos()
        if not equipos:
            log("AVISO: no hay equipos en el inventario")
            return {}, equipos

        if self.config.get("simulado"):
            return self._sondear_simulado(equipos)

        t0 = time.time()
        con_icmp = [e["ip"] for e in equipos if "icmp" in e["sondas"]]
        vivos = mod_sondas.ping_masivo(
            con_icmp,
            self.config.get("ping_timeout_ms", 800),
            self.config.get("ping_reintentos", 1),
            self.config.get("ping_intervalo_ms", 10))

        # El resto de sondas solo a quien responde al ping: si esta apagado,
        # comprobar su web es tirar segundos a la basura.
        tareas = []
        for e in equipos:
            if "icmp" in e["sondas"] and e["ip"] not in vivos:
                continue
            for nombre in e["sondas"]:
                if nombre != "icmp":
                    tareas.append((e, nombre))

        fallos = defaultdict(list)
        if tareas:
            hilos = self.config.get("hilos", 128)
            with ThreadPoolExecutor(max_workers=hilos) as pool:
                resultados = pool.map(
                    lambda t: (t[0], t[1],
                               mod_sondas.ejecutar(t[1], t[0], self.config)),
                    tareas)
                for equipo, nombre, resultado in resultados:
                    if resultado.vivo is False:
                        fallos[equipo["ip"]].append(
                            resultado.detalle or nombre)

        resultado = {}
        for e in equipos:
            ip = e["ip"]
            usa_icmp = "icmp" in e["sondas"]
            otras = [s for s in e["sondas"] if s != "icmp"]

            if usa_icmp and ip not in vivos:
                resultado[ip] = ("caido", None, "no responde al ping")
            elif fallos.get(ip):
                if not usa_icmp and len(fallos[ip]) == len(otras):
                    resultado[ip] = ("caido", None, "; ".join(fallos[ip])[:120])
                else:
                    resultado[ip] = ("aviso", vivos.get(ip),
                                     "; ".join(fallos[ip])[:120])
            else:
                resultado[ip] = ("ok", vivos.get(ip), "")

        log("Sondeo en %.1fs: %d equipos, %d sondas extra"
            % (time.time() - t0, len(equipos), len(tareas)))
        self.ultimo_sondeo = (resultado, time.time() - t0)
        return resultado, equipos

    def _sondear_simulado(self, equipos):
        """Estados de mentira, estables entre barridos salvo un poco de ruido.

        Sirve para desarrollar el panel sin tener la red delante. Se activa con
        "simulado": true en config.json.
        """
        import hashlib
        import random

        t0 = time.time()
        minuto = int(time.time() // 300)      # cambia algo cada 5 minutos
        resultado = {}
        for e in equipos:
            semilla = hashlib.md5(e["ip"].encode()).hexdigest()
            fijo = int(semilla[:8], 16) / 0xFFFFFFFF
            ruido = random.Random(semilla + str(minuto)).random()
            if fijo < 0.06:
                resultado[e["ip"]] = ("caido", None, "no responde al ping")
            elif fijo < 0.10:
                resultado[e["ip"]] = ("aviso", int(2 + ruido * 20),
                                      "sin web (simulado)")
            elif ruido < 0.01:
                resultado[e["ip"]] = ("caido", None, "no responde al ping")
            else:
                resultado[e["ip"]] = ("ok", int(1 + ruido * 30), "")
        log("Sondeo SIMULADO de %d equipos" % len(equipos))
        self.ultimo_sondeo = (resultado, time.time() - t0)
        return resultado, equipos

    # ------------------------------------------------------------- barrido

    def barrido(self):
        if self.sondeando:
            log("AVISO: barrido anterior aun en curso, salto este ciclo")
            return
        self.sondeando = True
        try:
            crudo, equipos = self.sondear()
            if crudo:
                self.componer(crudo, equipos, guardar=True)
        finally:
            self.sondeando = False

    def recomponer(self):
        """Rehace el estado con el ultimo sondeo, sin tocar la red."""
        if not self.ultimo_sondeo:
            return False
        crudo, _ = self.ultimo_sondeo
        self.componer(crudo, self.almacen.equipos(), guardar=False)
        return True

    # ----------------------------------------------------------- composicion

    def componer(self, crudo, equipos, guardar=True):
        with self.lock_barrido:
            ahora = time.time()
            marcas = self.almacen.mantenimiento()
            agrupar_por = self.config.get("agrupar_por", "sede")
            umbral_rojo = self.config.get("umbral_rojo", 85)
            para_aviso = self.config.get("problemas_para_aviso", 3)

            niveles = {}
            liberar = []
            for e in equipos:
                ip = e["ip"]
                nivel, ms, detalle = crudo.get(ip, ("caido", None, "sin datos"))
                if ip in marcas:
                    if nivel == "ok" and guardar:
                        # responde del todo: la intervencion se da por acabada.
                        # Solo tras un sondeo real; al recomponer con datos
                        # viejos la marca se deshara sola nada mas ponerla.
                        liberar.append(ip)
                        log("Equipo %s vuelve a responder: fuera de "
                            "mantenimiento" % ip)
                    else:
                        nivel = "mantenimiento"
                niveles[ip] = (nivel, ms, detalle)

            if liberar:
                self.almacen.liberar_mantenimiento(liberar)

            if guardar:
                self.almacen.anotar_muestras(
                    ahora, [(ip, n, ms) for ip, (n, ms, _) in niveles.items()])
                self._registrar_eventos(niveles, ahora)

            # los eventos abiertos dicen desde cuando falla cada equipo
            abiertos = self.almacen.eventos_abiertos()

            grupos = self._agrupar(equipos, niveles, marcas, abiertos,
                                   agrupar_por, umbral_rojo, para_aviso, ahora)
            resumen = self._resumir(grupos, equipos)

            with self.lock:
                anterior = self.estado.get("generado")
                self.estado = {
                    "generado": ahora if guardar else (anterior or ahora),
                    "titulo": self.config.get("titulo", "MONITOR"),
                    "agrupar_por": agrupar_por,
                    "grupos": grupos,
                    "resumen": resumen,
                }
            return self.estado

    def _registrar_eventos(self, niveles, ahora):
        abiertos = self.almacen.eventos_abiertos()
        for ip, (nivel, _, detalle) in niveles.items():
            abierto = abiertos.get(ip)
            if nivel == "ok":
                if abierto:
                    self.almacen.cerrar_evento(ip, ahora)
            else:
                if not abierto:
                    self.almacen.abrir_evento(ip, nivel, ahora, detalle or None)
                elif abierto["nivel"] != nivel:
                    self.almacen.cerrar_evento(ip, ahora)
                    self.almacen.abrir_evento(ip, nivel, ahora, detalle or None)

    def _agrupar(self, equipos, niveles, marcas, abiertos, agrupar_por,
                 umbral_rojo, para_aviso, ahora):
        cubos = defaultdict(lambda: {
            "ok": 0, "aviso": 0, "caido": 0, "mantenimiento": 0, "total": 0,
            "criticos_caidos": 0, "problemas": [],
            "desglose": defaultdict(lambda: {"ok": 0, "aviso": 0, "caido": 0,
                                             "mantenimiento": 0}),
        })
        segundo_eje = self.config.get("desglosar_por", "funcion")

        for e in equipos:
            ip = e["ip"]
            nivel, ms, detalle = niveles.get(ip, ("caido", None, ""))
            etiquetas = e["etiquetas"]
            grupo = etiquetas.get(agrupar_por) or "sin clasificar"
            tipo = etiquetas.get(segundo_eje) or "otros"
            critico = str(etiquetas.get("critico", "")).lower() in ("si", "sí",
                                                                    "true", "1")

            c = cubos[grupo]
            c["total"] += 1
            c[nivel] += 1
            c["desglose"][tipo][nivel] += 1
            if nivel != "ok":
                if critico and nivel == "caido":
                    c["criticos_caidos"] += 1
                marca = marcas.get(ip) or {}
                desde = (abiertos.get(ip) or {}).get("desde")
                if not desde and nivel == "mantenimiento":
                    desde = marca.get("desde")
                c["problemas"].append({
                    "ip": ip, "nombre": e["nombre"], "tipo": tipo,
                    "critico": critico, "nivel": nivel,
                    "detalle": detalle, "ms": ms,
                    "desde": desde or ahora,
                    "hasta": marca.get("hasta") if nivel == "mantenimiento"
                             else None,
                    "etiquetas": etiquetas,
                })

        salida = []
        for nombre, c in cubos.items():
            base = c["total"] - c["mantenimiento"]
            pct = (100.0 * c["ok"] / base) if base else 100.0
            problemas = c["caido"] + c["aviso"]
            if c["criticos_caidos"] or pct < umbral_rojo:
                nivel = "error"
            elif problemas >= para_aviso:
                nivel = "aviso"
            else:
                nivel = "ok"
            c["problemas"].sort(key=lambda p: (p["nivel"] == "mantenimiento",
                                               not p["critico"],
                                               p["nivel"] != "caido",
                                               p["nombre"]))
            salida.append({
                "nombre": nombre, "nivel": nivel,
                "total": c["total"], "ok": c["ok"], "aviso": c["aviso"],
                "caido": c["caido"], "mantenimiento": c["mantenimiento"],
                "criticos_caidos": c["criticos_caidos"],
                "porcentaje": round(pct, 1),
                "desglose": {k: dict(v) for k, v in c["desglose"].items()},
                "problemas": c["problemas"][:400],
                "problemas_totales": len(c["problemas"]),
            })

        orden = self.config.get("orden_grupos") or []
        def clave(g):
            try:
                posicion = orden.index(g["nombre"])
            except ValueError:
                posicion = len(orden)
            return (g["nivel"] == "ok", posicion, g["nombre"])
        salida.sort(key=clave)
        return salida

    def _resumir(self, grupos, equipos):
        return {
            "equipos": sum(g["total"] for g in grupos),
            "ok": sum(g["ok"] for g in grupos),
            "aviso": sum(g["aviso"] for g in grupos),
            "caido": sum(g["caido"] for g in grupos),
            "mantenimiento": sum(g["mantenimiento"] for g in grupos),
            "criticos_caidos": sum(g["criticos_caidos"] for g in grupos),
            "grupos": len(grupos),
            "grupos_con_problemas": sum(1 for g in grupos if g["nivel"] != "ok"),
            "duracion_s": round(self.ultimo_sondeo[1], 1)
                          if self.ultimo_sondeo else None,
            "metodo_ping": "fping" if mod_sondas.hay_fping() else "ping del sistema",
            "snmp": mod_sondas.hay_snmp(),
        }

    # --------------------------------------------------------------- bucle

    def bucle(self):
        intervalo = self.config.get("intervalo_segundos", 60)
        ultima_purga = 0
        while True:
            try:
                self.barrido()
                if time.time() - ultima_purga > 3600:
                    self.almacen.purgar()
                    ultima_purga = time.time()
            except Exception as e:
                log("ERROR en el barrido: %r" % e)
            time.sleep(intervalo)
