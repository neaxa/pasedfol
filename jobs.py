"""کارهای پس‌زمینه: ساخت محتوا، انتشار، زمان‌بندی و اتوپایلوت."""
import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import content
import store

log = logging.getLogger("jobs")
BASE = os.path.dirname(os.path.abspath(__file__))
PUBLISH_SCRIPT = os.path.join(BASE, "ig_publish.py")

_gen_sem = threading.Semaphore(2)
_pub_lock = threading.Lock()
on_login_required = None  # panel آن را تنظیم می‌کند


def start_task(pid, task="full"):
    # وضعیت همین‌جا (همزمان) عوض می‌شود تا صفحه‌ی بعدی وضعیت قدیمی «ناموفق» را نشان ندهد
    store.post_update(pid, status="generating", error="", step="در صف…")

    def run():
        with _gen_sem:
            content.run_task(pid, task)
    threading.Thread(target=run, daemon=True).start()


# ---------- انتشار ----------
def _publish(pid):
    with _pub_lock:
        p = store.post_get(pid)
        if not p or p["status"] != "publishing":
            return
        try:
            media_path = content.media_for_publish(pid, p["kind"])
            if not media_path or not os.path.exists(media_path):
                raise RuntimeError("فایل رسانه‌ی این پست وجود ندارد؛ دوباره بسازش.")
            if not (p["caption"] or "").strip() and p["kind"] != "story":
                raise RuntimeError("کپشن خالی است.")
            cap_file = os.path.join(store.post_dir(pid), "caption.txt")
            with open(cap_file, "w", encoding="utf-8") as f:
                f.write(p["caption"] or "")
            proc = subprocess.run(
                [sys.executable, "-u", PUBLISH_SCRIPT, p["kind"], media_path, cap_file],
                cwd=store.DATA_DIR, env=store.child_env(), capture_output=True, text=True, timeout=900,
            )
            res = None
            for line in reversed(proc.stdout.strip().splitlines()):
                try:
                    res = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
            if res is None:
                tail = (proc.stderr or "").strip().splitlines()[-3:]
                raise RuntimeError("پاسخ نامعتبر از فرآیند انتشار. " + " | ".join(tail)[:300])
            if res.get("ok"):
                store.post_update(pid, status="published", published_at=time.time(), result=res.get("url", ""),
                                  error="", step="", ig_pk=res.get("pk", ""))
                store.meta_set("last_publish_ts", time.time())
                _share_to_story(p, res)
            else:
                store.post_update(pid, status="failed", error=res.get("error", "خطای نامشخص")[:500], step="")
                if res.get("login_required") and on_login_required:
                    on_login_required()
        except subprocess.TimeoutExpired:
            store.post_update(pid, status="failed", step="",
                              error="انتشار بیش از ۱۵ دقیقه طول کشید؛ پیج را چک کن که تکراری نشود.")
        except Exception as e:  # noqa: BLE001
            log.exception("publish failed")
            store.post_update(pid, status="failed", step="", error=str(e)[:500])


def _share_to_story(p, res):
    """«هر پست/ریلز جدید استوری هم بشود»: بلافاصله بعد از انتشار، در صف استوری قرار می‌گیرد."""
    try:
        import growth
        cfg = store.load_config(False)
        if p["kind"] in ("post", "reel") and res.get("pk") and growth.own_share_on(cfg, p["kind"]):
            growth.queue_own({
                "pk": res["pk"], "code": res.get("code", ""), "kind": p["kind"],
                "author": res.get("author") or store.meta_get("ig_username", "") or "",
                "caption": (p.get("caption") or "")[:600], "thumb": f"/media/{p['id']}/image.jpg",
                "taken_at": time.time(),
            })
    except Exception:  # noqa: BLE001
        log.exception("queue own story failed")


def publish_async(pid):
    """وضعیت را publishing می‌کند و در thread منتشر می‌کند. اگر فرآیند دیگری در حال انتشار است، False."""
    if _pub_lock.locked():
        return False
    store.post_update(pid, status="publishing", step="در حال ارسال به اینستاگرام…", error="")
    threading.Thread(target=_publish, args=(pid,), daemon=True).start()
    return True


def schedule_now(pid, force=True):
    store.post_update(pid, status="scheduled", scheduled_at=time.time(), force=1 if force else 0, error="")


# ---------- اتوپایلوت ----------
def _jitter():
    return random.randint(60, 15 * 60)


def ap_long_secs(ap, kind):
    """فیلم بلند (۲۰ یا ۳۰ ثانیه) فقط برای استوری/ریلز؛ برای فیلم کوتاه و پست ۰ برمی‌گرداند."""
    if kind == "post" or ap.get("length") != "long":
        return 0
    return 30 if int(ap.get("long_seconds") or 20) >= 30 else 20


def ap_any_long(ap):
    return any(ap_long_secs(ap, k) for k in ap.get("kinds", []))


