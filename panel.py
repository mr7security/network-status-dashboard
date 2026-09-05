#!/usr/bin/env python3
"""
panel.py — Punto de entrada del monitor. Subcomandos:

    servir       arranca el sondeo y el servidor web (lo que usa systemd)
    sondear      un barrido y a la pantalla, sin levantar nada
    dude         trae el inventario de la base del Dude por FTP (automatico)
    importar     carga un inventario (CSV del Dude o JSON) en la base
    reglas       muestra como quedaria clasificado el inventario
    simular      genera un inventario de mentira para desarrollar sin red
    informe      disponibilidad y ranking de caidas

Ejemplos:

    python3 panel.py simular --sedes 5 --por-sede 60
    python3 panel.py sondear --detalle 10
    python3 panel.py importar Devices.csv
    python3 panel.py reglas --limite 30
    python3 panel.py servir
"""

import argparse
import copy
import json
import os
import sys
import threading
import time
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from monitor import dude_db as mod_dude  # noqa: E402
from monitor import inventario as mod_inventario  # noqa: E402
from monitor import web as mod_web  # noqa: E402
from monitor.almacen import Almacen  # noqa: E402
from monitor.estado import Motor, log  # noqa: E402
from monitor.reglas import ErrorReglas, Reglas  # noqa: E402

CONFIG_POR_DEFECTO = {
    "puerto": 8082,
    "titulo": "MONITOR DE RED",
    "intervalo_segundos": 60,
    "hilos": 128,
    "ping_timeout_ms": 800,
    "ping_reintentos": 1,
    "ping_intervalo_ms": 10,
    "timeouts": {"tcp": 3, "http": 3, "snmp": 2, "tls": 4, "dns": 3},
    "comunidad_snmp": "public",
    "dias_aviso_certificado": 21,
    "agrupar_por": "sede",
    "desglosar_por": "funcion",
    "orden_grupos": [],
    "umbral_rojo": 85,
    "problemas_para_aviso": 3,
    "dias_muestras": 60,
    "dias_eventos": 400,
    "permitir_subida_csv": True,
    "permitir_mantenimiento": True,
    # Inventario automatico desde la base del Dude, por FTP. Con host vacio
    # queda desactivado y el inventario se carga a mano.
    "dude": {"host": "", "puerto": 21, "usuario": "", "password": "",
             "horas_refresco": 24, "carpeta_copia": ""},
}


def cargar_config(directorio):
    # copia profunda: si no, escribir en config["dude"] mutaria la constante
    config = copy.deepcopy(CONFIG_POR_DEFECTO)
    ruta = os.path.join(directorio, "config.json")
    try:
        with open(ruta, encoding="utf-8") as f:
            usuario = json.load(f)
        for clave, valor in usuario.items():
            if clave.startswith("_"):        # comentarios en el JSON
                continue
            if isinstance(valor, dict) and isinstance(config.get(clave), dict):
                config[clave].update(valor)
            else:
                config[clave] = valor
    except FileNotFoundError:
        log("AVISO: no hay config.json, uso los valores por defecto")
    except ValueError as e:
        log("ERROR: config.json no es JSON valido (%s); uso los valores por "
            "defecto" % e)
    return config


def preparar(args):
    config = cargar_config(args.dir)
    almacen = Almacen(os.path.join(args.dir, "monitor.db"),
                      config["dias_muestras"], config["dias_eventos"])
    try:
        reglas = Reglas.desde_fichero(os.path.join(args.dir, "reglas.json"))
    except ErrorReglas as e:
        log("ERROR en reglas.json: %s" % e)
        sys.exit(2)
    return config, almacen, reglas, Motor(config, almacen, reglas)


# --------------------------------------------------- inventario desde el Dude

