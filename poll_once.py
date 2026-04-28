"""
Fetches all pending Telegram updates, handles them, then clears the queue.
Designed to run on a schedule (e.g. GitHub Actions every 5 min).
"""

import os
import sys
import requests
from handlers import handle_update

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
BASE  = f"https://api.telegram.org/bot{TOKEN}"


def get_updates() -> list:
    resp = requests.get(f"{BASE}/getUpdates", params={"timeout": 0}, timeout=10)
    return resp.json().get("result", [])


def ack(last_id: int) -> None:
    requests.get(f"{BASE}/getUpdates", params={"offset": last_id + 1, "timeout": 0}, timeout=10)


def main() -> None:
    if not TOKEN:
        print("ERROR: TELEGRAM_TOKEN not set")
        sys.exit(1)

    updates = get_updates()
    if not updates:
        print("No pending updates.")
        return

    print(f"Processing {len(updates)} update(s)...")
    for update in updates:
        try:
            handle_update(update)
        except Exception as e:
            print(f"  Error handling update {update.get('update_id')}: {e}")

    ack(updates[-1]["update_id"])
    print("Done.")


if __name__ == "__main__":
    main()
