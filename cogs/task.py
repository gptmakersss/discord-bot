# cogs/task.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt
from dataclasses import dataclass
from typing import Optional, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Jerusalem")
except Exception:
    TZ = None  # fallback to naive local time


# ===== local storage (kept inside this cog file) =====
_LOCK = threading.RLock()
DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "db.json")


def _ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _default_db() -> dict:
    return {
        "soldiers": [],
        "tasks": [],  # list of task dicts
        "meta": {
            "version": 1,
            "tasks": {
                "last_daily_refresh_date": None,   # "YYYY-MM-DD"
                "last_cleanup_date": None,         # "YYYY-MM-DD"
                "tasks_channel_message_id": None,  # int
            }
        }
    }


def load_db() -> dict:
    with _LOCK:
        _ensure_dirs()
        if not os.path.exists(DB_FILE):
            db = _default_db()
            save_db(db)
            return db
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)

        # migrations / ensure keys
        db.setdefault("tasks", [])
        db.setdefault("meta", {}).setdefault("tasks", {})
        mt = db["meta"]["tasks"]
        mt.setdefault("last_daily_refresh_date", None)
        mt.setdefault("last_cleanup_date", None)
        mt.setdefault("tasks_channel_message_id", None)
        return db


def save_db(db: dict) -> None:
    with _LOCK:
        _ensure_dirs()
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DB_FILE)


def _now() -> dt.datetime:
    if TZ is not None:
        return dt.datetime.now(TZ)
    return dt.datetime.now()


def _today_str() -> str:
    return _now().strftime("%Y-%m-%d")


def _parse_date(s: str) -> Optional[dt.date]:
    s = (s or "").strip()
    if not s:
        return None
    # Accept YYYY-MM-DD
    try:
        y, m, d = s.split("-")
        return dt.date(int(y), int(m), int(d))
    except Exception:
        return None


def _date_to_str(d: Optional[dt.date]) -> Optional[str]:
    return d.strftime("%Y-%m-%d") if d else None


def _priority_rank(priority: str, priorities: list[str]) -> int:
    try:
        return priorities.index(priority)
    except Exception:
        return len(priorities) + 9


def _safe_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


