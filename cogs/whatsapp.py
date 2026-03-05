# cogs/whatsapp.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import threading
import datetime as dt
from typing import Optional, Any, Dict, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Jerusalem")
except Exception:
    TZ = None


# ===== storage =====
_LOCK = threading.RLock()
DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "db.json")


def _ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def load_db() -> dict:
    with _LOCK:
        _ensure_dirs()
        if not os.path.exists(DB_FILE):
            # minimal db
            db = {
                "soldiers": [],
                "tasks": [],
                "sign": {},
                "status": {},
                "kashag": {},
                "inventory": {},
                "gun_storage": [],
                "incidents": [],
            }
            save_db(db)
            return db
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)

        # defaults
        db.setdefault("soldiers", [])
        db.setdefault("tasks", [])
        db.setdefault("sign", {})
        db.setdefault("status", {})
        db.setdefault("kashag", {})
        db.setdefault("inventory", {})
        db.setdefault("gun_storage", [])
        db.setdefault("incidents", [])
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


def _safe_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


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


# ===== common formatting =====
STATE_EMOJI = {
    "תקין": "✅",
    "חוסר": "❌",
    "בלאי": "🟠",
    "צריך להגיש בלאי": "🟠",
    "אחר": "⚪",
}

BAD_STATES = {"חוסר", "בלאי", "צריך להגיש בלאי", "אחר"}

PRIORITY_ORDER = {"דחוף": 0, "גבוה": 1, "בינוני": 2, "נמוך": 3}


def _fmt_date_iso(s: Optional[str]) -> str:
    if not s:
        return "—"
    try:
        if "T" in s:
            x = dt.datetime.fromisoformat(s)
            return x.strftime("%d/%m/%Y %H:%M")
        x = dt.date.fromisoformat(s)
        return x.strftime("%d/%m/%Y")
    except Exception:
        return str(s)


# ===== builders =====
def build_tasks(db: dict) -> str:
    rows: list[dict] = db.get("tasks", []) or []
    open_rows = [r for r in rows if _norm(r.get("status") or "פתוח") != "הושלם"]

    if not open_rows:
        return "🗒️ *משימות פתוחות*\n✅ אין משימות פתוחות."

    def key(r: dict):
        pr = _norm(r.get("priority") or "בינוני")
        pr_rank = PRIORITY_ORDER.get(pr, 99)
        d_raw = r.get("date")
        try:
            d = dt.date.fromisoformat(d_raw) if d_raw else dt.date(2100, 1, 1)
        except Exception:
            d = dt.date(2100, 1, 1)
        return (pr_rank, d, int(r.get("id", 0)))

    open_rows.sort(key=key)

    lines = ["🗒️ *משימות פתוחות*"]
    for r in open_rows[:120]:
        tid = r.get("id")
        title = _norm(r.get("title") or "")
        pr = _norm(r.get("priority") or "בינוני")
        st = _norm(r.get("status") or "פתוח")
        date_s = _fmt_date_iso(r.get("date"))
        det = _norm(r.get("details") or "")

        line = f"• #{tid} — *{title}* | {pr} | {date_s} | {st}"
        lines.append(line)
        if det:
            lines.append(f"  - {det[:200]}{'…' if len(det) > 200 else ''}")

    extra = max(0, len(open_rows) - 120)
    if extra:
        lines.append(f"… ועוד {extra} משימות.")
    return "\n".join(lines)


def _signed_items(sign_rec: dict) -> list[str]:
    items = [_norm(x) for x in (sign_rec.get("items") or []) if _norm(x)]
    other = _norm(sign_rec.get("other") or "")
    if other:
        items.append(f"אחר: {other}")

    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _get_status(status_db: dict, soldier: str, item: str) -> tuple[str, Optional[str]]:
    rec = (status_db.get(soldier, {}) or {}).get(item)
    if not rec:
        return ("תקין", None)
    st = _norm((rec or {}).get("state") or "תקין") or "תקין"
    note = _norm((rec or {}).get("note") or "")
    return (st, note if note else None)


