#!/usr/bin/env python3
"""
dude_db.py — Lee la base de datos de The Dude directamente.

La base del Dude es SQLite, y dentro de la tabla 'objs' cada objeto es un blob
con el formato de mensajes de MikroTik. Descifrado a base de volcados
hexadecimales sobre la base real; se decodifica el 99,6 % de los objetos
consumiendo el blob entero, que es la prueba de que el formato es correcto.

Esto sustituye al export manual del CSV: el monitor se trae la base por FTP y
saca el inventario solo, con nombres, direcciones, mapas y servicios.

FORMATO
    Cabecera: 4 bytes "M2\\x01\\x00"
    Propiedades seguidas: id (3 bytes LE) + tipo (1 byte) + valor

        0x00 falso     0x01 cierto        0x08 u32        0x09 u8
        0x10 u64       0x21 texto (u8+)   0x31 bloque (u8+)
        0x88 lista de u32 (u16 + n*4)     0xa0 lista de textos (u16 + n*(u8+))

PROPIEDADES IDENTIFICADAS
    0xfe0001  id del objeto            0xfe0010  nombre
    Dispositivos (0x101f4x):
        0x101f40  lista de direcciones (u32 en orden inverso)
        0x101f46  usuario               0x101f56  servicios asociados
        0x101f58  notas                 0x101f59  notas 2
    Servicios (0x102eex):
        0x102ee1  dispositivo           0x102ee3  sonda
    Elementos de mapa (0x105dcx):
        0x105dc0  mapa                  0x105dc4  dispositivo
"""

import ipaddress
import os
import sqlite3
import struct
import tempfile
import time
from collections import defaultdict
from ftplib import FTP

MAGIA = b"M2\x01\x00"
SIN_VALOR = {0x00: False, 0x01: True}

P_ID = 0xFE0001
P_NOMBRE = 0xFE0010
P_DIRECCIONES = 0x101F40
P_USUARIO = 0x101F46
P_SERVICIOS = 0x101F56
P_NOTAS = 0x101F58
P_NOTAS2 = 0x101F59
P_SRV_DISPOSITIVO = 0x102EE1
P_SRV_SONDA = 0x102EE3
P_MAPA = 0x105DC0
P_MAPA_DISPOSITIVO = 0x105DC4

FICHEROS_REMOTOS = ["dude/dude.db", "dude/dude.db-wal"]


class ErrorDude(Exception):
    pass


class Incompleto(ErrorDude):
    pass


# --------------------------------------------------------------- decodificar

def decodificar(blob, estricto=True):
    if not blob.startswith(MAGIA):
        raise Incompleto("sin cabecera M2")
    propiedades = {}
    i, n = 4, len(blob)
    while i + 4 <= n:
        ident = blob[i] | (blob[i + 1] << 8) | (blob[i + 2] << 16)
        tipo = blob[i + 3]
        i += 4
        try:
            if tipo in SIN_VALOR:
                valor = SIN_VALOR[tipo]
            elif tipo == 0x08:
                valor = struct.unpack_from("<I", blob, i)[0]; i += 4
            elif tipo == 0x09:
                valor = blob[i]; i += 1
            elif tipo == 0x10:
                valor = struct.unpack_from("<Q", blob, i)[0]; i += 8
            elif tipo == 0x21:
                largo = blob[i]; i += 1
                valor = blob[i:i + largo].decode("utf-8", "replace"); i += largo
            elif tipo == 0x31:
                largo = blob[i]; i += 1
                valor = blob[i:i + largo]; i += largo
            elif tipo == 0x88:
                cuenta = struct.unpack_from("<H", blob, i)[0]; i += 2
                valor = list(struct.unpack_from("<%dI" % cuenta, blob, i))
                i += cuenta * 4
            elif tipo == 0xA0:
                cuenta = struct.unpack_from("<H", blob, i)[0]; i += 2
                valor = []
                for _ in range(cuenta):
                    largo = blob[i]; i += 1
                    valor.append(blob[i:i + largo].decode("utf-8", "replace"))
                    i += largo
            else:
                raise Incompleto("tipo 0x%02x" % tipo)
        except (struct.error, IndexError):
            raise Incompleto("valor cortado (tipo 0x%02x)" % tipo)
        propiedades[ident] = valor

    if estricto and i != n:
        raise Incompleto("sobran %d bytes" % (n - i))
    return propiedades


def entero_a_ip(valor):
    """El Dude guarda la IP como u32 con los octetos al reves."""
    if not isinstance(valor, int) or not 0 < valor <= 0xFFFFFFFF:
        return None
    direccion = ipaddress.IPv4Address(
        int.from_bytes(struct.pack("<I", valor), "big"))
    if str(direccion) in ("0.0.0.0", "255.255.255.255"):
        return None
    return str(direccion)


# ------------------------------------------------------------------ descarga

def _borrar(ruta):
    try:
        os.unlink(ruta)
    except OSError:
        pass


