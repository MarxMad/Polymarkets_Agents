import os
import time
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, ApiCreds

load_dotenv(os.path.expanduser("~/.openclaw/.env"))

# 1. Configurar cliente
CLOB_HOST = "https://clob.polymarket.com"
POLYGON = 137
pk = os.getenv("POLYMARKET_PRIVATE_KEY")
api_key = os.getenv("POLYMARKET_API_KEY")
api_secret = os.getenv("POLYMARKET_API_SECRET")
api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")
PROXY_ADDRESS = "0x1294d2B89B08E8651124F04534FB2715a1437846"
creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
client = ClobClient(CLOB_HOST, key=pk, chain_id=POLYGON, creds=creds, signature_type=2, funder=PROXY_ADDRESS)

print("Clob Client Inicializado para Testing.")

# 2. Obtener posiciones activas reales
positions = client.get_open_orders()
print(f"Posiciones Abiertas encontradas: {positions}")

