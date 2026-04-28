"""
Telegram bot handlers for the goal-planner cloud function.
Commands: /pushtask  /pushevent  /newgoal  /goalcheck
"""

import json
import os
import re
import pytz
import requests
from collections import defaultdict
from datetime import datetime, date, timedelta, time as dtime

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# ── Config & auth ─────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
]

TASKS_COLS = [
    "Small Task ID", "Medium Goal ID", "Goal ID", "Title", "Order",
    "Estimated Hours", "Scheduled Start", "Scheduled End", "Calendar Event ID", "Status",
]
MEDIUM_GOALS_COLS = [
    "Medium Goal ID", "Goal ID", "Title", "Priority", "Estimated Hours",
    "Deadline (weeks)", "Deadline Date", "Depends On", "Small Task Count", "Status",
]
OVERVIEW_COLS = [
    "Goal ID", "Title", "Created Date",
    "Total Medium Goals", "Total Small Tasks", "Total Estimated Hours", "Status",
]


def get_config() -> dict:
    return json.loads(os.environ.get("CONFIG_JSON", "{}"))


def get_creds() -> Credentials:
    info = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON", "{}"))
    return Credentials.from_service_account_info(info, scopes=SCOPES)


# ── Telegram ──────────────────────────────────────────────────────────────────

def send(token: str, chat_id: str, text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )


# ── Google Sheets helpers ─────────────────────────────────────────────────────

def open_sheet(config: dict):
    client = gspread.authorize(get_creds())
    return client.open_by_key(config["sheet_id"])


def get_goal_tabs(spreadsheet) -> dict:
    """
    Returns {prefix: {"overview": ws, "medium_goals": ws, "tasks": ws}}
    by scanning worksheet names for the '— Overview / Medium Goals / Tasks' pattern.
    """
    result = {}
    for ws in spreadsheet.worksheets():
        name = ws.title
        for suffix, key in [(" — Overview", "overview"), (" — Medium Goals", "medium_goals"), (" — Tasks", "tasks")]:
            if name.endswith(suffix):
                prefix = name[: -len(suffix)]
                result.setdefault(prefix, {})[key] = ws
    return result


def read_pending_tasks(spreadsheet) -> tuple[list, list]:
    """Return (all_medium_goals, all_pending_tasks) across every goal tab."""
    tabs = get_goal_tabs(spreadsheet)
    all_mgs, all_tasks = [], []

    for tabs_dict in tabs.values():
        if "medium_goals" in tabs_dict:
            for row in tabs_dict["medium_goals"].get_all_records():
                dep_raw = row.get("Depends On", "")
                all_mgs.append({
                    "id":             row["Medium Goal ID"],
                    "goal_id":        row["Goal ID"],
                    "title":          row["Title"],
                    "priority":       int(row["Priority"]),
                    "deadline_weeks": int(row["Deadline (weeks)"]),
                    "depends_on":     [d.strip() for d in dep_raw.split(",") if d.strip()],
                })

        if "tasks" in tabs_dict:
            ws = tabs_dict["tasks"]
            for i, row in enumerate(ws.get_all_records(), start=2):  # row 1 = header
                if row.get("Status", "") == "Pending":
                    all_tasks.append({
                        "id":              row["Small Task ID"],
                        "mg_id":           row["Medium Goal ID"],
                        "goal_id":         row["Goal ID"],
                        "title":           row["Title"],
                        "order":           int(row["Order"]),
                        "estimated_hours": float(row["Estimated Hours"]),
                        "row_num":         i,
                        "ws":              ws,
                    })

    return all_mgs, all_tasks


# ── Scheduling helpers (adapted from planner.py) ───────────────────────────────

