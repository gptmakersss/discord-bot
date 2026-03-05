# cogs/sign.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt
from typing import Optional, Any, List, Dict

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
        "sign": {},  # name -> {"items":[...], "other": Optional[str], "updated_at": "..."}
        "meta": {
            "version": 1,
            "sign": {
                "last_daily_refresh_date": None,
                "sign_channel_message_id": None,
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

        db.setdefault("sign", {})
        db.setdefault("meta", {}).setdefault("sign", {})
        ms = db["meta"]["sign"]
        ms.setdefault("last_daily_refresh_date", None)
        ms.setdefault("sign_channel_message_id", None)
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


def _norm_name(name: str) -> str:
    return " ".join((name or "").strip().split())


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


def _get_soldiers(db: dict) -> list[str]:
    return list(db.get("soldiers", []))


def _get_sign_record(db: dict, name: str) -> dict:
    sign_db = db.setdefault("sign", {})
    rec = sign_db.get(name)
    if not rec:
        rec = {"items": [], "other": None, "updated_at": None}
        sign_db[name] = rec
    rec.setdefault("items", [])
    rec.setdefault("other", None)
    rec.setdefault("updated_at", None)
    return rec


def _format_signatures(db: dict) -> str:
    sign_db: dict = db.get("sign", {}) or {}
    soldiers = _get_soldiers(db)
    names = soldiers if soldiers else sorted(sign_db.keys())

    if not names:
        return "🧾 אין חתימות עדיין."

    lines = ["🧾 **חתימות (קבוע)**"]
    shown = 0
    for name in names:
        rec = sign_db.get(name) or {"items": [], "other": None}
        items: list[str] = rec.get("items") or []
        other: Optional[str] = rec.get("other")
        all_items = list(items)
        if other:
            all_items.append(f"אחר: {other}")

        if all_items:
            lines.append(f"- **{name}**: " + ", ".join(all_items))
        else:
            if soldiers:
                lines.append(f"- **{name}**: —")

        shown += 1
        if shown >= 120:
            lines.append("… (קיצרתי כדי לא לעבור מגבלת הודעה)")
            break

    return "\n".join(lines).strip()


async def _upsert_signatures_message(bot: commands.Bot) -> None:
    db = load_db()
    content = _format_signatures(db)

    ch_id = bot.CHANNELS.get("signatures")
    if not ch_id:
        return
    channel = bot.get_channel(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception:
            return

    prev_id = _safe_int(db.get("meta", {}).get("sign", {}).get("sign_channel_message_id"))
    if prev_id:
        try:
            msg = await channel.fetch_message(prev_id)
            await msg.delete()
        except Exception:
            pass

    try:
        new_msg = await channel.send(content)
        db.setdefault("meta", {}).setdefault("sign", {})["sign_channel_message_id"] = new_msg.id
        save_db(db)
    except Exception:
        pass


# ===== UI Components =====
class OtherModal(discord.ui.Modal, title="הכנסת ערך ל'אחר'"):
    value = discord.ui.TextInput(
        label="ערך 'אחר' (למשל מס' סידורי)",
        placeholder="לדוגמה: 4641843",
        required=True,
        max_length=50,
    )

    def __init__(self):
        super().__init__()
        self.result: Optional[str] = None

    async def on_submit(self, interaction: discord.Interaction):
        self.result = str(self.value).strip()
        await interaction.response.send_message("✅ נקלט.", ephemeral=True)


class MultiSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption], placeholder: str):
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=min(25, len(options)),
            options=options[:25],  # Discord limit per select
        )