def build_logistica(db: dict, soldier: Optional[str] = None) -> str:
    sign_db = db.get("sign", {}) or {}
    status_db = db.get("status", {}) or {}

    soldiers = list(db.get("soldiers", []))
    if not soldiers:
        soldiers = sorted(set(sign_db.keys()))

    if soldier:
        s = _norm(soldier)
        sign_rec = sign_db.get(s) or {"items": [], "other": None}
        items = _signed_items(sign_rec)
        if not items:
            return f"📋 *לוגיסטיקה — {s}*\nאין חתימות."

        lines = [f"📋 *לוגיסטיקה — {s}*"]
        for it in items:
            st, note = _get_status(status_db, s, it)
            em = STATE_EMOJI.get(st, "•")
            line = f"• {em} {it} — {st}"
            if note:
                line += f" ({note[:120]}{'…' if len(note) > 120 else ''})"
            lines.append(line)
        return "\n".join(lines)

    if not soldiers:
        return "📋 *לוגיסטיקה*\nאין נתונים."

    lines = ["📋 *לוגיסטיקה — חתימות + סטטוסים*"]
    count_blocks = 0
    for s in soldiers[:30]:
        sign_rec = sign_db.get(s) or {"items": [], "other": None}
        items = _signed_items(sign_rec)
        if not items:
            continue
        lines.append(f"\n👤 *{s}*")
        for it in items:
            st, note = _get_status(status_db, s, it)
            em = STATE_EMOJI.get(st, "•")
            line = f"• {em} {it} — {st}"
            if note:
                line += f" ({note[:120]}{'…' if len(note) > 120 else ''})"
            lines.append(line)
        count_blocks += 1

    if count_blocks == 0:
        return "📋 *לוגיסטיקה*\nאין חתימות."

    if len(soldiers) > 30:
        lines.append("\n… קיצרתי (אפשר לייצא חייל ספציפי)")
    return "\n".join(lines)


def build_attention(db: dict, soldier: Optional[str] = None) -> str:
    status_db = db.get("status", {}) or {}

    def collect(s: str) -> list[tuple[str, str, Optional[str]]]:
        out = []
        for item, rec in (status_db.get(s, {}) or {}).items():
            it = _norm(item)
            st = _norm((rec or {}).get("state") or "תקין")
            note = _norm((rec or {}).get("note") or "") or None
            if st in BAD_STATES:
                out.append((it, st, note))
        # sort: state then item
        def rank_state(st: str) -> int:
            if st == "חוסר": return 0
            if st in ("בלאי", "צריך להגיש בלאי"): return 1
            if st == "אחר": return 2
            return 9
        out.sort(key=lambda x: (rank_state(x[1]), x[0]))
        return out

    if soldier:
        s = _norm(soldier)
        issues = collect(s)
        if not issues:
            return f"🚨 *Attention — {s}*\n✅ אין פערים."
        lines = [f"🚨 *Attention — {s}*"]
        for it, st, note in issues:
            em = STATE_EMOJI.get(st, "•")
            line = f"• {em} {it} — {st}"
            if note:
                line += f" ({note[:140]}{'…' if len(note) > 140 else ''})"
            lines.append(line)
        return "\n".join(lines)

    soldiers = list(db.get("soldiers", []))
    if not soldiers:
        soldiers = sorted(set(status_db.keys()))

    all_lines = ["🚨 *Attention — כל מה שלא תקין*"]
    total = 0
    shown_soldiers = 0
    for s in soldiers[:40]:
        issues = collect(s)
        if not issues:
            continue
        shown_soldiers += 1
        all_lines.append(f"\n👤 *{s}*")
        for it, st, note in issues:
            total += 1
            em = STATE_EMOJI.get(st, "•")
            line = f"• {em} {it} — {st}"
            if note:
                line += f" ({note[:140]}{'…' if len(note) > 140 else ''})"
            all_lines.append(line)
        if len("\n".join(all_lines)) > 3300:
            all_lines.append("\n… קיצרתי")
            break

    if shown_soldiers == 0:
        return "🚨 *Attention*\n✅ אין פערים."

    all_lines.append(f"\nסה״כ פערים: *{total}*")
    return "\n".join(all_lines)


def build_inventory(db: dict) -> str:
    inv: dict = db.get("inventory", {}) or {}
    if not inv:
        return "📦 *ציוד סמליה*\nאין מלאי רשום."

    def key(kv):
        item, rec = kv
        st = _norm((rec or {}).get("state") or "אחר")
        st_rank = 0 if st == "חוסר" else 1 if st in ("בלאי", "צריך להגיש בלאי") else 2 if st == "אחר" else 3
        return (st_rank, item)

    lines = ["📦 *ציוד סמליה (מלאי)*"]
    for item, rec in sorted(inv.items(), key=key)[:200]:
        qty = rec.get("qty", 0)
        st = _norm(rec.get("state") or "אחר")
        note = _norm(rec.get("note") or "")
        em = STATE_EMOJI.get(st, "•")
        line = f"• {em} {item} — כמות: {qty} — {st}"
        if note:
            line += f" ({note[:120]}{'…' if len(note) > 120 else ''})"
        lines.append(line)
    return "\n".join(lines)


