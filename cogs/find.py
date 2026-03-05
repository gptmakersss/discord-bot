# cogs/find.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import re
import threading
import datetime as dt
from typing import Optional, Any, List, Dict

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
        "link": [],  # list of {"id": int, "soldier": str, "serial": str, "label": str, "updated_at": str}
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
        db.setdefault("link", [])
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


def _digits_only(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


class FindCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="find", description="איתור קישור: חפש לפי מס׳ סידורי או שם חייל")
    @app_commands.describe(query="מס׳ סידורי / שם חייל")
    async def find_cmd(self, interaction: discord.Interaction, query: str):
        q_raw = (query or "").strip()
        if not q_raw:
            return await interaction.response.send_message("❌ חייב query.", ephemeral=True)

        db = load_db()
        rows: list[dict] = db.get("link", []) or []

        q_digits = _digits_only(q_raw)
        # consider as serial search if user typed any digits and length >= 4
        is_serial = len(q_digits) >= 4

        if is_serial:
            serial = q_digits
            hits = [r for r in rows if _digits_only(str(r.get("serial", ""))) == serial]
            if not hits:
                return await interaction.response.send_message(f"❌ לא נמצא קישור עבור המספר: `{serial}`", ephemeral=True)

            # there should be unique, but just in case show all
            lines = [f"🔎 תוצאות עבור מס׳ `{serial}`:"]
            for r in hits[:10]:
                soldier = _norm(r.get("soldier") or "")
                label = _norm(r.get("label") or "")
                rid = r.get("id")
                lines.append(f"- `#{rid}` **{soldier}** — {label}")
            return await interaction.response.send_message("\n".join(lines), ephemeral=True)

        # soldier search (contains, case-insensitive)
        q = _norm(q_raw).lower()
        hits = [r for r in rows if q in _norm(r.get("soldier") or "").lower()]

        if not hits:
            return await interaction.response.send_message(f"❌ לא נמצאו קישורים עבור: **{q_raw}**", ephemeral=True)

        # group by soldier
        by_soldier: dict[str, list[dict]] = {}
        for r in hits:
            s = _norm(r.get("soldier") or "")
            by_soldier.setdefault(s, []).append(r)

        lines = [f"🔎 תוצאות עבור חיפוש: **{q_raw}**"]
        shown = 0
        for soldier, rs in sorted(by_soldier.items(), key=lambda x: x[0]):
            lines.append(f"\n👤 **{soldier}**")
            for r in rs[:15]:
                rid = r.get("id")
                serial = _digits_only(str(r.get("serial") or "")) or str(r.get("serial") or "")
                label = _norm(r.get("label") or "")
                lines.append(f"- `#{rid}` `{serial}` — {label}")
                shown += 1
                if shown >= 60:
                    lines.append("\n… (קיצרתי)")
                    break
            if shown >= 60:
                break

        msg = "\n".join(lines)
        if len(msg) > 1900:
            msg = msg[:1900] + "\n… (קיצרתי)"

        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FindCog(bot), guild=discord.Object(id=bot.GUILD_ID))
