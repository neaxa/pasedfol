"""
دو قابلیت رشد پیج:
  ۱) «کشف و استوری»: با یک موضوع، ریلز/پست‌های پربازدید را در اینستاگرام پیدا می‌کند و (با تأیید تو یا خودکار) در استوری می‌گذارد.
  ۲) «کامنت خودکار»: پست‌های پربازدید/پرکامنتِ یک موضوع (یا پیج‌ها و لینک‌هایی که خودت می‌گویی) را پیدا می‌کند
     و کامنتِ AI (بر اساس جهتی که تو می‌دهی) یا کامنت دست‌نویس تو را زیرشان می‌گذارد.

همه‌ی کارهای اینستاگرام در فرآیند جدا (ig_actions.py) و پشت همان قفل انتشار انجام می‌شود تا همزمان با انتشار پست تداخل نکند.
هر پست برای هر عملیات فقط یک بار پردازش می‌شود. سقف روزانه/ساعتی، فاصله‌ی تصادفی و توقف خودکار هنگام محدودیت اینستاگرام رعایت می‌شود.
"""
import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

import ai
import jobs
import store

log = logging.getLogger("growth")
BASE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(BASE, "ig_actions.py")

ACTIONS = ("story", "comment")
STATUS_FA = {
    "found": "پیدا شد", "pending": "منتظر تأیید", "queued": "در صف", "running": "در حال انجام",
    "done": "انجام شد", "failed": "ناموفق", "skipped": "ردشده",
}
KIND_FA = {"reel": "ریلز", "post": "پست"}
STYLE_FA = {"full": "کامل (خود عکس/ویدیو)", "card": "کارت پیش‌نمایش"}

_flag_lock = threading.Lock()
_scanning = {"story": False, "comment": False}
_act_busy = threading.Event()


# ---------- اجرای فرآیند اینستاگرام ----------
def run_worker(cmd, params, timeout=900):
    """(نتیجه‌ی JSON، کد خروج) را برمی‌گرداند."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="act_", dir=store.DATA_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False)
    try:
        with jobs._pub_lock:  # فقط یک عملیات اینستاگرام در هر لحظه
            proc = subprocess.run(
                [sys.executable, "-u", SCRIPT, cmd, path],
                cwd=store.DATA_DIR, env=store.child_env(), capture_output=True, text=True, timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "عملیات بیش از حد طول کشید."}, -1
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            return json.loads(line), proc.returncode
        except json.JSONDecodeError:
            continue
    tail = " | ".join((proc.stderr or "").strip().splitlines()[-3:])[:300]
    return {"ok": False, "error": "پاسخ نامعتبر از فرآیند اینستاگرام. " + tail}, proc.returncode


# ---------- هشتگ‌ها ----------
def norm_tags(topic):
    """«رنگ مو» → ['رنگ_مو', 'رنگمو']؛ «#fitness» → ['fitness']"""
    t = (topic or "").strip().lstrip("#").replace("_", " ")
    t = re.sub(r"[^\w\s‌]", "", t)
    words = t.split()
    if not words:
        return []
    if len(words) == 1:
        return [words[0].lower()]
    return ["_".join(words).lower(), "".join(words).lower()]


def ai_slot(cfg):
    for name in ("caption", "chat"):
        s = cfg["ai"][name]
        if s.get("api_key") or s.get("provider") == "custom":
            return s
    return None


def expand_hashtags(cfg, topic):
    """با AI چند هشتگ پرکاربرد مرتبط پیشنهاد می‌دهد (هفت روز کش می‌شود)."""
    slot = ai_slot(cfg)
    if not slot:
        return []
    key = "growth:tags:" + topic.strip().lower()
    cached = store.meta_get(key)
    if cached:
        try:
            c = json.loads(cached)
            if time.time() - c["ts"] < 7 * 86400:
                return c["tags"]
        except (ValueError, KeyError):
            pass
    try:
        raw = ai.chat(slot, [{"role": "user", "content": (
            f"موضوع: {topic}\nپنج هشتگ پرکاربرد و مرتبط اینستاگرام (فارسی یا انگلیسی، هرکدام یک کلمه یا با _) برای پیدا کردن "
            'بهترین پست‌های این موضوع بده. فقط JSON: {"hashtags": ["..."]}')}])
        data = ai.extract_json(raw) or {}
        tags = []
        for t in data.get("hashtags", []):
            tags += norm_tags(str(t))[:1]
        tags = list(dict.fromkeys(tags))[:6]
    except ai.AIError:
        return []
    store.meta_set(key, json.dumps({"ts": time.time(), "tags": tags}, ensure_ascii=False))
    return tags