async def send_log(bot: commands.Bot, title: str, details: str) -> None:
    """Just sends a log message to the logs channel (no persistence)."""
    ch_id = bot.CHANNELS.get("logs")
    if not ch_id:
        return
    channel = bot.get_channel(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception:
            return

    ts = _now().strftime("%Y-%m-%d %H:%M:%S")
    embed = discord.Embed(title=title, description=details)
    embed.set_footer(text=ts)
    try:
        await channel.send(embed=embed)
    except Exception:
        pass


def _format_tasks(tasks_list: list[dict], priorities: list[str]) -> str:
    """
    Returns a readable markdown string, grouped by priority and sorted by date.
    Shows only non-completed tasks.
    """
    open_tasks = [t for t in tasks_list if t.get("status") != "בוצע"]

    # sort: priority rank then date (None last), then created_at
    def key(t: dict):
        pr = _priority_rank(t.get("priority", ""), priorities)
        d = t.get("date")  # "YYYY-MM-DD" or None
        d_key = d if d else "9999-99-99"
        created = t.get("created_at") or "9999-99-99 99:99:99"
        return (pr, d_key, created)

    open_tasks.sort(key=key)

    if not open_tasks:
        return "✅ אין משימות פתוחות."

    groups: dict[str, list[dict]] = {p: [] for p in priorities}
    groups["(ללא דחיפות)"] = []

    for t in open_tasks:
        p = t.get("priority") or "(ללא דחיפות)"
        if p not in groups:
            groups[p] = []
        groups[p].append(t)

    lines: list[str] = []
    emoji = {"דחוף": "🔴", "גבוה": "🟠", "בינוני": "🟡", "נמוך": "🟢", "(ללא דחיפות)": "⚪"}

    # order: priorities then "(ללא דחיפות)"
    ordered = priorities + ["(ללא דחיפות)"]
    for p in ordered:
        items = groups.get(p) or []
        if not items:
            continue
        lines.append(f"{emoji.get(p,'•')} **{p}**")
        for t in items:
            tid = t.get("id")
            title = t.get("title", "").strip() or "(ללא כותרת)"
            date_s = t.get("date")
            details = (t.get("details") or "").strip()

            suffix = []
            if date_s:
                suffix.append(f"יעד: {date_s}")
            if t.get("status") and t.get("status") != "פתוח":
                suffix.append(f"סטטוס: {t.get('status')}")

            suffix_txt = (" — " + " | ".join(suffix)) if suffix else ""
            lines.append(f"- `#{tid}` **{title}**{suffix_txt}")
            if details:
                # keep it compact
                lines.append(f"  ↳ {details[:180]}{'…' if len(details) > 180 else ''}")
        lines.append("")  # spacer

    return "\n".join(lines).strip()


async def _upsert_tasks_message(bot: commands.Bot) -> None:
    """Delete previous tasks message (if exists) and post a refreshed one in tasks channel."""
    db = load_db()
    tasks_list = db.get("tasks", [])
    content = _format_tasks(tasks_list, bot.TASK_PRIORITIES)

    ch_id = bot.CHANNELS.get("tasks")
    if not ch_id:
        return

    channel = bot.get_channel(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception:
            return

    # delete previous message if we stored its id
    prev_id = _safe_int(db.get("meta", {}).get("tasks", {}).get("tasks_channel_message_id"))
    if prev_id:
        try:
            msg = await channel.fetch_message(prev_id)
            await msg.delete()
        except Exception:
            pass

    # send new message
    try:
        new_msg = await channel.send(content)
        db.setdefault("meta", {}).setdefault("tasks", {})["tasks_channel_message_id"] = new_msg.id
        save_db(db)
    except Exception:
        pass


class TaskCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clock_loop.start()

    def cog_unload(self):
        self.clock_loop.cancel()

    # ===== Slash commands group =====
    task = app_commands.Group(name="task", description="ניהול משימות (add/edit/list)")

    @task.command(name="add", description="הוספת משימה")
    @app_commands.describe(
        title="כותרת קצרה",
        details="פירוט (אופציונלי)",
        priority="דחיפות",
        date="תאריך יעד בפורמט YYYY-MM-DD (אופציונלי)"
    )
    @app_commands.choices(priority=[
        app_commands.Choice(name="דחוף", value="דחוף"),
        app_commands.Choice(name="גבוה", value="גבוה"),
        app_commands.Choice(name="בינוני", value="בינוני"),
        app_commands.Choice(name="נמוך", value="נמוך"),
    ])
    async def add_cmd(
        self,
        interaction: discord.Interaction,
        title: str,
        details: Optional[str] = None,
        priority: app_commands.Choice[str] = None,
        date: Optional[str] = None,
    ):
        title = (title or "").strip()
        if not title:
            return await interaction.response.send_message("❌ חייב כותרת.", ephemeral=True)

        d = _parse_date(date or "")
        if date and not d:
            return await interaction.response.send_message("❌ תאריך לא תקין. פורמט נכון: YYYY-MM-DD", ephemeral=True)

        db = load_db()
        tasks_list = db.setdefault("tasks", [])

        # create incremental id
        next_id = 1
        if tasks_list:
            try:
                next_id = max(int(t.get("id", 0)) for t in tasks_list) + 1
            except Exception:
                next_id = len(tasks_list) + 1

        pr = priority.value if priority else "בינוני"
        now_s = _now().strftime("%Y-%m-%d %H:%M:%S")

        task_obj = {
            "id": next_id,
            "title": title,
            "details": (details or "").strip(),
            "priority": pr,
            "date": _date_to_str(d),
            "status": "פתוח",
            "created_at": now_s,
            "updated_at": now_s,
        }

        tasks_list.append(task_obj)
        save_db(db)

        await interaction.response.send_message(f"✅ נוספה משימה `#{next_id}`: **{title}**", ephemeral=True)
        await send_log(self.bot, "TASK ADD", f"נוספה משימה #{next_id}: {title} | דחיפות: {pr} | יעד: {task_obj['date'] or '-'}")

        # refresh tasks room message (optional immediate)
        await _upsert_tasks_message(self.bot)

    @task.command(name="edit", description="עריכת משימה קיימת")
    @app_commands.describe(
        task_id="מספר המשימה (למשל 12)",
        title="כותרת חדשה (אופציונלי)",
        details="פירוט חדש (אופציונלי)",
        priority="דחיפות חדשה (אופציונלי)",
        date="תאריך יעד חדש YYYY-MM-DD (ריק = להסיר תאריך)",
        status="סטטוס (אופציונלי)"
    )
    @app_commands.choices(priority=[
        app_commands.Choice(name="דחוף", value="דחוף"),
        app_commands.Choice(name="גבוה", value="גבוה"),
        app_commands.Choice(name="בינוני", value="בינוני"),
        app_commands.Choice(name="נמוך", value="נמוך"),
    ])
    @app_commands.choices(status=[
        app_commands.Choice(name="פתוח", value="פתוח"),
        app_commands.Choice(name="בטיפול", value="בטיפול"),
        app_commands.Choice(name="בוצע", value="בוצע"),
    ])
    async def edit_cmd(
        self,
        interaction: discord.Interaction,
        task_id: int,
        title: Optional[str] = None,
        details: Optional[str] = None,
        priority: Optional[app_commands.Choice[str]] = None,
        date: Optional[str] = None,
        status: Optional[app_commands.Choice[str]] = None,
    ):
        db = load_db()
        tasks_list = db.get("tasks", [])

        t = next((x for x in tasks_list if int(x.get("id", -1)) == int(task_id)), None)
        if not t:
            return await interaction.response.send_message("❌ לא נמצאה משימה עם המספר הזה.", ephemeral=True)

        changes = []

        if title is not None:
            new_title = title.strip()
            if not new_title:
                return await interaction.response.send_message("❌ כותרת לא יכולה להיות ריקה.", ephemeral=True)
            if new_title != t.get("title"):
                changes.append(f"title: '{t.get('title')}' -> '{new_title}'")
                t["title"] = new_title

        if details is not None:
            new_details = details.strip()
            if new_details != (t.get("details") or ""):
                changes.append("details updated")
                t["details"] = new_details

        if priority is not None:
            new_pr = priority.value
            if new_pr != t.get("priority"):
                changes.append(f"priority: {t.get('priority')} -> {new_pr}")
                t["priority"] = new_pr

        if status is not None:
            new_st = status.value
            if new_st != t.get("status"):
                changes.append(f"status: {t.get('status')} -> {new_st}")
                t["status"] = new_st

        if date is not None:
            # empty string means remove date
            if date.strip() == "":
                if t.get("date") is not None:
                    changes.append(f"date: {t.get('date')} -> (removed)")
                    t["date"] = None
            else:
                d = _parse_date(date)
                if not d:
                    return await interaction.response.send_message("❌ תאריך לא תקין. פורמט נכון: YYYY-MM-DD", ephemeral=True)
                new_date_s = _date_to_str(d)
                if new_date_s != t.get("date"):
                    changes.append(f"date: {t.get('date') or '-'} -> {new_date_s}")
                    t["date"] = new_date_s

        if not changes:
            return await interaction.response.send_message("ℹ️ לא בוצע שינוי (לא שלחת ערכים חדשים).", ephemeral=True)

        t["updated_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")
        save_db(db)

        await interaction.response.send_message(f"✅ עודכנה משימה `#{task_id}`.", ephemeral=True)
        await send_log(self.bot, "TASK EDIT", f"עודכנה משימה #{task_id}: " + " | ".join(changes))

        await _upsert_tasks_message(self.bot)

    @task.command(name="list", description="הצגת משימות פתוחות (מסודר לפי דחיפות ואז תאריך)")
    async def list_cmd(self, interaction: discord.Interaction):
        db = load_db()
        tasks_list = db.get("tasks", [])
        content = _format_tasks(tasks_list, self.bot.TASK_PRIORITIES)
        await interaction.response.send_message(content, ephemeral=True)

    @edit_cmd.autocomplete("task_id")
    async def task_id_autocomplete(self, interaction: discord.Interaction, current: str):
        db = load_db()
        tasks_list = db.get("tasks", [])
        # only show not completed by default
        tasks_list = [t for t in tasks_list if t.get("status") != "בוצע"]

        cur = (current or "").strip()
        res = []
        for t in tasks_list:
            tid = str(t.get("id"))
            title = (t.get("title") or "").strip()
            label = f"#{tid} - {title}"[:95]
            if not cur or cur in tid or cur.lower() in title.lower():
                res.append(app_commands.Choice(name=label, value=int(tid)))
            if len(res) >= 25:
                break
        return res

    # ===== background scheduler (runs every 30s and triggers once per day per target time) =====
    @tasks.loop(seconds=30)
    async def clock_loop(self):
        now = _now()
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")

        db = load_db()
        meta = db.setdefault("meta", {}).setdefault("tasks", {})

        # 21:00 daily refresh message in tasks channel
        if hhmm == "21:00" and meta.get("last_daily_refresh_date") != today:
            meta["last_daily_refresh_date"] = today
            save_db(db)
            await _upsert_tasks_message(self.bot)
            await send_log(self.bot, "TASKS DAILY", "רענון יומי של הודעת משימות (21:00)")

        # 03:00 cleanup completed tasks
        if hhmm == "03:00" and meta.get("last_cleanup_date") != today:
            # remove completed
            tasks_list = db.get("tasks", [])
            before = len(tasks_list)
            tasks_list = [t for t in tasks_list if t.get("status") != "בוצע"]
            removed = before - len(tasks_list)

            db["tasks"] = tasks_list
            meta["last_cleanup_date"] = today
            save_db(db)

            if removed > 0:
                await send_log(self.bot, "TASKS CLEANUP", f"נמחקו {removed} משימות שבוצעו (03:00).")
            else:
                await send_log(self.bot, "TASKS CLEANUP", "לא היו משימות 'בוצע' למחיקה (03:00).")

            await _upsert_tasks_message(self.bot)

    @clock_loop.before_loop
    async def before_clock(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TaskCog(bot), guild=discord.Object(id=bot.GUILD_ID))
