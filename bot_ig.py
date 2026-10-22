"""
دستیار هوشمند دایرکت اینستاگرام (روی اکانت خودت) با هر ارائه‌دهنده‌ی AI
- تنظیمات دایرکت از config.json و تنظیم/کلید بخش «چت» از متغیر AI_CONFIG_JSON (پنل می‌دهد)؛ session از ig_session.json
- هر چند ثانیه دایرکت‌ها را چک می‌کند (polling)، تاریخچه را ذخیره می‌کند و سبک نوشتن تو را یاد می‌گیرد
- mode=draft : پیشنهاد جواب در drafts.jsonl ذخیره می‌شود و در پنل دیده می‌شود
- mode=auto  : مستقیم در دایرکت جواب می‌دهد
فقط به پیام‌هایی جواب می‌دهد که بعد از روشن شدن ربات رسیده‌اند.
اگر session منقضی شود ربات با کد خروج 3 تمام می‌شود و باید در پنل دوباره لاگین کنی.
"""
import json
import logging
import os
import random
import sqlite3
import sys
import time
from collections import deque

from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired, PleaseWaitFewMinutes, RateLimitError
import ai
import store

DATA_DIR = os.getenv("DATA_DIR", ".")
os.chdir(DATA_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("assistant")

_cfg = store.load_config(decrypt_keys=False)
dm = _cfg["dm"]
try:
    CHAT_SLOT = json.loads(os.environ.get("AI_CONFIG_JSON", "{}"))
except json.JSONDecodeError:
    CHAT_SLOT = {}

MODE = dm.get("mode", "draft")
REPLY_IN_GROUPS = bool(dm.get("reply_in_groups", False))
MAX_REPLIES_PER_HOUR = int(dm.get("max_replies_per_hour", 20))
DISCLOSE = bool(dm.get("disclose", False))
BLOCKLIST = {str(x).lstrip("@").lower() for x in dm.get("blocklist", [])}
MODEL = CHAT_SLOT.get("model") or CHAT_SLOT.get("provider", "?")
POLL = max(5, int(dm.get("poll_seconds", 45)))
SESSION_FILE = "ig_session.json"
EXIT_LOGIN_REQUIRED = 3

db = sqlite3.connect("ig_history.db")
db.execute(
    "CREATE TABLE IF NOT EXISTS msgs (id TEXT PRIMARY KEY, chat TEXT, sender TEXT,"
    " is_me INTEGER, text TEXT, ts REAL)"
)
db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
db.commit()

sent_log = deque()


def meta_get(k, default=None):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(k, v):
    db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (k, str(v)))
    db.commit()


def is_known(mid):
    return db.execute("SELECT 1 FROM msgs WHERE id=?", (mid,)).fetchone() is not None


def save(mid, chat, sender, is_me, text, ts):
    db.execute(
        "INSERT OR IGNORE INTO msgs (id, chat, sender, is_me, text, ts) VALUES (?,?,?,?,?,?)",
        (mid, chat, sender, int(is_me), text, ts),
    )
    db.commit()