def topological_sort(medium_goals: list) -> list:
    id_map = {mg["id"]: mg for mg in medium_goals}
    in_deg = {mg["id"]: 0 for mg in medium_goals}
    dependents: dict[str, list] = defaultdict(list)

    for mg in medium_goals:
        for dep in mg.get("depends_on", []):
            if dep in in_deg:
                dependents[dep].append(mg["id"])
                in_deg[mg["id"]] += 1

    available = sorted([mg for mg in medium_goals if in_deg[mg["id"]] == 0], key=lambda m: m["priority"])
    result = []
    while available:
        node = available.pop(0)
        result.append(node)
        for child_id in dependents[node["id"]]:
            in_deg[child_id] -= 1
            if in_deg[child_id] == 0:
                available.append(id_map[child_id])
                available.sort(key=lambda m: m["priority"])
    return result


def round_up_15(dt: datetime) -> datetime:
    rem = dt.minute % 15
    if rem == 0 and dt.second == 0:
        return dt.replace(second=0, microsecond=0)
    return (dt + timedelta(minutes=15 - rem)).replace(second=0, microsecond=0)


def parse_window_time(time_str: str, day: date, tz) -> datetime:
    h, m = map(int, time_str.split(":"))
    return tz.localize(datetime.combine(day, dtime(h, m)))


