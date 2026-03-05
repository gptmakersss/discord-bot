# cogs/soldier.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands


# ===== local storage (kept inside this cog file) =====
_LOCK = threading.RLock()
DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "db.json")


def _ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _default_db() -> dict:
    return {
        "soldiers": [],
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
            return json.load(f)


def save_db(db: dict) -> None:
    with _LOCK:
        _ensure_dirs()
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DB_FILE)


def normalize_name(name: str) -> str:
    return " ".join(name.strip().split())


def add_soldier(name: str) -> bool:
    name_n = normalize_name(name)
    if not name_n:
        raise ValueError("empty name")
    db = load_db()
    existing = {normalize_name(n) for n in db.get("soldiers", [])}
    if name_n in existing:
        return False
    db.setdefault("soldiers", []).append(name_n)
    db["soldiers"] = sorted(db["soldiers"])
    save_db(db)
    return True


def remove_soldier(name: str) -> bool:
    name_n = normalize_name(name)
    db = load_db()
    soldiers = [normalize_name(n) for n in db.get("soldiers", [])]
    if name_n not in soldiers:
        return False
    soldiers = [n for n in soldiers if n != name_n]
    db["soldiers"] = sorted(soldiers)
    save_db(db)
    return True


def list_soldiers() -> list[str]:
    db = load_db()
    return list(db.get("soldiers", []))


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

    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    embed = discord.Embed(title=title, description=details)
    embed.set_footer(text=ts)
    try:
        await channel.send(embed=embed)
    except Exception:
        pass


# ===== Cog =====
class SoldierCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    soldier = app_commands.Group(
        name="soldier",
        description="ניהול שמות חיילים (add/remove/list)"
    )

    @soldier.command(name="add", description="הוספת חייל לרשימה")
    @app_commands.describe(name="שם החייל")
    async def add_cmd(self, interaction: discord.Interaction, name: str):
        name_n = normalize_name(name)
        if not name_n:
            return await interaction.response.send_message("❌ שם לא תקין.", ephemeral=True)

        try:
            added = add_soldier(name_n)
        except Exception:
            return await interaction.response.send_message("❌ שגיאה בהוספה.", ephemeral=True)

        if added:
            await interaction.response.send_message(f"✅ נוסף: **{name_n}**", ephemeral=True)
            await send_log(self.bot, "SOLDIER ADD", f"נוסף חייל: {name_n}")
        else:
            await interaction.response.send_message(f"ℹ️ כבר קיים: **{name_n}**", ephemeral=True)

    @soldier.command(name="remove", description="הסרת חייל מהרשימה")
    @app_commands.describe(name="שם החייל")
    async def remove_cmd(self, interaction: discord.Interaction, name: str):
        name_n = normalize_name(name)
        if not name_n:
            return await interaction.response.send_message("❌ שם לא תקין.", ephemeral=True)

        removed = remove_soldier(name_n)
        if removed:
            await interaction.response.send_message(f"✅ הוסר: **{name_n}**", ephemeral=True)
            await send_log(self.bot, "SOLDIER REMOVE", f"הוסר חייל: {name_n}")
        else:
            await interaction.response.send_message(f"ℹ️ לא נמצא: **{name_n}**", ephemeral=True)

    @soldier.command(name="list", description="הצגת רשימת חיילים")
    async def list_cmd(self, interaction: discord.Interaction):
        soldiers = list_soldiers()
        if not soldiers:
            return await interaction.response.send_message("אין חיילים רשומים עדיין.", ephemeral=True)

        text = "\n".join(f"- {s}" for s in soldiers[:200])
        extra = len(soldiers) - min(len(soldiers), 200)
        if extra > 0:
            text += f"\n… ועוד {extra}."

        await interaction.response.send_message(text, ephemeral=True)

    @remove_cmd.autocomplete("name")
    async def remove_autocomplete(self, interaction: discord.Interaction, current: str):
        cur = normalize_name(current).lower()
        soldiers = list_soldiers()
        matches = [s for s in soldiers if cur in s.lower()]
        return [app_commands.Choice(name=s, value=s) for s in matches[:25]]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SoldierCog(bot), guild=discord.Object(id=bot.GUILD_ID))
