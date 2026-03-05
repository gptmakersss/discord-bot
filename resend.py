cogs/resend.py

# cogs/resend.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt
from typing import Any, Optional, Tuple, List, Dict

import discord
from discord import app_commands
from discord.ext import commands

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Jerusalem")
except Exception:
    TZ = None

_LOCK = threading.RLock()
DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "db.json")


def _now() -> dt.datetime:
    return dt.datetime.now(TZ) if TZ is not None else dt.datetime.now()


def _ensure_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump({"meta": {}}, f, ensure_ascii=False, indent=2)


def load_db() -> dict:
    with _LOCK:
        _ensure_db()
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f) or {}
        db.setdefault("meta", {})
        return db


def save_db(db: dict) -> None:
    with _LOCK:
        _ensure_db()
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DB_FILE)


def _norm(x: Any) -> str:
    return " ".join(str(x or "").strip().split())


def _safe_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


async def _get_channel(bot: commands.Bot, key: str) -> Optional[discord.abc.Messageable]:
    ch_id = (getattr(bot, "CHANNELS", {}) or {}).get(key)
    if not ch_id:
        return None
    ch = bot.get_channel(int(ch_id))
    if ch is None:
        try:
            ch = await bot.fetch_channel(int(ch_id))
        except Exception:
            return None
    return ch


async def _delete_prev_message(channel: discord.abc.Messageable, msg_id: Optional[int]) -> bool:
    if not msg_id:
        return False
    try:
        msg = await channel.fetch_message(int(msg_id))  # type: ignore
        await msg.delete()
        return True
    except Exception:
        return False


def _set_meta_message_id(db: dict, section: str, field: str, msg_id: int) -> None:
    db.setdefault("meta", {}).setdefault(section, {})[field] = int(msg_id)


def _get_meta_message_id(db: dict, section: str, field: str) -> Optional[int]:
    return _safe_int(db.get("meta", {}).get(section, {}).get(field))


def _make_embed(title: str, description: str) -> discord.Embed:
    desc = (description or "").strip()
    if len(desc) > 3900:
        desc = desc[:3899] + "…"
    e = discord.Embed(title=title, description=desc)
    e.timestamp = _now()
    return e


# =========================
# Renderers
# =========================
def _render_generic(db: dict, key: str, title: str) -> Tuple[str, str]:
    val = db.get(key)
    if not val:
        return title, "— אין נתונים."
    if isinstance(val, dict):
        lines = []
        for k, v in list(val.items())[:120]:
            lines.append(f"• **{k}** — {str(v)[:220]}")
        return title, "\n".join(lines) if lines else "— אין נתונים."
    if isinstance(val, list):
        lines = [f"• {str(x)[:250]}" for x in val[:140]]
        return title, "\n".join(lines) if lines else "— אין נתונים."
    return title, str(val)[:1500]


def _render_attention_from_status(db: dict) -> Tuple[str, str]:
    """
    מסדר סמל אמיתי לפי db["status"]:
    status[soldier][item] = {"state": "...", "note": "..."}
    מציג כל מה ש-state != "תקין"
    """
    status_db = db.get("status", {}) or {}
    soldiers = list(db.get("soldiers", []) or [])

    if not isinstance(status_db, dict) or not status_db:
        return "מסדר סמל 🧾", "✅ אין פערים (הכול תקין)."

    if not soldiers:
        soldiers = sorted(status_db.keys())

    STATE_EMOJI = {
        "תקין": "✅",
        "חוסר": "❌",
        "בלאי": "🟠",
        "צריך להגיש בלאי": "🟠",
        "אחר": "⚪",
    }

    def rank(st: str) -> int:
        if st == "חוסר":
            return 0
        if st in ("בלאי", "צריך להגיש בלאי"):
            return 1
        if st == "אחר":
            return 2
        return 9

    lines: List[str] = ["🚨 **מסדר סמל — פערים**"]
    total = 0
    shown = 0

    for soldier in soldiers:
        smap = status_db.get(soldier, {})
        if not isinstance(smap, dict) or not smap:
            continue

        issues: List[Tuple[int, str]] = []
        for item, rec in smap.items():
            item_n = _norm(item)
            if not item_n:
                continue

            if not isinstance(rec, dict):
                rec = {}

            st = _norm(rec.get("state") or "תקין")
            note = _norm(rec.get("note") or "")

            if st != "תקין":
                total += 1
                em = STATE_EMOJI.get(st, "•")
                line = f"- {em} **{item_n}** — {st}"
                if note:
                    line += f" _(הערה: {note[:140]}{'…' if len(note) > 140 else ''})_"
                issues.append((rank(st), line))

        if issues:
            issues.sort(key=lambda x: (x[0], x[1]))
            shown += 1
            lines.append(f"\n👤 **{soldier}**")
            lines.extend([ln for _, ln in issues])

        if len(lines) > 1600:
            lines.append("\n… (קיצרתי בגלל מגבלת הודעה)")
            break

    if shown == 0:
        return "מסדר סמל 🧾", "✅ אין פערים (הכול תקין)."

    lines.append(f"\nסה״כ פערים: **{total}**")
    return "מסדר סמל 🧾", "\n".join(lines).strip()