def build_hashtags(cfg, g, topic, max_tags=4):
    tags = norm_tags(topic)
    if g.get("ai_expand"):
        tags += expand_hashtags(cfg, topic)
    return list(dict.fromkeys(tags))[:max_tags]


# ---------- فیلتر و امتیاز ----------
def passes(g, it, now=None):
    now = now or time.time()
    if it.get("is_mine") or it.get("kind") not in g.get("types", ["reel", "post"]):
        return False
    if (it.get("author") or "").lower() in {str(x).lstrip("@").lower() for x in g.get("blocklist", [])}:
        return False
    max_age = int(g.get("max_age_days") or 0)
    if max_age and it.get("taken_at") and (now - it["taken_at"]) / 86400 > max_age:
        return False
    if (it.get("topic") or "").startswith("@") or it.get("topic") == "link":
        return True  # پیج/لینکی که خودت گفتی، به حد آستانه کاری ندارد
    conds = [it.get(f, 0) >= int(g.get(k) or 0)
             for k, f in (("min_views", "views"), ("min_likes", "likes"), ("min_comments", "comments")) if int(g.get(k) or 0) > 0]
    return any(conds) if conds else True  # «هرکدام از شرط‌ها» کافی است


def score(it):
    return it.get("views", 0) + it.get("likes", 0) * 10 + it.get("comments", 0) * 30


# ---------- دیتابیس هدف‌ها ----------
def exists(action, pk):
    with store.db() as c:
        return c.execute("SELECT 1 FROM targets WHERE action=? AND media_pk=?", (action, str(pk))).fetchone() is not None


def insert_target(action, it, status, text="", due=None, manual=0, force=0):
    """هدف تازه را ذخیره می‌کند؛ اگر قبلاً بوده None برمی‌گرداند."""
    with store.db() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO targets (action, media_pk, code, author, kind, views, likes, comments, taken_at, thumb,"
            " caption, topic, score, text, status, created, due, force, manual) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (action, str(it["pk"]), it.get("code", ""), it.get("author", ""), it.get("kind", "post"), it.get("views", 0),
             it.get("likes", 0), it.get("comments", 0), it.get("taken_at", 0), it.get("thumb", ""),
             (it.get("caption") or "")[:600], it.get("topic", ""), score(it), text, status, time.time(), due, force, manual))
        return cur.lastrowid if cur.rowcount else None


