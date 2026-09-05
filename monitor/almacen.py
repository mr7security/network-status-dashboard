#!/usr/bin/env python3
"""
almacen.py — Persistencia en SQLite (biblioteca estandar, cero dependencias).

Cuatro tablas:

  equipos         inventario vigente, con sus etiquetas y sondas
  muestras        una fila por equipo y barrido, para el historico
  eventos         cada tramo en un nivel distinto de "ok": permite calcular
                  disponibilidad y sacar el ranking de los que mas caen
  mantenimiento   marcas manuales, con caducidad opcional

Las muestras se compactan solas: se guarda una cada N minutos y se purga lo
que pase de los dias configurados. Los eventos se conservan mas tiempo porque
ocupan poquisimo y son los que dan valor a los informes.
"""

import json
import os
import sqlite3
import threading
import time

ESQUEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS equipos (
    ip         TEXT PRIMARY KEY,
    nombre     TEXT NOT NULL,
    etiquetas  TEXT NOT NULL DEFAULT '{}',   -- json: clave -> valor
    sondas     TEXT NOT NULL DEFAULT '[]',   -- json: lista de sondas
    origen     TEXT,                          -- de que fuente vino
    visto      REAL,                          -- ultima vez que aparecio en la fuente
    activo     INTEGER NOT NULL DEFAULT 1,
    meta       TEXT NOT NULL DEFAULT '{}'    -- json: mapas, servicios, dude_id...
);

