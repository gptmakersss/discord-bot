# cogs/attention.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt
from typing import Optional, Any, Dict, List, Tuple

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
        "sign": {},
        "status": {},
        "meta": {
            "version": 1,
            "attention": {
                "last_daily_refresh_date": None,   # "YYYY-MM-DD"
                "attention_channel_message_id": None,  # int
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

        db.setdefault("soldiers", [])
        db.setdefault("sign", {})
        db.setdefault("status", {})
        db.setdefault("meta", {}).setdefault("attention", {})
        ma = db["meta"]["attention"]
        ma.setdefault("last_daily_refresh_date", None)
        ma.setdefault("attention_channel_message_id", None)
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

# Any non-תקין is considered attention
ATTENTION_STATES = {"חוסר", "בלאי", "צריך להגיש בלאי", "אחר"}


def _get_signed_items(sign_rec: dict) -> list[str]:
    items = [_norm(x) for x in (sign_rec.get("items") or []) if _norm(x)]
    other = _norm(sign_rec.get("other") or "")
    if other:
        items.append(f"אחר: {other}")

    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _collect_attention_for_soldier(soldier: str, sign_db: dict, status_db: dict) -> list[Tuple[str, str, Optional[str]]]:
    """
    Returns list of (item, state, note) that are NOT תקין.
    Only items that have an explicit status record != תקין will appear.
    (Default if no status record: תקין -> not shown)
    """
    out: list[Tuple[str, str, Optional[str]]] = []

    soldier_map: dict = status_db.get(soldier, {}) or {}
    for item, rec in soldier_map.items():
        item_n = _norm(item)
        if not item_n:
            continue
        st = _norm((rec or {}).get("state") or "תקין")
        note = _norm((rec or {}).get("note") or "") or None

        if st != "תקין":
            out.append((item_n, st, note))

    # stable sort: state then item
    def key(x):
        item, st, _ = x
        st_rank = 0
        if st == "חוסר":
            st_rank = 0
        elif st in ("בלאי", "צריך להגיש בלאי"):
            st_rank = 1
        elif st == "אחר":
            st_rank = 2
        else:
            st_rank = 9
        return (st_rank, item)

    out.sort(key=key)
    return out


def _format_attention(db: dict, only_soldier: Optional[str] = None) -> str:
    soldiers = list(db.get("soldiers", []))
    sign_db = db.get("sign", {}) or {}
    status_db = db.get("status", {}) or {}

    # fallback if no soldiers list yet
    if not soldiers:
        soldiers = sorted(set(list(sign_db.keys()) + list(status_db.keys())))

    if only_soldier:
        s = _norm(only_soldier)
        if not s:
            return "❌ שם לא תקין."
        if s not in soldiers and s not in sign_db and s not in status_db:
            return f"❌ לא נמצא חייל בשם **{s}**."

        att = _collect_attention_for_soldier(s, sign_db, status_db)
        if not att:
            return f"✅ **{s}** — אין פערים (הכול תקין)."

        lines = [f"🚨 **מסדר סמל — {s}**"]
        for item, st, note in att:
            em = STATE_EMOJI.get(st, "•")
            line = f"- {em} **{item}** — {st}"
            if note:
                line += f" _(הערה: {note[:140]}{'…' if len(note) > 140 else ''})_"
            lines.append(line)
        return "\n".join(lines).strip()

    # all soldiers
    lines = ["🚨 **מסדר סמל — פערים לוגיסטיים**"]
    total_issues = 0
    shown_soldiers = 0

    for s in soldiers:
        att = _collect_attention_for_soldier(s, sign_db, status_db)
        if not att:
            continue
        shown_soldiers += 1
        lines.append(f"\n👤 **{s}**")
        for item, st, note in att:
            total_issues += 1
            em = STATE_EMOJI.get(st, "•")
            line = f"- {em} **{item}** — {st}"
            if note:
                line += f" _(הערה: {note[:140]}{'…' if len(note) > 140 else ''})_"
            lines.append(line)

        # prevent huge messages
        if len(lines) > 1700:
            lines.append("\n… (קיצרתי בגלל מגבלת הודעה. אפשר להריץ /attention עם חייל ספציפי)")
            break

    if shown_soldiers == 0:
        return "✅ אין פערים (הכול תקין)."

    lines.append(f"\nסה״כ פערים: **{total_issues}**")
    return "\n".join(lines).strip()


async def _upsert_attention_message(bot: commands.Bot) -> None:
    db = load_db()
    content = _format_attention(db)

    ch_id = bot.CHANNELS.get("logi_gaps")  # מסדר סמל
    if not ch_id:
        return

    channel = bot.get_channel(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception:
            return

    prev_id = _safe_int(db.get("meta", {}).get("attention", {}).get("attention_channel_message_id"))
    if prev_id:
        try:
            msg = await channel.fetch_message(prev_id)
            await msg.delete()
        except Exception:
            pass

    try:
        new_msg = await channel.send(content)
        db.setdefault("meta", {}).setdefault("attention", {})["attention_channel_message_id"] = new_msg.id
        save_db(db)
    except Exception:
        pass


class AttentionCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clock_loop.start()

    def cog_unload(self):
        self.clock_loop.cancel()

    @app_commands.command(name="attention", description="מסדר סמל: כל מה שלא תקין (אפשר לבחור חייל)")
    @app_commands.describe(soldier="שם חייל (אופציונלי)")
    async def attention_cmd(self, interaction: discord.Interaction, soldier: Optional[str] = None):
        db = load_db()
        content = _format_attention(db, soldier)

        if len(content) > 1900:
            content = content[:1900] + "\n… (קיצרתי בגלל מגבלת דיסקורד)"

        await interaction.response.send_message(content, ephemeral=True)
        await send_log(self.bot, "ATTENTION", f"requested by {interaction.user.display_name} | soldier={_norm(soldier or '') or 'ALL'}")

    @attention_cmd.autocomplete("soldier")
    async def soldier_autocomplete(self, interaction: discord.Interaction, current: str):
        db = load_db()
        soldiers = db.get("soldiers", [])
        cur = _norm(current).lower()
        matches = [s for s in soldiers if cur in s.lower()] if soldiers else []
        return [app_commands.Choice(name=s, value=s) for s in matches[:25]]

    # ===== daily auto refresh at 21:00 =====
    @tasks.loop(seconds=30)
    async def clock_loop(self):
        now = _now()
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")

        if hhmm != "21:00":
            return

        db = load_db()
        meta = db.setdefault("meta", {}).setdefault("attention", {})
        if meta.get("last_daily_refresh_date") == today:
            return

        meta["last_daily_refresh_date"] = today
        save_db(db)

        await _upsert_attention_message(self.bot)
        await send_log(self.bot, "ATTENTION DAILY", "רענון יומי של מסדר סמל (21:00)")

    @clock_loop.before_loop
    async def before_clock(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AttentionCog(bot), guild=discord.Object(id=bot.GUILD_ID))