# =========================
# Panels
# =========================
PANELS: list[dict] = [
    {"key": "tasks",     "section": "tasks",     "field": "tasks_channel_message_id",      "renderer": lambda db: _render_generic(db, "tasks", "משימות 📌")},
    {"key": "incidents", "section": "incident",  "field": "incidents_channel_message_id",  "renderer": lambda db: _render_generic(db, "incidents", "אירועים חריגים 🚨")},
    {"key": "notes",     "section": "notes",     "field": "notes_channel_message_id",      "renderer": lambda db: _render_generic(db, "notes", "פתקים 📝")},
    {"key": "inventory", "section": "inventory", "field": "inventory_channel_message_id",  "renderer": lambda db: _render_generic(db, "inventory", "ציוד סמליה 📦")},
    {"key": "signs",     "section": "sign",      "field": "signs_channel_message_id",      "renderer": lambda db: _render_generic(db, "sign", "חתימות ✍️")},
    {"key": "nishkia",   "section": "nishkia",   "field": "nishkia_channel_message_id",    "renderer": lambda db: _render_generic(db, "nishkia", "נשקייה 🧰")},
    {"key": "weapon_issues", "section": "weapon_issues", "field": "weapon_issues_channel_message_id", "renderer": lambda db: _render_generic(db, "weapon_issues", "פערי נשק 🎯")},

    # ✅ מסדר סמל לחדר הנכון אצלך: logi_gaps
    # ושומר/מוחק לפי meta.attention.attention_channel_message_id (כמו ב-DB שלך)
    {"key": "logi_gaps", "section": "attention", "field": "attention_channel_message_id", "renderer": _render_attention_from_status},
]


class ResendCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="resend", description="רענון: מוחק הודעה קודמת ושולח חדשה (Embed)")
    async def resend_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        db = load_db()
        sent = 0
        deleted = 0
        skipped = 0

        for p in PANELS:
            channel = await _get_channel(self.bot, p["key"])
            if channel is None:
                skipped += 1
                continue

            prev_id = _get_meta_message_id(db, p["section"], p["field"])
            if await _delete_prev_message(channel, prev_id):
                deleted += 1

            title, desc = p["renderer"](db)
            embed = _make_embed(title, desc)

            try:
                msg = await channel.send(embed=embed)  # type: ignore
            except Exception:
                skipped += 1
                continue

            _set_meta_message_id(db, p["section"], p["field"], msg.id)
            sent += 1

        save_db(db)

        await interaction.followup.send(
            f"✅ resend הסתיים.\nנשלחו: **{sent}** | נמחקו קודמות: **{deleted}** | דולגו: **{skipped}**",
            ephemeral=True
        )

        if hasattr(self.bot, "send_log"):
            try:
                await self.bot.send_log("RESEND", f"sent={sent} deleted={deleted} skipped={skipped}")
            except Exception:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ResendCog(bot), guild=bot.guild_obj)
