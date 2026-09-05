#!/usr/bin/env python3
"""
parsear_dude.py — Decodifica los objetos binarios de la base del Dude.

FORMATO (deducido a base de volcados hexadecimales)

    Cabecera: 4 bytes  "M2\\x01\\x00"
    Despues, una tira de propiedades:

        id     3 bytes little-endian
        tipo   1 byte
        valor  segun el tipo:

            0x00  sin valor (falso)
            0x01  sin valor (verdadero)
            0x08  4 bytes            entero
            0x09  1 byte             entero corto
            0x10  8 bytes            entero largo
            0x21  1 byte de longitud + texto
            0x31  1 byte de longitud + bloque binario (fuentes, iconos...)
            0x88  2 bytes de cuenta  + esa cuenta de enteros de 4 bytes
            0xa0  2 bytes de cuenta  + esa cuenta de (longitud + texto)

    El bit 0x80 marca las listas, igual que 0x08 -> 0x88.

Propiedades ya identificadas:
    0xfe0001  id del propio objeto
    0xfe0010  nombre

Uso:

    python3 parsear_dude.py --resumen
    python3 parsear_dude.py --dispositivos
    python3 parsear_dude.py --id 106255
    python3 parsear_dude.py --buscar 10.100
    python3 parsear_dude.py --ips           # busca donde se guardan las direcciones
"""

import argparse
import ipaddress
import sqlite3
import struct
from collections import Counter, defaultdict

MAGIA = b"M2\x01\x00"
SIN_VALOR = {0x00: False, 0x01: True}

PROP_ID = 0xFE0001
PROP_NOMBRE = 0xFE0010


class Incompleto(Exception):
    pass


class Bloque(bytes):
    """Bloque binario (tipo 0x31). Se muestra por su texto si lo tiene."""
    def __repr__(self):
        texto = "".join(chr(b) if 32 <= b < 127 else "" for b in self)
        return "<bloque %d B%s>" % (len(self), (" '%s'" % texto) if texto else "")


def decodificar(blob, estricto=True):
    if not blob.startswith(MAGIA):
        raise Incompleto("sin la cabecera M2")
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
                valor = Bloque(blob[i:i + largo]); i += largo
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
                raise Incompleto("tipo 0x%02x en el byte %d" % (tipo, i - 1))
        except (struct.error, IndexError):
            raise Incompleto("valor cortado en el byte %d (tipo 0x%02x)"
                             % (i, tipo))
        propiedades[ident] = valor

    if estricto and i != n:
        raise Incompleto("sobran %d bytes al final" % (n - i))
    return propiedades


def como_ip(valor):
    if not isinstance(valor, int) or not 0 < valor <= 0xFFFFFFFF:
        return None
    for crudo in (struct.pack("<I", valor), struct.pack(">I", valor)):
        d = ipaddress.IPv4Address(int.from_bytes(crudo, "big"))
        if d.is_private and not d.is_loopback and not d.is_link_local \
                and not str(d).endswith(".0"):
            return str(d)
    return None


def cargar(ruta):
    cx = sqlite3.connect("file:%s?mode=ro" % ruta, uri=True, timeout=10)
    cx.row_factory = sqlite3.Row
    objetos, fallos = {}, Counter()
    for f in cx.execute("SELECT id, obj FROM objs"):
        try:
            objetos[f["id"]] = decodificar(f["obj"] or b"")
        except Incompleto as e:
            fallos[str(e).split(" en el byte")[0]] += 1
    return cx, objetos, fallos


def texto_valor(valor, objetos=None):
    if isinstance(valor, list):
        if objetos:
            nombres = [objetos[v].get(PROP_NOMBRE) for v in valor[:8]
                       if isinstance(v, int) and v in objetos]
            nombres = [n for n in nombres if n]
            if nombres:
                return "%s  -> %s" % (valor[:8], ", ".join(nombres))
        return str(valor[:12])
    return str(valor)[:70]


