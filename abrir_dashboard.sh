#!/bin/bash

echo "=========================================================="
echo "🛡️ MARXMAD // NEURAL CORTEX (PUMACLAW V3) 🛡️"
echo "=========================================================="
echo ""
echo ">> Conectando a AWS EC2 y abriendo puerto 8050..."
echo ">> Todo el procesamiento ocurrirá en la Nube (AWS)."
echo ">> Tu Mac solo recibirá los gráficos. 100% aislado."
echo ""

# Variables de conexión
PEM_KEY="~/Documents/AWS/PassPuma.pem"
EC2_HOST="ubuntu@ec2-3-255-209-58.eu-west-1.compute.amazonaws.com"
REMOTE_DIR="~/.openclaw/workspace/skills/polymarket"
VENV_PYTHON="~/.openclaw/workspace/skills/polymarket/.venv/bin/python3"

echo "⏳ Limpiando procesos previos (Local + Servidor)..."
# Limpiar túnel local
lsof -ti:8050 | xargs kill -9 2>/dev/null
# Limpiar puerto remoto
ssh -i $PEM_KEY -o StrictHostKeyChecking=no $EC2_HOST "fuser -k 8050/tcp 2>/dev/null || true; pkill -9 -f montecarlo 2>/dev/null || true"

echo "⏳ Iniciando Neural Cortex en AWS..."
echo "⚠️  NOTA: La gráfica aparecerá en aprox. 5 - 10 segundos ⚠️ "
echo ">> URL: http://localhost:8050"
echo "(Presiona Control+C para cerrar el puente seguro cuando termines)"
echo ""

# Creamos el túnel y ejecutamos el servidor de Dash
ssh -i $PEM_KEY -o StrictHostKeyChecking=no -L 8050:localhost:8050 $EC2_HOST "cd $REMOTE_DIR && $VENV_PYTHON scripts/montecarlo_cortex.py"

echo ""
echo "🛑 Neural Cortex Desconectado."
