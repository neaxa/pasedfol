"""
ذخیره‌سازی پنل
- تنظیمات در config.json (کلیدهای API رمزنگاری‌شده با کلید مشتق از PANEL_PASSWORD)
- پست‌ها و صف انتشار در panel.db (SQLite)
"""
import base64
import copy
import glob
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", os.path.join(BASE, "data")))
MEDIA_DIR = os.path.join(DATA_DIR, "media")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "panel.db")
SESSION_FILE = "ig_session.json"
os.makedirs(MEDIA_DIR, exist_ok=True)

SLOTS = ("chat", "caption", "image", "video", "voice")
ENC = "enc1:"

DEFAULT_SLOT = {"provider": "pollinations", "api_key": "", "model": "", "base_url": "", "voice": ""}

DEFAULTS = {
    "timezone": "Asia/Tehran",
    "language": "fa",
    "brand": {"niche": "", "tone": "", "extra": ""},
    "ai": {
        "chat": dict(DEFAULT_SLOT),
        "caption": dict(DEFAULT_SLOT),
        "image": dict(DEFAULT_SLOT),
        "video": dict(DEFAULT_SLOT, provider="deapi", seconds=5),
        "voice": dict(DEFAULT_SLOT, provider="openai"),
    },
    "dm": {
        "mode": "draft",
        "reply_in_groups": False,
        "max_replies_per_hour": 20,
        "poll_seconds": 45,
        "disclose": False,
        "blocklist": [],
    },
    "autopilot": {
        "enabled": False,
        "topics": [],
        "times": ["10:00", "19:00"],
        "kinds": ["post"],
        "approve": "review",
        "with_voice": False,
        "video": False,
        "sound": True,
        # طول فیلم برای استوری/ریلز: short = عادی؛ long = چند کلیپ ۱۰ ثانیه‌ای با کاراکتر ثابت که به هم چسبانده می‌شوند
        "length": "short",
        "long_seconds": 20,
    },
    "publish": {"min_gap_minutes": 10, "max_per_day": 4},
    "growth": {
        # کشف پست/ریلز پربازدید یک موضوع و گذاشتن آن در استوری
        "story": {
            "enabled": False, "mode": "review", "topics": [], "sources": "both", "types": ["reel", "post"],
            "min_views": 5000, "min_likes": 300, "min_comments": 0, "max_age_days": 30,
            "per_run": 1, "times": ["13:00", "21:00"], "max_per_day": 3, "min_gap_minutes": 30,
            "ai_expand": True, "blocklist": [],
            # شکل استوری: full = کل عکس/ویدیو داخل خود استوری دیده می‌شود؛ card = کارت پیش‌نمایش که با زدن روی آن پست اصلی باز می‌شود
            "style": "full", "add_link": True,
            # هر پست/ریلز جدیدی که در پیج خودت گذاشته شود، خودکار استوری هم بشود
            "share_own": False, "share_own_kinds": ["post", "reel"], "share_own_delay_minutes": 3,
        },
        # کامنت خودکار زیر پست‌های پربازدید/پرکامنت
        "comment": {
            "enabled": False, "mode": "review", "topics": [], "accounts": [], "sources": "both",
            "types": ["reel", "post"], "min_views": 20000, "min_likes": 0, "min_comments": 50, "max_age_days": 7,
            "style": "ai", "ai_brief": "", "manual_comments": [], "ai_expand": True, "blocklist": [],
            "per_scan": 2, "scan_every_minutes": 120, "max_per_day": 10, "max_per_hour": 3,
            "min_gap_seconds": 300, "hours_from": 9, "hours_to": 23,
        },
        "cooldown_hours": 6,
    },
    # «میلیونیم»: آنالیز روزانه‌ی کل پیج و رقبا و ساخت محتوا با هدف یک پست میلیونی و ۱۰ هزار فالوور
    "million": {
        "enabled": False, "goal_views": 1_000_000, "goal_followers": 10_000, "analyze_time": "09:00",
        "posts_per_day": 2, "kinds": ["reel", "post"], "approve": "review", "video": False, "with_voice": True,
        "sound": True, "competitors": True, "extra_hashtags": [], "competitor_accounts": [],
    },
}

_cfg_lock = threading.Lock()