CREATE TABLE IF NOT EXISTS muestras (
    ts     INTEGER NOT NULL,
    ip     TEXT    NOT NULL,
    nivel  TEXT    NOT NULL,                 -- ok | aviso | caido | mantenimiento
    ms     INTEGER,                          -- latencia si la sonda la da
    PRIMARY KEY (ts, ip)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS eventos (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ip     TEXT    NOT NULL,
    nivel  TEXT    NOT NULL,
    desde  REAL    NOT NULL,
    hasta  REAL,                             -- NULL mientras siga abierto
    detalle TEXT
);
CREATE INDEX IF NOT EXISTS idx_eventos_ip      ON eventos(ip, desde);
CREATE INDEX IF NOT EXISTS idx_eventos_abierto ON eventos(ip) WHERE hasta IS NULL;

CREATE TABLE IF NOT EXISTS mantenimiento (
    ip    TEXT PRIMARY KEY,
    desde REAL NOT NULL,
    hasta REAL
);
"""


class Almacen:
    def __init__(self, ruta, dias_muestras=60, dias_eventos=400):
        self.ruta = ruta
        self.dias_muestras = dias_muestras
        self.dias_eventos = dias_eventos
        self._local = threading.local()
        carpeta = os.path.dirname(os.path.abspath(ruta))
        if carpeta:
            os.makedirs(carpeta, exist_ok=True)
        with self.conexion() as cx:
            cx.executescript(ESQUEMA)
            # bases creadas con la version anterior no tienen la columna meta
            columnas = {f["name"] for f in cx.execute("PRAGMA table_info(equipos)")}
            if "meta" not in columnas:
                cx.execute("ALTER TABLE equipos ADD COLUMN "
                           "meta TEXT NOT NULL DEFAULT '{}'")

    # sqlite3 no comparte conexiones entre hilos: una por hilo
    def conexion(self):
        cx = getattr(self._local, "cx", None)
        if cx is None:
            cx = sqlite3.connect(self.ruta, timeout=15,
                                 detect_types=sqlite3.PARSE_DECLTYPES)
            cx.row_factory = sqlite3.Row
            cx.execute("PRAGMA foreign_keys = ON")
            self._local.cx = cx
        return cx

    def cerrar(self):
        cx = getattr(self._local, "cx", None)
        if cx:
            cx.close()
            self._local.cx = None

    # ----------------------------------------------------------- inventario

    def guardar_inventario(self, equipos, origen, exclusivo=True):
        """Sustituye el inventario. Devuelve (altas, bajas).

        exclusivo=True da de baja TODO lo que no venga en esta carga, aunque
        sea de otro origen. Es lo que se quiere al importar un inventario
        completo: si no, los equipos de una simulacion anterior se quedarian
        para siempre saliendo como caidos.
        """
        ahora = time.time()
        cx = self.conexion()
        with cx:
            sql = "SELECT ip FROM equipos WHERE activo = 1"
            parametros = ()
            if not exclusivo:
                sql += " AND origen = ?"
                parametros = (origen,)
            previos = {f["ip"] for f in cx.execute(sql, parametros)}
            nuevos = set()
            for e in equipos:
                nuevos.add(e["ip"])
                cx.execute("""
                    INSERT INTO equipos (ip, nombre, etiquetas, sondas, origen,
                                         visto, activo, meta)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(ip) DO UPDATE SET
                        nombre    = excluded.nombre,
                        etiquetas = excluded.etiquetas,
                        sondas    = excluded.sondas,
                        origen    = excluded.origen,
                        visto     = excluded.visto,
                        activo    = 1,
                        meta      = excluded.meta
                """, (e["ip"], e.get("nombre") or e["ip"],
                      json.dumps(e.get("etiquetas") or {}, ensure_ascii=False),
                      json.dumps(e.get("sondas") or [], ensure_ascii=False),
                      origen, ahora,
                      json.dumps(e.get("meta") or {}, ensure_ascii=False)))
            bajas = previos - nuevos
            if bajas:
                cx.executemany("UPDATE equipos SET activo = 0 WHERE ip = ?",
                               [(ip,) for ip in bajas])
        return len(nuevos - previos), len(bajas)

    def equipos(self, incluir_inactivos=False):
        cx = self.conexion()
        sql = "SELECT * FROM equipos"
        if not incluir_inactivos:
            sql += " WHERE activo = 1"
        salida = []
        for f in cx.execute(sql + " ORDER BY ip"):
            salida.append({
                "ip": f["ip"],
                "nombre": f["nombre"],
                "etiquetas": json.loads(f["etiquetas"]),
                "sondas": json.loads(f["sondas"]),
                "origen": f["origen"],
                "activo": bool(f["activo"]),
                "meta": json.loads(f["meta"] or "{}"),
            })
        return salida

    # -------------------------------------------------------------- muestras

    PASO_MUESTRAS = 300      # segundos: una muestra por equipo cada 5 minutos

    def anotar_muestras(self, ts, filas):
        """filas: iterable de (ip, nivel, ms o None).

        El instante se redondea al bucket de 5 minutos: con 2500 equipos y un
        barrido por minuto, guardarlo todo serian millones de filas al dia y
        varios gigas al mes. Como la clave primaria es (ts, ip), el propio
        INSERT OR REPLACE se encarga de quedarse con la ultima de cada bucket.
        """
        bucket = int(ts) // self.PASO_MUESTRAS * self.PASO_MUESTRAS
        cx = self.conexion()
        with cx:
            cx.executemany(
                "INSERT OR REPLACE INTO muestras (ts, ip, nivel, ms) "
                "VALUES (?, ?, ?, ?)",
                [(bucket, ip, nivel, ms) for ip, nivel, ms in filas])

    def serie(self, desde, hasta=None, etiqueta=None, valor=None):
        """Serie agregada por instante: cuantos ok/aviso/caido/mantenimiento."""
        cx = self.conexion()
        hasta = hasta or time.time()
        if etiqueta:
            sql = """
                SELECT m.ts AS ts, m.nivel AS nivel, COUNT(*) AS n
                FROM muestras m JOIN equipos e ON e.ip = m.ip
                WHERE m.ts BETWEEN ? AND ?
                  AND json_extract(e.etiquetas, '$.' || ?) = ?
                GROUP BY m.ts, m.nivel ORDER BY m.ts
            """
            parametros = (int(desde), int(hasta), etiqueta, valor)
        else:
            sql = """
                SELECT ts, nivel, COUNT(*) AS n FROM muestras
                WHERE ts BETWEEN ? AND ?
                GROUP BY ts, nivel ORDER BY ts
            """
            parametros = (int(desde), int(hasta))

        puntos = {}
        for f in cx.execute(sql, parametros):
            p = puntos.setdefault(f["ts"], {"t": f["ts"], "ok": 0, "aviso": 0,
                                            "caido": 0, "mantenimiento": 0})
            if f["nivel"] in p:
                p[f["nivel"]] = f["n"]
        return list(puntos.values())

    # --------------------------------------------------------------- eventos

    def abrir_evento(self, ip, nivel, desde, detalle=None):
        cx = self.conexion()
        with cx:
            cx.execute("INSERT INTO eventos (ip, nivel, desde, detalle) "
                       "VALUES (?, ?, ?, ?)", (ip, nivel, desde, detalle))

    def cerrar_evento(self, ip, hasta):
        cx = self.conexion()
        with cx:
            cx.execute("UPDATE eventos SET hasta = ? "
                       "WHERE ip = ? AND hasta IS NULL", (hasta, ip))

    def eventos_abiertos(self):
        cx = self.conexion()
        return {f["ip"]: {"nivel": f["nivel"], "desde": f["desde"]}
                for f in cx.execute(
                    "SELECT ip, nivel, desde FROM eventos WHERE hasta IS NULL")}

    MARCA_IMPORTADO = "importado del Dude"

    def importar_eventos(self, tramos):
        """Mete caidas historicas. tramos: [(ip, inicio, duracion), ...].

        Es idempotente: primero borra lo importado antes, asi que se puede
        repetir sin duplicar nada.
        """
        cx = self.conexion()
        with cx:
            cx.execute("DELETE FROM eventos WHERE detalle = ?",
                       (self.MARCA_IMPORTADO,))
            cx.executemany(
                "INSERT INTO eventos (ip, nivel, desde, hasta, detalle) "
                "VALUES (?, 'caido', ?, ?, ?)",
                [(ip, inicio, inicio + max(0, duracion), self.MARCA_IMPORTADO)
                 for ip, inicio, duracion in tramos])
            return len(tramos)

    def ranking_caidas(self, desde, limite=20):
        """Los equipos que mas tiempo o mas veces han estado caidos."""
        cx = self.conexion()
        sql = """
            SELECT e.ip AS ip, q.nombre AS nombre,
                   COUNT(*) AS veces,
                   SUM(COALESCE(e.hasta, ?) - e.desde) AS segundos
            FROM eventos e LEFT JOIN equipos q ON q.ip = e.ip
            WHERE e.nivel = 'caido' AND e.desde >= ?
            GROUP BY e.ip ORDER BY segundos DESC LIMIT ?
        """
        ahora = time.time()
        return [dict(f) for f in cx.execute(sql, (ahora, desde, limite))]

    def disponibilidad(self, desde, etiqueta=None, valor=None):
        """Porcentaje de tiempo en 'ok' por equipo, a partir de las muestras."""
        cx = self.conexion()
        filtro = ""
        parametros = [int(desde)]
        if etiqueta:
            filtro = " AND json_extract(q.etiquetas, '$.' || ?) = ? "
            parametros += [etiqueta, valor]
        sql = """
            SELECT m.ip AS ip, q.nombre AS nombre,
                   SUM(CASE WHEN m.nivel = 'ok' THEN 1 ELSE 0 END) AS buenas,
                   SUM(CASE WHEN m.nivel = 'mantenimiento' THEN 0 ELSE 1 END) AS validas
            FROM muestras m JOIN equipos q ON q.ip = m.ip
            WHERE m.ts >= ? %s
            GROUP BY m.ip
        """ % filtro
        salida = []
        for f in cx.execute(sql, parametros):
            validas = f["validas"] or 0
            salida.append({
                "ip": f["ip"], "nombre": f["nombre"],
                "porcentaje": round(100.0 * (f["buenas"] or 0) / validas, 2)
                              if validas else None,
            })
        salida.sort(key=lambda x: (x["porcentaje"] is None, x["porcentaje"]))
        return salida

    # --------------------------------------------------------- mantenimiento

    def marcar_mantenimiento(self, ip, horas):
        """horas: 0 indefinido, negativo para liberar. Devuelve la marca."""
        ahora = time.time()
        cx = self.conexion()
        with cx:
            if horas is not None and horas < 0:
                cx.execute("DELETE FROM mantenimiento WHERE ip = ?", (ip,))
                return None
            hasta = (ahora + horas * 3600) if horas else None
            cx.execute("INSERT INTO mantenimiento (ip, desde, hasta) "
                       "VALUES (?, ?, ?) ON CONFLICT(ip) DO UPDATE SET "
                       "desde = excluded.desde, hasta = excluded.hasta",
                       (ip, ahora, hasta))
            return {"desde": ahora, "hasta": hasta}

    def mantenimiento(self):
        """Marcas vigentes. De paso limpia las caducadas."""
        ahora = time.time()
        cx = self.conexion()
        with cx:
            cx.execute("DELETE FROM mantenimiento "
                       "WHERE hasta IS NOT NULL AND hasta <= ?", (ahora,))
        return {f["ip"]: {"desde": f["desde"], "hasta": f["hasta"]}
                for f in cx.execute("SELECT * FROM mantenimiento")}

    def liberar_mantenimiento(self, ips):
        if not ips:
            return
        cx = self.conexion()
        with cx:
            cx.executemany("DELETE FROM mantenimiento WHERE ip = ?",
                           [(ip,) for ip in ips])

    # ------------------------------------------------------------- limpieza

    def purgar(self):
        ahora = time.time()
        cx = self.conexion()
        with cx:
            cx.execute("DELETE FROM muestras WHERE ts < ?",
                       (int(ahora - self.dias_muestras * 86400),))
            # lo importado del Dude no se purga: es historial heredado y ocupa
            # poquisimo comparado con lo que aporta a los informes
            cx.execute("DELETE FROM eventos WHERE hasta IS NOT NULL "
                       "AND hasta < ? AND COALESCE(detalle,'') <> ?",
                       (ahora - self.dias_eventos * 86400, self.MARCA_IMPORTADO))