def target_get(tid):
    with store.db() as c:
        r = c.execute("SELECT * FROM targets WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def target_update(tid, **f):
    if not f:
        return
    with store.db() as c:
        c.execute(f"UPDATE targets SET {','.join(k + '=?' for k in f)} WHERE id=?", [*f.values(), tid])


def target_list(action, statuses=None, limit=60):
    q, args = "SELECT * FROM targets WHERE action=?", [action]
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        args += list(statuses)
    q += " ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'found' THEN 1 WHEN 'running' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END,"
    q += " score DESC, id DESC LIMIT ?"
    args.append(limit)
    with store.db() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def count_status(action, status):
    with store.db() as c:
        return c.execute("SELECT COUNT(*) FROM targets WHERE action=? AND status=?", (action, status)).fetchone()[0]


def count_done(action, since):
    with store.db() as c:
        return c.execute("SELECT COUNT(*) FROM targets WHERE action=? AND status='done' AND acted_at>=?",
                         (action, since)).fetchone()[0]


def _day_start(cfg):
    return datetime.now(store.tz_of(cfg)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def in_hours(cfg, g):
    h = datetime.now(store.tz_of(cfg)).hour
    a, b = int(g.get("hours_from", 0)), int(g.get("hours_to", 24))
    return (a <= h < b) if a < b else (h >= a or h < b)


# ---------- ساخت کامنت ----------
def _used_texts(limit=12):
    with store.db() as c:
        return [r[0] for r in c.execute(
            "SELECT text FROM targets WHERE action='comment' AND text!='' ORDER BY id DESC LIMIT ?", (limit,))]


def _pick_manual(manual):
    """کم‌استفاده‌ترین متن دست‌نویس را برمی‌دهد تا کامنت تکراری زیاد نشود."""
    with store.db() as c:
        used = {r[0]: r[1] for r in c.execute(
            "SELECT text, COUNT(*) FROM targets WHERE action='comment' AND status IN ('done','queued','running','pending')"
            " GROUP BY text")}
    low = min(used.get(m, 0) for m in manual)
    return random.choice([m for m in manual if used.get(m, 0) == low])


def _clean_comment(text):
    text = re.sub(r"https?://\S+", "", text or "")
    text = text.strip().strip('"«»“”').strip()
    return " ".join(text.split())[:300]


def ai_comment(cfg, it):
    g = cfg["growth"]["comment"]
    slot = ai_slot(cfg)
    if not slot:
        raise ai.AIError("برای کامنت AI اول کلید API بخش «نوشتن کپشن» (یا چت) را در تنظیمات AI وارد کن.")
    lang = "فارسی روان و محاوره‌ای (نه ترجمه‌ای)" if cfg.get("language") != "en" else "English"
    b = cfg["brand"]
    brief = (g.get("ai_brief") or "").strip()
    avoid = "\n".join(f"- {t}" for t in _used_texts()) or "(هنوز کامنتی نگذاشته‌ای)"
    system = (
        "تو صاحب یک پیج اینستاگرام هستی و زیر پست دیگران کامنت می‌گذاری. کامنتت باید مثل یک آدم واقعی باشد: "
        "کوتاه (یک یا دو جمله، حداکثر ۱۴۰ کاراکتر)، مشخصاً درباره‌ی محتوای همین پست، صمیمی و بدون کلیشه.\n"
        "ممنوع: لینک، هشتگ، منشن، تبلیغ مستقیم، «فالو کن»، «به پیج من بیا»، و کامنت‌های کلی مثل فقط «عالی». "
        "حداکثر یک ایموجی. شروع و ساختار کامنت‌ها با هم فرق کند. هرگز نگو که ربات یا AI هستی.\n"
        f"زبان: {lang}\n"
        + (f"حوزه‌ی پیج تو: {b['niche']}\n" if b.get("niche") else "")
        + (f"لحن تو: {b['tone']}\n" if b.get("tone") else "")
        + (f"جهت/موضوعی که صاحب پیج برای کامنت‌ها خواسته (دقیقاً رعایت کن):\n{brief}\n" if brief else "")
        + "فقط متن کامنت را بنویس، بدون توضیح و بدون گیومه.\n\nکامنت‌های اخیر تو (شبیهشان ننویس):\n" + avoid
    )
    user = (f"نویسنده‌ی پست: @{it.get('author', '')}\nنوع: {KIND_FA.get(it.get('kind'), 'پست')}\n"
            f"کپشن پست: {it.get('caption') or '(بدون کپشن)'}\nموضوع جست‌وجو: {it.get('topic', '')}")
    for _ in range(2):
        text = _clean_comment(ai.chat(slot, [{"role": "system", "content": system}, {"role": "user", "content": user}]))
        if text and text not in _used_texts(50):
            return text
    raise ai.AIError("AI متن مناسبی برای کامنت نساخت.")


def gen_comment(cfg, it):
    g = cfg["growth"]["comment"]
    manual = [m.strip() for m in g.get("manual_comments", []) if m.strip()]
    style = g.get("style", "ai")
    if style == "manual" or (style == "both" and manual and random.random() < 0.5):
        if not manual:
            raise ai.AIError("متن دست‌نویسی برای کامنت وارد نشده است.")
        return _pick_manual(manual).replace("{author}", it.get("author", ""))
    return ai_comment(cfg, it)


# ---------- جستجو ----------
def _set_last(action, msg):
    store.meta_set(f"growth:last:{action}", msg)


def _rotate(action, items):
    n = int(store.meta_get(f"growth:rot:{action}", "0"))
    store.meta_set(f"growth:rot:{action}", n + 1)
    return items[n % len(items)]


def _handle_failure(res, code):
    if res.get("login_required") or code == 3:
        store.meta_set("ig_expired", "1")
    if res.get("cooldown") or code == 4:
        hours = float(store.load_config(False)["growth"].get("cooldown_hours", 6))
        store.meta_set("growth:pause_until", time.time() + hours * 3600)


def scan(action, topic=None, manual=False, limit=None):
    """یک دور جست‌وجو و ذخیره‌ی نتایج. (تعداد جدید، پیام) را برمی‌گرداند."""
    cfg = store.load_config()
    g = cfg["growth"][action]
    topics = [topic.strip()] if topic and topic.strip() else [t for t in g.get("topics", []) if t.strip()]
    accounts = [] if topic else [a.lstrip("@") for a in g.get("accounts", []) if a.strip()]
    if not topics and not accounts:
        return 0, "موضوع یا پیجی برای جست‌وجو وارد نشده است."
    params = {"source": g.get("sources", "both"), "amount": 24, "hashtags": [], "accounts": [], "urls": []}
    if topics:
        t = _rotate(action, topics) if len(topics) > 1 and not topic else topics[0]
        params["hashtags"] = build_hashtags(cfg, g, t)
    if accounts:
        params["accounts"] = random.sample(accounts, min(3, len(accounts)))
    if not params["hashtags"] and not params["accounts"]:
        return 0, "هشتگی از این موضوع ساخته نشد؛ یک کلمه یا عبارت دیگر امتحان کن."
    res, code = run_worker("search", params)
    if not res.get("ok"):
        _handle_failure(res, code)
        return 0, res.get("error", "جست‌وجو ناموفق بود.")
    now = time.time()
    good = sorted((it for it in res.get("items", []) if passes(g, it, now)), key=score, reverse=True)
    errs = "؛ ".join(res.get("errors", [])[:2])
    if not good:
        return 0, f"پستی با شرایط تو پیدا نشد ({len(res.get('items', []))} پست بررسی شد؛ آستانه‌ها را کمتر کن).{(' ' + errs) if errs else ''}"
    if action == "story":
        return _store_story_candidates(g, good, manual, limit), _done_msg(len(good), errs)
    return _store_comment_candidates(cfg, g, good, manual, limit)


def _done_msg(n, errs):
    return f"{n} پست مناسب پیدا شد." + (f" (هشدار: {errs})" if errs else "")


def _store_story_candidates(g, good, manual, limit):
    new = []
    for it in good[: (limit or 20)]:
        tid = insert_target("story", it, "found")
        if tid:
            new.append(tid)
    if g.get("mode") == "auto" and not manual:
        for tid in new[: max(1, int(g.get("per_run", 1)))]:
            target_update(tid, status="queued", due=time.time() + random.randint(120, 20 * 60))
    return len(new)


def _store_comment_candidates(cfg, g, good, manual, limit):
    want = limit or max(1, int(g.get("per_scan", 2)))
    added, err = 0, ""
    for it in good:
        if added >= want:
            break
        if exists("comment", it["pk"]) or it.get("comments_disabled"):
            continue
        try:
            text = gen_comment(cfg, it)
        except ai.AIError as e:
            err = str(e)
            break
        if g.get("mode") == "auto":
            tid = insert_target("comment", it, "queued", text, due=time.time() + random.randint(60, 25 * 60))
        else:
            tid = insert_target("comment", it, "pending", text)
        added += 1 if tid else 0
    if err and not added:
        return 0, err
    return added, (f"{added} کامنت آماده شد." if added else "پست جدیدی برای کامنت نبود (همه‌شان قبلاً پردازش شده‌اند).")


def add_urls(urls, text=""):
    """پست‌هایی که خودت می‌گویی. اگر متن بدهی همان گذاشته می‌شود؛ وگرنه طبق «سبک کامنت» ساخته می‌شود."""
    cfg = store.load_config()
    g = cfg["growth"]["comment"]
    res, code = run_worker("search", {"urls": urls, "hashtags": [], "accounts": []})
    if not res.get("ok"):
        _handle_failure(res, code)
        return 0, res.get("error", "خواندن لینک‌ها ناموفق بود.")
    added, errs = 0, "؛ ".join(res.get("errors", [])[:2])
    for it in res.get("items", []):
        if exists("comment", it["pk"]):
            continue
        if it.get("comments_disabled"):
            errs += f" کامنت @{it['author']} بسته است."
            continue
        try:
            body = text.strip() or gen_comment(cfg, it)
        except ai.AIError as e:
            return added, str(e)
        direct = bool(text.strip()) or g.get("mode") == "auto"
        tid = insert_target("comment", it, "queued" if direct else "pending", body.replace("{author}", it["author"]),
                            due=time.time() + random.randint(30, 300) if direct else None, manual=1)
        added += 1 if tid else 0
    return added, f"{added} پست به صف کامنت اضافه شد." + (f" ({errs})" if errs else "")


def scan_async(action, topic=None, manual=False, limit=None, urls=None, text=""):
    with _flag_lock:
        if _scanning[action]:
            return False
        _scanning[action] = True
    _set_last(action, "در حال جست‌وجو…")

    def run():
        try:
            n, msg = add_urls(urls, text) if urls else scan(action, topic, manual, limit)
        except Exception as e:  # noqa: BLE001
            log.exception("growth scan failed")
            msg = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            _scanning[action] = False
        _set_last(action, msg)

    threading.Thread(target=run, daemon=True).start()
    return True


# ---------- صف اجرا ----------
def queue_story(tid):
    t = target_get(tid)
    if t and t["action"] == "story" and t["status"] in ("found", "failed"):
        target_update(tid, status="queued", due=time.time(), force=1, manual=1, error="")
        return True
    return False


def approve_comment(tid, text):
    t = target_get(tid)
    text = _clean_comment(text)
    if not t or t["action"] != "comment" or t["status"] not in ("pending", "failed") or not text:
        return False
    target_update(tid, status="queued", text=text, due=time.time() + random.randint(20, 180), error="")
    return True


def reject(tid):
    t = target_get(tid)
    if t and t["status"] in ("found", "pending", "queued", "failed"):
        target_update(tid, status="skipped")
        return True
    return False


def _can_run(cfg, action, t, now):
    if t["force"]:
        return True
    g = cfg["growth"][action]
    if now < float(store.meta_get(f"growth:next_ok:{action}", "0")):
        return False
    if count_done(action, _day_start(cfg)) >= int(g.get("max_per_day", 5)):
        return False
    if action == "comment":
        if count_done(action, now - 3600) >= int(g.get("max_per_hour", 3)) or not in_hours(cfg, g):
            return False
    return True


def _gap_seconds(g, action):
    return float(g.get("min_gap_seconds", 300)) if action == "comment" else float(g.get("min_gap_minutes", 30)) * 60


def _execute(t):
    action = t["action"]
    try:
        target_update(t["id"], status="running", error="")
        params = {"pk": t["media_pk"], "kind": t["kind"], "text": t["text"], "author": t["author"]}
        if action == "story":
            sg = store.load_config(False)["growth"]["story"]
            params.update(style=sg.get("style", "full"), add_link=bool(sg.get("add_link", True)))
        res, code = run_worker(action, params)
        now = time.time()
        if res.get("ok"):
            target_update(t["id"], status="done", acted_at=now, error="")
            g = store.load_config(False)["growth"][action]
            store.meta_set(f"growth:next_ok:{action}", now + _gap_seconds(g, action) * random.uniform(1.0, 1.7))
        elif res.get("login_required") or code == 3:
            _handle_failure(res, code)
            target_update(t["id"], status="failed", error=res.get("error", "نیاز به ورود دوباره")[:400])
        elif res.get("cooldown") or code == 4:
            _handle_failure(res, code)
            cd = float(store.load_config(False)["growth"].get("cooldown_hours", 6)) * 3600
            target_update(t["id"], status="queued", due=now + cd, force=0,
                          error=("متوقف تا برطرف شدن محدودیت اینستاگرام: " + res.get("error", ""))[:400])
        elif res.get("skip"):
            target_update(t["id"], status="skipped", error=res.get("error", "")[:400])
        else:
            target_update(t["id"], status="failed", error=res.get("error", "خطای نامشخص")[:400])
    except Exception as e:  # noqa: BLE001
        log.exception("growth execute failed")
        target_update(t["id"], status="failed", error=f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        _act_busy.clear()


def _run_next(cfg, now):
    if _act_busy.is_set():
        return
    for action in ACTIONS:
        with store.db() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM targets WHERE action=? AND status='queued' AND due<=? ORDER BY force DESC, due ASC LIMIT 5",
                (action, now))]
        for t in rows:
            if _can_run(cfg, action, t, now):
                _act_busy.set()
                threading.Thread(target=_execute, args=(t,), daemon=True).start()
                return


def _auto_scan(cfg, now):
    g = cfg["growth"]["story"]
    if g.get("enabled") and g.get("topics"):
        tz_now = datetime.now(store.tz_of(cfg))
        today = tz_now.strftime("%Y-%m-%d")
        for t in g.get("times", []):
            try:
                hh, mm = (int(x) for x in t.split(":"))
                slot = tz_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            except ValueError:
                continue
            key = f"gs:{today}:{t}"
            if tz_now >= slot and (tz_now - slot).total_seconds() < 4 * 3600 and store.meta_get(key) is None:
                store.meta_set(key, "1")
                scan_async("story")
    c = cfg["growth"]["comment"]
    if c.get("enabled") and (c.get("topics") or c.get("accounts")) and in_hours(cfg, c):
        last = float(store.meta_get("growth:scan_ts:comment", "0"))
        backlog = count_status("comment", "pending") + count_status("comment", "queued")
        if (now - last >= int(c.get("scan_every_minutes", 120)) * 60 and backlog < 2 * int(c.get("per_scan", 2))
                and count_done("comment", _day_start(cfg)) + backlog < int(c.get("max_per_day", 10))):
            store.meta_set("growth:scan_ts:comment", now)
            scan_async("comment")


# ---------- استوری خودکار پست‌های جدید خود پیج ----------
OWN_POLL_MINUTES = 30


def own_share_on(cfg, kind=None):
    g = cfg["growth"]["story"]
    return bool(g.get("share_own")) and (kind is None or kind in g.get("share_own_kinds", ["post", "reel"]))


def set_share_own(cfg, on):
    """روشن/خاموش کردن «هر پست جدید استوری شود». از لحظه‌ی روشن شدن حساب می‌شود (پست‌های قدیمی استوری نمی‌شوند)."""
    g = cfg["growth"]["story"]
    if on and not g.get("share_own"):
        store.meta_set("growth:own_since", time.time() - 60)
        store.meta_set("growth:own_poll_ts", 0)
    g["share_own"] = bool(on)


def queue_own(it, delay=None):
    """یک پست/ریلز خود پیج را در صف استوری می‌گذارد (اگر قبلاً استوری نشده باشد)."""
    if not it.get("pk") or exists("story", it["pk"]):
        return None
    g = store.load_config(False)["growth"]["story"]
    if delay is None:
        delay = float(g.get("share_own_delay_minutes", 3)) * 60
    it = dict(it, topic="own")
    return insert_target("story", it, "queued", due=time.time() + max(30, delay) + random.randint(0, 90), force=1)


def _own_poll(cfg, now):
    """هر نیم ساعت تازه‌ترین پست‌های خود پیج را نگاه می‌کند؛ پست/ریلز جدید (حتی اگر از خود اپ گذاشته باشی) استوری می‌شود."""
    if not own_share_on(cfg) or _act_busy.is_set():
        return
    if now - float(store.meta_get("growth:own_poll_ts", "0")) < OWN_POLL_MINUTES * 60:
        return
    store.meta_set("growth:own_poll_ts", now)
    since = float(store.meta_get("growth:own_since", "0") or 0)
    kinds = cfg["growth"]["story"].get("share_own_kinds", ["post", "reel"])

    def run():
        try:
            res, code = run_worker("mine", {"amount": 6}, timeout=300)
            if not res.get("ok"):
                _handle_failure(res, code)
                return
            for it in res.get("items", []):
                if it.get("kind") in kinds and (it.get("taken_at") or 0) >= since:
                    queue_own(it)
        except Exception:  # noqa: BLE001
            log.exception("own poll failed")
    threading.Thread(target=run, daemon=True).start()


def tick(cfg):
    """هر ۲۰ ثانیه از زمان‌بند اصلی صدا زده می‌شود."""
    if not store.session_files():
        return
    now = time.time()
    if now < float(store.meta_get("growth:pause_until", "0")):
        return
    _auto_scan(cfg, now)
    _run_next(cfg, now)
    _own_poll(cfg, now)


def paused_until():
    t = float(store.meta_get("growth:pause_until", "0"))
    return t if t > time.time() else 0


def recover_on_start():
    with store.db() as c:
        c.execute("UPDATE targets SET status='failed', error=? WHERE status='running'",
                  ("وسط کار قطع شد؛ اول خودت چک کن انجام شده یا نه.",))


def sig():
    """امضای وضعیت برای رفرش خودکار صفحه."""
    with store.db() as c:
        rows = c.execute("SELECT action, status, COUNT(*) FROM targets GROUP BY action, status ORDER BY 1, 2").fetchall()
    return ";".join(f"{a}.{s}.{n}" for a, s, n in rows) + f"|{_scanning['story']}{_scanning['comment']}|" \
        + (store.meta_get("growth:last:story", "") or "")[:30] + (store.meta_get("growth:last:comment", "") or "")[:30]