def mostrar(ident, props, objetos=None):
    nombre = props.get(PROP_NOMBRE, "")
    print("\n=== objeto %d  %s  (%d propiedades) ==="
          % (ident, ("'%s'" % nombre) if nombre else "", len(props)))
    for prop in sorted(props):
        valor = props[prop]
        extra = ""
        if isinstance(valor, int) and not isinstance(valor, bool):
            ip = como_ip(valor)
            if ip:
                extra = "   <- %s" % ip
            elif objetos and valor in objetos:
                otro = objetos[valor].get(PROP_NOMBRE)
                if otro:
                    extra = "   -> '%s'" % otro
        print("  0x%06x  %-58s%s" % (prop, texto_valor(valor, objetos), extra))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/tmp/dude/dude.db")
    ap.add_argument("--id", type=int)
    ap.add_argument("--resumen", action="store_true")
    ap.add_argument("--dispositivos", action="store_true")
    ap.add_argument("--ips", action="store_true")
    ap.add_argument("--buscar")
    ap.add_argument("--limite", type=int, default=8)
    args = ap.parse_args()

    cx, objetos, fallos = cargar(args.db)
    total = len(objetos) + sum(fallos.values())
    print("Decodificados %d de %d objetos (%.1f%%)"
          % (len(objetos), total, 100.0 * len(objetos) / max(1, total)))
    for motivo, n in fallos.most_common(6):
        print("   falla: %-46s %5d" % (motivo, n))

    if args.id is not None:
        if args.id in objetos:
            mostrar(args.id, objetos[args.id], objetos)
        else:
            print("El objeto %d no se ha podido decodificar" % args.id)
        return

    if args.buscar:
        aguja = args.buscar.lower()
        vistos = 0
        for ident, props in sorted(objetos.items()):
            if any(isinstance(v, str) and aguja in v.lower()
                   for v in props.values()):
                mostrar(ident, props, objetos)
                vistos += 1
                if vistos >= args.limite:
                    break
        print("\n%d objetos mostrados" % vistos)
        return

    if args.dispositivos:
        ids = [f[0] for f in cx.execute(
            "SELECT deviceID, count(*) n FROM outages GROUP BY deviceID "
            "ORDER BY n DESC LIMIT ?", (args.limite,))]
        print("\nLos %d dispositivos con mas caidas: %s" % (len(ids), ids))
        for ident in ids:
            if ident in objetos:
                mostrar(ident, objetos[ident], objetos)
            else:
                print("\n  el objeto %d no se pudo decodificar" % ident)
        return

    if args.ips:
        # que propiedad guarda direcciones: buscamos enteros que sean IPs
        candidatas = Counter()
        ejemplos = defaultdict(list)
        for ident, props in objetos.items():
            for prop, valor in props.items():
                if isinstance(valor, bool):
                    continue
                if isinstance(valor, int):
                    ip = como_ip(valor)
                    if ip:
                        candidatas[prop] += 1
                        if len(ejemplos[prop]) < 4:
                            ejemplos[prop].append((ident, ip))
                elif isinstance(valor, list) and valor:
                    ips = [como_ip(v) for v in valor if isinstance(v, int)]
                    ips = [x for x in ips if x]
                    if ips:
                        candidatas[prop] += 1
                        if len(ejemplos[prop]) < 4:
                            ejemplos[prop].append((ident, ips[:3]))
        print("\nPropiedades que contienen algo con pinta de IP privada:")
        for prop, n in candidatas.most_common(15):
            print("  0x%06x  %5d objetos   %s"
                  % (prop, n, ejemplos[prop][:3]))
        return

    # --- resumen
    uso = defaultdict(Counter)
    ejemplos = {}
    for props in objetos.values():
        for prop, valor in props.items():
            tipo = ("bool" if isinstance(valor, bool) else
                    "texto" if isinstance(valor, str) else
                    "lista" if isinstance(valor, list) else
                    "bloque" if isinstance(valor, Bloque) else "entero")
            uso[prop][tipo] += 1
            if prop not in ejemplos and valor not in (0, "", False, True, []):
                ejemplos[prop] = valor
    print("\n%-10s %7s  %-20s %s" % ("PROPIEDAD", "VECES", "TIPOS", "EJEMPLO"))
    print("-" * 100)
    for prop, tipos in sorted(uso.items(), key=lambda x: -sum(x[1].values())):
        if sum(tipos.values()) < 20:
            continue
        ej = ejemplos.get(prop, "")
        ip = como_ip(ej) if isinstance(ej, int) else None
        print("0x%06x %7d  %-20s %s%s"
              % (prop, sum(tipos.values()),
                 ",".join("%s:%d" % t for t in tipos.most_common(2)),
                 str(ej)[:44], ("   (¿%s?)" % ip) if ip else ""))


if __name__ == "__main__":
    main()
