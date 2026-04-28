#!/usr/bin/env python3
"""
weekly_checkin.py
Sends a Telegram message on Monday and Wednesday asking about schedule exceptions.
Run via Windows Task Scheduler at 12:00 on Mon and Wed.
"""

import json
import os
import sys
import requests
from pathlib import Path
from datetime import date

SKILL_DIR = Path(__file__).parent.parent
CONFIG_PATH = SKILL_DIR / "config.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    return r.status_code == 200


def main():
    config = load_config()
    token   = os.environ.get("TELEGRAM_TOKEN")   or config.get("telegram_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or config.get("telegram_chat_id", "")

    if not token or not chat_id:
        print("ERROR: telegram_token or telegram_chat_id missing from config.json")
        sys.exit(1)

    today     = date.today()
    day_name  = today.strftime("%A")
    date_str  = today.strftime("%d %b %Y")

    msg = (
        f"<b>Good morning! Its {day_name}, {date_str}.</b>\n\n"
        f"Any schedule exceptions this week?\n"
        f"e.g. <i>Thursday all day, Friday after 3pm, Monday meeting 15:00-16:00</i>\n\n"
        f"Open Claude Code and say what needs changing."
    )

    ok = send_telegram(token, chat_id, msg)
    if ok:
        print(f"Check-in message sent for {day_name}.")
    else:
        print("ERROR: Failed to send Telegram message.")
        sys.exit(1)


if __name__ == "__main__":
    main()
