# cogs/link.py
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
        "link": [],  # list of {"id": int, "soldier": str, "serial": str, "label": str, "updated_at": str}
        "meta": {
            "version": 1,
            "link": {
                "last_daily_refresh_date": None,
                "assets_channel_message_id": None,
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
        db.setdefault("link", [])
        db.setdefault("meta", {}).setdefault("link", {})
        ml = db["meta"]["link"]
        ml.setdefault("last_daily_refresh_date", None)
        ml.setdefault("assets_channel_message_id", None)
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


def _norm_serial(s: str) -> str:
    # keep only digits if user typed spaces/dashes
    raw = "".join(ch for ch in (s or "").strip() if ch.isdigit())
    return raw or _norm(s)


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


def _format_links(db: dict) -> str:
    links: list[dict] = db.get("link", []) or []
    if not links:
        return "🔗 אין קישורים עדיין."

    # sort by soldier then label
    def key(x: dict):
        return (_norm(x.get("soldier") or ""), _norm(x.get("label") or ""), _norm_serial(x.get("serial") or ""))

    links = sorted(links, key=key)

    lines = ["🔗 **נכסל / צלמים — קישורים (חייל ↔ מס׳ ↔ שם)**"]
    shown = 0
    for r in links:
        rid = r.get("id")
        soldier = _norm(r.get("soldier") or "")
        serial = _norm_serial(r.get("serial") or "")
        label = _norm(r.get("label") or "")
        updated = r.get("updated_at") or ""
        lines.append(f"- `#{rid}` **{soldier}** — `{serial}` — {label}" + (f"  _(עודכן: {updated})_" if updated else ""))
        shown += 1
        if shown >= 120:
            lines.append("… (קיצרתי)")
            break
    return "\n".join(lines).strip()


async def _upsert_assets_message(bot: commands.Bot) -> None:
    db = load_db()
    content = _format_links(db)

    ch_id = bot.CHANNELS.get("assets")  # נכסל-צלמים
    if not ch_id:
        return
    channel = bot.get_channel(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception:
            return

    prev_id = _safe_int(db.get("meta", {}).get("link", {}).get("assets_channel_message_id"))
    if prev_id:
        try:
            msg = await channel.fetch_message(prev_id)
            await msg.delete()
        except Exception:
            pass

    try:
        new_msg = await channel.send(content)
        db.setdefault("meta", {}).setdefault("link", {})["assets_channel_message_id"] = new_msg.id
        save_db(db)
    except Exception:
        pass


def _next_id(rows: list[dict]) -> int:
    if not rows:
        return 1
    try:
        return max(int(r.get("id", 0)) for r in rows) + 1
    except Exception:
        return len(rows) + 1


class LinkCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clock_loop.start()

    def cog_unload(self):
        self.clock_loop.cancel()

    link = app_commands.Group(name="link", description="קישור חייל ↔ מס׳ סידורי ↔ שם/כינוי")

    @link.command(name="add", description="הוספת קישור (חייל, מס׳ סידורי, שם)")
    @app_commands.describe(soldier="שם החייל", serial="מספר סידורי", label="שם/כינוי (למשל נגב/אמרל)")
    async def add_cmd(self, interaction: discord.Interaction, soldier: str, serial: str, label: str):
        soldier_n = _norm(soldier)
        serial_n = _norm_serial(serial)
        label_n = _norm(label)

        if not soldier_n or not serial_n or not label_n:
            return await interaction.response.send_message("❌ חייב למלא soldier + serial + label.", ephemeral=True)

        db = load_db()
        rows: list[dict] = db.setdefault("link", [])

        # allow multiple assets per soldier, but serial must be unique
        if any(_norm_serial(r.get("serial") or "") == serial_n for r in rows):
            return await interaction.response.send_message("❌ המספר הסידורי הזה כבר מקושר. תעשה /link edit או /link remove.", ephemeral=True)

        rid = _next_id(rows)
        now_s = _now().strftime("%Y-%m-%d %H:%M:%S")
        rows.append({
            "id": rid,
            "soldier": soldier_n,
            "serial": serial_n,
            "label": label_n,
            "updated_at": now_s,
        })
        save_db(db)

        await interaction.response.send_message(f"✅ נוסף קישור `#{rid}`: **{soldier_n}** — `{serial_n}` — {label_n}", ephemeral=True)
        await send_log(self.bot, "LINK ADD", f"#{rid} | {soldier_n} | {serial_n} | {label_n}")
        await _upsert_assets_message(self.bot)

    @link.command(name="edit", description="עריכת קישור לפי ID")
    @app_commands.describe(
        link_id="מספר הקישור (ID)",
        field="איזה שדה לערוך",
        value="הערך החדש"
    )
    @app_commands.choices(field=[
        app_commands.Choice(name="soldier", value="soldier"),
        app_commands.Choice(name="serial", value="serial"),
        app_commands.Choice(name="label", value="label"),
    ])
    async def edit_cmd(self, interaction: discord.Interaction, link_id: int, field: app_commands.Choice[str], value: str):
        db = load_db()
        rows: list[dict] = db.get("link", [])

        row = next((r for r in rows if int(r.get("id", -1)) == int(link_id)), None)
        if not row:
            return await interaction.response.send_message("❌ לא נמצא קישור עם ID כזה.", ephemeral=True)

        f = field.value
        new_val = _norm(value)
        if f == "serial":
            new_val = _norm_serial(value)

        if not new_val:
            return await interaction.response.send_message("❌ ערך חדש לא תקין.", ephemeral=True)

        # if editing serial - ensure unique
        if f == "serial":
            if any(int(r.get("id", -1)) != int(link_id) and _norm_serial(r.get("serial") or "") == new_val for r in rows):
                return await interaction.response.send_message("❌ המספר הסידורי הזה כבר קיים בקישור אחר.", ephemeral=True)

        old_val = row.get(f)
        row[f] = new_val
        row["updated_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")
        save_db(db)

        await interaction.response.send_message(f"✅ עודכן `#{link_id}`: {f} -> **{new_val}**", ephemeral=True)
        await send_log(self.bot, "LINK EDIT", f"#{link_id} | {f}: {old_val} -> {new_val}")
        await _upsert_assets_message(self.bot)

    @link.command(name="remove", description="מחיקת קישור לפי ID")
    @app_commands.describe(link_id="מספר הקישור (ID) למחיקה")
    async def remove_cmd(self, interaction: discord.Interaction, link_id: int):
        db = load_db()
        rows: list[dict] = db.get("link", [])

        before = len(rows)
        rows2 = [r for r in rows if int(r.get("id", -1)) != int(link_id)]
        if len(rows2) == before:
            return await interaction.response.send_message("❌ לא נמצא קישור עם ID כזה.", ephemeral=True)

        db["link"] = rows2
        save_db(db)

        await interaction.response.send_message(f"✅ נמחק קישור `#{link_id}`.", ephemeral=True)
        await send_log(self.bot, "LINK REMOVE", f"removed link #{link_id}")
        await _upsert_assets_message(self.bot)

    @link.command(name="view", description="תצוגת כל הקישורים")
    async def view_cmd(self, interaction: discord.Interaction):
        db = load_db()
        content = _format_links(db)
        if len(content) > 1900:
            content = content[:1900] + "\n… (קיצרתי)"
        await interaction.response.send_message(content, ephemeral=True)

    @edit_cmd.autocomplete("link_id")
    @remove_cmd.autocomplete("link_id")
    async def link_id_autocomplete(self, interaction: discord.Interaction, current: str):
        db = load_db()
        rows: list[dict] = db.get("link", [])
        cur = _norm(current).lower()

        res = []
        for r in sorted(rows, key=lambda x: int(x.get("id", 0)), reverse=True):
            rid = str(r.get("id"))
            soldier = _norm(r.get("soldier") or "")
            serial = _norm_serial(r.get("serial") or "")
            label = _norm(r.get("label") or "")
            name = f"#{rid} - {soldier} - {serial} - {label}"[:95]
            if not cur or cur in rid or cur in soldier.lower() or cur in serial.lower() or cur in label.lower():
                res.append(app_commands.Choice(name=name, value=int(rid)))
            if len(res) >= 25:
                break
        return res

    @add_cmd.autocomplete("soldier")
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
        meta = db.setdefault("meta", {}).setdefault("link", {})
        if meta.get("last_daily_refresh_date") == today:
            return

        meta["last_daily_refresh_date"] = today
        save_db(db)

        await _upsert_assets_message(self.bot)
        await send_log(self.bot, "LINK DAILY", "רענון יומי של נכסל-צלמים (21:00)")

    @clock_loop.before_loop
    async def before_clock(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LinkCog(bot), guild=discord.Object(id=bot.GUILD_ID))
