# cogs/logistica.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt
from typing import Optional, Any, Dict, List

import discord
from discord import app_commands
from discord.ext import commands

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
        "sign": {},     # soldier -> {"items":[...], "other": str|None}
        "status": {},   # soldier -> item -> {"state":..., "note":...}
        "meta": {"version": 1},
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

        db.setdefault("soldiers", [])
        db.setdefault("sign", {})
        db.setdefault("status", {})
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


def _norm(s: str) -> str:
    return " ".join((s or "").strip().split())


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


STATE_EMOJI = {
    "תקין": "✅",
    "חוסר": "❌",
    "בלאי": "🟠",
    "צריך להגיש בלאי": "🟠",
    "אחר": "⚪",
}


def _get_signed_items(sign_rec: dict) -> list[str]:
    items = [ _norm(x) for x in (sign_rec.get("items") or []) if _norm(x) ]
    other = _norm(sign_rec.get("other") or "")
    if other:
        # represent other as a "virtual item" so it can get a status too
        items.append(f"אחר: {other}")
    # unique but stable ordering
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _get_item_status(status_db: dict, soldier: str, item: str) -> tuple[str, Optional[str]]:
    """
    Returns (state, note) for item.
    Default state: תקין
    """
    soldier_map = status_db.get(soldier, {}) or {}
    rec = soldier_map.get(item)
    if not rec:
        return ("תקין", None)
    st = _norm(rec.get("state") or "תקין")
    note = _norm(rec.get("note") or "")
    return (st if st else "תקין", note if note else None)


def _format_soldier_section(soldier: str, sign_db: dict, status_db: dict) -> str:
    sign_rec = sign_db.get(soldier) or {"items": [], "other": None}
    items = _get_signed_items(sign_rec)

    if not items:
        return f"👤 **{soldier}**\n— אין חתימות.\n"

    lines = [f"👤 **{soldier}**"]
    for it in items:
        st, note = _get_item_status(status_db, soldier, it)
        em = STATE_EMOJI.get(st, "•")
        line = f"- {em} **{it}** — {st}"
        if note:
            line += f" _(הערה: {note[:120]}{'…' if len(note) > 120 else ''})_"
        lines.append(line)

    return "\n".join(lines) + "\n"


def _format_logistica(db: dict, only_soldier: Optional[str] = None) -> str:
    soldiers = list(db.get("soldiers", []))
    sign_db = db.get("sign", {}) or {}
    status_db = db.get("status", {}) or {}

    # if no soldiers list yet, fallback to whoever exists in sign_db
    if not soldiers:
        soldiers = sorted(set(sign_db.keys()))

    if only_soldier:
        only_soldier = _norm(only_soldier)
        if only_soldier not in soldiers and only_soldier not in sign_db:
            return f"❌ לא נמצא חייל בשם **{only_soldier}**."

        header = f"📋 **לוגיסטיקה — {only_soldier}**"
        return header + "\n\n" + _format_soldier_section(only_soldier, sign_db, status_db)

    if not soldiers:
        return "📋 אין נתונים עדיין."

    blocks = ["📋 **לוגיסטיקה — חתימות + סטטוסים**"]
    shown = 0
    for s in soldiers:
        blocks.append(_format_soldier_section(s, sign_db, status_db))
        shown += 1
        if shown >= 30:  # prevent huge message
            blocks.append("… (קיצרתי. אפשר להריץ /logistica עם חייל ספציפי)")
            break

    return "\n".join(blocks).strip()


class LogisticaCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="logistica", description="דוח לוגיסטיקה: חתימות + סטטוסים (אפשר לבחור חייל)")
    @app_commands.describe(soldier="שם חייל (אופציונלי)")
    async def logistica_cmd(self, interaction: discord.Interaction, soldier: Optional[str] = None):
        db = load_db()
        content = _format_logistica(db, soldier)

        # Discord message limit safety
        if len(content) > 1900:
            content = content[:1900] + "\n… (קיצרתי בגלל מגבלת דיסקורד)"

        await interaction.response.send_message(content, ephemeral=True)
        await send_log(self.bot, "LOGISTICA", f"requested by {interaction.user.display_name} | soldier={_norm(soldier or '') or 'ALL'}")

    @logistica_cmd.autocomplete("soldier")
    async def soldier_autocomplete(self, interaction: discord.Interaction, current: str):
        db = load_db()
        soldiers = db.get("soldiers", [])
        cur = _norm(current).lower()
        matches = [s for s in soldiers if cur in s.lower()] if soldiers else []
        return [app_commands.Choice(name=s, value=s) for s in matches[:25]]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LogisticaCog(bot), guild=discord.Object(id=bot.GUILD_ID))
