# cogs/incident.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt
from typing import Optional, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Jerusalem")
except Exception:
    TZ = None


# ===== local storage (kept inside this cog file) =====
_LOCK = threading.RLock()
DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "db.json")


def _ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _default_db() -> dict:
    return {
        "soldiers": [],
        "tasks": [],
        "incidents": [],  # list[dict]
        "meta": {
            "version": 1,
            "incidents": {
                "last_daily_refresh_date": None,     # "YYYY-MM-DD"
                "incidents_channel_message_id": None # int
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

        # ensure keys
        db.setdefault("incidents", [])
        db.setdefault("meta", {}).setdefault("incidents", {})
        mi = db["meta"]["incidents"]
        mi.setdefault("last_daily_refresh_date", None)
        mi.setdefault("incidents_channel_message_id", None)
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


def _format_incidents(inc_list: list[dict]) -> str:
    if not inc_list:
        return "✅ אין אירועים חריגים רשומים."

    # sort: newest first
    def key(x: dict):
        # created_at: "YYYY-MM-DD HH:MM:SS"
        return x.get("created_at") or ""

    inc_list = sorted(inc_list, key=key, reverse=True)

    lines = ["📌 **אירועים חריגים**"]
    for inc in inc_list[:80]:
        iid = inc.get("id")
        title = (inc.get("title") or "").strip() or "(ללא כותרת)"
        details = (inc.get("details") or "").strip()
        status = (inc.get("status") or "פתוח").strip()
        created = inc.get("created_at") or ""
        who = (inc.get("by") or "").strip()

        lines.append(f"- `#{iid}` **{title}** — סטטוס: **{status}**")
        meta = []
        if created:
            meta.append(created)
        if who:
            meta.append(f"ע\"י {who}")
        if meta:
            lines.append(f"  ↳ {' | '.join(meta)}")
        if details:
            lines.append(f"  ↳ {details[:220]}{'…' if len(details) > 220 else ''}")

    extra = max(0, len(inc_list) - 80)
    if extra:
        lines.append(f"\n… ועוד {extra} אירועים.")
    return "\n".join(lines).strip()


async def _upsert_incidents_message(bot: commands.Bot) -> None:
    db = load_db()
    inc_list = db.get("incidents", [])
    content = _format_incidents(inc_list)

    ch_id = bot.CHANNELS.get("incidents")
    if not ch_id:
        return
    channel = bot.get_channel(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception:
            return

    prev_id = _safe_int(db.get("meta", {}).get("incidents", {}).get("incidents_channel_message_id"))
    if prev_id:
        try:
            msg = await channel.fetch_message(prev_id)
            await msg.delete()
        except Exception:
            pass

    try:
        new_msg = await channel.send(content)
        db.setdefault("meta", {}).setdefault("incidents", {})["incidents_channel_message_id"] = new_msg.id
        save_db(db)
    except Exception:
        pass


class IncidentCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clock_loop.start()

    def cog_unload(self):
        self.clock_loop.cancel()

    incident = app_commands.Group(name="incident", description="אירועים חריגים (add/edit/remove/list)")

    @incident.command(name="add", description="הוספת אירוע חריג")
    @app_commands.describe(
        title="כותרת האירוע",
        details="פירוט (אופציונלי)",
        status="סטטוס (ברירת מחדל פתוח)"
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="פתוח", value="פתוח"),
        app_commands.Choice(name="בטיפול", value="בטיפול"),
        app_commands.Choice(name="סגור", value="סגור"),
    ])
    async def add_cmd(
        self,
        interaction: discord.Interaction,
        title: str,
        details: Optional[str] = None,
        status: Optional[app_commands.Choice[str]] = None,
    ):
        title = (title or "").strip()
        if not title:
            return await interaction.response.send_message("❌ חייב כותרת.", ephemeral=True)

        db = load_db()
        inc_list = db.setdefault("incidents", [])

        next_id = 1
        if inc_list:
            try:
                next_id = max(int(i.get("id", 0)) for i in inc_list) + 1
            except Exception:
                next_id = len(inc_list) + 1

        st = status.value if status else "פתוח"
        now_s = _now().strftime("%Y-%m-%d %H:%M:%S")
        author = interaction.user.display_name

        inc_obj = {
            "id": next_id,
            "title": title,
            "details": (details or "").strip(),
            "status": st,
            "created_at": now_s,
            "updated_at": now_s,
            "by": author,
        }
        inc_list.append(inc_obj)
        save_db(db)

        await interaction.response.send_message(f"✅ נוסף אירוע `#{next_id}`: **{title}**", ephemeral=True)
        await send_log(self.bot, "INCIDENT ADD", f"נוסף אירוע #{next_id}: {title} | סטטוס: {st}")

        await _upsert_incidents_message(self.bot)

    @incident.command(name="edit", description="עריכת אירוע חריג")
    @app_commands.describe(
        incident_id="מספר האירוע",
        title="כותרת חדשה (אופציונלי)",
        details="פירוט חדש (אופציונלי)",
        status="סטטוס חדש (אופציונלי)"
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="פתוח", value="פתוח"),
        app_commands.Choice(name="בטיפול", value="בטיפול"),
        app_commands.Choice(name="סגור", value="סגור"),
    ])
    async def edit_cmd(
        self,
        interaction: discord.Interaction,
        incident_id: int,
        title: Optional[str] = None,
        details: Optional[str] = None,
        status: Optional[app_commands.Choice[str]] = None,
    ):
        db = load_db()
        inc_list = db.get("incidents", [])
        inc = next((x for x in inc_list if int(x.get("id", -1)) == int(incident_id)), None)
        if not inc:
            return await interaction.response.send_message("❌ לא נמצא אירוע עם המספר הזה.", ephemeral=True)

        changes = []
        if title is not None:
            new_title = title.strip()
            if not new_title:
                return await interaction.response.send_message("❌ כותרת לא יכולה להיות ריקה.", ephemeral=True)
            if new_title != inc.get("title"):
                changes.append(f"title: '{inc.get('title')}' -> '{new_title}'")
                inc["title"] = new_title

        if details is not None:
            new_details = details.strip()
            if new_details != (inc.get("details") or ""):
                changes.append("details updated")
                inc["details"] = new_details

        if status is not None:
            new_st = status.value
            if new_st != inc.get("status"):
                changes.append(f"status: {inc.get('status')} -> {new_st}")
                inc["status"] = new_st

        if not changes:
            return await interaction.response.send_message("ℹ️ לא בוצע שינוי.", ephemeral=True)

        inc["updated_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")
        save_db(db)

        await interaction.response.send_message(f"✅ עודכן אירוע `#{incident_id}`.", ephemeral=True)
        await send_log(self.bot, "INCIDENT EDIT", f"עודכן אירוע #{incident_id}: " + " | ".join(changes))
        await _upsert_incidents_message(self.bot)

    @incident.command(name="remove", description="מחיקת אירוע חריג")
    @app_commands.describe(incident_id="מספר האירוע למחיקה")
    async def remove_cmd(self, interaction: discord.Interaction, incident_id: int):
        db = load_db()
        inc_list = db.get("incidents", [])

        before = len(inc_list)
        inc_list = [x for x in inc_list if int(x.get("id", -1)) != int(incident_id)]
        after = len(inc_list)

        if after == before:
            return await interaction.response.send_message("❌ לא נמצא אירוע עם המספר הזה.", ephemeral=True)

        db["incidents"] = inc_list
        save_db(db)

        await interaction.response.send_message(f"✅ נמחק אירוע `#{incident_id}`.", ephemeral=True)
        await send_log(self.bot, "INCIDENT REMOVE", f"נמחק אירוע #{incident_id}")
        await _upsert_incidents_message(self.bot)

    @incident.command(name="list", description="הצגת רשימת אירועים חריגים (תצוגה מהירה)")
    async def list_cmd(self, interaction: discord.Interaction):
        db = load_db()
        inc_list = db.get("incidents", [])
        content = _format_incidents(inc_list)
        await interaction.response.send_message(content, ephemeral=True)

    @edit_cmd.autocomplete("incident_id")
    @remove_cmd.autocomplete("incident_id")
    async def incident_id_autocomplete(self, interaction: discord.Interaction, current: str):
        db = load_db()
        inc_list = db.get("incidents", [])
        cur = (current or "").strip().lower()

        res = []
        for inc in sorted(inc_list, key=lambda x: x.get("created_at") or "", reverse=True):
            iid = str(inc.get("id"))
            title = (inc.get("title") or "").strip()
            label = f"#{iid} - {title}"[:95]
            if not cur or cur in iid or cur in title.lower():
                res.append(app_commands.Choice(name=label, value=int(iid)))
            if len(res) >= 25:
                break
        return res

    # ===== daily auto refresh at 21:00 =====
    @tasks.loop(seconds=30)
    async def clock_loop(self):
        now = _now()
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")

        if hhmm != "21:00":
            return

        db = load_db()
        meta = db.setdefault("meta", {}).setdefault("incidents", {})
        if meta.get("last_daily_refresh_date") == today:
            return

        meta["last_daily_refresh_date"] = today
        save_db(db)

        await _upsert_incidents_message(self.bot)
        await send_log(self.bot, "INCIDENTS DAILY", "רענון יומי של אירועים חריגים (21:00)")

    @clock_loop.before_loop
    async def before_clock(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(IncidentCog(bot), guild=discord.Object(id=bot.GUILD_ID))
