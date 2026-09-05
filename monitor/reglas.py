#!/usr/bin/env python3
"""
reglas.py — Clasificacion declarativa de los equipos.

La leccion del proyecto anterior: tener las tablas de sede y funcion metidas en
el codigo obliga a editar Python y desplegar cada vez que cambia la red. Aqui
todo eso vive en reglas.json y se puede tocar en caliente.

Formato:

{
  "reglas": [
    {"cuando": {"red": "192.0.2.0/24"},
     "etiquetas": {"funcion": "camaras"},
     "sondas": ["icmp", "http"]},

    {"cuando": {"octeto2": "100"},
     "etiquetas": {"sede": "SEDE-A"}},

    {"cuando": {"nombre": "(?i)\\b(core|sophos|firewall)\\b"},
     "etiquetas": {"critico": "si"},
     "sondas": ["icmp", "tcp:443"]},

    {"cuando": {"etiqueta": {"funcion": "switches"}},
     "etiquetas": {"critico": "si"},
     "sondas": ["icmp", "snmp"]}
  ],
  "por_defecto": {"etiquetas": {"funcion": "otros"}, "sondas": ["icmp"]}
}

Se aplican TODAS las reglas que casen, en orden: las de mas abajo pisan las
etiquetas de las de mas arriba. Eso permite ir de lo general a lo particular.

Condiciones admitidas dentro de "cuando" (todas deben cumplirse):
  red        una o varias subredes CIDR
  octeto2/3  valor exacto de ese octeto (atajo comodo para planes tipo 10.x.y)
  nombre     expresion regular contra el nombre del equipo
  etiqueta   diccionario de etiquetas que el equipo ya debe tener
  origen     de que fuente vino el equipo
"""

import ipaddress
import json
import re


class ErrorReglas(ValueError):
    pass


class Reglas:
    def __init__(self, definicion=None):
        definicion = definicion or {}
        self.crudo = definicion
        self.reglas = []
        for i, r in enumerate(definicion.get("reglas") or []):
            try:
                self.reglas.append(_Regla(r))
            except (ValueError, TypeError, re.error) as e:
                raise ErrorReglas("regla %d invalida: %s" % (i + 1, e))
        por_defecto = definicion.get("por_defecto") or {}
        self.etiquetas_defecto = dict(por_defecto.get("etiquetas") or {})
        self.sondas_defecto = list(por_defecto.get("sondas") or ["icmp"])

    @classmethod
    def desde_fichero(cls, ruta):
        try:
            with open(ruta, encoding="utf-8") as f:
                definicion = json.load(f)
        except FileNotFoundError:
            return cls({})
        except json.JSONDecodeError as e:
            raise ErrorReglas("%s no es un JSON valido: %s" % (ruta, e))
        return cls(definicion)      # sus errores ya vienen etiquetados

    def aplicar(self, equipo):
        """Devuelve (etiquetas, sondas) para un equipo {ip, nombre, ...}."""
        etiquetas = dict(self.etiquetas_defecto)
        etiquetas.update(equipo.get("etiquetas") or {})
        sondas = None

        for regla in self.reglas:
            if regla.casa(equipo, etiquetas):
                etiquetas.update(regla.etiquetas)
                if regla.sondas is not None:
                    sondas = list(regla.sondas)

        if sondas is None:
            sondas = list(self.sondas_defecto)
        return etiquetas, sondas

    def clasificar(self, equipos):
        salida = []
        for e in equipos:
            etiquetas, sondas = self.aplicar(e)
            salida.append(dict(e, etiquetas=etiquetas, sondas=sondas))
        return salida


class _Regla:
    def __init__(self, definicion):
        cuando = definicion.get("cuando") or {}
        self.etiquetas = dict(definicion.get("etiquetas") or {})
        self.sondas = definicion.get("sondas")
        if self.sondas is not None and not isinstance(self.sondas, list):
            raise ValueError("'sondas' debe ser una lista")

        redes = cuando.get("red")
        if isinstance(redes, str):
            redes = [redes]
        self.redes = [ipaddress.ip_network(r, strict=False)
                      for r in (redes or [])]

        self.octetos = {}
        for clave in ("octeto1", "octeto2", "octeto3", "octeto4"):
            if clave in cuando:
                valores = cuando[clave]
                if not isinstance(valores, list):
                    valores = [valores]
                self.octetos[int(clave[-1])] = {str(v) for v in valores}

        patron = cuando.get("nombre")
        self.nombre = re.compile(patron) if patron else None
        self.etiqueta = dict(cuando.get("etiqueta") or {})
        self.origen = cuando.get("origen")
        self.siempre = not any([self.redes, self.octetos, self.nombre,
                                self.etiqueta, self.origen])

    def casa(self, equipo, etiquetas):
        if self.siempre:
            return True
        ip = equipo.get("ip") or ""

        if self.redes:
            try:
                direccion = ipaddress.ip_address(ip)
            except ValueError:
                return False
            if not any(direccion in red for red in self.redes):
                return False

        if self.octetos:
            partes = ip.split(".")
            if len(partes) != 4:
                return False
            for indice, valores in self.octetos.items():
                if partes[indice - 1] not in valores:
                    return False

        if self.nombre and not self.nombre.search(equipo.get("nombre") or ""):
            return False

        for clave, valor in self.etiqueta.items():
            if str(etiquetas.get(clave)) != str(valor):
                return False

        return not (self.origen and equipo.get("origen") != self.origen)
