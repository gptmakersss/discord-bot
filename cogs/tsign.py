# cogs/tsign.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

DB_FILE = os.path.join("data", "db.json")
_LOCK = threading.RLock()


# ======================
# DB helpers
# ======================
def _ensure_db():
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)


def load_db():
    with _LOCK:
        _ensure_db()
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)

        db.setdefault("tsign", [])     # חתימות זמניות
        db.setdefault("soldiers", [])  # חיילים
        return db


def save_db(db):
    with _LOCK:
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DB_FILE)


def _norm(s: str) -> str:
    return " ".join((s or "").strip().split())


def _next_id(rows):
    return max([r.get("id", 0) for r in rows], default=0) + 1


def _parse_date(date_str: str | None):
    if not date_str:
        return None

    date_str = date_str.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(date_str, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ======================
# Cog
# ======================
class TSignCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    tsign = app_commands.Group(
        name="tsign",
        description="חתימות זמניות (ציוד לתקופה מוגבלת)"
    )

    # -------- add --------
    @tsign.command(name="add", description="הוספת חתימה זמנית")
    async def add(
        self,
        interaction: discord.Interaction,
        soldier: str,
        item: str,
        return_date: str | None = None,
    ):
        soldier = _norm(soldier)
        item = _norm(item)
        rdate = _parse_date(return_date)

        db = load_db()
        tid = _next_id(db["tsign"])

        db["tsign"].append({
            "id": tid,
            "soldier": soldier,
            "item": item,
            "return_date": rdate,
            "created_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        save_db(db)

        await interaction.response.send_message(
            f"✅ נוספה חתימה זמנית `#{tid}`\n"
            f"חייל: **{soldier}**\n"
            f"פריט: **{item}**\n"
            f"החזרה משוערת: **{rdate or 'לא צוין'}**",
            ephemeral=True
        )

        if hasattr(self.bot, "send_log"):
            await self.bot.send_log(
                "TSIGN ADD",
                f"{soldier} | {item} | החזרה: {rdate or '-'}"
            )

    # -------- edit --------
    @tsign.command(name="edit", description="עריכת חתימה זמנית")
    @app_commands.choices(
        field=[
            app_commands.Choice(name="פריט", value="item"),
            app_commands.Choice(name="תאריך החזרה", value="return_date"),
        ]
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        tsign_id: int,
        field: app_commands.Choice[str],
        value: str,
    ):
        db = load_db()
        row = next((r for r in db["tsign"] if r["id"] == tsign_id), None)
        if not row:
            return await interaction.response.send_message(
                "❌ לא נמצאה חתימה עם ID כזה",
                ephemeral=True
            )

        if field.value == "item":
            row["item"] = _norm(value)
        elif field.value == "return_date":
            row["return_date"] = _parse_date(value)

        save_db(db)

        await interaction.response.send_message(
            f"✅ עודכנה חתימה זמנית `#{tsign_id}`",
            ephemeral=True
        )

        if hasattr(self.bot, "send_log"):
            await self.bot.send_log(
                "TSIGN EDIT",
                f"#{tsign_id} | {field.value} -> {value}"
            )

    # -------- remove --------
    @tsign.command(name="remove", description="מחיקת חתימה זמנית")
    async def remove(self, interaction: discord.Interaction, tsign_id: int):
        db = load_db()
        before = len(db["tsign"])
        db["tsign"] = [r for r in db["tsign"] if r["id"] != tsign_id]

        if len(db["tsign"]) == before:
            return await interaction.response.send_message(
                "❌ לא נמצאה חתימה עם ID כזה",
                ephemeral=True
            )

        save_db(db)

        await interaction.response.send_message(
            f"🗑️ נמחקה חתימה זמנית `#{tsign_id}`",
            ephemeral=True
        )

        if hasattr(self.bot, "send_log"):
            await self.bot.send_log(
                "TSIGN REMOVE",
                f"#{tsign_id}"
            )

    # -------- autocomplete --------
    @add.autocomplete("soldier")
    async def ac_soldier(self, interaction: discord.Interaction, current: str):
        db = load_db()
        cur = _norm(current).lower()
        soldiers = db.get("soldiers", [])
        matches = [s for s in soldiers if cur in s.lower()] if cur else soldiers
        return [app_commands.Choice(name=s, value=s) for s in matches[:25]]

    @edit.autocomplete("tsign_id")
    @remove.autocomplete("tsign_id")
    async def ac_tsign_id(self, interaction: discord.Interaction, current: str):
        db = load_db()
        cur = current.strip()
        out = []
        for r in db.get("tsign", []):
            rid = str(r["id"])
            label = f"#{rid} - {r['soldier']} - {r['item']}"[:95]
            if not cur or cur in rid:
                out.append(app_commands.Choice(name=label, value=int(rid)))
            if len(out) >= 25:
                break
        return out


async def setup(bot: commands.Bot):
    await bot.add_cog(TSignCog(bot), guild=bot.guild_obj)
