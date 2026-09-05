#!/usr/bin/env python3
"""
explorar_db_dude.py — Se trae la base de datos del Dude por FTP y mira que hay.

Los ficheros dude.db-wal y dude.db-shm delatan que la base del Dude es SQLite,
asi que se puede leer directamente: adios al export manual del CSV.

Este script SOLO LEE. Descarga una copia, la abre en modo solo lectura y
enseña las tablas, sus columnas y unas filas de muestra, buscando donde estan
los dispositivos, sus direcciones y los mapas.

Uso:

    python3 explorar_db_dude.py --host 192.0.2.10 --user USUARIO

La contrasena se pide por teclado. Hace falta que el usuario tenga la politica
'ftp' en RouterOS (el grupo 'read' trae !ftp):

    /user group add name=lectura-ftp policy=read,ftp,winbox,api
    /user set USUARIO group=lectura-ftp

Opciones:
    --salida /tmp/dude          donde dejar la copia
    --filas 3                   filas de muestra por tabla
    --tabla NOMBRE              vuelca esa tabla entera a CSV y sale
"""

import argparse
import csv
import getpass
import os
import sqlite3
import sys
import time
from ftplib import FTP, error_perm

FICHEROS = ["dude/dude.db", "dude/dude.db-wal"]

# Palabras que delatan a las tablas que nos interesan
INTERESANTES = ("device", "address", "map", "service", "probe", "network",
                "link", "notification", "type", "group", "obj")


def descargar(host, usuario, password, destino, puerto=21):
    os.makedirs(destino, exist_ok=True)
    ftp = FTP()
    print("Conectando a %s:%d ..." % (host, puerto))
    ftp.connect(host, puerto, timeout=30)
    ftp.login(usuario, password)
    print("  dentro como %s" % usuario)

    traidos = []
    for remoto in FICHEROS:
        local = os.path.join(destino, os.path.basename(remoto))
        try:
            tamano = ftp.size(remoto)
        except error_perm:
            tamano = None
        print("  bajando %s%s ..."
              % (remoto, " (%.1f MiB)" % (tamano / 1048576.0) if tamano else ""))
        t0 = time.time()
        try:
            with open(local, "wb") as f:
                ftp.retrbinary("RETR " + remoto, f.write, blocksize=1 << 16)
        except error_perm as e:
            print("    no se pudo: %s" % e)
            if remoto.endswith(".db"):
                ftp.quit()
                sys.exit(1)
            continue
        print("    %.1f MiB en %.1fs"
              % (os.path.getsize(local) / 1048576.0, time.time() - t0))
        traidos.append(local)
    ftp.quit()
    return traidos


def abrir(ruta):
    """Abre en solo lectura. Si el WAL da guerra, prueba en modo inmutable."""
    for uri in ("file:%s?mode=ro" % ruta, "file:%s?immutable=1" % ruta):
        try:
            cx = sqlite3.connect(uri, uri=True, timeout=10)
            cx.row_factory = sqlite3.Row
            cx.execute("SELECT count(*) FROM sqlite_master").fetchone()
            print("Abierta con %s" % uri.split("?")[1])
            return cx
        except sqlite3.Error as e:
            print("  %s -> %s" % (uri.split("?")[1], e))
    print("No se ha podido abrir la base. Puede que no sea SQLite, o que el "
          "WAL este a medias: prueba a bajarla de nuevo.")
    sys.exit(1)


def explorar(cx, filas_muestra):
    tablas = [f["name"] for f in cx.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("\n%d tablas\n" % len(tablas))

    resumen = []
    for tabla in tablas:
        try:
            n = cx.execute("SELECT count(*) FROM [%s]" % tabla).fetchone()[0]
        except sqlite3.Error:
            n = -1
        columnas = [c["name"] for c in cx.execute("PRAGMA table_info([%s])" % tabla)]
        resumen.append((tabla, n, columnas))

    print("%-34s %8s  %s" % ("TABLA", "FILAS", "COLUMNAS"))
    print("-" * 110)
    for tabla, n, _columnas in sorted(resumen, key=lambda x: -x[1]):
        print("%-34s %8d  %s" % (tabla[:34], n, ", ".join(columnas)[:70]))

    print("\n\n===== MUESTRAS DE LAS TABLAS QUE PINTAN UTILES =====")
    for tabla, n, _columnas in sorted(resumen, key=lambda x: -x[1]):
        if n <= 0:
            continue
        if not any(p in tabla.lower() for p in INTERESANTES):
            continue
        print("\n--- %s (%d filas) ---" % (tabla, n))
        try:
            for fila in cx.execute("SELECT * FROM [%s] LIMIT %d"
                                   % (tabla, filas_muestra)):
                trozos = []
                for clave in fila:
                    valor = fila[clave]
                    if isinstance(valor, bytes):
                        valor = "<%d bytes>" % len(valor)
                    texto = str(valor)
                    if texto and texto != "None" and len(texto) < 60:
                        trozos.append("%s=%s" % (clave, texto))
                print("   " + " | ".join(trozos[:12]))
        except sqlite3.Error as e:
            print("   error: %s" % e)


def volcar(cx, tabla, destino):
    ruta = os.path.join(destino, "%s.csv" % tabla.replace("/", "_"))
    cursor = cx.execute("SELECT * FROM [%s]" % tabla)
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        escritor = csv.writer(f)
        escritor.writerow([d[0] for d in cursor.description])
        for fila in cursor:
            escritor.writerow(["<binario>" if isinstance(v, bytes) else v
                               for v in fila])
    print("Volcada %s en %s" % (tabla, ruta))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--puerto", type=int, default=21)
    ap.add_argument("--user", required=True)
    ap.add_argument("--salida", default="/tmp/dude")
    ap.add_argument("--filas", type=int, default=3)
    ap.add_argument("--tabla", help="vuelca esa tabla a CSV y sale")
    ap.add_argument("--sin-descargar", action="store_true",
                    help="usa la copia que ya haya en --salida")
    args = ap.parse_args()

    if not args.sin_descargar:
        password = getpass.getpass("Contrasena de %s en %s: "
                                   % (args.user, args.host))
        descargar(args.host, args.user, password, args.salida, args.puerto)

    ruta = os.path.join(args.salida, "dude.db")
    if not os.path.isfile(ruta):
        print("No encuentro %s" % ruta)
        sys.exit(1)

    with open(ruta, "rb") as f:
        cabecera = f.read(16)
    if not cabecera.startswith(b"SQLite format 3"):
        print("OJO: el fichero NO empieza por 'SQLite format 3'.")
        print("Los primeros bytes son: %r" % cabecera)
        sys.exit(1)
    print("Confirmado: es una base SQLite.\n")

    cx = abrir(ruta)
    if args.tabla:
        volcar(cx, args.tabla, args.salida)
    else:
        explorar(cx, args.filas)
    cx.close()


if __name__ == "__main__":
    main()