def ap_wants_video(ap, kind):
    """ویدیوی deAPI وقتی ساخته می‌شود که گزینه‌اش روشن باشد (فقط استوری/ریلز).
    فیلم بلند همیشه ویدیوی AI است، پس خودبه‌خود روشن حساب می‌شود."""
    return kind != "post" and (bool(ap.get("video")) or ap_long_secs(ap, kind) > 0)


def ap_wants_voice(ap, kind):
    """ویس: برای ریلز (که همیشه ویدیوست) و برای استوری فقط وقتی ویدیوی AI روشن است.
    در حالت عادی استوری عکسِ ساده می‌ماند و به ویدیو تبدیل نمی‌شود."""
    if not ap.get("with_voice"):
        return False
    return kind == "reel" or (kind == "story" and ap_wants_video(ap, kind))


def create_autopilot_post(cfg):
    ap = cfg["autopilot"]
    counter = int(store.meta_get("ap_counter", "0"))
    kinds = [k for k in ap.get("kinds", []) if k in ("post", "story", "reel")] or ["post"]
    kind = kinds[counter % len(kinds)]
    topics = [t for t in ap.get("topics", []) if t.strip()]
    topic = topics[counter % len(topics)] if topics else ""
    store.meta_set("ap_counter", counter + 1)
    auto = ap.get("approve") == "auto"
    pid = store.post_create(
        kind=kind, topic=topic, status="generating", source="autopilot", want_image=1,
        out_type="video" if ap_wants_video(ap, kind) else "image", long_secs=ap_long_secs(ap, kind),
        want_voice=1 if ap_wants_voice(ap, kind) else 0, sound=1 if ap.get("sound", True) else 0,
        scheduled_at=(time.time() + _jitter()) if auto else None,
    )
    start_task(pid, "full")
    return pid


def autopilot_tick(cfg):
    ap = cfg["autopilot"]
    if not ap.get("enabled") or not store.session_files():
        return
    kinds = ap.get("kinds", [])
    need_image = any(not ap_wants_video(ap, k) for k in kinds)
    need_video = any(ap_wants_video(ap, k) for k in kinds)
    if not cfg["ai"]["caption"].get("api_key"):
        return
    if (need_image and not cfg["ai"]["image"].get("api_key")) or (need_video and not cfg["ai"]["video"].get("api_key")):
        return
    tz = store.tz_of(cfg)
    now = datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    for t in ap.get("times", []):
        try:
            hh, mm = (int(x) for x in t.split(":"))
            slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            continue
        key = f"ap:{today}:{t}"
        if now >= slot and now - slot < timedelta(hours=4) and store.meta_get(key) is None:
            store.meta_set(key, "1")
            log.info("autopilot: creating post for slot %s", t)
            create_autopilot_post(cfg)


# ---------- زمان‌بندی ----------
def _published_today(cfg):
    tz = store.tz_of(cfg)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return len(store.post_list(limit=500, where="status='published' AND published_at >= ?", args=(start,)))


def publish_tick(cfg):
    if _pub_lock.locked():
        return
    due = store.post_list(limit=20, where="status='scheduled' AND scheduled_at <= ?", args=(time.time(),))
    if not due:
        return
    p = sorted(due, key=lambda r: r["scheduled_at"])[0]
    if not p["force"]:
        gap = float(cfg["publish"].get("min_gap_minutes", 10)) * 60
        last = float(store.meta_get("last_publish_ts", "0"))
        if time.time() - last < gap:
            return
        if _published_today(cfg) >= int(cfg["publish"].get("max_per_day", 4)):
            return
    publish_async(p["id"])


def recover_on_start():
    for p in store.post_list(limit=500, status="generating"):
        store.post_update(p["id"], status="failed", step="", error="ساخت محتوا قطع شد؛ دوباره بسازش.")
    for p in store.post_list(limit=500, status="publishing"):
        store.post_update(p["id"], status="failed", step="",
                          error="وسط انتشار قطع شد. اول پیجت را چک کن؛ اگر منتشر نشده بود دوباره منتشر کن.")


def scheduler_loop():
    import growth  # اینجا import می‌شود چون growth خودش jobs را import می‌کند
    import million
    recover_on_start()
    growth.recover_on_start()
    while True:
        try:
            cfg = store.load_config()
            autopilot_tick(cfg)
            publish_tick(cfg)
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        try:  # خطای بخش رشد هیچ‌وقت نباید انتشار و اتوپایلوت را متوقف کند
            growth.tick(store.load_config())
        except Exception:  # noqa: BLE001
            log.exception("growth tick failed")
        try:
            million.tick(store.load_config())
        except Exception:  # noqa: BLE001
            log.exception("million tick failed")
        time.sleep(20)


def start_scheduler():
    threading.Thread(target=scheduler_loop, daemon=True).start()