# ---------- رمزنگاری کلیدها ----------
@lru_cache(maxsize=1)
def _fernet():
    pw = os.getenv("PANEL_PASSWORD", "")
    path = os.path.join(DATA_DIR, "enc.salt")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(16))
    with open(path, "rb") as f:
        salt = f.read()
    key = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000, 32)
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value):
    if not value or value.startswith(ENC):
        return value
    return ENC + _fernet().encrypt(value.encode()).decode()


def decrypt(value):
    """برمی‌گرداند (متن، خوانا؟)"""
    if not value or not value.startswith(ENC):
        return value, True
    try:
        return _fernet().decrypt(value[len(ENC):].encode()).decode(), True
    except InvalidToken:
        return "", False


# ---------- config ----------
def _merge(default, loaded):
    out = copy.deepcopy(default)
    for k, v in (loaded or {}).items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _migrate(raw):
    """نسخه‌ی قدیمی (فقط Pollinations در سطح بالا) را به ساختار جدید تبدیل می‌کند."""
    if "ai" in raw or "api_key" not in raw:
        return raw
    new = {"ai": {}, "dm": {}}
    for s in ("chat", "caption"):
        new["ai"][s] = {"provider": "pollinations", "api_key": raw.get("api_key", ""), "model": raw.get("model", "")}
    for k in DEFAULTS["dm"]:
        if k in raw:
            new["dm"][k] = raw[k]
    return new


def read_raw():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_config(decrypt_keys=True):
    cfg = _merge(DEFAULTS, _migrate(read_raw()))
    cfg["_unreadable"] = []
    for slot in SLOTS:
        s = cfg["ai"][slot]
        if decrypt_keys:
            s["api_key"], ok = decrypt(s.get("api_key", ""))
            if not ok:
                cfg["_unreadable"].append(slot)
    return cfg


def save_config(cfg):
    cfg = copy.deepcopy(cfg)
    cfg.pop("_unreadable", None)
    for slot in SLOTS:
        cfg["ai"][slot]["api_key"] = encrypt(cfg["ai"][slot].get("api_key", ""))
    with _cfg_lock:
        tmp = CONFIG_PATH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)


def session_files():
    return glob.glob(os.path.join(DATA_DIR, "ig_session*"))


def child_env():
    env = {k: v for k, v in os.environ.items() if k not in ("PANEL_PASSWORD", "PANEL_EMAIL")}
    env["DATA_DIR"] = DATA_DIR
    return env


# ---------- زمان ----------
def tz_of(cfg=None):
    name = (cfg or load_config(False)).get("timezone") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def fmt_ts(ts, cfg=None):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz_of(cfg)).strftime("%Y-%m-%d %H:%M")


def parse_local(text, cfg=None):
    """«2026-10-20T18:30» به ساعت منطقه‌ی پنل → epoch"""
    try:
        dt = datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return dt.replace(tzinfo=tz_of(cfg)).timestamp()


def local_input_value(ts, cfg=None):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz_of(cfg)).strftime("%Y-%m-%dT%H:%M")


