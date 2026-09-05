# network-status-dashboard

[![CI](https://github.com/mr7security/network-status-dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/mr7security/network-status-dashboard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: none](https://img.shields.io/badge/dependencias-solo%20stdlib-success.svg)](#requisitos-previos--requirements)

**Monitor de red para pared y para seguimiento.** Toma el inventario de
**MikroTik The Dude**, pero el sondeo lo hace él: ping, HTTP, TCP, SNMP,
certificados y DNS. Clasifica los equipos con **reglas declarativas** —ni una
sola VLAN dentro del código— y guarda el histórico en SQLite para dar
disponibilidad y ranking de caídas. Solo librería estándar de Python.

*Network monitoring dashboard for a wall screen. Takes its inventory from
**MikroTik The Dude**, but does the probing itself: ping, HTTP, TCP, SNMP,
certificates and DNS. Classifies devices through **declarative rules** — not a
single VLAN lives in the code — and keeps history in SQLite for availability
reports and a flapping ranking. Python standard library only.*

---

## Lo interesante: la base de The Dude es SQLite

The Dude no ofrece una API para su inventario. `/dude/device print detail` por
la API de RouterOS devuelve **solo el nombre**, incluso con `admin` y con la
política `dude` en el grupo. La vía habitual es exportar un CSV a mano desde el
cliente y volver a hacerlo cada vez que la red cambia.

Este proyecto no hace eso. Los ficheros `dude.db-wal` y `dude.db-shm` del
RouterOS delatan que **la base es SQLite**. Dentro, la tabla `objs` guarda cada
objeto como un blob con el formato de mensajes de MikroTik, descifrado en
[`monitor/dude_db.py`](monitor/dude_db.py):

```
Cabecera: "M2\x01\x00"
Propiedades: id (3 bytes LE) + tipo (1 byte) + valor
  0x00 falso   0x01 cierto   0x08 u32   0x09 u8   0x10 u64
  0x21 texto   0x31 bloque   0x88 lista de u32   0xa0 lista de textos
```

Las direcciones van como `u32` con los octetos al revés: `34384064` →
`C0 A8 0C 02` → `192.168.12.2`.

**Se decodifica el 99,6 % de los objetos consumiendo el blob entero**, que es la
prueba de que el formato es correcto: si la interpretación fuera errónea, el
parser se desalinearía y no llegaría al final. El resultado es que el inventario
se refresca solo por FTP, con nombres, direcciones, mapas y servicios. Ningún
CSV manual.

Las herramientas de exploración usadas para llegar ahí — [`explorar_db_dude.py`](explorar_db_dude.py)
y [`parsear_dude.py`](parsear_dude.py) — se incluyen: sirven para verificar el
formato contra otra versión de RouterOS.

## Características principales / Features

- **Clasificación fuera del código.** Todo vive en `reglas.json`: montar otra
  red es rellenar un fichero, no editar Python.
- **Etiquetas en vez de campos fijos.** Un equipo no «tiene sede y función»:
  lleva las etiquetas que le pongan las reglas. El panel agrupa por la que
  quieras cambiando una línea de `config.json`.
- **Inventario automático desde The Dude**, por FTP. Sin exportar CSV.
- **Importa el histórico heredado.** La base del Dude guarda sus propios
  `outages` — decenas de miles de tramos. Se vuelcan de una vez, de forma
  idempotente, y los informes salen poblados desde el primer día.
- **Seis sondas**: `icmp`, `tcp:PUERTO`, `http`, `https`, `snmp`, `tls:PUERTO`,
  `dns:NOMBRE`. Solo se lanzan a quien responde al ping: comprobar la web de un
  equipo apagado es tirar segundos.
- **Aviso de certificado a punto de caducar**, con días configurables.
- **Modo mantenimiento.** Marca un equipo 8 h, 24 h, 7 días o indefinido: sale
  en azul, no cuenta como caído ni para el porcentaje ni para el semáforo. Se
  libera solo al caducar o cuando vuelve a responder. **Es una marca del panel:
  no toca nada en el Dude ni en el equipo.**
- **SQLite con eventos**: disponibilidad por equipo y ranking de los que más caen.
- **Modo TV** a pantalla completa y **modo simulado** para desarrollar sin red.

## Requisitos previos / Requirements

| | |
|---|---|
| Python | 3.9 o superior (solo librería estándar) |
| SO | Cualquiera para ejecutarlo; el despliegue automático asume Ubuntu con `systemd` |
| Opcionales | `fping` (sondeo mucho más rápido), `snmpget` (sonda SNMP) |
| The Dude | Solo si quieres el inventario automático: un usuario de RouterOS con política `ftp` |

## Instalación / Installation

### Prueba rápida, sin red / Quick start

```bash
git clone https://github.com/mr7security/network-status-dashboard.git
cd network-status-dashboard
cp config.json.example config.json
cp reglas.json.example reglas.json

python3 panel.py simular    # inventario de mentira, para ver el panel ya
python3 panel.py servir
```

Abre `http://localhost:8082`. El modo simulado genera una red completa de
ejemplo: es la forma de ver la interfaz sin tocar nada real.

### Contra tu red

```bash
python3 panel.py dude          # trae el inventario de la base del Dude por FTP
python3 panel.py reglas        # comprueba como quedaria clasificado
python3 panel.py reglas --guardar
sudo bash deploy.sh
```

O si prefieres el CSV: **Devices → Ctrl+A → botón csv** en el cliente del Dude, y
`python3 panel.py importar Devices.csv`.

### Usuario de RouterOS

La política `ftp` no viene en el grupo `read`, así que hace falta un grupo propio:

```
/user group add name=lectura-ftp policy=read,ftp,winbox,api,dude,test,password,web,sensitive,local,ssh
/user set USUARIO group=lectura-ftp
```

## Uso / Usage

### Subcomandos

| Comando | Para qué |
|---|---|
| `servir` | Sondeo continuo y servidor web. Es lo que arranca systemd. |
| `sondear` | Un barrido y a la pantalla. `--detalle 10` lista los fallos. |
| `dude` | Trae el inventario de la base del Dude por FTP. `--historico` importa además sus `outages`. |
| `importar` | Carga un `Devices.csv` o un JSON de equipos. |
| `reglas` | Muestra cómo quedaría clasificado el inventario. `--guardar` lo aplica. |
| `simular` | Inventario de mentira para desarrollar sin red. |
| `informe` | Disponibilidad y ranking de caídas. `--dias 365`. |

El ciclo típico al ajustar la clasificación: editas `reglas.json`, lanzas
`panel.py reglas` para ver el resultado **sin tocar nada**, y cuando cuadra,
`panel.py reglas --guardar`.

### Reglas

Se aplican **todas** las que casen, en orden: las de abajo pisan a las de
arriba. Se va de lo general a lo particular.

```json
{"cuando": {"octeto2": "4"},          "etiquetas": {"sede": "SEDE-A"}},
{"cuando": {"octeto3": ["150", "199"]},
 "etiquetas": {"funcion": "switches", "critico": "si"},
 "sondas": ["icmp", "snmp"]},
{"cuando": {"nombre": "(?i)\\bcore\\b"}, "etiquetas": {"critico": "si"}}
```

Condiciones: `red` (CIDR), `octeto1..4`, `nombre` (expresión regular),
`etiqueta` (las que ya lleve) y `origen`.

### Cómo se decide el estado

Por equipo: **ok** si todas sus sondas responden; **aviso** si contesta al ping
pero falla algún servicio; **caído** si no contesta; **mantenimiento** si está
marcado a mano.

Por grupo: **rojo** si cae un equipo crítico o si el porcentaje de activos baja
de `umbral_rojo`; **ámbar** a partir de `problemas_para_aviso` incidencias;
**verde** el resto — un equipo suelto caído no pinta el grupo de ámbar, pero
sigue apareciendo en su lista.

### API

```
GET  /api/v1/estado
GET  /api/v1/serie?horas=24
GET  /api/v1/equipo/<ip>
GET  /api/v1/informes/caidas?dias=30
GET  /api/v1/informes/disponibilidad?dias=30
POST /api/v1/mantenimiento     {"ip": "...", "horas": 8}
POST /api/v1/inventario        multipart con el Devices.csv
```

Los POST exigen la cabecera `X-Monitor: 1`. Eso obliga al *preflight* y corta
que una web ajena lance la petición desde el navegador de quien tenga el panel
abierto.

### Atajos

- **T** o el botón *Modo TV*: pantalla completa para la pared.
- El logo es un `logo.jpg` dentro de `static/`. Si no está, no se muestra.

## Estructura del proyecto / Project structure

```
network-status-dashboard/
├── panel.py                   punto de entrada, con subcomandos
├── monitor/
│   ├── almacen.py             SQLite: equipos, muestras, eventos, mantenimiento
│   ├── reglas.py              motor de clasificacion declarativa
│   ├── inventario.py          fuentes: JSON, CSV del Dude, simulada
│   ├── dude_db.py             lectura de la base SQLite de The Dude
│   ├── sondas.py              icmp, tcp, http, snmp, tls, dns
│   ├── estado.py              barrido, niveles y agregacion por etiqueta
│   └── web.py                 API v1 y ficheros estaticos
├── static/                    index.html, estilo.css, panel.js
├── explorar_db_dude.py        herramienta: explora la base del Dude
├── parsear_dude.py            herramienta: verifica el formato de los blobs
├── config.json.example        plantilla de configuracion
├── reglas.json.example        plantilla de clasificacion
├── deploy.sh                  instalacion con systemd
├── ruff.toml
├── .github/workflows/ci.yml
├── LICENSE
└── README.md
```

Ficheros que **no** se versionan: `config.json`, `reglas.json`, `monitor.db`,
`copia-dude/` y el `logo.*` corporativo.

## Seguridad / Security

- **`config.json` lleva la contraseña del RouterOS.** Está en `.gitignore`, se
  instala en modo `600` y el CI falla si aparece en el repositorio.
- **`reglas.json` describe tu red** (segmentos, VLANs, nombres de sede).
  También está ignorado, por el mismo motivo.
- El usuario de RouterOS debe ser **de solo lectura + ftp**. Nunca `admin`.
- Cambia la `comunidad_snmp`: `public` no es una credencial.
- El sondeo **no escribe en ningún equipo**: ping, conexiones TCP, `GET` y
  `snmpget`. La marca de mantenimiento vive solo en la base del panel.
- El panel **no tiene autenticación**: publícalo solo en la VLAN de gestión, o
  detrás de un proxy inverso que la añada.

## Contribución / Contributing

Las incidencias y las *pull request* son bienvenidas. Antes de abrir una PR:

```bash
pip install ruff
ruff check panel.py monitor/ explorar_db_dude.py parsear_dude.py
python -m compileall -q panel.py monitor
python3 panel.py simular && python3 panel.py reglas
```

Los commits siguen [Conventional Commits](https://www.conventionalcommits.org/):
`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`.

## Licencia / License

MIT — ver [LICENSE](LICENSE).
