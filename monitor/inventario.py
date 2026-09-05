#!/usr/bin/env python3
"""
inventario.py — De donde salen los equipos.

Cada fuente devuelve una lista de diccionarios crudos:

    {"ip": "192.0.2.14", "nombre": "Camara nave 3",
     "etiquetas": {...opcional...}, "meta": {...lo que traiga la fuente...}}

Las etiquetas definitivas las pone despues el motor de reglas. Aqui solo se
extrae lo que la fuente sabe, sin interpretarlo.

Fuentes:
    json        lista declarada a mano (util para empezar sin depender de nada)
    csv_dude    export "Devices" del cliente de The Dude
    simulada    inventario de mentira para desarrollar sin red
"""

import csv
import io
import ipaddress
import json
import random


class ErrorInventario(ValueError):
    pass


def _ip_valida(texto):
    try:
        direccion = ipaddress.ip_address(texto)
    except ValueError:
        return None
    if str(direccion) == "0.0.0.0":
        return None
    return str(direccion)


# ------------------------------------------------------------------- JSON

def desde_json(ruta):
    """Fichero con una lista de equipos, o {"equipos": [...]}"""
    try:
        with open(ruta, encoding="utf-8") as f:
            datos = json.load(f)
    except FileNotFoundError:
        return []
    except ValueError as e:
        raise ErrorInventario("%s no es JSON valido: %s" % (ruta, e))

    if isinstance(datos, dict):
        datos = datos.get("equipos") or []
    if not isinstance(datos, list):
        raise ErrorInventario("se esperaba una lista de equipos")

    salida = []
    for i, e in enumerate(datos):
        if not isinstance(e, dict):
            raise ErrorInventario("el equipo %d no es un objeto" % (i + 1))
        ip = _ip_valida(str(e.get("ip", "")).strip())
        if not ip:
            raise ErrorInventario("el equipo %d no tiene una IP valida" % (i + 1))
        salida.append({
            "ip": ip,
            "nombre": (e.get("nombre") or ip).strip(),
            "etiquetas": dict(e.get("etiquetas") or {}),
            "meta": {},
        })
    return salida


# ------------------------------------------------------- CSV de The Dude

COLUMNAS_MINIMAS = {"name", "addresses"}
COLUMNAS_DUDE = {"flag", "name", "addresses", "mac", "type", "maps",
                 "services down", "notes", "dns", "status", "parents"}


def validar_csv_dude(texto):
    """Comprueba que el texto es de verdad un export del Dude."""
    lector = csv.reader(io.StringIO(texto, newline=""))
    try:
        cabecera = next(lector)
    except StopIteration:
        raise ErrorInventario("el fichero esta vacio")
    except csv.Error as e:
        raise ErrorInventario("CSV mal formado (%s)" % e)

    columnas = [c.strip().lower() for c in cabecera]
    faltan = COLUMNAS_MINIMAS - set(columnas)
    if faltan:
        raise ErrorInventario(
            "faltan las columnas %s; exporta desde Devices con Ctrl+A y el "
            "boton csv" % ", ".join(sorted(faltan)))
    reconocidas = sum(1 for c in columnas if c in COLUMNAS_DUDE)
    if reconocidas < len(columnas) * 0.6:
        raise ErrorInventario("las columnas no son las del Dude (%s)"
                              % ", ".join(columnas[:8]))
    return columnas


def desde_csv_dude(texto, incluir_deshabilitados=False):
    """Recibe el CONTENIDO del CSV (ya decodificado), no la ruta."""
    validar_csv_dude(texto)
    lector = csv.DictReader(io.StringIO(texto, newline=""))

    equipos = {}
    descartes = {"deshabilitados": 0, "sin ip": 0, "duplicados": 0}
    nombres = set()

    try:
        for fila in lector:
            # si una fila trae mas columnas que la cabecera, DictReader mete
            # la sobra en la clave None y como una lista
            campos = {k.strip().lower(): (v.strip() if isinstance(v, str) else "")
                      for k, v in fila.items() if k}
            nombre = campos.get("name", "")
            if nombre:
                nombres.add(nombre)

            if "disabled" in campos.get("flag", "").lower() and not incluir_deshabilitados:
                descartes["deshabilitados"] += 1
                continue

            ip = _ip_valida(campos.get("addresses", "").split(",")[0].strip())
            if not ip:
                descartes["sin ip"] += 1
                continue

            mapa = campos.get("maps", "")
            if ip in equipos:
                descartes["duplicados"] += 1
                previo = equipos[ip]
                if mapa and mapa not in previo["meta"]["mapas"]:
                    previo["meta"]["mapas"].append(mapa)
                if len(nombre) > len(previo["nombre"]):
                    previo["nombre"] = nombre
                continue

            equipos[ip] = {
                "ip": ip,
                "nombre": nombre or ip,
                "etiquetas": {},
                "meta": {
                    "mapas": [mapa] if mapa else [],
                    "tipo": campos.get("type", ""),
                    "mac": campos.get("mac", ""),
                },
            }
    except csv.Error as e:
        raise ErrorInventario("CSV mal formado (%s)" % e)

    if len(equipos) < 5:
        raise ErrorInventario("solo se han reconocido %d equipos con IP"
                              % len(equipos))

    return {
        "equipos": sorted(equipos.values(), key=lambda e: e["ip"]),
        "descartes": {k: v for k, v in descartes.items() if v},
        "nombres": sorted(nombres),
    }


def desde_csv_dude_fichero(ruta, incluir_deshabilitados=False):
    with open(ruta, "rb") as f:
        crudo = f.read()
    try:
        texto = crudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = crudo.decode("latin-1")
    return desde_csv_dude(texto, incluir_deshabilitados)


# -------------------------------------------------------------- simulada

PLANTILLAS = [
    ("Switch {n}",        "10.{s}.150.{h}"),
    ("AP {n}",            "10.{s}.151.{h}"),
    ("Camara {n}",        "10.{s}.30.{h}"),
    ("PC oficina {n}",    "10.{s}.12.{h}"),
    ("Servidor {n}",      "10.{s}.210.{h}"),
    ("Bascula {n}",       "10.{s}.15.{h}"),
    ("Core {n}",          "10.{s}.0.{h}"),
]


def simulada(sedes=4, por_sede=40, semilla=7):
    """Inventario de mentira, para desarrollar sin tener la red delante."""
    aleatorio = random.Random(semilla)
    salida = []
    for s in range(1, sedes + 1):
        for i in range(por_sede):
            patron, red = PLANTILLAS[i % len(PLANTILLAS)]
            n = i // len(PLANTILLAS) + 1
            salida.append({
                "ip": red.format(s=s, h=10 + i),
                "nombre": patron.format(n=n) + " S%d" % s,
                "etiquetas": {},
                "meta": {"simulado": True,
                         "vivo": aleatorio.random() > 0.08},
            })
    return salida


# ---------------------------------------------------------------- despacho

def cargar(fuente, **opciones):
    if fuente == "json":
        return desde_json(opciones["ruta"])
    if fuente == "csv_dude":
        return desde_csv_dude_fichero(
            opciones["ruta"],
            opciones.get("incluir_deshabilitados", False))["equipos"]
    if fuente == "simulada":
        return simulada(opciones.get("sedes", 4), opciones.get("por_sede", 40))
    raise ErrorInventario("fuente desconocida: %s" % fuente)