def refrescar_desde_dude(config, almacen, reglas, ruta_local=None):
    """Trae la base del Dude y actualiza el inventario. Devuelve el resumen."""
    cfg = config.get("dude") or {}
    if ruta_local:
        dispositivos, estadisticas = mod_dude.leer(ruta_local)
    else:
        if not cfg.get("host") or not cfg.get("usuario"):
            raise mod_dude.ErrorDude(
                "falta el bloque 'dude' (host, usuario, password) en config.json")
        dispositivos, estadisticas = mod_dude.cargar(
            cfg["host"], cfg["usuario"], cfg.get("password", ""),
            cfg.get("puerto", 21),
            destino=cfg.get("carpeta_copia") or None,
            conservar=bool(cfg.get("carpeta_copia")))

    if len(dispositivos) < 5:
        raise mod_dude.ErrorDude("solo se han leido %d dispositivos; no se "
                                 "toca el inventario" % len(dispositivos))

    # el mapa del Dude entra como etiqueta, para poder agrupar por el
    for d in dispositivos:
        mapas = d["meta"].get("mapas") or []
        if mapas:
            d["etiquetas"]["mapa"] = mapas[0]

    equipos = reglas.clasificar(dispositivos)
    altas, bajas = almacen.guardar_inventario(equipos, "dude_db")
    estadisticas.update({"altas": altas, "bajas": bajas,
                         "equipos": len(equipos)})
    log("Inventario del Dude: %d equipos (%d altas, %d bajas) en %s s"
        % (len(equipos), altas, bajas, estadisticas.get("segundos", "?")))
    return estadisticas


def importar_historico(almacen, ruta_db, dias=None):
    """Vuelca las caidas historicas del Dude a la tabla de eventos.

    El Dude las identifica por su propio id de dispositivo, asi que hay que
    traducirlas a IP con lo que guardamos en meta['dude_id'].
    """
    por_dude_id = {}
    for e in almacen.equipos(incluir_inactivos=True):
        dude_id = (e.get("meta") or {}).get("dude_id")
        if dude_id is not None:
            por_dude_id[dude_id] = e["ip"]
    if not por_dude_id:
        raise mod_dude.ErrorDude("el inventario no viene del Dude; ejecuta "
                                 "antes 'panel.py dude'")

    desde = (time.time() - dias * 86400) if dias else None
    tramos, huerfanos = [], 0
    for dude_id, inicio, duracion in mod_dude.leer_caidas(ruta_db, desde):
        ip = por_dude_id.get(dude_id)
        if not ip:
            huerfanos += 1
            continue
        tramos.append((ip, float(inicio), float(duracion or 0)))
    almacen.importar_eventos(tramos)
    return {"importadas": len(tramos), "de_equipos_borrados": huerfanos}


def bucle_dude(config, almacen, reglas, motor):
    horas = (config.get("dude") or {}).get("horas_refresco", 24)
    if not horas or not (config.get("dude") or {}).get("host"):
        return
    time.sleep(120)          # deja que arranque el sondeo tranquilo
    while True:
        try:
            refrescar_desde_dude(config, almacen, reglas)
            motor.recomponer()
        except Exception as e:
            log("ERROR trayendo el inventario del Dude: %s" % e)
        time.sleep(horas * 3600)


# ------------------------------------------------------------- subcomandos

def cmd_servir(args):
    config, almacen, reglas, motor = preparar(args)
    if not almacen.equipos():
        log("No hay inventario. Usa 'dude', 'importar' o 'simular' antes de "
            "servir.")
        sys.exit(1)

    threading.Thread(target=motor.bucle, daemon=True).start()
    threading.Thread(target=bucle_dude,
                     args=(config, almacen, reglas, motor), daemon=True).start()
    servidor = mod_web.servir(config, almacen, motor,
                              os.path.join(BASE, "static"))
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        log("Parando.")


def cmd_sondear(args):
    config, almacen, reglas, motor = preparar(args)
    motor.barrido()
    with motor.lock:
        estado = motor.estado
    r = estado.get("resumen", {})
    print("\n%-28s %6s %6s %6s %6s %6s" % ("GRUPO", "TOTAL", "OK", "AVISO",
                                           "CAIDO", "MANT"))
    print("-" * 66)
    for g in estado.get("grupos", []):
        print("%-28s %6d %6d %6d %6d %6d"
              % (g["nombre"][:28], g["total"], g["ok"], g["aviso"],
                 g["caido"], g["mantenimiento"]))
    print("-" * 66)
    print("%-28s %6d %6d %6d %6d %6d" % ("TOTAL", r.get("equipos", 0),
                                         r.get("ok", 0), r.get("aviso", 0),
                                         r.get("caido", 0),
                                         r.get("mantenimiento", 0)))
    print("\nSondeo: %s s con %s" % (r.get("duracion_s"), r.get("metodo_ping")))
    if args.detalle:
        for g in estado.get("grupos", []):
            if not g["problemas"]:
                continue
            print("\n== %s ==" % g["nombre"])
            for p in g["problemas"][:args.detalle]:
                print("  %-38s %-16s %s"
                      % (p["nombre"][:38], p["ip"], p["detalle"]))