# ---------- دیتابیس ----------
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, kind TEXT,
            topic TEXT DEFAULT '', caption TEXT DEFAULT '', voice_text TEXT DEFAULT '',
            image_prompt TEXT DEFAULT '', status TEXT, step TEXT DEFAULT '', error TEXT DEFAULT '',
            scheduled_at REAL, published_at REAL, result TEXT DEFAULT '', source TEXT DEFAULT 'manual',
            want_image INTEGER DEFAULT 1, want_voice INTEGER DEFAULT 0, user_image INTEGER DEFAULT 0,
            force INTEGER DEFAULT 0, out_type TEXT DEFAULT 'image', user_video INTEGER DEFAULT 0)"""
        )
        cols = {r[1] for r in c.execute("PRAGMA table_info(posts)")}
        if "out_type" not in cols:  # مهاجرت دیتابیس نسخه‌ی قبل
            c.execute("ALTER TABLE posts ADD COLUMN out_type TEXT DEFAULT 'image'")
        if "user_video" not in cols:  # ویدیوی آپلودی خود کاربر (استودیو)
            c.execute("ALTER TABLE posts ADD COLUMN user_video INTEGER DEFAULT 0")
        if "sound" not in cols:  # ویدیو صدا داشته باشد یا نه (۱ = با صدا)
            c.execute("ALTER TABLE posts ADD COLUMN sound INTEGER DEFAULT 1")
        if "brief" not in cols:  # دستور استراتژی (مثلاً از بخش «میلیونیم») برای AI هنگام نوشتن
            c.execute("ALTER TABLE posts ADD COLUMN brief TEXT DEFAULT ''")
        if "note" not in cols:  # یادداشت اطلاعاتی برای کاربر (مثلاً «ویدیو صدای خودش را نداشت»)
            c.execute("ALTER TABLE posts ADD COLUMN note TEXT DEFAULT ''")
        if "ig_pk" not in cols:  # شناسه‌ی پست منتشرشده در اینستاگرام
            c.execute("ALTER TABLE posts ADD COLUMN ig_pk TEXT DEFAULT ''")
        if "long_secs" not in cols:  # ۰ = فیلم کوتاه؛ ۲۰ یا ۳۰ = فیلم بلند (چند کلیپ ۱۰ ثانیه‌ای)
            c.execute("ALTER TABLE posts ADD COLUMN long_secs INTEGER DEFAULT 0")
        if "storyboard" not in cols:  # JSON: کاراکتر ثابت + صحنه‌های فیلم بلند
            c.execute("ALTER TABLE posts ADD COLUMN storyboard TEXT DEFAULT ''")
        c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        # هدف‌های «کشف و استوری» و «کامنت خودکار»: هر پست اینستاگرام برای هر عملیات فقط یک بار
        c.execute(
            """CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, media_pk TEXT, code TEXT DEFAULT '',
            author TEXT DEFAULT '', kind TEXT DEFAULT 'post', views INTEGER DEFAULT 0, likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0, taken_at REAL DEFAULT 0, thumb TEXT DEFAULT '', caption TEXT DEFAULT '',
            topic TEXT DEFAULT '', score REAL DEFAULT 0, text TEXT DEFAULT '', status TEXT, error TEXT DEFAULT '',
            created REAL, due REAL, acted_at REAL, force INTEGER DEFAULT 0, manual INTEGER DEFAULT 0,
            UNIQUE(action, media_pk))"""
        )
        # عکس فوری روزانه‌ی پیج برای بخش «میلیونیم» (فالوور، بازدید پست‌ها، تحلیل AI)
        c.execute(
            """CREATE TABLE IF NOT EXISTS mil_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, followers INTEGER DEFAULT 0, following INTEGER DEFAULT 0,
            media_count INTEGER DEFAULT 0, best_views INTEGER DEFAULT 0, total_views INTEGER DEFAULT 0,
            data TEXT DEFAULT '{}', strategy TEXT DEFAULT '{}')"""
        )


POST_COLS = {
    "kind", "topic", "caption", "voice_text", "image_prompt", "status", "step", "error", "scheduled_at",
    "published_at", "result", "source", "want_image", "want_voice", "user_image", "force", "out_type", "user_video",
    "sound", "brief", "note", "ig_pk", "long_secs", "storyboard",
}


def post_create(**fields):
    fields = {k: v for k, v in fields.items() if k in POST_COLS}
    fields["created"] = time.time()
    cols = ",".join(fields)
    with db() as c:
        cur = c.execute(f"INSERT INTO posts ({cols}) VALUES ({','.join('?' * len(fields))})", list(fields.values()))
        return cur.lastrowid


def post_update(pid, **fields):
    fields = {k: v for k, v in fields.items() if k in POST_COLS}
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE posts SET {sets} WHERE id=?", [*fields.values(), pid])


def post_get(pid):
    with db() as c:
        r = c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def post_list(limit=100, status=None, where="", args=()):
    q = "SELECT * FROM posts"
    conds, params = [], []
    if status:
        conds.append("status=?")
        params.append(status)
    if where:
        conds.append(where)
        params.extend(args)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def post_delete(pid):
    with db() as c:
        c.execute("DELETE FROM posts WHERE id=?", (pid,))


def meta_get(k, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(k, v):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (k, str(v)))


def post_dir(pid):
    d = os.path.join(MEDIA_DIR, str(int(pid)))
    os.makedirs(d, exist_ok=True)
    return d