def fetch_busy(service, calendar_id: str, t_min: datetime, t_max: datetime) -> list:
    body = {"timeMin": t_min.isoformat(), "timeMax": t_max.isoformat(), "items": [{"id": calendar_id}]}
    result = service.freebusy().query(body=body).execute()
    busy = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    return [
        (datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
         datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
        for b in busy
    ]


def find_slot(cursor: datetime, hours: float, schedule: dict, busy: list, tz) -> tuple:
    duration = timedelta(hours=hours)
    for _ in range(365):
        day_name = cursor.strftime("%A").lower()
        day_sched = schedule.get(day_name)
        if not day_sched:
            cursor = tz.localize(datetime.combine((cursor + timedelta(days=1)).date(), dtime(0, 0)))
            continue

        windows = sorted([
            (parse_window_time(w["start"], cursor.date(), tz),
             parse_window_time(w["end"],   cursor.date(), tz))
            for w in day_sched.get("windows", [])
        ])
        day_busy = sorted([
            (bs.astimezone(tz), be.astimezone(tz))
            for bs, be in busy
            if bs.astimezone(tz).date() == cursor.date()
        ])

        for w_start, w_end in windows:
            if cursor >= w_end:
                continue
            slot_start = max(cursor, w_start)
            for bs, be in day_busy:
                if slot_start >= be:
                    continue
                if slot_start + duration <= bs:
                    break
                if slot_start < be:
                    slot_start = be
            slot_end = slot_start + duration
            if slot_end <= w_end:
                return slot_start, slot_end

        cursor = tz.localize(datetime.combine((cursor + timedelta(days=1)).date(), dtime(0, 0)))

    return None, None


# ── /pushtask ─────────────────────────────────────────────────────────────────

def handle_pushtask(config: dict, token: str, chat_id: str) -> None:
    send(token, chat_id, "Scheduling your tasks... ⏳")
    try:
        spreadsheet = open_sheet(config)
        all_mgs, pending = read_pending_tasks(spreadsheet)

        if not pending:
            send(token, chat_id, "No pending tasks found. All done! 🎉")
            return

        cal = build("calendar", "v3", credentials=get_creds())
        tz = pytz.timezone(config["timezone"])
        gap = timedelta(minutes=config.get("min_task_gap_minutes", 5))

        max_weeks = max((mg["deadline_weeks"] for mg in all_mgs), default=12)
        horizon_start = tz.localize(datetime.combine(date.today(), dtime(0, 0)))
        horizon_end   = tz.localize(datetime.combine(date.today() + timedelta(weeks=max_weeks + 2), dtime(23, 59)))
        busy = fetch_busy(cal, config["calendar_id"], horizon_start, horizon_end)

        sorted_mgs = topological_sort(all_mgs)
        mg_order = {mg["id"]: i for i, mg in enumerate(sorted_mgs)}
        mg_lookup = {mg["id"]: mg for mg in all_mgs}

        sorted_tasks = sorted(pending, key=lambda t: (mg_order.get(t["mg_id"], 999), t["order"]))

        cursor = round_up_15(datetime.now(tz))
        mg_last_end: dict[str, datetime] = {}
        created_lines = []

        for task in sorted_tasks:
            mg = mg_lookup.get(task["mg_id"], {})

            for dep_id in mg.get("depends_on", []):
                dep_end = mg_last_end.get(dep_id)
                if dep_end and cursor < dep_end:
                    cursor = dep_end

            slot_start, slot_end = find_slot(cursor, task["estimated_hours"], config["schedule"], busy, tz)
            if not slot_start:
                send(token, chat_id, f"⚠️ Could not find a slot for '{task['title']}'")
                continue

            cursor = slot_end + gap

            event_body = {
                "summary":     f"{task['title']} [{mg.get('title', '')}]",
                "description": f"Task ID: {task['id']}",
                "start":       {"dateTime": slot_start.isoformat(), "timeZone": config["timezone"]},
                "end":         {"dateTime": slot_end.isoformat(),   "timeZone": config["timezone"]},
            }
            created = cal.events().insert(calendarId=config["calendar_id"], body=event_body).execute()
            event_id = created["id"]

            task["ws"].update(
                values=[[slot_start.strftime("%Y-%m-%d %H:%M"), slot_end.strftime("%Y-%m-%d %H:%M"), event_id, "Scheduled"]],
                range_name=f"G{task['row_num']}:J{task['row_num']}",
            )

            mg_last_end[task["mg_id"]] = cursor
            created_lines.append(f"• {task['title']} — {slot_start.strftime('%d %b %H:%M')}")

        summary = f"✅ Scheduled {len(created_lines)} tasks:\n\n" + "\n".join(created_lines[:20])
        if len(created_lines) > 20:
            summary += f"\n…and {len(created_lines) - 20} more"
        send(token, chat_id, summary)

    except Exception as e:
        send(token, chat_id, f"❌ Error in /pushtask: {e}")


# ── /pushevent ────────────────────────────────────────────────────────────────

def handle_pushevent(config: dict, token: str, chat_id: str, args: str) -> None:
    # Format: YYYY-MM-DD HH:MM HH:MM description
    parts = args.strip().split(" ", 3)
    if len(parts) < 4:
        send(token, chat_id,
             "Usage: /pushevent YYYY-MM-DD HH:MM HH:MM description\n"
             "Example: /pushevent 2026-05-01 14:00 16:00 Doctor appointment")
        return
    try:
        date_str, start_str, end_str, description = parts
        tz = pytz.timezone(config["timezone"])
        start_dt = tz.localize(datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M"))
        end_dt   = tz.localize(datetime.strptime(f"{date_str} {end_str}",   "%Y-%m-%d %H:%M"))

        cal = build("calendar", "v3", credentials=get_creds())
        event_body = {
            "summary": description,
            "start":   {"dateTime": start_dt.isoformat(), "timeZone": config["timezone"]},
            "end":     {"dateTime": end_dt.isoformat(),   "timeZone": config["timezone"]},
        }
        cal.events().insert(calendarId=config["calendar_id"], body=event_body).execute()

        send(token, chat_id,
             f"✅ Event added!\n<b>{description}</b>\n{date_str}  {start_str}–{end_str}\n\n"
             f"Tip: run /pushtask again if this overlaps with scheduled tasks.")

    except ValueError as e:
        send(token, chat_id, f"❌ Could not parse date/time: {e}")
    except Exception as e:
        send(token, chat_id, f"❌ Error in /pushevent: {e}")


# ── /newgoal ──────────────────────────────────────────────────────────────────

DECOMPOSE_SYSTEM = """You decompose goals into medium goals and small tasks.
Return ONLY valid JSON — no markdown, no explanation — in this exact schema:
{
  "goal": {"id": "goal_001", "title": "...", "created_date": "YYYY-MM-DD"},
  "medium_goals": [
    {
      "id": "mg_001", "title": "...", "estimated_hours": 8,
      "deadline_weeks": 2, "priority": 1, "depends_on": [],
      "small_tasks": [
        {"id": "st_001", "title": "...", "estimated_hours": 1.5, "order": 1}
      ]
    }
  ]
}
Rules:
- 3–8 medium goals
- 2–6 small tasks per medium goal
- each task: estimated_hours between 0.5 and 4.0
- priority: unique integers starting at 1 (1 = highest)
- depends_on: list of mg_XXX IDs whose tasks must ALL complete before this starts"""


def handle_newgoal(config: dict, token: str, chat_id: str, goal_text: str) -> None:
    import anthropic as anthropic_lib

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        send(token, chat_id, "❌ ANTHROPIC_API_KEY not configured in the Cloud Function.")
        return

    send(token, chat_id, f"🧠 Decomposing: <b>{goal_text}</b>\nThis takes ~15 seconds…")

    try:
        client = anthropic_lib.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=DECOMPOSE_SYSTEM,
            messages=[{"role": "user", "content": f"Goal: {goal_text}\nToday: {date.today()}"}],
        )
        raw = msg.content[0].text.strip()
        # Strip accidental markdown code fences
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())

        data = json.loads(raw)
        goal = data["goal"]
        medium_goals = data["medium_goals"]

        spreadsheet = open_sheet(config)
        short = goal["title"][:30] + ("…" if len(goal["title"]) > 30 else "")

        def _get_or_create(name, cols):
            try:
                ws = spreadsheet.worksheet(name)
            except gspread.WorksheetNotFound:
                ws = spreadsheet.add_worksheet(name, rows=1000, cols=len(cols))
            if not ws.row_values(1):
                ws.append_row(cols)
            return ws

        ov_ws  = _get_or_create(f"{short} — Overview",     OVERVIEW_COLS)
        mg_ws  = _get_or_create(f"{short} — Medium Goals", MEDIUM_GOALS_COLS)
        tsk_ws = _get_or_create(f"{short} — Tasks",        TASKS_COLS)

        total_tasks = sum(len(mg["small_tasks"]) for mg in medium_goals)
        total_hours = sum(st["estimated_hours"] for mg in medium_goals for st in mg["small_tasks"])

        ov_ws.append_row([goal["id"], goal["title"], goal["created_date"],
                          len(medium_goals), total_tasks, round(total_hours, 1), "Pending"])

        created_dt = datetime.strptime(goal["created_date"], "%Y-%m-%d").date()
        for mg in medium_goals:
            deadline = (created_dt + timedelta(weeks=mg["deadline_weeks"])).strftime("%Y-%m-%d")
            mg_ws.append_row([
                mg["id"], goal["id"], mg["title"], mg["priority"],
                mg["estimated_hours"], mg["deadline_weeks"], deadline,
                ", ".join(mg.get("depends_on", [])), len(mg["small_tasks"]), "Pending",
            ])
            for st in mg["small_tasks"]:
                tsk_ws.append_row([
                    st["id"], mg["id"], goal["id"], st["title"],
                    st["order"], st["estimated_hours"], "", "", "", "Pending",
                ])

        send(token, chat_id,
             f"✅ Goal created!\n\n<b>{goal['title']}</b>\n"
             f"{len(medium_goals)} medium goals | {total_tasks} tasks | ~{total_hours:.0f}h total\n\n"
             f"Send /pushtask to schedule them in your calendar.")

    except json.JSONDecodeError as e:
        send(token, chat_id, f"❌ AI returned invalid JSON: {e}")
    except Exception as e:
        send(token, chat_id, f"❌ Error in /newgoal: {e}")


