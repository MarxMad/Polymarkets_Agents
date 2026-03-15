import os
import json
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams

# Load env manually
env = {}
env_path = '/home/ubuntu/.openclaw/.env'
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                env[k] = v

def get_clob_client():
    creds = ApiCreds(
        api_key=env.get('POLYMARKET_API_KEY'),
        api_secret=env.get('POLYMARKET_API_SECRET'),
        api_passphrase=env.get('POLYMARKET_API_PASSPHRASE'),
    )
    return ClobClient('https://clob.polymarket.com', key=env.get('POLYMARKET_PRIVATE_KEY'), creds=creds, chain_id=137)

client = get_clob_client()
trades_path = '/home/ubuntu/.openclaw/workspace/trades.json'

if not os.path.exists(trades_path):
    print(f'Trades file not found at {trades_path}')
    exit(1)

with open(trades_path) as f:
    trades_log = json.load(f)

# Unify tokens from trades log
token_metadata = {}
for t in trades_log.get('trades', []):
    if t.get('status') in ('executed', 'simulated') and 'token_id' in t:
        token_metadata[t['token_id']] = t.get('question', 'Unknown')[:40]

print(f'Analyzing {len(token_metadata)} unique tokens found in history...')
total_val = 0
found_positions = 0

for tid, name in token_metadata.items():
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
        b = client.get_balance_allowance(params)
        qty = float(b.get('balance', 0)) / 1e6
        if qty > 0.01:
            try:
                p_info = client.get_midpoint(tid)
                price = float(p_info.get('mid', 0))
            except:
                price = 0
            
            val = qty * price
            total_val += val
            found_positions += 1
            print(f' - {name}\n   ID: {tid}\n   Qty: {qty:.2f} | Price: {price:.3f} | Value: ${val:.2f}')
    except Exception as e:
        print(f'Error auditing {tid}: {e}')

print('='*40)
print(f'TOTAL CALCULATED POSITIONS: {found_positions}')
print(f'TOTAL VALUE IN TOKENS: ${total_val:.2f}')
print('='*40)
