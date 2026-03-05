# cogs/weapon_issues.py
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
        "soldiers": [],
        "status": {},  # soldier -> item -> {"state","note","updated_at"}
        "meta": {
            "version": 1,
            "weapon_issues": {
                "last_daily_refresh_date": None,
                "weapon_issues_message_id": None,
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
        db.setdefault("status", {})
        db.setdefault("meta", {}).setdefault("weapon_issues", {})
        mw = db["meta"]["weapon_issues"]
        mw.setdefault("last_daily_refresh_date", None)
        mw.setdefault("weapon_issues_message_id", None)
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

BAD_STATES = {"חוסר", "בלאי", "צריך להגיש בלאי", "אחר"}


def _is_weapon_item(item: str) -> bool:
    it = _norm(item)
    if it in ("נשק", "צלם"):
        return True
    # allow "אחר: 12345" to represent a specific weapon serial or asset
    if it.startswith("אחר:"):
        return True
    return False


def _format_weapon_issues(db: dict) -> str:
    st_db: dict = db.get("status", {}) or {}

    # gather issues
    by_soldier: dict[str, list[Tuple[str, str, Optional[str]]]] = {}
    for soldier, items in st_db.items():
        s = _norm(soldier)
        if not s:
            continue
        for item, rec in (items or {}).items():
            it = _norm(item)
            if not it or not _is_weapon_item(it):
                continue
            st = _norm((rec or {}).get("state") or "תקין")
            note = _norm((rec or {}).get("note") or "") or None
            if st in BAD_STATES:
                by_soldier.setdefault(s, []).append((it, st, note))

    if not by_soldier:
        return "🛠️ **פערי נשק**\n✅ אין פערים (הכול תקין)."

    # sort
    def rank_state(st: str) -> int:
        if st == "חוסר":
            return 0
        if st in ("בלאי", "צריך להגיש בלאי"):
            return 1
        if st == "אחר":
            return 2
        return 9

    lines = ["🛠️ **פערי נשק (נשק/צלמים לא תקינים)**"]
    total = 0

    for soldier in sorted(by_soldier.keys()):
        lines.append(f"\n👤 **{soldier}**")
        lst = sorted(by_soldier[soldier], key=lambda x: (rank_state(x[1]), x[0]))
        for it, st, note in lst:
            total += 1
            em = STATE_EMOJI.get(st, "•")
            line = f"- {em} **{it}** — {st}"
            if note:
                line += f" _(הערה: {note[:140]}{'…' if len(note) > 140 else ''})_"
            lines.append(line)

        if len("\n".join(lines)) > 1750:
            lines.append("\n… (קיצרתי)")
            break

    lines.append(f"\nסה״כ פערים: **{total}**")
    return "\n".join(lines).strip()


async def _upsert_weapon_issues_message(bot: commands.Bot) -> None:
    db = load_db()
    content = _format_weapon_issues(db)

    ch_id = bot.CHANNELS.get("weapon_issues")  # פערי נשק
    if not ch_id:
        return
    channel = bot.get_channel(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception:
            return

    prev_id = _safe_int(db.get("meta", {}).get("weapon_issues", {}).get("weapon_issues_message_id"))
    if prev_id:
        try:
            msg = await channel.fetch_message(prev_id)
            await msg.delete()
        except Exception:
            pass

    try:
        new_msg = await channel.send(content)
        db.setdefault("meta", {}).setdefault("weapon_issues", {})["weapon_issues_message_id"] = new_msg.id
        save_db(db)
    except Exception:
        pass


class WeaponIssuesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clock_loop.start()

    def cog_unload(self):
        self.clock_loop.cancel()

    @app_commands.command(name="weapon_issues", description="תצוגת פערי נשק (נשק/צלמים לא תקינים)")
    async def weapon_issues_cmd(self, interaction: discord.Interaction):
        db = load_db()
        content = _format_weapon_issues(db)
        if len(content) > 1900:
            content = content[:1900] + "\n… (קיצרתי)"
        await interaction.response.send_message(content, ephemeral=True)
        await send_log(self.bot, "WEAPON_ISSUES", f"requested by {interaction.user.display_name}")

    # ===== daily auto refresh at 21:00 =====
    @tasks.loop(seconds=30)
    async def clock_loop(self):
        now = _now()
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")

        if hhmm != "21:00":
            return

        db = load_db()
        meta = db.setdefault("meta", {}).setdefault("weapon_issues", {})
        if meta.get("last_daily_refresh_date") == today:
            return

        meta["last_daily_refresh_date"] = today
        save_db(db)

        await _upsert_weapon_issues_message(self.bot)
        await send_log(self.bot, "WEAPON_ISSUES DAILY", "רענון יומי של פערי נשק (21:00)")

    @clock_loop.before_loop
    async def before_clock(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WeaponIssuesCog(bot), guild=discord.Object(id=bot.GUILD_ID))
