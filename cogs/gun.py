# cogs/gun.py
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
        "link": [],  # from /link: {"id", "soldier", "serial", "label"}
        "gun_storage": [],  # {"id","kind","serial","soldier","label","location","custom_location","stored_date","remind","next_due","reminder_message_id","updated_at"}
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
        db.setdefault("gun_storage", [])
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


def _safe_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


def _parse_date(s: Optional[str]) -> dt.date:
    """
    Accepts:
      - None -> today
      - "1.2.26" / "01.02.2026" / "1/2/26" / "2026-02-01"
    Returns date (no tz).
    """
    if not s or not str(s).strip():
        return _now().date()

    raw = str(s).strip()
    # ISO
    try:
        return dt.date.fromisoformat(raw)
    except Exception:
        pass

    # dd.mm.yy / dd/mm/yy / dd-mm-yy
    for sep in (".", "/", "-"):
        if sep in raw:
            parts = raw.split(sep)
            if len(parts) == 3:
                d, m, y = parts
                d = int(d)
                m = int(m)
                y = int(y)
                if y < 100:
                    y += 2000
                return dt.date(y, m, d)

    # fallback: today
    return _now().date()


def _at_noon(d: dt.date) -> dt.datetime:
    # next due time is 12:00 local
    if TZ is not None:
        return dt.datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=TZ)
    return dt.datetime(d.year, d.month, d.day, 12, 0, 0)


def _next_due_from(stored: dt.date) -> dt.datetime:
    return _at_noon(stored + dt.timedelta(days=3))


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


def _next_id(rows: list[dict]) -> int:
    if not rows:
        return 1
    try:
        return max(int(r.get("id", 0)) for r in rows) + 1
    except Exception:
        return len(rows) + 1


def _find_link_by_serial(db: dict, serial: str) -> Optional[dict]:
    serial_d = _digits_only(serial)
    for r in (db.get("link", []) or []):
        if _digits_only(str(r.get("serial", ""))) == serial_d and serial_d:
            return r
    return None


def _find_links_by_soldier_and_kind(db: dict, soldier: str, kind: str) -> list[dict]:
    # kind is "נשק" / "צלם" / "אחר"
    s = _norm(soldier).lower()
    out = []
    for r in (db.get("link", []) or []):
        rs = _norm(r.get("soldier", "")).lower()
        label = _norm(r.get("label", ""))
        if s and s in rs:
            # if kind is specified, filter roughly by label containing kind
            if kind == "אחר":
                out.append(r)
            elif kind in label:
                out.append(r)
            else:
                # allow "נשק" to match common labels (נגב/מיקרו/תבור וכו')? נשאיר רק kind-in-label.
                pass
    return out


def _get_storage_row(db: dict, serial: str) -> Optional[dict]:
    serial_d = _digits_only(serial)
    for r in (db.get("gun_storage", []) or []):
        if _digits_only(str(r.get("serial", ""))) == serial_d and serial_d:
            return r
    return None