# ── /goalcheck ────────────────────────────────────────────────────────────────

def handle_goalcheck(config: dict, token: str, chat_id: str) -> None:
    try:
        spreadsheet = open_sheet(config)
        tabs = get_goal_tabs(spreadsheet)

        if not tabs:
            send(token, chat_id, "No goals found in the Sheet yet.")
            return

        summaries = []
        for tabs_dict in tabs.values():
            if "overview" not in tabs_dict or "tasks" not in tabs_dict:
                continue

            ov_rows = tabs_dict["overview"].get_all_records()
            if not ov_rows:
                continue
            ov = ov_rows[0]

            task_rows = tabs_dict["tasks"].get_all_records()
            total     = len(task_rows)
            done      = sum(1 for r in task_rows if r.get("Status") == "Done")
            scheduled = sum(1 for r in task_rows if r.get("Status") == "Scheduled")
            pending   = sum(1 for r in task_rows if r.get("Status") == "Pending")

            nearest_deadline = None
            if "medium_goals" in tabs_dict:
                for mg_row in tabs_dict["medium_goals"].get_all_records():
                    dl_str = mg_row.get("Deadline Date", "")
                    if dl_str:
                        try:
                            dl = datetime.strptime(dl_str, "%Y-%m-%d").date()
                            if nearest_deadline is None or dl < nearest_deadline:
                                nearest_deadline = dl
                        except ValueError:
                            pass

            summaries.append({
                "title":    ov.get("Title", "?"),
                "total":    total,
                "done":     done,
                "scheduled": scheduled,
                "pending":  pending,
                "deadline": nearest_deadline,
            })

        # Sort: most done first, then earliest deadline
        summaries.sort(key=lambda g: (-g["done"], g["deadline"] or date(2099, 1, 1)))

        lines = [f"📊 <b>Goal Status — {date.today().strftime('%d %b %Y')}</b>\n"]
        for i, g in enumerate(summaries):
            pct = int(g["done"] / g["total"] * 100) if g["total"] else 0
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            dl  = f" | 📅 {g['deadline'].strftime('%d %b')}" if g["deadline"] else ""
            star = "⭐ " if i == 0 and g["done"] > 0 else ""
            lines.append(
                f"{star}<b>{g['title']}</b>\n"
                f"[{bar}] {pct}%\n"
                f"✅ {g['done']} done  📆 {g['scheduled']} scheduled  ⏳ {g['pending']} pending{dl}\n"
            )

        send(token, chat_id, "\n".join(lines))

    except Exception as e:
        send(token, chat_id, f"❌ Error in /goalcheck: {e}")


