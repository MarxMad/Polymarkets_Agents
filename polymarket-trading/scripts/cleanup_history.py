import json
import os

TRADES_PATH = os.path.expanduser("~/.openclaw/workspace/trades.json")

def cleanup():
    if not os.path.exists(TRADES_PATH):
        print("No trades.json found.")
        return

    with open(TRADES_PATH) as f:
        data = json.load(f)

    original_count = len(data.get("trades", []))
    # Filter out anything with MegaETH in the title or question
    data["trades"] = [t for t in data.get("trades", []) if "megaeth" not in t.get("event", "").lower() and "megaeth" not in t.get("question", "").lower()]
    new_count = len(data["trades"])
    
    # Also reset stats if they are biased (optional, but let's keep it clean)
    if not data["trades"]:
        data["stats"] = {"total_wagered": 0, "wins": 0, "losses": 0, "pending": 0}

    with open(TRADES_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"Cleanup complete. Removed {original_count - new_count} MegaETH-related entries.")

if __name__ == "__main__":
    cleanup()
