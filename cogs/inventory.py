# cogs/inventory.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt
from typing import Optional, Any, Dict, List

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
        "notes": [],
        "kashag": {},
        "sign": {},
        "status": {},
        "inventory": {},  # item -> {"qty": int, "state": str, "note": str|None, "updated_at": str}
        "meta": {
            "version": 1,
            "inventory": {
                "last_daily_refresh_date": None,
                "inventory_channel_message_id": None,
            }
        },
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

        db.setdefault("inventory", {})
        db.setdefault("meta", {}).setdefault("inventory", {})
        mi = db["meta"]["inventory"]
        mi.setdefault("last_daily_refresh_date", None)
        mi.setdefault("inventory_channel_message_id", None)
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

VALID_STATES = ["תקין", "חוסר", "צריך להגיש בלאי", "אחר"]


def _format_inventory(inv: dict) -> str:
    if not inv:
        return "📦 אין מלאי רשום עדיין."

    # sort by state then item
    def key(kv):
        item, rec = kv
        st = rec.get("state") or "אחר"
        st_rank = VALID_STATES.index(st) if st in VALID_STATES else 99
        return (st_rank, item)

    items = sorted(inv.items(), key=key)

    lines = ["📦 **ציוד סמליה (מלאי)**"]
    for item, rec in items[:180]:
        qty = rec.get("qty", 0)
        st = rec.get("state") or "אחר"
        note = (rec.get("note") or "").strip()
        em = STATE_EMOJI.get(st, "•")
        line = f"- {em} **{item}** — כמות: **{qty}** — מצב: **{st}**"
        if note:
            line += f"  _(הערה: {note[:120]}{'…' if len(note) > 120 else ''})_"
        lines.append(line)

    extra = max(0, len(items) - 180)
    if extra:
        lines.append(f"\n… ועוד {extra} פריטים.")

    return "\n".join(lines).strip()