# ── Router ────────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "Available commands:\n\n"
    "/pushtask — schedule all pending tasks into your calendar\n"
    "/pushevent YYYY-MM-DD HH:MM HH:MM description — add a personal event\n"
    "/newgoal &lt;goal&gt; — decompose a new goal and save to Sheet\n"
    "/goalcheck — status summary of all goals"
)


def handle_update(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message") or {}
    text = msg.get("text", "").strip()
    if not text.startswith("/"):
        return

    config  = get_config()
    token   = os.environ.get("TELEGRAM_TOKEN") or config.get("telegram_token", "")
    chat_id = str(msg["chat"]["id"])

    parts   = text.split(" ", 1)
    command = parts[0].lower().split("@")[0]
    args    = parts[1] if len(parts) > 1 else ""

    if command == "/pushtask":
        handle_pushtask(config, token, chat_id)
    elif command == "/pushevent":
        handle_pushevent(config, token, chat_id, args)
    elif command == "/newgoal":
        if not args:
            send(token, chat_id, "Usage: /newgoal &lt;your goal&gt;\nExample: /newgoal Learn Spanish in 3 months")
        else:
            handle_newgoal(config, token, chat_id, args)
    elif command == "/goalcheck":
        handle_goalcheck(config, token, chat_id)
    else:
        send(token, chat_id, HELP_TEXT)
