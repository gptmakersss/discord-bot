# cogs/nishkia.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt
from typing import Optional, Any, List, Dict, Tuple

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
        "gun_storage": [],
        "meta": {
            "version": 1,
            "nishkia": {
                "last_daily_refresh_date": None,
                "nishkia_channel_message_id": None,
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

        db.setdefault("gun_storage", [])
        db.setdefault("meta", {}).setdefault("nishkia", {})
        mn = db["meta"]["nishkia"]
        mn.setdefault("last_daily_refresh_date", None)
        mn.setdefault("nishkia_channel_message_id", None)
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


def _norm(s: str) -> str:
    return " ".join((s or "").strip().split())


def _fmt_date_iso(d: Optional[str]) -> str:
    if not d:
        return "—"
    try:
        if "T" in d:
            # datetime
            x = dt.datetime.fromisoformat(d)
            return x.strftime("%d/%m/%Y %H:%M")
        # date
        x = dt.date.fromisoformat(d)
        return x.strftime("%d/%m/%Y")
    except Exception:
        return str(d)


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


def _row_line(r: dict) -> str:
    kind = _norm(r.get("kind") or "—")
    serial = _norm(str(r.get("serial") or "—"))
    soldier = _norm(r.get("soldier") or "לא ידוע")
    label = _norm(r.get("label") or "")
    stored = _fmt_date_iso(r.get("stored_date"))
    loc = _norm(r.get("location") or "—")
    custom_loc = _norm(r.get("custom_location") or "")
    remind = bool(r.get("remind"))
    due = _fmt_date_iso(r.get("next_due"))

    loc_show = loc
    if loc == "אחר" and custom_loc:
        loc_show = f"אחר: {custom_loc}"

    reminder_part = f" | תזכורת: {due}" if (remind and r.get("next_due")) else ""
    return f"- **{kind}** `{serial}` — **{soldier}** — {label} | אפסון: {stored} | מיקום: **{loc_show}**{reminder_part}"


def _format_nishkia(db: dict) -> str:
    rows: list[dict] = db.get("gun_storage", []) or []
    # keep only stored items (exclude "לא מאופסן")
    stored_rows = [r for r in rows if _norm(r.get("location") or "") and _norm(r.get("location") or "") != "לא מאופסן"]

    if not stored_rows:
        return "🏠 **נשקייה / בטחונית**\nאין פריטים מאופסנים כרגע."

    # group by location
    groups: dict[str, list[dict]] = {"נשקייה": [], "בטחונית": [], "אחר": []}
    other_bucket: list[dict] = []
    for r in stored_rows:
        loc = _norm(r.get("location") or "")
        if loc in groups:
            groups[loc].append(r)
        else:
            other_bucket.append(r)

    def sort_key(r: dict):
        # weapons with reminders first, then by due, then serial
        remind = 0 if r.get("remind") else 1
        due = r.get("next_due") or "9999"
        serial = str(r.get("serial") or "")
        return (remind, due, serial)

    for k in list(groups.keys()):
        groups[k].sort(key=sort_key)
    other_bucket.sort(key=sort_key)

    lines = ["🏠 **נשקייה / בטחונית — סטטוס אפסון**"]

    def add_section(title: str, lst: list[dict]):
        if not lst:
            return
        lines.append(f"\n**{title}:**")
        for r in lst[:120]:
            lines.append(_row_line(r))
        if len(lst) > 120:
            lines.append("… (קיצרתי)")

    add_section("נשקייה", groups["נשקייה"])
    add_section("בטחונית", groups["בטחונית"])
    add_section("אחר", groups["אחר"])
    if other_bucket:
        add_section("מיקומים לא מזוהים", other_bucket)

    return "\n".join(lines).strip()


async def _upsert_nishkia_message(bot: commands.Bot) -> None:
    db = load_db()
    content = _format_nishkia(db)

    ch_id = bot.CHANNELS.get("armory")  # חדר נשקייה לעדכון
    if not ch_id:
        return
    channel = bot.get_channel(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception:
            return

    prev_id = _safe_int(db.get("meta", {}).get("nishkia", {}).get("nishkia_channel_message_id"))
    if prev_id:
        try:
            msg = await channel.fetch_message(prev_id)
            await msg.delete()
        except Exception:
            pass

    try:
        new_msg = await channel.send(content)
        db.setdefault("meta", {}).setdefault("nishkia", {})["nishkia_channel_message_id"] = new_msg.id
        save_db(db)
    except Exception:
        pass


class NishkiaCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clock_loop.start()

    def cog_unload(self):
        self.clock_loop.cancel()

    @app_commands.command(name="nishkia", description="תצוגת סטטוס נשקייה/בטחונית")
    async def nishkia_cmd(self, interaction: discord.Interaction):
        db = load_db()
        content = _format_nishkia(db)
        if len(content) > 1900:
            content = content[:1900] + "\n… (קיצרתי)"
        await interaction.response.send_message(content, ephemeral=True)
        await send_log(self.bot, "NISHKIA", f"requested by {interaction.user.display_name}")

    # ===== daily auto refresh at 21:00 =====
    @tasks.loop(seconds=30)
    async def clock_loop(self):
        now = _now()
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")

        if hhmm != "21:00":
            return

        db = load_db()
        meta = db.setdefault("meta", {}).setdefault("nishkia", {})
        if meta.get("last_daily_refresh_date") == today:
            return

        meta["last_daily_refresh_date"] = today
        save_db(db)

        await _upsert_nishkia_message(self.bot)
        await send_log(self.bot, "NISHKIA DAILY", "רענון יומי של נשקייה/בטחונית (21:00)")

    @clock_loop.before_loop
    async def before_clock(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NishkiaCog(bot), guild=discord.Object(id=bot.GUILD_ID))