def descargar(host, usuario, password, destino, puerto=21, timeout=60):
    """Trae una copia de la base por FTP. Devuelve la ruta del .db."""
    os.makedirs(destino, exist_ok=True)
    ftp = FTP()
    ftp.connect(host, puerto, timeout=timeout)
    try:
        ftp.login(usuario, password)
        for remoto in FICHEROS_REMOTOS:
            local = os.path.join(destino, os.path.basename(remoto))
            parcial = local + ".parcial"
            try:
                with open(parcial, "wb") as f:
                    ftp.retrbinary("RETR " + remoto, f.write, blocksize=1 << 16)
            except Exception as e:
                _borrar(parcial)
                if remoto.endswith(".db"):
                    raise ErrorDude("no se pudo traer %s: %s" % (remoto, e))
                # El WAL puede no existir. Si nos quedamos con el de la
                # descarga anterior leeriamos una mezcla de dos momentos.
                _borrar(local)
                continue
            os.replace(parcial, local)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    return os.path.join(destino, "dude.db")


# --------------------------------------------------------------------- leer

def _abrir(ruta):
    for uri in ("file:%s?mode=ro" % ruta, "file:%s?immutable=1" % ruta):
        try:
            cx = sqlite3.connect(uri, uri=True, timeout=10)
            cx.row_factory = sqlite3.Row
            cx.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return cx
        except sqlite3.Error:
            continue
    raise ErrorDude("no se puede abrir %s como base SQLite" % ruta)


def leer(ruta):
    """Devuelve (dispositivos, estadisticas) a partir de una copia de la base."""
    cx = _abrir(ruta)
    objetos, fallos = {}, 0
    try:
        for f in cx.execute("SELECT id, obj FROM objs"):
            try:
                objetos[f["id"]] = decodificar(f["obj"] or b"")
            except Incompleto:
                fallos += 1

        # mapas a los que pertenece cada dispositivo
        mapas = defaultdict(list)
        for props in objetos.values():
            if P_MAPA in props and P_MAPA_DISPOSITIVO in props:
                mapa = objetos.get(props[P_MAPA], {}).get(P_NOMBRE)
                if mapa:
                    mapas[props[P_MAPA_DISPOSITIVO]].append(mapa)

        # servicios de cada dispositivo, con el nombre de su sonda
        servicios = defaultdict(list)
        for props in objetos.values():
            if P_SRV_DISPOSITIVO in props:
                sonda = objetos.get(props.get(P_SRV_SONDA), {}).get(P_NOMBRE)
                servicios[props[P_SRV_DISPOSITIVO]].append(
                    sonda or props.get(P_NOMBRE) or "?")

        dispositivos = []
        sin_direccion = 0
        for ident, props in objetos.items():
            if P_DIRECCIONES not in props:
                continue                       # no es un dispositivo
            crudas = props[P_DIRECCIONES]
            if isinstance(crudas, int):
                crudas = [crudas]      # un solo equipo, una sola direccion
            elif not isinstance(crudas, list):
                continue
            direcciones = [entero_a_ip(v) for v in crudas
                           if isinstance(v, int)]
            direcciones = [d for d in direcciones if d]
            if not direcciones:
                sin_direccion += 1
                continue
            notas = " ".join(t for t in (props.get(P_NOTAS),
                                         props.get(P_NOTAS2)) if t)
            dispositivos.append({
                "ip": direcciones[0],
                "nombre": (props.get(P_NOMBRE) or direcciones[0]).strip(),
                "etiquetas": {},
                "meta": {
                    "dude_id": ident,
                    "direcciones": direcciones,
                    "mapas": sorted(set(mapas.get(ident, []))),
                    "servicios": sorted(set(servicios.get(ident, []))),
                    "usuario": props.get(P_USUARIO) or "",
                    "notas": notas.strip(),
                },
            })

        dispositivos.sort(key=lambda e: tuple(int(x) for x in e["ip"].split(".")))
        estadisticas = {
            "objetos": len(objetos),
            "objetos_ilegibles": fallos,
            "dispositivos": len(dispositivos),
            "sin_direccion": sin_direccion,
            "mapas": len({m for lista in mapas.values() for m in lista}),
        }
        return dispositivos, estadisticas
    finally:
        cx.close()


def leer_caidas(ruta, desde=None):
    """Historico de caidas del Dude: [(dude_id, inicio, duracion), ...]."""
    cx = _abrir(ruta)
    try:
        sql = "SELECT deviceID, time, duration FROM outages"
        parametros = ()
        if desde:
            sql += " WHERE time >= ?"
            parametros = (int(desde),)
        try:
            return [(f["deviceID"], f["time"], f["duration"])
                    for f in cx.execute(sql + " ORDER BY time", parametros)]
        except sqlite3.Error as e:
            raise ErrorDude("no se pudo leer el historico de caidas: %s" % e)
    finally:
        cx.close()


# ------------------------------------------------------------------ fachada

def cargar(host, usuario, password, puerto=21, destino=None, conservar=False):
    """Descarga la base y devuelve (dispositivos, estadisticas).

    Sin 'destino' la copia va a un temporal que se borra al terminar. Para
    poder importar el historico despues hay que dar una carpeta fija
    (config.json -> dude.carpeta_copia) o pedir conservar=True.
    """
    temporal = destino is None
    destino = destino or tempfile.mkdtemp(prefix="dude-")
    t0 = time.time()
    try:
        ruta = descargar(host, usuario, password, destino, puerto)
        dispositivos, estadisticas = leer(ruta)
        estadisticas["segundos"] = round(time.time() - t0, 1)
        estadisticas["copia"] = ruta
        return dispositivos, estadisticas
    finally:
        if temporal and not conservar:
            import shutil
            shutil.rmtree(destino, ignore_errors=True)