def build_nishkia(db: dict) -> str:
    rows: list[dict] = db.get("gun_storage", []) or []
    stored = [r for r in rows if _norm(r.get("location") or "") and _norm(r.get("location") or "") != "לא מאופסן"]
    if not stored:
        return "🏠 *נשקייה/בטחונית*\nאין פריטים מאופסנים כרגע."

    groups: Dict[str, List[dict]] = {"נשקייה": [], "בטחונית": [], "אחר": []}
    for r in stored:
        loc = _norm(r.get("location") or "")
        if loc in groups:
            groups[loc].append(r)
        else:
            groups["אחר"].append(r)

    def sort_key(r: dict):
        remind = 0 if r.get("remind") else 1
        due = r.get("next_due") or "9999"
        serial = str(r.get("serial") or "")
        return (remind, due, serial)

    for k in groups:
        groups[k].sort(key=sort_key)

    def row_line(r: dict) -> str:
        kind = _norm(r.get("kind") or "—")
        serial = _norm(str(r.get("serial") or "—"))
        soldier = _norm(r.get("soldier") or "לא ידוע")
        label = _norm(r.get("label") or "")
        stored_d = _fmt_date_iso(r.get("stored_date"))
        loc = _norm(r.get("location") or "—")
        cl = _norm(r.get("custom_location") or "")
        loc_show = f"אחר: {cl}" if loc == "אחר" and cl else loc
        remind = bool(r.get("remind"))
        due = _fmt_date_iso(r.get("next_due")) if remind else "—"
        due_part = f" | תזכורת: {due}" if (remind and r.get("next_due")) else ""
        return f"• {kind} {serial} — {soldier} — {label} | אפסון: {stored_d} | מיקום: {loc_show}{due_part}"

    lines = ["🏠 *נשקייה/בטחונית — סטטוס אפסון*"]
    for title in ("נשקייה", "בטחונית", "אחר"):
        lst = groups.get(title, [])
        if not lst:
            continue
        lines.append(f"\n*{title}:*")
        for r in lst[:120]:
            lines.append(row_line(r))
    return "\n".join(lines)


def build_weapon_issues(db: dict) -> str:
    status_db = db.get("status", {}) or {}

    def is_weapon_item(it: str) -> bool:
        itn = _norm(it)
        if itn in ("נשק", "צלם"):
            return True
        if itn.startswith("אחר:"):
            return True
        return False

    by_soldier: Dict[str, List[Tuple[str, str, Optional[str]]]] = {}
    for soldier, items in status_db.items():
        s = _norm(soldier)
        for it, rec in (items or {}).items():
            itn = _norm(it)
            if not itn or not is_weapon_item(itn):
                continue
            st = _norm((rec or {}).get("state") or "תקין")
            note = _norm((rec or {}).get("note") or "") or None
            if st in BAD_STATES:
                by_soldier.setdefault(s, []).append((itn, st, note))

    if not by_soldier:
        return "🛠️ *פערי נשק*\n✅ אין פערים."

    def rank_state(st: str) -> int:
        if st == "חוסר": return 0
        if st in ("בלאי", "צריך להגיש בלאי"): return 1
        if st == "אחר": return 2
        return 9

    lines = ["🛠️ *פערי נשק (נשק/צלמים לא תקינים)*"]
    total = 0
    for s in sorted(by_soldier.keys()):
        lines.append(f"\n👤 *{s}*")
        lst = sorted(by_soldier[s], key=lambda x: (rank_state(x[1]), x[0]))
        for it, st, note in lst:
            total += 1
            em = STATE_EMOJI.get(st, "•")
            line = f"• {em} {it} — {st}"
            if note:
                line += f" ({note[:140]}{'…' if len(note) > 140 else ''})"
            lines.append(line)

    lines.append(f"\nסה״כ פערים: *{total}*")
    return "\n".join(lines)