class SignAddView(discord.ui.View):
    def __init__(self, bot: commands.Bot, soldier_name: str, catalog: list[str]):
        super().__init__(timeout=120)
        self.bot = bot
        self.soldier_name = soldier_name
        self.catalog = catalog
        self.selected: list[str] = []
        self.pick_other: bool = False

        opts = [discord.SelectOption(label=item, value=item) for item in catalog[:24]]
        # reserve one slot for "אחר"
        opts.append(discord.SelectOption(label="אחר", value="__OTHER__"))
        self.select = MultiSelect(opts, "בחר פריטים לחתימה (אפשר כמה).")
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        vals = list(self.select.values)
        self.pick_other = "__OTHER__" in vals
        self.selected = [v for v in vals if v != "__OTHER__"]
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="אישור", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = load_db()
        rec = _get_sign_record(db, self.soldier_name)

        added_items = []
        for it in self.selected:
            if it not in rec["items"]:
                rec["items"].append(it)
                added_items.append(it)

        other_added = None
        if self.pick_other:
            # enforce only one other
            if rec.get("other"):
                await interaction.response.send_message("❌ כבר קיים 'אחר' לחייל הזה. תסיר קודם ואז תוסיף שוב.", ephemeral=True)
                return
            modal = OtherModal()
            await interaction.response.send_modal(modal)
            await modal.wait()
            if modal.result:
                rec["other"] = modal.result
                other_added = modal.result

        rec["items"] = sorted(set(rec["items"]))
        rec["updated_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")
        save_db(db)

        msg = f"✅ עודכנו חתימות ל־**{self.soldier_name}**.\n"
        if added_items:
            msg += f"- נוסף: {', '.join(added_items)}\n"
        if other_added:
            msg += f"- נוסף אחר: {other_added}\n"
        if not added_items and not other_added:
            msg += "- לא נוסף כלום (כבר היה קיים).\n"

        await send_log(self.bot, "SIGN ADD", f"{self.soldier_name} | added: {', '.join(added_items) if added_items else '-'} | other: {other_added or '-'}")
        await _upsert_signatures_message(self.bot)

        # close view
        for child in self.children:
            child.disabled = True
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(label="ביטול", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.send_message("בוטל.", ephemeral=True)


class SignRemoveView(discord.ui.View):
    def __init__(self, bot: commands.Bot, soldier_name: str, current_items: list[str], has_other: bool):
        super().__init__(timeout=120)
        self.bot = bot
        self.soldier_name = soldier_name
        self.has_other = has_other

        opts = [discord.SelectOption(label=item, value=item) for item in current_items[:25]]
        if has_other and len(opts) < 25:
            opts.append(discord.SelectOption(label="אחר", value="__OTHER__"))

        self.select = MultiSelect(opts, "בחר מה להסיר (אפשר כמה).")
        self.select.callback = self.on_select
        self.add_item(self.select)

        self.to_remove: list[str] = []
        self.remove_other: bool = False

    async def on_select(self, interaction: discord.Interaction):
        vals = list(self.select.values)
        self.remove_other = "__OTHER__" in vals
        self.to_remove = [v for v in vals if v != "__OTHER__"]
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="אישור הסרה", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = load_db()
        rec = _get_sign_record(db, self.soldier_name)

        removed = []
        for it in self.to_remove:
            if it in rec["items"]:
                rec["items"].remove(it)
                removed.append(it)

        other_removed = False
        if self.remove_other and rec.get("other"):
            rec["other"] = None
            other_removed = True

        rec["items"] = sorted(set(rec["items"]))
        rec["updated_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")
        save_db(db)

        msg = f"✅ הוסרו חתימות מ־**{self.soldier_name}**.\n"
        if removed:
            msg += f"- הוסר: {', '.join(removed)}\n"
        if other_removed:
            msg += "- הוסר: אחר\n"
        if not removed and not other_removed:
            msg += "- לא הוסר כלום.\n"

        await send_log(self.bot, "SIGN REMOVE", f"{self.soldier_name} | removed: {', '.join(removed) if removed else '-'} | other_removed: {other_removed}")
        await _upsert_signatures_message(self.bot)

        for child in self.children:
            child.disabled = True
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(label="ביטול", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.send_message("בוטל.", ephemeral=True)


# ===== Cog =====
class SignCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clock_loop.start()

    def cog_unload(self):
        self.clock_loop.cancel()

    sign = app_commands.Group(name="sign", description="חתימות קבועות (add/remove/view)")

    @sign.command(name="add", description="הוספת חתימות קבועות לחייל (multi-select)")
    @app_commands.describe(name="שם החייל")
    async def add_cmd(self, interaction: discord.Interaction, name: str):
        name = _norm_name(name)
        if not name:
            return await interaction.response.send_message("❌ שם לא תקין.", ephemeral=True)

        catalog = list(self.bot.SIGN_CATALOG or [])
        if not catalog:
            return await interaction.response.send_message(
                "❌ אין קטלוג פריטים עדיין.\n"
                "תמלא ב־bot.py את SIGN_CATALOG = [ ... ] ואז הפקודה תעבוד עם תפריט.",
                ephemeral=True
            )

        view = SignAddView(self.bot, name, catalog)
        await interaction.response.send_message(f"בחר פריטים להוספה עבור **{name}**:", view=view, ephemeral=True)

    @sign.command(name="remove", description="הסרת חתימות קבועות מחייל (multi-select)")
    @app_commands.describe(name="שם החייל")
    async def remove_cmd(self, interaction: discord.Interaction, name: str):
        name = _norm_name(name)
        if not name:
            return await interaction.response.send_message("❌ שם לא תקין.", ephemeral=True)

        db = load_db()
        rec = _get_sign_record(db, name)
        current_items = list(rec.get("items") or [])
        has_other = bool(rec.get("other"))

        if not current_items and not has_other:
            return await interaction.response.send_message("ℹ️ אין לחייל הזה חתימות להסרה.", ephemeral=True)

        view = SignRemoveView(self.bot, name, current_items, has_other)
        await interaction.response.send_message(f"בחר מה להסיר עבור **{name}**:", view=view, ephemeral=True)

    @sign.command(name="view", description="תצוגת חתימות")
    @app_commands.describe(name="שם חייל אופציונלי")
    async def view_cmd(self, interaction: discord.Interaction, name: Optional[str] = None):
        db = load_db()
        sign_db: dict = db.get("sign", {}) or {}

        if name:
            name = _norm_name(name)
            rec = sign_db.get(name)
            if not rec:
                return await interaction.response.send_message(f"ℹ️ אין חתימות עבור **{name}**.", ephemeral=True)
            items = list(rec.get("items") or [])
            other = rec.get("other")
            all_items = items + ([f"אחר: {other}"] if other else [])
            if not all_items:
                return await interaction.response.send_message(f"ℹ️ אין חתימות עבור **{name}**.", ephemeral=True)
            return await interaction.response.send_message(f"**{name}**: " + ", ".join(all_items), ephemeral=True)

        await interaction.response.send_message(_format_signatures(db), ephemeral=True)

    # autocomplete from soldiers list
    @add_cmd.autocomplete("name")
    @remove_cmd.autocomplete("name")
    @view_cmd.autocomplete("name")
    async def name_autocomplete(self, interaction: discord.Interaction, current: str):
        db = load_db()
        soldiers = db.get("soldiers", [])
        cur = (current or "").strip().lower()
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
        meta = db.setdefault("meta", {}).setdefault("sign", {})
        if meta.get("last_daily_refresh_date") == today:
            return

        meta["last_daily_refresh_date"] = today
        save_db(db)

        await _upsert_signatures_message(self.bot)
        await send_log(self.bot, "SIGN DAILY", "רענון יומי של חתימות (21:00)")

    @clock_loop.before_loop
    async def before_clock(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SignCog(bot), guild=discord.Object(id=bot.GUILD_ID))
