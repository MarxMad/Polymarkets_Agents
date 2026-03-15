#!/usr/bin/env python3
import time
import requests
import subprocess
import os
import logging
import json
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.openclaw/.env"))

TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
URL = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
WORKSPACE_DIR = "/home/ubuntu/.openclaw/workspace"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
TRADER_PATH = os.path.join(WORKSPACE_DIR, "skills/polymarket/scripts/trader.py")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pumaclaw-listener")

ENG_KEYWORDS = [
    "arregla", "edita", "cambia", "revisa", "analiza", "instala",
    "terminal", "logs", "codigo", "code", "modify", "fix",
    "repair", "healing", "restart", "systemctl", "actualiza",
    "debug", "error", "falla", "script", "mejora", "modifica"
]

CHAT_KEYWORDS = [
    "escanea", "observa", "datos", "balance", "lista",
    "orden", "ordenar", "cuanto", "dinero", "quedo", "queda", "posiciones",
    "datos", "apuesta", "apostado", "ejecuta", "radar", "jugoso", "sabroso"
]

def send_message(chat_id, text):
    try:
        if len(text) > 4000:
            chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
            for chunk in chunks:
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "Markdown"
                }, timeout=10)
        else:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }, timeout=10)
    except Exception as e:
        log.error(f"Failed to send message: {e}")

def get_trader_report():
    try:
        cmd = f"export $(grep -v '^#' ~/.openclaw/.env | xargs) && ~/.venv/bin/python3 {TRADER_PATH} --report"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return result.stdout
        return f"Error al obtener reporte (RC={result.returncode}): {result.stderr}"
    except Exception as e:
        return f"Excepcion en reporte: {str(e)}"

def call_haiku(prompt, data_context=None):
    try:
        headers = {
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }

        system_msg = "Eres el PumaClaw, un bot de trading de Polymarket. Responde de forma concisa y con personalidad felina."
        if data_context:
            system_msg += f"\n\nCONTEXTO REAL DEL SISTEMA (USA ESTOS DATOS):\n{data_context}"

        data = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "messages": [
                {"role": "user", "content": f"{system_msg}\n\nEl usuario dice: {prompt}"}
            ]
        }
        resp = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=data, timeout=30)
        if resp.status_code != 200:
             log.error(f"Anthropic API Error: {resp.status_code} - {resp.text}")
             return f"Error API Haiku ({resp.status_code}): {resp.text[:100]}"

        return resp.json()["content"][0]["text"]
    except Exception as e:
        log.error(f"Haiku call Exception: {e}")
        return f"Error en Chat Mode (Haiku): {str(e)}"

def main():
    if not TOKEN or not ANTHROPIC_KEY:
        log.error("Missing TELEGRAM_TOKEN or ANTHROPIC_API_KEY in .env")
        return

    offset = 0
    log.info("PumaClaw Hybrid Listener Active (Haiku + CLI). Waiting...")

    while True:
        try:
            resp = requests.get(URL, params={"offset": offset, "timeout": 30}, timeout=40)
            if resp.status_code != 200:
                time.sleep(5)
                continue

            data = resp.json()
            if not data.get("ok"):
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg or not msg.get("text"):
                    continue

                chat_id = msg["chat"]["id"]
                user_id = msg["from"]["id"]
                text = msg["text"]

                if user_id != ALLOWED_ID:
                    log.warning(f"Unauthorized access attempt from {user_id}")
                    continue

                log.info(f"Command received: {text}")

                is_eng = any(kw in text.lower() for kw in ENG_KEYWORDS)

                if is_eng:
                    send_message(chat_id, "PumaClaw Engineering Mode\nInvocando motor pesado de Claude Code...")
                    env = os.environ.copy()
                    env["ANTHROPIC_API_KEY"] = ANTHROPIC_KEY
                    env["HOME"] = "/home/ubuntu"

                    system_context = "Eres el PumaClaw, un bot autonomo de trading en Polymarket. " \
                                     "Tus archivos principales son: trader.py (logica de trading), " \
                                     "listener.py (este bridge de Telegram), strategy.json y trades.json. " \
                                     "Responde a la solicitud de ingenieria del usuario: "
                    cmd = ["claude", "-p", f"{system_context} {text}"]
                    try:
                        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180, cwd=WORKSPACE_DIR)
                        output = result.stdout if result.returncode == 0 else result.stderr
                        if not output.strip():
                            output = "Claude Code completo la tarea sin devolver texto."
                        response = f"*Resultado Ingenieria*:\n\n```\n{output[:3500]}\n```"
                        send_message(chat_id, response)
                    except Exception as e:
                        send_message(chat_id, f"Error en CLI: {str(e)}")
                else:
                    data_keywords = ["balance", "dinero", "quedo", "queda", "posiciones", "datos", "apuestas", "holding", "wallet"]
                    context = None
                    if any(kw in text.lower() for kw in data_keywords):
                        log.info("L6: Data keyword detected. Fetching real-time context...")
                        context = get_trader_report()

                    response = call_haiku(text, data_context=context)
                    send_message(chat_id, f"*PumaChat (Haiku)*:\n\n{response}")

        except Exception as e:
            log.error(f"Listener loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
