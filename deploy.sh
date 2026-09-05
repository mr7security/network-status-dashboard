#!/usr/bin/env bash
# deploy.sh — Instala o actualiza el monitor.
#
#   cd ~/network-status-dashboard && git pull && sudo bash deploy.sh
#
# Idempotente. NO pisa config.json, reglas.json ni monitor.db: la
# configuracion y los datos viven en el servidor, no en el repositorio.

set -euo pipefail

DESTINO=/opt/network-status-dashboard
USUARIO=netdash
SERVICIO=network-status-dashboard
ORIGEN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Ejecutalo con sudo: sudo bash deploy.sh" >&2
  exit 1
fi

echo "==> Dependencias"
FALTAN=()
command -v fping   >/dev/null 2>&1 || FALTAN+=(fping)
command -v snmpget >/dev/null 2>&1 || FALTAN+=(snmp)
if ((${#FALTAN[@]})); then
  echo "    instalando: ${FALTAN[*]}"
  apt-get update -qq && apt-get install -y -qq "${FALTAN[@]}"
else
  echo "    fping y snmpget ya estan"
fi
command -v python3 >/dev/null 2>&1 || { echo "Falta python3" >&2; exit 1; }
python3 -c "import sqlite3" || { echo "Falta el modulo sqlite3" >&2; exit 1; }

echo "==> Usuario de servicio"
if ! id -u "$USUARIO" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$USUARIO"
  echo "    creado $USUARIO"
else
  echo "    $USUARIO ya existe"
fi

echo "==> Ficheros en $DESTINO"
mkdir -p "$DESTINO/monitor" "$DESTINO/static"
chown "$USUARIO:$USUARIO" "$DESTINO" "$DESTINO/monitor" "$DESTINO/static"

install -m 0755 -o "$USUARIO" -g "$USUARIO" "$ORIGEN/panel.py" "$DESTINO/"
for f in "$ORIGEN"/monitor/*.py; do
  install -m 0644 -o "$USUARIO" -g "$USUARIO" "$f" "$DESTINO/monitor/"
done
for f in "$ORIGEN"/static/*; do
  [[ -f "$f" ]] || continue
  install -m 0644 -o "$USUARIO" -g "$USUARIO" "$f" "$DESTINO/static/"
done
# restos de versiones anteriores que podrian pisar a los modulos nuevos
rm -rf "$DESTINO/monitor/__pycache__"
rm -f  "$DESTINO/monitor.py"

for f in config.json reglas.json; do
  if [[ -f "$DESTINO/$f" ]]; then
    chown "$USUARIO:$USUARIO" "$DESTINO/$f"
    echo "    conservo $f existente"
  elif [[ -f "$ORIGEN/$f.example" ]]; then
    install -m 0644 -o "$USUARIO" -g "$USUARIO" "$ORIGEN/$f.example" "$DESTINO/$f"
    echo "    creado $f desde el ejemplo -- revisalo"
  fi
done
[[ -f "$DESTINO/monitor.db" ]] && chown "$USUARIO:$USUARIO" "$DESTINO"/monitor.db*

PUERTO=$(python3 - "$DESTINO/config.json" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("puerto", 8082))
except Exception:
    print(8082)
PY
)

echo "==> Servicio systemd"
cat > /etc/systemd/system/$SERVICIO.service <<EOF
[Unit]
Description=Monitor de red por etiquetas (Dude 2)
After=network-online.target
Wants=network-online.target
StartLimitBurst=5
StartLimitIntervalSec=120

[Service]
Type=simple
User=$USUARIO
Group=$USUARIO
WorkingDirectory=$DESTINO
ExecStart=/usr/bin/python3 $DESTINO/panel.py --dir $DESTINO servir
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
# fping necesita sockets ICMP en bruto
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW
NoNewPrivileges=no
ProtectSystem=full
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICIO" >/dev/null

if ! sudo -u "$USUARIO" python3 "$DESTINO/panel.py" --dir "$DESTINO" \
        reglas --limite 0 >/dev/null 2>&1; then
  echo
  echo "!! Todavia no hay inventario. Cargalo con uno de estos:"
  echo "   sudo -u $USUARIO python3 $DESTINO/panel.py --dir $DESTINO importar Devices.csv"
  echo "   sudo -u $USUARIO python3 $DESTINO/panel.py --dir $DESTINO simular"
  echo
fi

systemctl restart "$SERVICIO" || true
sleep 2

echo
if systemctl is-active --quiet "$SERVICIO"; then
  IP=$(hostname -I | awk '{print $1}')
  echo "Listo. Panel en:  http://$IP:$PUERTO/"
else
  echo "El servicio no ha arrancado. Ultimas lineas del log:"
  journalctl -u "$SERVICIO" -n 30 --no-pager || true
fi
echo
echo "  Estado:    systemctl status $SERVICIO"
echo "  Log:       journalctl -u $SERVICIO -f"
echo "  Un barrido:sudo -u $USUARIO python3 $DESTINO/panel.py --dir $DESTINO sondear"
echo "  Informes:  sudo -u $USUARIO python3 $DESTINO/panel.py --dir $DESTINO informe"
echo "  Config:    $DESTINO/config.json  y  $DESTINO/reglas.json"
