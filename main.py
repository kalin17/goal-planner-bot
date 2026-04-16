#!/usr/bin/env python3
"""
GoalPlannerBot — two-way Telegram bot
Handles: task briefs at wake/school/gym, chat via Claude API, progress tracking
Deploy as Railway worker. Requires env vars (see README).
"""

import os
import json
import logging
from datetime import date
from dotenv import load_dotenv
load_dotenv()

import pytz
import gspread
import google.generativeai as genai
from google.oauth2 import service_account
from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    filters, ContextTypes,
)
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SOFIA          = pytz.timezone("Europe/Sofia")
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_KEY     = os.environ["GEMINI_KEY"]
SHEET_ID       = os.environ["SHEET_ID"]
CHAT_ID        = int(os.environ["CHAT_ID"])
GOOGLE_CREDS   = json.loads(os.environ["GOOGLE_CREDENTIALS"])

genai.configure(api_key=GEMINI_KEY)
gemini = genai.GenerativeModel("gemini-1.5-flash")


# ── Google Sheets helpers ─────────────────────────────────────────────────────
def _sheets_client():
    creds = service_account.Credentials.from_service_account_info(
        GOOGLE_CREDS,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)


def get_todays_tasks() -> list[dict]:
    today = date.today().isoformat()
    tasks = []
    gc = _sheets_client()
    sh = gc.open_by_key(SHEET_ID)
    for ws in sh.worksheets():
        if not (ws.title.endswith("Tasks") or "— Tasks" in ws.title):
            continue
        for row in ws.get_all_records():
            if str(row.get("Scheduled Start", "")).startswith(today):
                tasks.append({
                    "id":     row.get("Small Task ID", ""),
                    "title":  row.get("Title", ""),
                    "hours":  float(row.get("Estimated Hours", 0) or 0),
                    "status": str(row.get("Status", "Pending")),
                })
    return tasks


def get_progress_summary() -> list[str]:
    gc = _sheets_client()
    sh = gc.open_by_key(SHEET_ID)
    lines = []
    for ws in sh.worksheets():
        if not (ws.title.endswith("Tasks") or "— Tasks" in ws.title):
            continue
        rows  = ws.get_all_records()
        total = len(rows)
        done  = sum(1 for r in rows if str(r.get("Status", "")).lower() == "done")
        goal  = ws.title.replace(" — Tasks", "")
        pct   = int(done / total * 100) if total else 0
        lines.append(f"{goal}: {done}/{total} ({pct}%)")
    return lines


# ── Notification builder ──────────────────────────────────────────────────────
def build_brief(tasks: list[dict], trigger: str) -> str:
    pending = [t for t in tasks if t["status"].lower() not in ("done", "complete")]
    done    = [t for t in tasks if t["status"].lower() in ("done", "complete")]

    headers = {
        "wake":   f"Good morning! {date.today().strftime('%a %d %b')}",
        "school": "School done!",
        "gym":    "Gym done!",
    }
    footers = {
        "wake":   "Make it count.",
        "school": "You have time before gym.",
        "gym":    "Evening session — until 5am.",
    }

    header = headers.get(trigger, "")
    footer = footers.get(trigger, "")

    if not pending:
        extra = f" ({len(done)} already done)" if done else ""
        return f"{header}\n\nNothing pending today{extra}. Rest up."

    lines     = "\n".join(f"• {t['title']} ({t['hours']:.1f}h)" for t in pending)
    total_h   = sum(t["hours"] for t in pending)
    done_note = f"  ({len(done)} done)" if done else ""
    return f"{header}\n\n{lines}\n\nTotal: {total_h:.1f}h{done_note}  {footer}"


# ── Notification sender ───────────────────────────────────────────────────────
async def send_notification(bot, trigger: str):
    try:
        tasks = get_todays_tasks()
        msg   = build_brief(tasks, trigger)
    except Exception as e:
        msg = f"Could not load tasks: {e}"
        logger.exception("Notification error")
    await bot.send_message(chat_id=CHAT_ID, text=msg)


# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = build_brief(get_todays_tasks(), "wake")
    except Exception as e:
        msg = f"Error loading tasks: {e}"
    await update.message.reply_text(msg)


async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        lines = get_progress_summary()
        msg   = "Progress:\n\n" + "\n".join(lines) if lines else "No goals tracked yet."
    except Exception as e:
        msg = f"Error: {e}"
    await update.message.reply_text(msg)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/tasks — today's task list\n"
        "/progress — goal completion %\n\n"
        "Or just message me anything — I'll help you stay on track.\n"
        "To add a new goal, open Claude Code and describe it."
    )


# ── Chat handler ──────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    try:
        tasks    = get_todays_tasks()
        progress = get_progress_summary()
        t_str    = "\n".join(f"- {t['title']} ({t['hours']}h) [{t['status']}]" for t in tasks) or "None"
        p_str    = "\n".join(progress) or "No goals."
    except Exception:
        t_str, p_str = "unavailable", "unavailable"

    system = (
        f"You are a concise personal planner assistant. Today: {date.today()}.\n\n"
        f"Today's tasks:\n{t_str}\n\nGoal progress:\n{p_str}\n\n"
        "Rules: keep replies under 80 words. No markdown. Plain text only. "
        "Be direct and motivating. If they finished a task, confirm it. "
        "If they want to add a new goal, tell them to open Claude Code."
    )

    try:
        resp  = gemini.generate_content(f"{system}\n\nUser: {text}")
        reply = resp.text
    except Exception as e:
        reply = f"Error reaching Gemini: {e}"

    await update.message.reply_text(reply)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("tasks",    cmd_tasks))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Schedule notifications via APScheduler (accessed through job_queue)
    sched = app.job_queue.scheduler
    bot   = app.bot

    async def wake(ctx=None):   await send_notification(bot, "wake")
    async def school(ctx=None): await send_notification(bot, "school")
    async def gym(ctx=None):    await send_notification(bot, "gym")

    # Wake up — daily 12:00 Sofia
    sched.add_job(wake, CronTrigger(hour=12, minute=0, timezone=SOFIA), id="wake")
    # Gym end — daily 21:30 Sofia
    sched.add_job(gym,  CronTrigger(hour=21, minute=30, timezone=SOFIA), id="gym")
    # School end — per day
    sched.add_job(school, CronTrigger(day_of_week="mon,fri", hour=17, minute=40, timezone=SOFIA), id="school_mf")
    sched.add_job(school, CronTrigger(day_of_week="tue",     hour=19, minute=15, timezone=SOFIA), id="school_tu")
    sched.add_job(school, CronTrigger(day_of_week="wed,thu", hour=18, minute=30, timezone=SOFIA), id="school_wt")
    sched.add_job(school, CronTrigger(day_of_week="sat",     hour=17, minute=0,  timezone=SOFIA), id="school_sa")

    logger.info("GoalPlannerBot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