def cmd_importar(args):
    config, almacen, reglas, motor = preparar(args)
    if args.fichero.lower().endswith(".json"):
        crudos = mod_inventario.desde_json(args.fichero)
        origen = "json"
    else:
        resultado = mod_inventario.desde_csv_dude_fichero(
            args.fichero, args.incluir_deshabilitados)
        crudos = resultado["equipos"]
        origen = "csv_dude"
        if resultado["descartes"]:
            print("Descartados: %s" % ", ".join(
                "%s %d" % (k, v) for k, v in resultado["descartes"].items()))
    equipos = reglas.clasificar(crudos)
    altas, bajas = almacen.guardar_inventario(equipos, origen)
    print("Inventario: %d equipos (%d altas, %d bajas)"
          % (len(equipos), altas, bajas))
    _resumen_clasificacion(equipos, config)


def cmd_reglas(args):
    config, almacen, reglas, motor = preparar(args)
    guardados = almacen.equipos()
    if not guardados:
        print("No hay inventario cargado.")
        sys.exit(1)
    # reaplicamos las reglas por si se han editado desde la ultima importacion
    equipos = reglas.clasificar([{"ip": e["ip"], "nombre": e["nombre"],
                                  "etiquetas": {}, "origen": e["origen"],
                                  "meta": e.get("meta") or {}}
                                 for e in guardados])
    _resumen_clasificacion(equipos, config)
    if args.limite:
        print("\nMuestra:")
        for e in equipos[:args.limite]:
            etiquetas = " ".join("%s=%s" % (k, v)
                                 for k, v in sorted(e["etiquetas"].items()))
            print("  %-16s %-32s %-44s %s"
                  % (e["ip"], e["nombre"][:32], etiquetas[:44],
                     ",".join(e["sondas"])))
    if args.guardar:
        por_origen = {}
        for e in equipos:
            por_origen.setdefault(e.get("origen") or "json", []).append(e)
        for origen, lista in por_origen.items():
            # exclusivo=False: aqui solo reetiquetamos, no cambia el inventario
            altas, bajas = almacen.guardar_inventario(lista, origen,
                                                      exclusivo=False)
            print("\n%s: %d equipos actualizados" % (origen, len(lista)))


def cmd_dude(args):
    config, almacen, reglas, motor = preparar(args)
    if args.password:
        config.setdefault("dude", {})["password"] = args.password
    if args.host:
        config.setdefault("dude", {})["host"] = args.host
    if args.usuario:
        config.setdefault("dude", {})["usuario"] = args.usuario

    if not args.copia and not (config.get("dude") or {}).get("password"):
        import getpass
        config.setdefault("dude", {})["password"] = getpass.getpass(
            "Contrasena de %s en %s: " % ((config["dude"].get("usuario") or "?"),
                                          (config["dude"].get("host") or "?")))
    try:
        estadisticas = refrescar_desde_dude(config, almacen, reglas, args.copia)
        if args.historico:
            ruta = args.copia or estadisticas.get("copia")
            if not ruta:
                raise mod_dude.ErrorDude(
                    "no se ha conservado la copia de la base; pon "
                    "'carpeta_copia' en config.json o usa --copia")
            print("\nImportando el historico de caidas del Dude...")
            resumen = importar_historico(almacen, ruta, args.dias)
    except mod_dude.ErrorDude as e:
        print("No se ha podido leer la base del Dude: %s" % e)
        sys.exit(1)

    print("\nObjetos leidos:        %d (%d ilegibles)"
          % (estadisticas["objetos"], estadisticas["objetos_ilegibles"]))
    print("Dispositivos:          %d" % estadisticas["dispositivos"])
    print("Sin direccion:         %d" % estadisticas["sin_direccion"])
    print("Mapas encontrados:     %d" % estadisticas["mapas"])
    print("Altas / bajas:         %d / %d"
          % (estadisticas["altas"], estadisticas["bajas"]))
    equipos = almacen.equipos()
    _resumen_clasificacion(equipos, config)
    if args.limite:
        print("\nMuestra:")
        for e in equipos[:args.limite]:
            mapas = ", ".join((e.get("meta") or {}).get("mapas") or [])
            print("  %-16s %-42s %s" % (e["ip"], e["nombre"][:42], mapas[:30]))

    if args.historico:
        print("\n  %d caidas importadas" % resumen["importadas"])
        if resumen["de_equipos_borrados"]:
            print("  %d descartadas: son de equipos que ya no existen"
                  % resumen["de_equipos_borrados"])


