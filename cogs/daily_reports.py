# cogs/daily_reports.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt
from typing import Optional, Any, Dict, Tuple, List

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Jerusalem")
except Exception:
    TZ = None

print("BOT INSTANCE STARTED")
_LOCK = threading.RLock()
DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "db.json")


def _now() -> dt.datetime:
    if TZ is not None:
        return dt.datetime.now(TZ)
    return dt.datetime.now()


def _today_str() -> str:
    return _now().strftime("%Y-%m-%d")


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
        "tsign": {},
        "status": {},
        "inventory": {},
        "link": [],
        "gun_storage": [],
        "nishkia": {},
        "attention": {},
        "meta": {
            "version": 1,
            "daily_reports": {
                "last_sent_date": None,     # "YYYY-MM-DD"
                "message_ids": {}           # channel_key -> message_id
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

        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
                if not raw:
                    db = _default_db()
                    save_db(db)
                    return db
                db = json.loads(raw)
        except Exception:
            db = _default_db()
            save_db(db)
            return db

        base = _default_db()
        for k, v in base.items():
            if k not in db:
                db[k] = v

        db.setdefault("meta", {})
        db["meta"].setdefault("daily_reports", {})
        dr = db["meta"]["daily_reports"]
        dr.setdefault("last_sent_date", None)
        dr.setdefault("message_ids", {})
        if not isinstance(dr.get("message_ids"), dict):
            dr["message_ids"] = {}

        return db


def save_db(db: dict) -> None:
    with _LOCK:
        _ensure_dirs()
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DB_FILE)


def _safe_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


def _norm(s: str) -> str:
    return " ".join((s or "").strip().split())


def _trim(s: str, limit: int = 3800) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit - 1] + "…"