async def _upsert_inventory_message(bot: commands.Bot) -> None:
    db = load_db()
    inv = db.get("inventory", {}) or {}
    content = _format_inventory(inv)

    ch_id = bot.CHANNELS.get("inventory")
    if not ch_id:
        return
    channel = bot.get_channel(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception:
            return

    prev_id = _safe_int(db.get("meta", {}).get("inventory", {}).get("inventory_channel_message_id"))
    if prev_id:
        try:
            msg = await channel.fetch_message(prev_id)
            await msg.delete()
        except Exception:
            pass

    try:
        new_msg = await channel.send(content)
        db.setdefault("meta", {}).setdefault("inventory", {})["inventory_channel_message_id"] = new_msg.id
        save_db(db)
    except Exception:
        pass


def _autocomplete_items(db: dict, bot: commands.Bot) -> list[str]:
    inv = db.get("inventory", {}) or {}
    items = set(inv.keys())
    for it in (bot.INVENTORY_CATALOG or []):
        itn = _norm(it)
        if itn:
            items.add(itn)
    return sorted(items)


class InventoryCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clock_loop.start()

    def cog_unload(self):
        self.clock_loop.cancel()

    inventory = app_commands.Group(name="inventory", description="ניהול מלאי ציוד סמליה")

    @inventory.command(name="set", description="הוספה/עדכון פריט במלאי (כמות + מצב)")
    @app_commands.describe(
        item="שם הפריט",
        qty="כמות (מספר שלם)",
        state="מצב",
        note="הערה (אופציונלי)"
    )
    @app_commands.choices(state=[
        app_commands.Choice(name="תקין", value="תקין"),
        app_commands.Choice(name="חוסר", value="חוסר"),
        app_commands.Choice(name="צריך להגיש בלאי", value="צריך להגיש בלאי"),
        app_commands.Choice(name="אחר", value="אחר"),
    ])
    async def set_cmd(
        self,
        interaction: discord.Interaction,
        item: str,
        qty: int,
        state: app_commands.Choice[str],
        note: Optional[str] = None,
    ):
        item_n = _norm(item)
        if not item_n:
            return await interaction.response.send_message("❌ שם פריט לא תקין.", ephemeral=True)
        if qty < 0:
            return await interaction.response.send_message("❌ כמות לא יכולה להיות שלילית.", ephemeral=True)

        st = state.value
        note_n = _norm(note or "") or None

        db = load_db()
        inv = db.setdefault("inventory", {})

        inv[item_n] = {
            "qty": int(qty),
            "state": st,
            "note": note_n,
            "updated_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_db(db)

        await interaction.response.send_message(
            f"✅ עודכן מלאי: **{item_n}**\nכמות: **{qty}** | מצב: **{st}**" + (f"\nהערה: {note_n}" if note_n else ""),
            ephemeral=True
        )
        await send_log(self.bot, "INVENTORY SET", f"{item_n} | qty={qty} | state={st}" + (f" | note={note_n}" if note_n else ""))

        await _upsert_inventory_message(self.bot)

    @inventory.command(name="remove", description="מחיקת פריט מהמלאי")
    @app_commands.describe(item="שם הפריט למחיקה")
    async def remove_cmd(self, interaction: discord.Interaction, item: str):
        item_n = _norm(item)
        if not item_n:
            return await interaction.response.send_message("❌ שם פריט לא תקין.", ephemeral=True)

        db = load_db()
        inv = db.get("inventory", {}) or {}
        if item_n not in inv:
            return await interaction.response.send_message("❌ הפריט לא נמצא במלאי.", ephemeral=True)

        inv.pop(item_n, None)
        db["inventory"] = inv
        save_db(db)

        await interaction.response.send_message(f"✅ נמחק מהמלאי: **{item_n}**", ephemeral=True)
        await send_log(self.bot, "INVENTORY REMOVE", f"removed: {item_n}")

        await _upsert_inventory_message(self.bot)

    @inventory.command(name="view", description="תצוגת מלאי (או פריט ספציפי)")
    @app_commands.describe(item="פריט אופציונלי")
    async def view_cmd(self, interaction: discord.Interaction, item: Optional[str] = None):
        db = load_db()
        inv = db.get("inventory", {}) or {}

        if item:
            item_n = _norm(item)
            rec = inv.get(item_n)
            if not rec:
                return await interaction.response.send_message(f"❌ אין פריט כזה: **{item_n}**", ephemeral=True)
            st = rec.get("state") or "אחר"
            qty = rec.get("qty", 0)
            note = rec.get("note")
            em = STATE_EMOJI.get(st, "•")
            msg = f"{em} **{item_n}**\nכמות: **{qty}**\nמצב: **{st}**"
            if note:
                msg += f"\nהערה: {note}"
            return await interaction.response.send_message(msg, ephemeral=True)

        await interaction.response.send_message(_format_inventory(inv), ephemeral=True)

    @set_cmd.autocomplete("item")
    @remove_cmd.autocomplete("item")
    @view_cmd.autocomplete("item")
    async def item_autocomplete(self, interaction: discord.Interaction, current: str):
        db = load_db()
        items = _autocomplete_items(db, self.bot)
        cur = _norm(current).lower()
        matches = [it for it in items if cur in it.lower()] if cur else items
        return [app_commands.Choice(name=it[:100], value=it) for it in matches[:25]]

    # ===== daily auto refresh at 21:00 =====
    @tasks.loop(seconds=30)
    async def clock_loop(self):
        now = _now()
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")

        if hhmm != "21:00":
            return

        db = load_db()
        meta = db.setdefault("meta", {}).setdefault("inventory", {})
        if meta.get("last_daily_refresh_date") == today:
            return

        meta["last_daily_refresh_date"] = today
        save_db(db)

        await _upsert_inventory_message(self.bot)
        await send_log(self.bot, "INVENTORY DAILY", "רענון יומי של מלאי ציוד סמליה (21:00)")

    @clock_loop.before_loop
    async def before_clock(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InventoryCog(bot), guild=discord.Object(id=bot.GUILD_ID))
