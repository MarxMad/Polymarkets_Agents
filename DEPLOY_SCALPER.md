# Desplegar Contrarian Scalper (versión estable)

La versión **estable** está en este repo: `scripts/contrarian_scalper.py` (v2, solo bid, TP 30%, SL 90%).  
**No desplegar** versiones con midpoint ni sin SL sin acuerdo explícito.

## Requisitos

- Llave SSH: `~/Documents/AWS/PumaPass2.pem`
- Host: `ec2-3-255-209-58.eu-west-1.compute.amazonaws.com` (verificar IP actual en AWS si cambió)

## Deploy y arranque (1 solo proceso)

Desde la raíz del repo claudebot:

```bash
# 1) Subir el script del repo (versión estable)
scp -i ~/Documents/AWS/PumaPass2.pem -o StrictHostKeyChecking=no \
  skills/polymarket-trading/scripts/contrarian_scalper.py \
  ubuntu@ec2-3-255-209-58.eu-west-1.compute.amazonaws.com:/home/ubuntu/.openclaw/workspace/skills/polymarket/scripts/contrarian_scalper.py

# 2) Conectar y matar cualquier proceso previo, luego arrancar UNO solo
ssh -i ~/Documents/AWS/PumaPass2.pem -o StrictHostKeyChecking=no \
  ubuntu@ec2-3-255-209-58.eu-west-1.compute.amazonaws.com \
  "pkill -f contrarian_scalper 2>/dev/null; sleep 2; rm -f /tmp/contrarian_scalper.lock; cd /home/ubuntu/.openclaw/workspace/skills/polymarket/scripts && nohup env PYTHONUNBUFFERED=1 /home/ubuntu/.venv/bin/python3 -u contrarian_scalper.py > /tmp/scalper.log 2>&1 & echo PID: \$!; sleep 2; ps aux | grep contrarian_scalper | grep -v grep"
```

Debe aparecer **un solo** proceso. Si aparecen dos, matar el que no tenga `-u` en la línea de comando:

```bash
ssh -i ~/Documents/AWS/PumaPass2.pem ubuntu@ec2-3-255-209-58.eu-west-1.compute.amazonaws.com "ps aux | grep contrarian_scalper | grep -v grep"
# Luego: kill -9 <PID_DEL_DUPLICADO>
```

## Verificar versión y logs

```bash
ssh -i ~/Documents/AWS/PumaPass2.pem ubuntu@ec2-3-255-209-58.eu-west-1.compute.amazonaws.com \
  "head -6 /home/ubuntu/.openclaw/workspace/skills/polymarket/scripts/contrarian_scalper.py && echo '---' && tail -20 /tmp/scalper.log"
```

Debe decir "Contrarian Scalper v2" y "ESTABLE — solo bid".

## Parar el bot

```bash
ssh -i ~/Documents/AWS/PumaPass2.pem ubuntu@ec2-3-255-209-58.eu-west-1.compute.amazonaws.com "pkill -f contrarian_scalper"
```
