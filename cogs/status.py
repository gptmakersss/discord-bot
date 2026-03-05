# cogs/status.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
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
        "tasks": [],
        "incidents": [],
        "notes": [],
        "kashag": {},
        "sign": {},
        "status": {},  # soldier -> item -> {"state":..., "note":..., "updated_at":...}
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

        db.setdefault("status", {})
        db.setdefault("sign", {})
        db.setdefault("soldiers", [])
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


def _items_for_soldier(db: dict, bot: commands.Bot, soldier: str) -> list[str]:
    """Autocomplete source: SIGN_CATALOG + items already signed by soldier + items already in status db."""
    items: set[str] = set()

    # global catalog (from bot.py)
    for it in (bot.SIGN_CATALOG or []):
        itn = _norm(it)
        if itn:
            items.add(itn)

    # items signed for this soldier
    sign_db: dict = db.get("sign", {}) or {}
    rec = sign_db.get(soldier) or {}
    for it in (rec.get("items") or []):
        itn = _norm(it)
        if itn:
            items.add(itn)
    # if soldier has "other" in sign, expose as an option label
    other = _norm(rec.get("other") or "")
    if other:
        items.add(f"אחר(חתימה): {other}")

    # items already used in status for this soldier
    st_db: dict = db.get("status", {}) or {}
    soldier_map: dict = st_db.get(soldier, {}) or {}
    for it in soldier_map.keys():
        itn = _norm(it)
        if itn:
            items.add(itn)

    # common weapon/means defaults (until we add link/gun modules)
    items.update(["נשק", "צלם"])

    return sorted(items)


VALID_STATES = ["תקין", "חוסר", "בלאי", "אחר"]


class StatusCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="status", description="שינוי סטטוס לפריט אצל חייל (תקין/חוסר/בלאי/אחר)")
    @app_commands.describe(
        name="שם החייל",
        item="שם הפריט (נבחר מהרשימה)",
        state="סטטוס",
        note="הערה/פירוט (חובה אם סטטוס=אחר)"
    )
    @app_commands.choices(state=[
        app_commands.Choice(name="תקין", value="תקין"),
        app_commands.Choice(name="חוסר", value="חוסר"),
        app_commands.Choice(name="בלאי", value="בלאי"),
        app_commands.Choice(name="אחר", value="אחר"),
    ])
    async def status_cmd(
        self,
        interaction: discord.Interaction,
        name: str,
        item: str,
        state: app_commands.Choice[str],
        note: Optional[str] = None,
    ):
        soldier = _norm(name)
        item_n = _norm(item)
        st = state.value
        note_n = _norm(note or "")

        if not soldier or not item_n:
            return await interaction.response.send_message("❌ שם/פריט לא תקין.", ephemeral=True)

        if st == "אחר" and not note_n:
            return await interaction.response.send_message("❌ כשסטטוס הוא 'אחר' חייבים למלא note.", ephemeral=True)

        db = load_db()
        st_db = db.setdefault("status", {})
        soldier_map = st_db.setdefault(soldier, {})

        # store
        soldier_map[item_n] = {
            "state": st,
            "note": note_n if note_n else None,
            "updated_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_db(db)

        # user feedback
        extra = f" | note: {note_n}" if note_n else ""
        await interaction.response.send_message(
            f"✅ עודכן סטטוס ל־**{soldier}**\n"
            f"פריט: **{item_n}** → **{st}**{extra}",
            ephemeral=True
        )

        await send_log(self.bot, "STATUS SET", f"{soldier} | {item_n} -> {st}" + (f" | note: {note_n}" if note_n else ""))

    @status_cmd.autocomplete("name")
    async def name_autocomplete(self, interaction: discord.Interaction, current: str):
        db = load_db()
        soldiers = db.get("soldiers", [])
        cur = _norm(current).lower()
        matches = [s for s in soldiers if cur in s.lower()] if soldiers else []
        return [app_commands.Choice(name=s, value=s) for s in matches[:25]]

    @status_cmd.autocomplete("item")
    async def item_autocomplete(self, interaction: discord.Interaction, current: str):
        # try to read currently selected soldier from interaction namespace
        soldier = None
        try:
            soldier = _norm(getattr(interaction.namespace, "name", "") or "")
        except Exception:
            soldier = ""

        db = load_db()
        items = _items_for_soldier(db, self.bot, soldier) if soldier else sorted(set((self.bot.SIGN_CATALOG or []) + ["נשק", "צלם"]))

        cur = _norm(current).lower()
        matches = [it for it in items if cur in it.lower()] if cur else items
        return [app_commands.Choice(name=it[:100], value=it) for it in matches[:25]]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatusCog(bot), guild=discord.Object(id=bot.GUILD_ID))