def my_style_samples(n=150):
    rows = db.execute(
        "SELECT text FROM msgs WHERE is_me=1 AND length(text) > 1 ORDER BY ts DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in reversed(rows)]


def chat_context(chat, n=15):
    rows = db.execute(
        "SELECT is_me, text FROM msgs WHERE chat=? ORDER BY ts DESC LIMIT ?", (chat, n)
    ).fetchall()
    return [("من" if r[0] else "طرف مقابل", r[1]) for r in reversed(rows)]


def generate_reply(chat):
    samples = "\n".join(f"- {s}" for s in my_style_samples())
    convo = "\n".join(f"{who}: {t}" for who, t in chat_context(chat))
    system = (
        "تو دستیار شخصی صاحب این اکانت اینستاگرام هستی و به جای او در دایرکت جواب می‌دهی. "
        "لحن، طول جمله، شکلک‌ها و سبک نوشتن او را از نمونه‌ها تقلید کن. "
        "کوتاه و طبیعی جواب بده. هیچ‌وقت اطلاعات حساس (پول، رمز، آدرس، تصمیم مهم) نده، "
        "قرار یا تعهد جدید نپذیر و اگر مطمئن نیستی بنویس: «بعداً خودم جواب می‌دم». "
        "اگر پیام نیاز به جواب ندارد (مثلاً فقط تشکر یا ایموجی)، فقط کلمه SKIP را بنویس. "
        "فقط متن پیام را بنویس، بدون توضیح اضافه.\n\n"
        f"نمونه پیام‌های واقعی صاحب اکانت:\n{samples or '(هنوز نمونه‌ای نیست)'}"
    )
    return ai.chat(CHAT_SLOT, [
        {"role": "system", "content": system},
        {"role": "user", "content": f"مکالمه تا اینجا:\n{convo}\n\nجواب بعدی من چیست؟"},
    ])


def rate_ok():
    now = time.time()
    while sent_log and now - sent_log[0] > 3600:
        sent_log.popleft()
    return len(sent_log) < MAX_REPLIES_PER_HOUR


def write_draft(thread_id, who, incoming, suggestion):
    rec = {"ts": time.time(), "thread": thread_id, "user": who, "message": incoming, "suggestion": suggestion}
    with open("drafts.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def connect():
    if not os.path.exists(SESSION_FILE):
        log.error("session پیدا نشد؛ در پنل وارد اینستاگرام شو.")
        sys.exit(EXIT_LOGIN_REQUIRED)
    cl = Client()
    cl.delay_range = [1, 3]
    if os.getenv("IG_PROXY"):
        cl.set_proxy(os.environ["IG_PROXY"])
    cl.load_settings(SESSION_FILE)
    try:
        cl.account_info()  # بررسی معتبر بودن session
    except (LoginRequired, ChallengeRequired) as e:
        log.error("session منقضی شده یا اینستاگرام تأیید هویت خواسته (%s). در پنل دوباره وارد شو.", type(e).__name__)
        sys.exit(EXIT_LOGIN_REQUIRED)
    return cl


def process_thread(cl, t, since):
    users = {str(u.pk): u.username for u in (t.users or [])}
    names = {n.lower() for n in users.values()}
    blocked = str(t.id) in BLOCKLIST or bool(names & BLOCKLIST)
    chat = str(t.id)
    # قدیمی‌ترین به جدیدترین
    for m in reversed(list(t.messages or [])):
        mid = str(m.id)
        if is_known(mid):
            continue
        text = (getattr(m, "text", None) or "").strip()
        ts = m.timestamp.timestamp() if getattr(m, "timestamp", None) else time.time()
        mine = bool(getattr(m, "is_sent_by_viewer", False)) or str(m.user_id) == str(cl.user_id)
        if not text:
            continue
        save(mid, chat, str(m.user_id), mine, text, ts)
        if mine or blocked or ts < since:
            continue
        if getattr(t, "is_group", False) and not REPLY_IN_GROUPS:
            continue
        if MODE == "auto" and not rate_ok():
            log.warning("rate limit reached, skipping %s", chat)
            continue
        try:
            reply = generate_reply(chat)
        except Exception:  # noqa: BLE001
            log.exception("AI call failed")
            continue
        if not reply or reply.upper().startswith("SKIP"):
            continue
        who = users.get(str(m.user_id), str(m.user_id))
        time.sleep(min(12, 2 + len(reply) / 15) + random.uniform(0, 3))
        if MODE == "auto":
            out = reply + ("\n(پاسخ خودکار)" if DISCLOSE else "")
            cl.direct_send(out, thread_ids=[int(t.id)])
            save(f"out-{time.time()}", chat, str(cl.user_id), True, out, time.time())
            sent_log.append(time.time())
            log.info("replied to %s", who)
        else:
            write_draft(chat, who, text, reply)
            log.info("draft saved for %s", who)


def main():
    cl = connect()
    log.info("logged in as %s, mode=%s, model=%s, poll=%ss", cl.user_id, MODE, MODEL, POLL)
    if meta_get("since") is None:
        meta_set("since", time.time())
    last_dump = time.time()
    fails = 0
    while True:
        cycle_start = time.time()
        since = float(meta_get("since"))
        try:
            threads = cl.direct_threads(amount=20)
            for t in threads:
                process_thread(cl, t, since)
            meta_set("since", cycle_start - 5)
            fails = 0
        except (LoginRequired, ChallengeRequired) as e:
            log.error("session معتبر نیست (%s). در پنل دوباره وارد شو.", type(e).__name__)
            sys.exit(EXIT_LOGIN_REQUIRED)
        except (PleaseWaitFewMinutes, RateLimitError):
            log.warning("اینستاگرام محدودیت زمانی داد؛ ۱۰ دقیقه صبر می‌کنم.")
            time.sleep(600)
        except Exception:  # noqa: BLE001
            fails += 1
            log.exception("poll failed (%d)", fails)
            time.sleep(min(600, 30 * fails))
        if time.time() - last_dump > 1200:
            try:
                cl.dump_settings(SESSION_FILE)
            except Exception:  # noqa: BLE001
                pass
            last_dump = time.time()
        time.sleep(POLL * random.uniform(0.8, 1.3))


if __name__ == "__main__":
    main()