def build_kashag(db: dict, soldier: Optional[str] = None) -> str:
    """
    Supports flexible structures.
    Expected common shape:
      db["kashag"][soldier] = {
        "run": ..., "sprits": ..., "pullups": ..., "dips": ..., "trapbar": ...,
        "kir": "עבר/לא עבר", "hevel": "עבר/לא עבר", "bahmas": ...,
        "updated_at": ...
      }
    """
    kdb: dict = db.get("kashag", {}) or {}
    if not kdb:
        return "🏃‍♂️ *תוצאות כושר*\nאין תוצאות."

    def fmt_one(s: str, rec: dict) -> List[str]:
        r = []
        r.append(f"👤 *{s}*")
        # כש״ג
        a = []
        for key, label in [
            ("run", "ריצה"),
            ("sprits", "ספרינטים"),
            ("pullups", "מתח"),
            ("dips", "מקבילים"),
            ("trapbar", "טראפ בר"),
        ]:
            v = rec.get(key)
            if v is None or str(v).strip() == "":
                continue
            a.append(f"{label}: {v}")
        if a:
            r.append("כש״ג: " + " | ".join(a))

        # בחמס
        b = []
        for key, label in [
            ("kir", "קיר"),
            ("hevel", "חבל"),
            ("bahmas", "זמן בחמס"),
        ]:
            v = rec.get(key)
            if v is None or str(v).strip() == "":
                continue
            b.append(f"{label}: {v}")
        if b:
            r.append("בחמס: " + " | ".join(b))

        upd = rec.get("updated_at") or rec.get("date") or ""
        if upd:
            r.append(f"עודכן: {upd}")
        return r

    if soldier:
        s = _norm(soldier)
        rec = kdb.get(s)
        if not rec:
            return f"🏃‍♂️ *תוצאות כושר*\nלא נמצאו תוצאות עבור {s}."
        lines = ["🏃‍♂️ *תוצאות כושר*"]
        lines.extend(fmt_one(s, rec if isinstance(rec, dict) else {}))
        return "\n".join(lines)

    lines = ["🏃‍♂️ *תוצאות כושר*"]
    shown = 0
    for s in sorted(kdb.keys())[:40]:
        rec = kdb.get(s)
        if not isinstance(rec, dict):
            continue
        lines.append("")
        lines.extend(fmt_one(s, rec))
        shown += 1
    if len(kdb.keys()) > 40:
        lines.append("\n… קיצרתי")
    return "\n".join(lines).strip()


def build_all(db: dict, soldier: Optional[str] = None) -> str:
    parts = [
        build_tasks(db),
        "",
        build_attention(db, soldier=None),
        "",
        build_logistica(db, soldier=soldier),
        "",
        build_inventory(db),
        "",
        build_nishkia(db),
        "",
        build_weapon_issues(db),
        "",
        build_kashag(db, soldier=soldier),
    ]
    return "\n".join(parts).strip()


# ===== cog =====
class WhatsAppCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    whatsapp = app_commands.Group(name="whatsapp", description="ייצוא נתונים בפורמט קריא לוואטסאפ")

    @whatsapp.command(name="export", description="ייצוא לוואטסאפ (משימות/לוגיסטיקה/attention/כשג/מלאי/נשקייה/פערי נשק/הכל)")
    @app_commands.describe(
        what="מה לייצא",
        soldier="חייל אופציונלי (ללוגיסטיקה/כשג/הכל)",
    )
    @app_commands.choices(what=[
        app_commands.Choice(name="tasks", value="tasks"),
        app_commands.Choice(name="attention", value="attention"),
        app_commands.Choice(name="logistica", value="logistica"),
        app_commands.Choice(name="kashag", value="kashag"),
        app_commands.Choice(name="inventory", value="inventory"),
        app_commands.Choice(name="nishkia", value="nishkia"),
        app_commands.Choice(name="weapon_issues", value="weapon_issues"),
        app_commands.Choice(name="all", value="all"),
    ])
    async def export_cmd(
        self,
        interaction: discord.Interaction,
        what: app_commands.Choice[str],
        soldier: Optional[str] = None,
    ):
        db = load_db()
        w = what.value
        sol = _norm(soldier or "") or None

        if w == "tasks":
            txt = build_tasks(db)
        elif w == "attention":
            txt = build_attention(db, soldier=None)  # attention is generally all
        elif w == "logistica":
            txt = build_logistica(db, soldier=sol)
        elif w == "kashag":
            txt = build_kashag(db, soldier=sol)
        elif w == "inventory":
            txt = build_inventory(db)
        elif w == "nishkia":
            txt = build_nishkia(db)
        elif w == "weapon_issues":
            txt = build_weapon_issues(db)
        else:
            txt = build_all(db, soldier=sol)

        # WhatsApp copy-paste friendly: send inside code block
        if len(txt) > 3900:
            txt = txt[:3900] + "\n… (קיצרתי בגלל מגבלת הודעה)"

        await interaction.response.send_message(f"```text\n{txt}\n```", ephemeral=True)
        await send_log(self.bot, "WHATSAPP EXPORT", f"what={w} | soldier={sol or '-'} | by={interaction.user.display_name}")

    @export_cmd.autocomplete("soldier")
    async def soldier_autocomplete(self, interaction: discord.Interaction, current: str):
        db = load_db()
        soldiers = db.get("soldiers", [])
        cur = _norm(current).lower()
        matches = [s for s in soldiers if cur in s.lower()] if soldiers else []
        return [app_commands.Choice(name=s, value=s) for s in matches[:25]]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WhatsAppCog(bot), guild=discord.Object(id=bot.GUILD_ID))