async def _get_channel(bot: commands.Bot, key: str) -> Optional[discord.TextChannel]:
    ch_id = None
    try:
        ch_id = (bot.CHANNELS or {}).get(key)
    except Exception:
        ch_id = None

    if not ch_id:
        return None

    ch = bot.get_channel(ch_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(ch_id)
        except Exception:
            return None
    if isinstance(ch, discord.TextChannel) or isinstance(ch, discord.Thread):
        return ch
    return None


async def _delete_previous_message(bot: commands.Bot, channel: discord.abc.Messageable, key: str) -> None:
    db = load_db()
    dr = db.get("meta", {}).get("daily_reports", {}) or {}
    mid = _safe_int((dr.get("message_ids") or {}).get(key))

    if mid:
        try:
            msg = await channel.fetch_message(mid)  # type: ignore
            await msg.delete()
        except Exception:
            pass

    try:
        async for m in channel.history(limit=15):  # type: ignore
            if m.author and getattr(m.author, "id", None) == bot.user.id:
                if m.embeds:
                    await m.delete()
                    break
    except Exception:
        pass

    db = load_db()
    dr = db.setdefault("meta", {}).setdefault("daily_reports", {})
    mids = dr.setdefault("message_ids", {})
    if key in mids:
        save_db(db)


def _embed(title: str, description: str) -> discord.Embed:
    e = discord.Embed(title=title, description=_trim(description, 3900))
    e.set_footer(text=_now().strftime("%Y-%m-%d %H:%M:%S"))
    return e


# ========= Builders =========

def _build_tasks(db: dict) -> discord.Embed:
    tasks_list = db.get("tasks", []) or []
    open_tasks = [t for t in tasks_list if t.get("status") != "בוצע"]
    if not open_tasks:
        return _embed("משימות פתוחות 📌", "✅ אין משימות פתוחות.")

    pr_rank = {"דחוף": 0, "גבוה": 1, "בינוני": 2, "נמוך": 3}
    def key(t):
        pr = pr_rank.get(t.get("priority") or "בינוני", 9)
        d = t.get("date") or "9999-99-99"
        return (pr, d, t.get("created_at") or "")

    open_tasks.sort(key=key)

    lines = []
    for t in open_tasks[:40]:
        tid = t.get("id")
        title = (t.get("title") or "").strip() or "(ללא כותרת)"
        date_s = t.get("date")
        pr = t.get("priority") or "בינוני"
        details = (t.get("details") or "").strip()
        suffix = []
        if date_s:
            suffix.append(f"יעד: {date_s}")
        suffix.append(f"דחיפות: {pr}")
        lines.append(f"• `#{tid}` **{title}** — " + " | ".join(suffix))
        if details:
            lines.append(f"  ↳ {_trim(details, 180)}")

    if len(open_tasks) > 40:
        lines.append(f"\n… ועוד {len(open_tasks) - 40} משימות.")

    return _embed("משימות פתוחות 📌", "\n".join(lines))


def _build_incidents(db: dict) -> discord.Embed:
    inc = db.get("incidents", []) or []
    if not inc:
        return _embed("אירועים חריגים 🚨", "✅ אין אירועים חריגים רשומים.")

    def key(x):
        return (x.get("date") or "", x.get("id") or 0)
    inc_sorted = sorted(inc, key=key, reverse=True)

    lines = []
    for r in inc_sorted[:25]:
        iid = r.get("id")
        title = (r.get("title") or r.get("type") or "אירוע").strip()
        who = (r.get("name") or r.get("soldier") or "").strip()
        date_s = (r.get("date") or r.get("created_at") or "").strip()
        details = (r.get("details") or r.get("desc") or "").strip()
        head = f"• `#{iid}` **{title}**"
        if who:
            head += f" — {who}"
        if date_s:
            head += f" _( {date_s} )_"
        lines.append(head)
        if details:
            lines.append(f"  ↳ {_trim(details, 220)}")

    return _embed("אירועים חריגים 🚨", "\n".join(lines))


def _build_kashag(db: dict) -> discord.Embed:
    kdb = db.get("kashag", {}) or {}
    if not kdb:
        return _embed("תוצאות כושר 🏃‍♂️", "— אין נתונים.")

    names = sorted(kdb.keys())
    lines = []
    for name in names[:25]:
        rec = kdb.get(name) or {}
        run = rec.get("run")
        bahmas = rec.get("bahmas")
        pullups = rec.get("pullups")
        sprints = rec.get("sprints")
        dips = rec.get("dips")
        trap = rec.get("trapbar")
        kir = rec.get("kir") or "—"
        hev = rec.get("hevel") or "—"

        def fmt(x):
            if x is None:
                return "—"
            if isinstance(x, (int, float)) and abs(x - int(x)) < 1e-9:
                return str(int(x))
            return str(x)

        lines.append(
            f"• **{name}** — ריצה: {fmt(run)} | ספרינטים: {fmt(sprints)} | מתח: {fmt(pullups)} | מקבילים: {fmt(dips)} | טראפ: {fmt(trap)}"
        )
        lines.append(f"  ↳ בחמ״ס: קיר {kir} | חבל {hev} | זמן {fmt(bahmas)}")

    if len(names) > 25:
        lines.append(f"\n… ועוד {len(names) - 25} חיילים.")

    return _embed("תוצאות כושר 🏃‍♂️", "\n".join(lines))


def _build_notes(db: dict) -> discord.Embed:
    notes = db.get("notes", []) or []
    if not notes:
        return _embed("פתקים 📝", "📝 אין פתקים.")

    try:
        notes_sorted = sorted(notes, key=lambda x: int(x.get("id", 0)), reverse=True)
    except Exception:
        notes_sorted = list(reversed(notes))

    lines = []
    for n in notes_sorted[:30]:
        nid = n.get("id")
        title = (n.get("title") or n.get("text") or "פתק").strip()
        tag = (n.get("tag") or "").strip()
        created = (n.get("created_at") or n.get("date") or "").strip()
        line = f"• `#{nid}` {title}"
        if tag:
            line += f" _(tag: {tag})_"
        if created:
            line += f"\n  ↳ {created}"
        lines.append(line)

    return _embed("פתקים 📝", "\n".join(lines))


def _build_inventory(db: dict) -> discord.Embed:
    inv = db.get("inventory", {}) or {}
    if not inv:
        return _embed("ציוד סמליה 📦", "📦 אין מלאי רשום עדיין.")

    order = ["תקין", "חוסר", "צריך להגיש בלאי", "אחר"]
    def rk(st):
        return order.index(st) if st in order else 99

    items = sorted(inv.items(), key=lambda kv: (rk((kv[1] or {}).get("state") or "אחר"), kv[0]))

    lines = []
    for item, rec in items[:60]:
        qty = rec.get("qty", 0)
        st = rec.get("state") or "אחר"
        note = (rec.get("note") or "").strip()
        line = f"• **{item}** — כמות: **{qty}** | מצב: **{st}**"
        if note:
            line += f"\n  ↳ {_trim(note, 160)}"
        lines.append(line)

    if len(items) > 60:
        lines.append(f"\n… ועוד {len(items) - 60} פריטים.")

    return _embed("ציוד סמליה 📦", "\n".join(lines))


def _build_signs(db: dict) -> discord.Embed:
    # נשאר פה, אבל לא נשלח יותר אוטומטית כי הורדנו מ-REPORTS
    sdb = db.get("sign", {}) or {}
    tdb = db.get("tsign", {}) or {}
    cnt = 0
    try:
        if isinstance(sdb, dict):
            for _, rec in sdb.items():
                if isinstance(rec, dict) and isinstance(rec.get("items"), list):
                    cnt += len(rec.get("items") or [])
        if isinstance(tdb, dict):
            for _, rec in tdb.items():
                if isinstance(rec, dict) and isinstance(rec.get("items"), list):
                    cnt += len(rec.get("items") or [])
    except Exception:
        pass
    if cnt == 0:
        return _embed("חתימות ✍️", "✅ אין חתימות רשומות.")
    return _embed("חתימות ✍️", f"סה״כ פריטים חתומים: **{cnt}**")


def _build_link(db: dict) -> discord.Embed:
    rows = db.get("link", []) or []
    if not rows:
        return _embed("נכסל/צלמים 🔗", "— אין קישורים רשומים (/link).")

    by_s: Dict[str, int] = {}
    for r in rows:
        soldier = _norm(r.get("soldier", "לא ידוע"))
        by_s[soldier] = by_s.get(soldier, 0) + 1

    top = sorted(by_s.items(), key=lambda kv: kv[1], reverse=True)
    lines = [f"סה״כ קישורים: **{len(rows)}**", ""]
    for soldier, c in top[:25]:
        lines.append(f"• **{soldier}** — {c}")

    if len(top) > 25:
        lines.append(f"\n… ועוד {len(top) - 25}.")

    return _embed("נכסל/צלמים 🔗", "\n".join(lines))


def _build_nishkia(db: dict) -> discord.Embed:
    ndb = db.get("nishkia", {}) or {}
    if not ndb:
        return _embed("נשקייה 🧰", "— אין נתונים.")
    lines = []
    if isinstance(ndb, dict):
        for k in ("items", "weapons", "records"):
            v = ndb.get(k)
            if isinstance(v, list):
                lines.append(f"סה״כ רשומות: **{len(v)}**")
                break
        if not lines:
            lines.append("✅ נתוני נשקייה קיימים (מבנה פנימי לא מוצג כאן).")
    else:
        lines.append("✅ נתוני נשקייה קיימים.")
    return _embed("נשקייה 🧰", "\n".join(lines))


def _build_attention_all(db: dict) -> discord.Embed:
    adb = db.get("attention", {}) or {}
    if not adb:
        return _embed("מסדר סמל 🧾", "✅ אין פערים (הכול תקין).")

    gaps: List[str] = []
    if isinstance(adb, list):
        gaps = [str(x) for x in adb if str(x).strip()]
    elif isinstance(adb, dict):
        if isinstance(adb.get("gaps"), list):
            gaps = [str(x) for x in adb["gaps"] if str(x).strip()]
        else:
            for soldier, v in adb.items():
                if isinstance(v, list) and v:
                    gaps.append(f"{soldier}: " + ", ".join(map(str, v)))
                elif isinstance(v, dict) and v:
                    gaps.append(f"{soldier}: " + ", ".join(f"{a}={b}" for a, b in v.items()))

    if not gaps:
        return _embed("מסדר סמל 🧾", "✅ אין פערים (הכול תקין).")

    lines = []
    for g in gaps[:60]:
        lines.append(f"• {g}")
    if len(gaps) > 60:
        lines.append(f"\n… ועוד {len(gaps) - 60}.")
    return _embed("מסדר סמל 🧾", "\n".join(lines))


def _build_weapon_gaps(db: dict) -> discord.Embed:
    st = db.get("status", {}) or {}
    links = db.get("link", []) or []
    if not links:
        return _embed("פערי נשק 🎯", "— אין קישורי נשק/צלם (/link), אין איך לחשב פערים.")

    by_s: Dict[str, List[str]] = {}
    for r in links:
        soldier = _norm(r.get("soldier", ""))
        label = _norm(r.get("label", "")) or _norm(r.get("serial", ""))
        if soldier:
            by_s.setdefault(soldier, []).append(label)

    gaps = []
    for soldier, assets in by_s.items():
        smap = st.get(soldier, {}) if isinstance(st, dict) else {}
        has_weapon_status = False
        if isinstance(smap, dict):
            for item in smap.keys():
                it = _norm(str(item))
                if "נשק" in it or "צלם" in it:
                    has_weapon_status = True
                    break
        if not has_weapon_status:
            gaps.append(f"**{soldier}** — מקושר: {len(assets)} פריטים (אין סטטוס נשק/צלם)")

    if not gaps:
        return _embed("פערי נשק 🎯", "✅ אין פערי נשק/צלם (לפי סטטוס).")

    lines = ["החיילים הבאים מקושרים לנשק/צלם אבל אין להם סטטוס נשק/צלם:\n"]
    lines += [f"• {g}" for g in gaps[:60]]
    if len(gaps) > 60:
        lines.append(f"\n… ועוד {len(gaps) - 60}.")
    return _embed("פערי נשק 🎯", "\n".join(lines))


# ❌ הורדנו את signs כדי ש-#חתימות לא יקבל דוח יומי
REPORTS = [
    ("tasks", "tasks", _build_tasks),
    ("incidents", "incidents", _build_incidents),
    ("fitness", "fitness", _build_kashag),
    ("notes", "notes", _build_notes),
    ("inventory", "inventory", _build_inventory),
    ("attention", "attention", _build_attention_all),
    ("link", "link", _build_link),
    ("nishkia", "nishkia", _build_nishkia),
    ("weapon_gaps", "weapon_gaps", _build_weapon_gaps),
]


class DailyReportsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # שים לב: אין clock_loop בקובץ שלך, זה רק /resend.
        # השארתי כמו אצלך.
        self.clock_loop.start()

    def cog_unload(self):
        self.clock_loop.cancel()

    async def _send_one(self, key: str, builder) -> bool:
        db = load_db()
        ch = await _get_channel(self.bot, key)
        if ch is None:
            return False

        await _delete_previous_message(self.bot, ch, key)
        emb = builder(db)

        try:
            msg = await ch.send(embed=emb)
        except Exception:
            return False

        db = load_db()
        dr = db.setdefault("meta", {}).setdefault("daily_reports", {})
        mids = dr.setdefault("message_ids", {})
        mids[key] = msg.id
        save_db(db)
        return True

    async def send_all(self) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        for _, key, builder in REPORTS:
            results[key] = await self._send_one(key, builder)
        return results

    @app_commands.command(name="resend", description="מוחק את הודעת הדוח הקודמת ושולח מחדש (Embed) לכל החדרים או חדר ספציפי")
    @app_commands.describe(channel="איזה חדר לרענן (ריק = כולם)")
    async def resend_cmd(self, interaction: discord.Interaction, channel: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)

        if not channel:
            results = await self.send_all()
            ok = [k for k, v in results.items() if v]
            bad = [k for k, v in results.items() if not v]
            msg = "✅ ריענון בוצע.\n"
            msg += f"עבד: {', '.join(ok) if ok else '—'}\n"
            msg += f"נכשל/לא קיים CHANNELS: {', '.join(bad) if bad else '—'}"
            return await interaction.followup.send(msg, ephemeral=True)

        key = _norm(channel)
        mapping = {k: (k, b) for _, k, b in REPORTS}
        if key not in mapping:
            return await interaction.followup.send(
                "❌ channel לא מוכר. דוגמאות: tasks / incidents / fitness / notes / inventory / attention / link / nishkia / weapon_gaps",
                ephemeral=True
            )

        _, builder = mapping[key]
        ok = await self._send_one(key, builder)
        if ok:
            return await interaction.followup.send(f"✅ רוענן: {key}", ephemeral=True)
        return await interaction.followup.send(f"❌ נכשל לרענן: {key} (בדוק bot.CHANNELS והרשאות כתיבה/מחיקה).", ephemeral=True)

    @tasks.loop(seconds=30)
    async def clock_loop(self):
        # אם אצלך אין שימוש בזה - זה נשאר כדי לא לשבור.
        pass

    @clock_loop.before_loop
    async def before_clock(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DailyReportsCog(bot), guild=discord.Object(id=bot.GUILD_ID))