def cmd_simular(args):
    config, almacen, reglas, motor = preparar(args)
    equipos = reglas.clasificar(
        mod_inventario.simulada(args.sedes, args.por_sede))
    almacen.guardar_inventario(equipos, "simulada")
    print("Inventario simulado: %d equipos" % len(equipos))
    _resumen_clasificacion(equipos, config)


def cmd_informe(args):
    config, almacen, reglas, motor = preparar(args)
    desde = time.time() - args.dias * 86400
    print("== Equipos que mas tiempo han estado caidos (%d dias) ==" % args.dias)
    for f in almacen.ranking_caidas(desde, args.limite):
        horas = (f["segundos"] or 0) / 3600.0
        print("  %-16s %-32s %3d caidas  %8.1f h"
              % (f["ip"], (f["nombre"] or "")[:32], f["veces"], horas))
    print("\n== Menor disponibilidad ==")
    mostrados = 0
    for f in almacen.disponibilidad(desde):
        if f["porcentaje"] is None or mostrados >= args.limite:
            continue
        print("  %-16s %-32s %6.2f %%"
              % (f["ip"], (f["nombre"] or "")[:32], f["porcentaje"]))
        mostrados += 1
    if not mostrados:
        print("  (todavia no hay muestras suficientes)")


def _resumen_clasificacion(equipos, config):
    for eje in (config.get("agrupar_por", "sede"),
                config.get("desglosar_por", "funcion")):
        cuenta = Counter(e["etiquetas"].get(eje, "sin clasificar")
                         for e in equipos)
        print("\nPor %s:" % eje)
        for valor, n in cuenta.most_common():
            print("   %-24s %5d" % (valor, n))
    sondas = Counter(s for e in equipos for s in e["sondas"])
    print("\nSondas: %s" % ", ".join("%s %d" % (k, v)
                                     for k, v in sondas.most_common()))
    criticos = sum(1 for e in equipos
                   if str(e["etiquetas"].get("critico", "")).lower()
                   in ("si", "sí", "true", "1"))
    print("Criticos: %d de %d" % (criticos, len(equipos)))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=BASE,
                    help="donde viven config.json, reglas.json y monitor.db")
    sub = ap.add_subparsers(dest="comando", required=True)

    sub.add_parser("servir").set_defaults(func=cmd_servir)

    p = sub.add_parser("sondear")
    p.add_argument("--detalle", type=int, default=0,
                   help="mostrar hasta N problemas por grupo")
    p.set_defaults(func=cmd_sondear)

    p = sub.add_parser("importar")
    p.add_argument("fichero")
    p.add_argument("--incluir-deshabilitados", action="store_true")
    p.set_defaults(func=cmd_importar)

    p = sub.add_parser("reglas")
    p.add_argument("--limite", type=int, default=20)
    p.add_argument("--guardar", action="store_true",
                   help="reaplica las reglas al inventario ya cargado")
    p.set_defaults(func=cmd_reglas)

    p = sub.add_parser("dude", help="inventario desde la base del Dude por FTP")
    p.add_argument("--host")
    p.add_argument("--usuario")
    p.add_argument("--password", help="mejor dejarlo en config.json")
    p.add_argument("--copia", help="usar una copia ya descargada del dude.db")
    p.add_argument("--limite", type=int, default=15)
    p.add_argument("--historico", action="store_true",
                   help="ademas, importa las caidas historicas del Dude")
    p.add_argument("--dias", type=int,
                   help="con --historico, limitar a los ultimos N dias")
    p.set_defaults(func=cmd_dude)

    p = sub.add_parser("simular")
    p.add_argument("--sedes", type=int, default=4)
    p.add_argument("--por-sede", type=int, default=40)
    p.set_defaults(func=cmd_simular)

    p = sub.add_parser("informe")
    p.add_argument("--dias", type=int, default=30)
    p.add_argument("--limite", type=int, default=15)
    p.set_defaults(func=cmd_informe)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
