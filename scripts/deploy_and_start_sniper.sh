#!/bin/bash
# Despliega montecarlo_sniper.py en EC2 y lo arranca como servicio systemd de usuario.
# Así el proceso NO depende de la sesión SSH y sobrevive al cierre de la conexión.

set -e
PEM_KEY="${PEM_KEY:-$HOME/Documents/AWS/PassPuma.pem}"
EC2_HOST="${EC2_HOST:-ubuntu@ec2-3-255-209-58.eu-west-1.compute.amazonaws.com}"
REMOTE_DIR="/home/ubuntu/.openclaw/workspace/skills/polymarket"

echo ">> Deploy sniper + servicio systemd a EC2..."
rsync -avz -e "ssh -i $PEM_KEY -o StrictHostKeyChecking=no" \
  polymarket-trading/scripts/montecarlo_sniper.py \
  "$EC2_HOST:$REMOTE_DIR/scripts/"

rsync -avz -e "ssh -i $PEM_KEY -o StrictHostKeyChecking=no" \
  polymarket-trading/pumaclaw-sniper.service \
  "$EC2_HOST:$REMOTE_DIR/"

echo ">> Instalando servicio e iniciando sniper..."
ssh -i "$PEM_KEY" -o StrictHostKeyChecking=no "$EC2_HOST" "bash -s" << 'REMOTE'
  set -e
  mkdir -p ~/.config/systemd/user
  cp /home/ubuntu/.openclaw/workspace/skills/polymarket/pumaclaw-sniper.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user stop pumaclaw-sniper.service 2>/dev/null || true
  systemctl --user start pumaclaw-sniper.service
  systemctl --user enable pumaclaw-sniper.service
  sleep 2
  systemctl --user status pumaclaw-sniper.service --no-pager
  echo ""
  echo "Log (últimas líneas):"
  tail -n 5 /home/ubuntu/sniper_v5.log 2>/dev/null || echo "(log aún vacío)"
REMOTE

echo ""
echo ">> Sniper corriendo como servicio. Para ver log: ssh ... 'tail -f /home/ubuntu/sniper_v5.log'"
echo ">> Parar: ssh ... 'systemctl --user stop pumaclaw-sniper'"
echo ">> Estado: ssh ... 'systemctl --user status pumaclaw-sniper'"
