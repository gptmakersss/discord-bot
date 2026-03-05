# cogs/kashag.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt
from typing import Optional, Any, Tuple

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
        "kashag": {},  # name -> record dict
        "meta": {
            "version": 1,
            "kashag": {
                "last_daily_refresh_date": None,
                "fitness_channel_message_id": None,
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

        db.setdefault("kashag", {})
        db.setdefault("meta", {}).setdefault("kashag", {})
        mk = db["meta"]["kashag"]
        mk.setdefault("last_daily_refresh_date", None)
        mk.setdefault("fitness_channel_message_id", None)
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


def _parse_number(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    # allow digits + dot + comma
    s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def _fmt_num(x: Optional[float]) -> str:
    if x is None:
        return "—"
    # if integer-like, show without .0
    if abs(x - int(x)) < 1e-9:
        return str(int(x))
    return str(x)


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
    soldiers = db.get("soldiers", [])
    # already normalized in soldier cog
    return list(soldiers)


def _format_record(name: str, rec: dict) -> str:
    # Kashag block
    run = _fmt_num(rec.get("run"))
    sprints = _fmt_num(rec.get("sprints"))
    pullups = _fmt_num(rec.get("pullups"))
    dips = _fmt_num(rec.get("dips"))
    trapbar = _fmt_num(rec.get("trapbar"))

    # Bahamas / route test
    kir = rec.get("kir") or "—"   # "עבר"/"לא עבר"/None
    hevel = rec.get("hevel") or "—"
    bahmas = _fmt_num(rec.get("bahmas"))

    updated = rec.get("updated_at") or ""

    lines = [
        f"👤 **{name}**",
        f"**כש״ג:** ריצה {run} | ספרינטים {sprints} | מתח {pullups} | מקבילים {dips} | טראפבר {trapbar}",
        f"**בחמ״ס:** קיר {kir} | חבל {hevel} | זמן {bahmas}",
    ]
    if updated:
        lines.append(f"↳ עודכן: {updated}")
    return "\n".join(lines)


def _format_all(db: dict) -> str:
    kdb: dict = db.get("kashag", {}) or {}
    soldiers = _get_soldiers(db)

    # If no soldiers list yet, still show whatever exists in kdb
    names = soldiers if soldiers else sorted(kdb.keys())

    if not names:
        return "🏋️ אין תוצאות כושר עדיין."

    blocks = ["🏋️ **תוצאות כושר** (כש״ג + בחמ״ס)"]
    shown = 0
    for name in names:
        rec = kdb.get(name)
        if rec:
            blocks.append(_format_record(name, rec))
            blocks.append("")  # spacer
            shown += 1
        else:
            # show empty line only if soldiers list exists
            if soldiers:
                blocks.append(f"👤 **{name}**\nאין נתונים עדיין.\n")
                shown += 1

        if shown >= 60:
            blocks.append("… (המשך קיים, אבל קיצרתי כדי לא לעבור מגבלת הודעה)")
            break

    return "\n".join(blocks).strip()


async def _upsert_fitness_message(bot: commands.Bot) -> None:
    db = load_db()
    content = _format_all(db)

    ch_id = bot.CHANNELS.get("fitness")
    if not ch_id:
        return
    channel = bot.get_channel(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception:
            return

    prev_id = _safe_int(db.get("meta", {}).get("kashag", {}).get("fitness_channel_message_id"))
    if prev_id:
        try:
            msg = await channel.fetch_message(prev_id)
            await msg.delete()
        except Exception:
            pass

    try:
        new_msg = await channel.send(content)
        db.setdefault("meta", {}).setdefault("kashag", {})["fitness_channel_message_id"] = new_msg.id
        save_db(db)
    except Exception:
        pass


class KashagCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.clock_loop.start()

    def cog_unload(self):
        self.clock_loop.cancel()

    kashag = app_commands.Group(name="kashag", description="תוצאות כש״ג + בחמ״ס")

    @kashag.command(name="add", description="הוספה/עדכון תוצאות כושר (אפשר להשאיר שדות ריקים)")
    @app_commands.describe(
        name="שם החייל",
        run="ריצה (מספר)",
        sprints="ספרינטים (מספר)",
        pullups="מתח (מספר)",
        dips="מקבילים (מספר)",
        trapbar="טראפבר (מספר)",
        kir="קיר (עבר/לא עבר)",
        hevel="חבל (עבר/לא עבר)",
        bahmas="זמן בחמ״ס (מספר)",
    )
    @app_commands.choices(
        kir=[
            app_commands.Choice(name="עבר", value="עבר"),
            app_commands.Choice(name="לא עבר", value="לא עבר"),
        ],
        hevel=[
            app_commands.Choice(name="עבר", value="עבר"),
            app_commands.Choice(name="לא עבר", value="לא עבר"),
        ],
    )
    async def add_cmd(
        self,
        interaction: discord.Interaction,
        name: str,
        run: Optional[str] = None,
        sprints: Optional[str] = None,
        pullups: Optional[str] = None,
        dips: Optional[str] = None,
        trapbar: Optional[str] = None,
        kir: Optional[app_commands.Choice[str]] = None,
        hevel: Optional[app_commands.Choice[str]] = None,
        bahmas: Optional[str] = None,
    ):
        name = " ".join((name or "").strip().split())
        if not name:
            return await interaction.response.send_message("❌ שם לא תקין.", ephemeral=True)

        # parse numeric fields (allow empty)
        num_fields = {
            "run": _parse_number(run),
            "sprints": _parse_number(sprints),
            "pullups": _parse_number(pullups),
            "dips": _parse_number(dips),
            "trapbar": _parse_number(trapbar),
            "bahmas": _parse_number(bahmas),
        }
        # if user typed something non-numeric (not empty) -> reject
        def _is_bad(inp: Optional[str], parsed: Optional[float]) -> bool:
            if inp is None:
                return False
            if str(inp).strip() == "":
                return False
            return parsed is None

        if any([
            _is_bad(run, num_fields["run"]),
            _is_bad(sprints, num_fields["sprints"]),
            _is_bad(pullups, num_fields["pullups"]),
            _is_bad(dips, num_fields["dips"]),
            _is_bad(trapbar, num_fields["trapbar"]),
            _is_bad(bahmas, num_fields["bahmas"]),
        ]):
            return await interaction.response.send_message("❌ אחד השדות המספריים לא תקין (צריך רק מספרים).", ephemeral=True)

        db = load_db()
        kdb = db.setdefault("kashag", {})
        rec = kdb.get(name) or {}

        changes = []

        # update only if provided (not None and not empty string)
        for k in ["run", "sprints", "pullups", "dips", "trapbar", "bahmas"]:
            inp = locals()[k]
            if inp is None or str(inp).strip() == "":
                continue
            newv = num_fields[k]
            if rec.get(k) != newv:
                rec[k] = newv
                changes.append(k)

        if kir is not None:
            if rec.get("kir") != kir.value:
                rec["kir"] = kir.value
                changes.append("kir")

        if hevel is not None:
            if rec.get("hevel") != hevel.value:
                rec["hevel"] = hevel.value
                changes.append("hevel")

        if not rec:
            # user sent everything empty -> do nothing
            return await interaction.response.send_message("ℹ️ לא שלחת שום ערך לעדכון.", ephemeral=True)

        rec["updated_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")
        kdb[name] = rec
        save_db(db)

        await interaction.response.send_message(f"✅ עודכן: **{name}**", ephemeral=True)
        await send_log(self.bot, "KASHAG ADD/EDIT", f"{name} עודכן. שדות: {', '.join(changes) if changes else '—'}")

        await _upsert_fitness_message(self.bot)

    @kashag.command(name="edit", description="עריכה (זהה ל-add) - אפשר להשאיר שדות ריקים")
    async def edit_cmd(
        self,
        interaction: discord.Interaction,
        name: str,
        run: Optional[str] = None,
        sprints: Optional[str] = None,
        pullups: Optional[str] = None,
        dips: Optional[str] = None,
        trapbar: Optional[str] = None,
        kir: Optional[app_commands.Choice[str]] = None,
        hevel: Optional[app_commands.Choice[str]] = None,
        bahmas: Optional[str] = None,
    ):
        # call add logic (same behavior)
        await self.add_cmd(interaction, name, run, sprints, pullups, dips, trapbar, kir, hevel, bahmas)

    @kashag.command(name="view", description="הצגת תוצאות כושר (חייל אופציונלי)")
    @app_commands.describe(name="שם החייל (אופציונלי)")
    async def view_cmd(self, interaction: discord.Interaction, name: Optional[str] = None):
        db = load_db()
        kdb = db.get("kashag", {}) or {}

        if name:
            name = " ".join(name.strip().split())
            rec = kdb.get(name)
            if not rec:
                return await interaction.response.send_message(f"❌ אין נתונים עבור **{name}**.", ephemeral=True)
            return await interaction.response.send_message(_format_record(name, rec), ephemeral=True)

        # show all (compact)
        content = _format_all(db)
        await interaction.response.send_message(content, ephemeral=True)

    # autocomplete from soldiers list
    @add_cmd.autocomplete("name")
    @edit_cmd.autocomplete("name")
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
        meta = db.setdefault("meta", {}).setdefault("kashag", {})
        if meta.get("last_daily_refresh_date") == today:
            return

        meta["last_daily_refresh_date"] = today
        save_db(db)

        await _upsert_fitness_message(self.bot)
        await send_log(self.bot, "KASHAG DAILY", "רענון יומי של תוצאות כושר (21:00)")

    @clock_loop.before_loop
    async def before_clock(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KashagCog(bot), guild=discord.Object(id=bot.GUILD_ID))
