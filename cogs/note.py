# cogs/note.py
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
        "incidents": [],
        "notes": [],  # list[dict]
        "meta": {
            "version": 1,
            "notes": {
                "last_daily_refresh_date": None,  # "YYYY-MM-DD"
                "notes_channel_message_id": None, # int
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

        db.setdefault("notes", [])
        db.setdefault("meta", {}).setdefault("notes", {})
        mn = db["meta"]["notes"]
        mn.setdefault("last_daily_refresh_date", None)
        mn.setdefault("notes_channel_message_id", None)
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


def _safe_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


async def send_log(bot: commands.Bot, title: str, details: str) -> None:
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


def _format_notes(notes: list[dict]) -> str:
    if not notes:
        return "📝 אין פתקים."

    # newest first
    notes_sorted = sorted(notes, key=lambda x: x.get("created_at") or "", reverse=True)

    lines = ["🗒️ **פתקים**"]
    for n in notes_sorted[:120]:
        nid = n.get("id")
        text = (n.get("text") or "").strip()
        created = n.get("created_at") or ""
        tag = (n.get("tag") or "").strip()

        head = f"- `#{nid}` {text[:220]}{'…' if len(text) > 220 else ''}"
        if tag:
            head += f"  _(tag: {tag})_"
        lines.append(head)
        if created:
            lines.append(f"  ↳ {created}")

    extra = max(0, len(notes_sorted) - 120)
    if extra:
        lines.append(f"\n… ועוד {extra} פתקים.")
    return "\n".join(lines).strip()


async def _upsert_notes_message(bot: commands.Bot) -> None:
    db = load_db()
    notes = db.get("notes", [])
    content = _format_notes(notes)

    ch_id = bot.CHANNELS.get("notes")
    if not ch_id:
        return
    channel = bot.get_channel(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception:
            return

    prev_id = _safe_int(db.get("meta", {}).get("notes", {}).get("notes_channel_message_id"))
    if prev_id:
        try:
            msg = await channel.fetch_message(prev_id)
            await msg.delete()
        except Exception:
            pass

    try:
        new_msg = await channel.send(content)
        db.setdefault("meta", {}).setdefault("notes", {})["notes_channel_message_id"] = new_msg.id
        save_db(db)
    except Exception:
        pass


class NoteCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clock_loop.start()

    def cog_unload(self):
        self.clock_loop.cancel()

    note = app_commands.Group(name="note", description="פתקים (add/remove/list)")

    @note.command(name="add", description="הוספת פתק")
    @app_commands.describe(text="טקסט הפתק", tag="תגית קצרה (אופציונלי)")
    async def add_cmd(self, interaction: discord.Interaction, text: str, tag: Optional[str] = None):
        text = (text or "").strip()
        if not text:
            return await interaction.response.send_message("❌ טקסט ריק.", ephemeral=True)

        db = load_db()
        notes = db.setdefault("notes", [])

        next_id = 1
        if notes:
            try:
                next_id = max(int(n.get("id", 0)) for n in notes) + 1
            except Exception:
                next_id = len(notes) + 1

        now_s = _now().strftime("%Y-%m-%d %H:%M:%S")

        note_obj = {
            "id": next_id,
            "text": text,
            "tag": (tag or "").strip(),
            "created_at": now_s,
        }
        notes.append(note_obj)
        save_db(db)

        await interaction.response.send_message(f"✅ נוסף פתק `#{next_id}`.", ephemeral=True)
        await send_log(self.bot, "NOTE ADD", f"נוסף פתק #{next_id}: {text[:180]}")

        # immediate refresh
        await _upsert_notes_message(self.bot)

    @note.command(name="remove", description="מחיקת פתק")
    @app_commands.describe(note_id="מספר הפתק למחיקה")
    async def remove_cmd(self, interaction: discord.Interaction, note_id: int):
        db = load_db()
        notes = db.get("notes", [])

        before = len(notes)
        notes = [n for n in notes if int(n.get("id", -1)) != int(note_id)]
        after = len(notes)

        if after == before:
            return await interaction.response.send_message("❌ לא נמצא פתק עם המספר הזה.", ephemeral=True)

        db["notes"] = notes
        save_db(db)

        await interaction.response.send_message(f"✅ נמחק פתק `#{note_id}`.", ephemeral=True)
        await send_log(self.bot, "NOTE REMOVE", f"נמחק פתק #{note_id}")
        await _upsert_notes_message(self.bot)

    @note.command(name="list", description="הצגת פתקים")
    async def list_cmd(self, interaction: discord.Interaction):
        db = load_db()
        notes = db.get("notes", [])
        await interaction.response.send_message(_format_notes(notes), ephemeral=True)

    @remove_cmd.autocomplete("note_id")
    async def note_id_autocomplete(self, interaction: discord.Interaction, current: str):
        db = load_db()
        notes = db.get("notes", [])
        cur = (current or "").strip().lower()

        res = []
        for n in sorted(notes, key=lambda x: x.get("created_at") or "", reverse=True):
            nid = str(n.get("id"))
            text = (n.get("text") or "").strip()
            label = f"#{nid} - {text}"[:95]
            if not cur or cur in nid or cur in text.lower():
                res.append(app_commands.Choice(name=label, value=int(nid)))
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
        meta = db.setdefault("meta", {}).setdefault("notes", {})
        if meta.get("last_daily_refresh_date") == today:
            return

        meta["last_daily_refresh_date"] = today
        save_db(db)

        await _upsert_notes_message(self.bot)
        await send_log(self.bot, "NOTES DAILY", "רענון יומי של פתקים (21:00)")

    @clock_loop.before_loop
    async def before_clock(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NoteCog(bot), guild=discord.Object(id=bot.GUILD_ID))