class GunReminderView(discord.ui.View):
    def __init__(self, bot: commands.Bot, asset_id: int):
        super().__init__(timeout=None)  # persistent
        self.bot = bot
        self.asset_id = int(asset_id)

    @discord.ui.button(
        label="תזכיר לי עוד 3 ימים",
        style=discord.ButtonStyle.primary,
        custom_id="gun:remind3"
    )
    async def remind3(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = load_db()
        rows: list[dict] = db.get("gun_storage", []) or []
        row = next((r for r in rows if int(r.get("id", -1)) == self.asset_id), None)
        if not row:
            return await interaction.response.send_message("❌ הרשומה לא נמצאה.", ephemeral=True)

        # update stored_date to today (meaning: you did the cycle today)
        today = _now().date()
        row["stored_date"] = today.isoformat()
        row["next_due"] = _next_due_from(today).isoformat()
        row["updated_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")
        row["remind"] = True
        save_db(db)

        await send_log(self.bot, "GUN REMIND +3", f"id={self.asset_id} | serial={row.get('serial')} | next_due={row.get('next_due')}")
        await interaction.response.send_message("✅ עודכן. אזכיר לך עוד 3 ימים ב־12:00.", ephemeral=True)

    @discord.ui.button(
        label="הוצאתי",
        style=discord.ButtonStyle.success,
        custom_id="gun:takenout"
    )
    async def taken_out(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = load_db()
        rows: list[dict] = db.get("gun_storage", []) or []
        row = next((r for r in rows if int(r.get("id", -1)) == self.asset_id), None)
        if not row:
            return await interaction.response.send_message("❌ הרשומה לא נמצאה.", ephemeral=True)

        row["location"] = "לא מאופסן"
        row["custom_location"] = None
        row["remind"] = False
        row["next_due"] = None
        row["updated_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")
        save_db(db)

        await send_log(self.bot, "GUN TAKEN OUT", f"id={self.asset_id} | serial={row.get('serial')} -> לא מאופסן")
        await interaction.response.send_message("✅ עודכן ל׳לא מאופסן׳ (הוסרו תזכורות).", ephemeral=True)


def _reminder_text(row: dict) -> str:
    kind = row.get("kind") or "נשק"
    serial = row.get("serial") or ""
    soldier = row.get("soldier") or "לא ידוע"
    label = row.get("label") or ""
    loc = row.get("location") or "לא ידוע"
    custom_loc = row.get("custom_location") or None
    stored_date = row.get("stored_date") or ""
    next_due = row.get("next_due") or ""

    loc_show = loc
    if loc == "אחר" and custom_loc:
        loc_show = f"אחר: {custom_loc}"

    due_show = ""
    if next_due:
        try:
            due_dt = dt.datetime.fromisoformat(next_due)
            due_show = due_dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            due_show = next_due

    return (
        f"⏰ **תזכורת אפסון (כל 3 ימים)**\n"
        f"- סוג: **{kind}**\n"
        f"- מס׳: `{serial}`\n"
        f"- שייך ל: **{soldier}**\n"
        f"- שם/כינוי: {label}\n"
        f"- מיקום נוכחי: **{loc_show}**\n"
        f"- תאריך אפסון אחרון: **{stored_date}**\n"
        f"- תזכורת הבאה: **{due_show}**\n"
        f"\nסיימת? לחץ **הוצאתי**. עשית סבב והחזרת? לחץ **תזכיר לי עוד 3 ימים**."
    )


class GunCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.reminder_loop.start()
        self._views_loaded = False

    def cog_unload(self):
        self.reminder_loop.cancel()

    async def _load_persistent_views(self):
        # Register persistent views for existing reminder messages (so buttons keep working after restart)
        if self._views_loaded:
            return
        db = load_db()
        for row in (db.get("gun_storage", []) or []):
            mid = row.get("reminder_message_id")
            if mid:
                try:
                    self.bot.add_view(GunReminderView(self.bot, int(row["id"])), message_id=int(mid))
                except Exception:
                    pass
        self._views_loaded = True

    gun = app_commands.Group(name="gun", description="ניהול נשקים/צלמים")

    @gun.command(name="storage", description="עדכון אפסון נשק/צלם + תזכורת כל 3 ימים ב-12:00")
    @app_commands.describe(
        kind="סוג (נשק/צלם/אחר)",
        target="מס׳ סידורי או שם חייל",
        location="מיקום אפסון",
        custom_location="אם בחרת 'אחר' — תרשום מיקום",
        stored_date="תאריך אפסון (אופציונלי: 1.2.26 / 2026-02-01)",
        remind="צריך תזכורת? (ברירת מחדל לא צריך)",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="נשק", value="נשק"),
            app_commands.Choice(name="צלם", value="צלם"),
            app_commands.Choice(name="אחר", value="אחר"),
        ],
        location=[
            app_commands.Choice(name="נשקייה", value="נשקייה"),
            app_commands.Choice(name="בטחונית", value="בטחונית"),
            app_commands.Choice(name="אחר", value="אחר"),
            app_commands.Choice(name="לא מאופסן", value="לא מאופסן"),
        ],
        remind=[
            app_commands.Choice(name="לא צריך", value="לא צריך"),
            app_commands.Choice(name="צריך", value="צריך"),
        ],
    )
    async def storage_cmd(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        target: str,
        location: app_commands.Choice[str],
        custom_location: Optional[str] = None,
        stored_date: Optional[str] = None,
        remind: Optional[app_commands.Choice[str]] = None,
    ):
        await self._load_persistent_views()

        kind_v = kind.value
        loc_v = location.value
        remind_v = (remind.value if remind else "לא צריך") == "צריך"

        if loc_v == "אחר":
            cl = _norm(custom_location or "")
            if not cl:
                return await interaction.response.send_message("❌ בחרת 'אחר' — חייב למלא custom_location.", ephemeral=True)
        else:
            cl = None

        # resolve target
        db = load_db()
        t = _norm(target)
        digits = _digits_only(t)
        serials: list[str] = []
        soldier_name: Optional[str] = None
        label: Optional[str] = None

        if len(digits) >= 4:
            # serial path
            serials = [digits]
            link = _find_link_by_serial(db, digits)
            if link:
                soldier_name = _norm(link.get("soldier", "")) or None
                label = _norm(link.get("label", "")) or None
        else:
            # soldier path -> find linked assets of this kind
            soldier_name = t
            links = _find_links_by_soldier_and_kind(db, soldier_name, kind_v)
            if not links:
                # fallback: allow update by soldier name even without link; create a "virtual" record using soldier as serial key is impossible
                return await interaction.response.send_message(
                    "❌ לא מצאתי קישור (/link) לחייל הזה ולסוג שבחרת.\n"
                    "תעשה קודם /link add (חייל, מס׳ סידורי, שם) ואז תריץ /gun storage שוב.",
                    ephemeral=True
                )
            serials = [_digits_only(str(r.get("serial", ""))) for r in links if _digits_only(str(r.get("serial", "")))]
            # if multiple, we update all
            # label will vary per serial so we won't set one here

        if not serials:
            return await interaction.response.send_message("❌ לא הצלחתי להבין מס׳ סידורי מהקלט.", ephemeral=True)

        d = _parse_date(stored_date)

        updated_ids: list[int] = []
        for serial in serials:
            link = _find_link_by_serial(db, serial)
            s_name = soldier_name or (_norm(link.get("soldier", "")) if link else None) or "לא ידוע"
            lbl = (_norm(link.get("label", "")) if link else "") or ""

            rows: list[dict] = db.setdefault("gun_storage", [])
            row = _get_storage_row(db, serial)
            if not row:
                rid = _next_id(rows)
                row = {
                    "id": rid,
                    "kind": kind_v,
                    "serial": serial,
                    "soldier": s_name,
                    "label": lbl,
                    "location": loc_v,
                    "custom_location": cl,
                    "stored_date": d.isoformat(),
                    "remind": bool(remind_v),
                    "next_due": None,
                    "reminder_message_id": None,
                    "updated_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                rows.append(row)
            else:
                row["kind"] = kind_v
                row["soldier"] = s_name
                row["label"] = lbl
                row["location"] = loc_v
                row["custom_location"] = cl
                row["stored_date"] = d.isoformat()
                row["remind"] = bool(remind_v)
                row["updated_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")

            # reminder logic
            if loc_v == "לא מאופסן" or not remind_v:
                row["next_due"] = None
            else:
                row["next_due"] = _next_due_from(d).isoformat()

            updated_ids.append(int(row["id"]))

        save_db(db)

        # log + response
        await send_log(self.bot, "GUN STORAGE SET", f"kind={kind_v} | target={target} | serials={serials} | loc={loc_v} | remind={remind_v} | date={d.isoformat()}")
        if len(serials) == 1:
            await interaction.response.send_message(f"✅ עודכן אפסון `{serials[0]}`.", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ עודכנו {len(serials)} פריטים: {', '.join('`'+s+'`' for s in serials)}", ephemeral=True)

    # ===== reminder sender loop =====
    @tasks.loop(seconds=60)
    async def reminder_loop(self):
        await self._load_persistent_views()

        db = load_db()
        rows: list[dict] = db.get("gun_storage", []) or []
        if not rows:
            return

        now = _now()
        armory_id = self.bot.CHANNELS.get("armory")  # נשקייה channel
        if not armory_id:
            return
        channel = self.bot.get_channel(armory_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(armory_id)
            except Exception:
                return

        changed = False
        for row in rows:
            if not row.get("remind"):
                continue
            if row.get("location") == "לא מאופסן":
                continue
            nd = row.get("next_due")
            if not nd:
                continue

            try:
                due = dt.datetime.fromisoformat(nd)
            except Exception:
                continue

            if now >= due:
                # Send reminder message and push next_due forward only if user presses button;
                # Here we just send the reminder (one message per due cycle).
                # Avoid spamming: if we already sent a reminder message for current due, keep message_id and don't re-send.
                # We store reminder_message_id per cycle: if it exists and due already passed, don't send again.
                if row.get("reminder_message_id"):
                    continue

                try:
                    msg = await channel.send(
                        _reminder_text(row),
                        view=GunReminderView(self.bot, int(row["id"]))
                    )
                    row["reminder_message_id"] = msg.id
                    changed = True
                    await send_log(self.bot, "GUN REMINDER SENT", f"id={row.get('id')} | serial={row.get('serial')} | due={nd}")
                    # Register persistent view for this message_id
                    try:
                        self.bot.add_view(GunReminderView(self.bot, int(row["id"])), message_id=msg.id)
                    except Exception:
                        pass
                except Exception:
                    continue

        if changed:
            save_db(db)

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GunCog(bot), guild=discord.Object(id=bot.GUILD_ID))
