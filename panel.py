"""
پنل وب دستیار اینستاگرام (تک‌کاربره)
- ورود به پنل با رمز (و در صورت تنظیم PANEL_EMAIL، با ایمیل + رمز)
- اتصال به اینستاگرام (session) و انتشار پست/استوری/ریلز
- ساخت محتوا با AI: کپشن، تصویر، ویس — برای هر بخش ارائه‌دهنده و کلید جدا
- صف، زمان‌بندی و اتوپایلوت؛ ربات پاسخ دایرکت
"""
import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

from markupsafe import Markup
from flask import (Flask, abort, jsonify, redirect, render_template, request, send_from_directory,
                   session)

import ai
import content
import growth
import jobs
import media
import million
import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = store.DATA_DIR
LOG_PATH = os.path.join(DATA_DIR, "bot.log")
LOGIN_LOG_PATH = os.path.join(DATA_DIR, "login.log")
DRAFTS_PATH = os.path.join(DATA_DIR, "drafts.jsonl")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")
PANEL_EMAIL = os.getenv("PANEL_EMAIL", "").strip()

if len(PANEL_PASSWORD) < 8:
    sys.exit("PANEL_PASSWORD باید تنظیم شود و حداقل ۸ کاراکتر باشد.")

store.init_db()

STATUS_FA = {
    "generating": "در حال ساخت", "ready": "آماده", "scheduled": "زمان‌بندی‌شده",
    "publishing": "در حال انتشار", "published": "منتشر شد", "failed": "ناموفق",
}
KIND_FA = {"post": "پست", "story": "استوری", "reel": "ریلز"}
EDITABLE = ("ready", "failed")
MULTI_KEY_SLOTS = ("video", "image")  # فقط برای deAPI چند کلید پشتیبانی می‌شود


def _eq(a, b):
    return hmac.compare_digest(a.encode(), b.encode())


# ---------- ربات دایرکت ----------
bot = {"proc": None, "want": False}
bot_lock = threading.Lock()


def bot_running():
    p = bot["proc"]
    return p is not None and p.poll() is None


def start_bot():
    cfg = store.load_config()
    chat = cfg["ai"]["chat"]
    if not chat.get("api_key") and chat.get("provider") != "custom":
        return "ابتدا کلید API بخش «پاسخ دایرکت» را در تنظیمات AI وارد کن."
    if not store.session_files():
        return "ابتدا وارد اینستاگرام شو."
    with bot_lock:
        if bot_running():
            return None
        env = store.child_env()
        env["AI_CONFIG_JSON"] = json.dumps(chat)
        logf = open(LOG_PATH, "ab")
        bot["proc"] = subprocess.Popen(
            [sys.executable, "-u", os.path.join(BASE, "bot_ig.py")],
            cwd=DATA_DIR, env=env, stdout=logf, stderr=subprocess.STDOUT,
        )
        bot["want"] = True
        store.meta_set("bot_want", "1")
    return None


def stop_bot(remember=True):
    with bot_lock:
        bot["want"] = False
        if remember:
            store.meta_set("bot_want", "0")
        p = bot["proc"]
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


def monitor():
    while True:
        time.sleep(5)
        p = bot["proc"]
        if bot["want"] and p is not None and p.poll() == 3:
            bot["want"] = False  # session منقضی شده؛ باید دوباره وارد شد
            store.meta_set("ig_expired", "1")
            continue
        if bot["want"] and not bot_running():
            start_bot()


# ---------- ورود به اینستاگرام ----------
login = {"proc": None, "state": "idle", "message": ""}
login_lock = threading.Lock()


def set_login(state, message=""):
    with login_lock:
        login["state"], login["message"] = state, message


def send_to_worker(text):
    p = login["proc"]
    if not p or p.poll() is not None:
        return False
    try:
        p.stdin.write(text + "\n")
        p.stdin.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


def login_reader(proc):
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("event")
        if kind == "need":
            set_login("need_code" if ev.get("kind") == "code" else "need_password")
        elif kind == "done":
            store.meta_set("ig_username", ev.get("username", ""))
            store.meta_set("ig_expired", "0")
            set_login("done", "ورود موفق بود.")
            return
        elif kind == "error":
            set_login("error", ev.get("message", "خطای نامشخص"))
            return
    if login["state"] not in ("done", "error"):
        set_login("error", "فرآیند لاگین ناگهان بسته شد.")


def start_login(username, password, sessionid):
    cancel_login()
    stop_bot(remember=False)
    env = store.child_env()
    env["IG_USERNAME"], env["IG_PASSWORD"], env["IG_SESSIONID"] = username, password, sessionid
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(BASE, "ig_login.py")],
        cwd=DATA_DIR, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=open(LOGIN_LOG_PATH, "wb"), text=True, bufsize=1,
    )
    login["proc"] = proc
    set_login("starting")
    threading.Thread(target=login_reader, args=(proc,), daemon=True).start()


def cancel_login():
    p = login["proc"]
    if p and p.poll() is None:
        p.terminate()
    login["proc"] = None
    set_login("idle")


jobs.on_login_required = lambda: store.meta_set("ig_expired", "1")

# ---------- وب ----------
app = Flask(__name__, template_folder=BASE)
app.config["MAX_CONTENT_LENGTH"] = 150 * 1024 * 1024  # ویدیوی آپلودی


def _fmt_num(v):
    v = int(v or 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    return f"{v / 1000:.1f}K" if v >= 1000 else str(v)


def _fmt_ago(ts):
    if not ts:
        return ""
    h = (time.time() - ts) / 3600
    return f"{int(h * 60)} دقیقه پیش" if h < 1 else (f"{int(h)} ساعت پیش" if h < 48 else f"{int(h / 24)} روز پیش")


app.jinja_env.filters["num"] = _fmt_num
app.jinja_env.filters["ago"] = _fmt_ago
if os.getenv("BEHIND_PROXY", "0") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def _secret_key():
    path = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.secret_key = _secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)
fails = {}


def authed():
    return session.get("ok") is True


def guard():
    if not authed():
        return redirect("/login")
    if request.method == "POST" and not _eq(request.form.get("csrf", ""), session.get("csrf", "")):
        abort(400)
    return None


def flash(msg, ok=True):
    session["flash"] = {"msg": msg, "ok": ok}


def snapshot():
    return {
        "login": login["state"], "bot": bot_running(),
        "posts": {str(p["id"]): p["status"] for p in store.post_list(limit=300)},
        "growth": growth.sig(), "million": million.sig(),
    }


def signature(snap, watch):
    parts = []
    for w in watch:
        if w == "login":
            parts.append(snap["login"])
        elif w == "bot":
            parts.append(str(snap["bot"]))
        elif w == "posts":
            parts.append(",".join(f"{k}:{v}" for k, v in sorted(snap["posts"].items())))
        elif w == "growth":
            parts.append(snap["growth"])
        elif w == "million":
            parts.append(snap["million"])
        elif w.startswith("post:"):
            parts.append(snap["posts"].get(w[5:], "gone"))
    return "|".join(parts)


def page(template, watch=(), **ctx):
    watch = list(watch)
    return render_template(template, watch=watch, sig=signature(snapshot(), watch), **ctx)


@app.context_processor
def inject():
    cfg = store.load_config(False)
    return {
        "csrf": session.get("csrf", ""), "flash": session.pop("flash", None), "panel_email": PANEL_EMAIL,
        "STATUS_FA": STATUS_FA, "KIND_FA": KIND_FA, "tz_name": cfg.get("timezone"),
        "ts": lambda v: (Markup('<bdo dir="ltr">') + store.fmt_ts(v) + Markup("</bdo>")) if v else "", "ig_expired": store.meta_get("ig_expired") == "1",
        "ig_connected": bool(store.session_files()), "nav_path": request.path,
    }


@app.errorhandler(413)
def too_large(_e):
    flash("حجم فایل بیشتر از ۱۵۰ مگابایت است.", False)
    return redirect(request.referrer or "/")


@app.get("/healthz")
def healthz():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        ip = request.remote_addr or "?"
        now = time.time()
        recent = [t for t in fails.get(ip, []) if now - t < 300]
        fails[ip] = recent
        if len(recent) >= 5:
            return render_template("login.html", error="تلاش بیش از حد. چند دقیقه صبر کن."), 429
        email_ok = (not PANEL_EMAIL) or _eq(request.form.get("email", "").strip().lower(), PANEL_EMAIL.lower())
        pass_ok = _eq(request.form.get("password", ""), PANEL_PASSWORD)
        if email_ok and pass_ok:
            session.clear()
            session["ok"] = True
            session["csrf"] = secrets.token_hex(16)
            session.permanent = True
            return redirect("/")
        fails[ip].append(now)
        time.sleep(1)
        return render_template("login.html", error="ایمیل یا رمز اشتباه است."), 401
    return render_template("login.html", error=None)


@app.post("/logout")
def logout():
    r = guard()
    if r:
        return r
    session.clear()
    return redirect("/login")


@app.get("/status")
def status():
    if not authed():
        abort(401)
    watch = [w for w in request.args.get("watch", "").split(",") if w]
    return jsonify(sig=signature(snapshot(), watch))


# ---------- داشبورد ----------
@app.get("/")
def index():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    slots = {s: bool(cfg["ai"][s]["api_key"]) or cfg["ai"][s]["provider"] == "custom" for s in store.SLOTS}
    upcoming = sorted(store.post_list(limit=50, status="scheduled"), key=lambda p: p["scheduled_at"] or 0)[:6]
    review = store.post_list(limit=20, where="status='ready' AND source='autopilot'")
    return page(
        "dashboard.html", watch=("login", "bot", "posts"), cfg=cfg, slots=slots, slot_labels=ai.SLOT_LABELS,
        upcoming=upcoming, review=review, recent=store.post_list(limit=6),
        username=store.meta_get("ig_username", ""), bot_on=bot_running(),
    )


# ---------- استودیو ----------
def _missing_keys(cfg, need):
    out = []
    for s in need:
        slot = cfg["ai"][s]
        if not slot["api_key"] and slot["provider"] != "custom":
            out.append(ai.SLOT_LABELS[s])
    return out


@app.get("/studio")
def studio():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    return page("studio.html", cfg=cfg, brand=cfg["brand"])


@app.post("/studio")
def studio_create():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    f = request.form
    kind = f.get("kind", "post")
    if kind not in KIND_FA:
        kind = "post"
    topic = f.get("topic", "").strip()[:600]
    caption = f.get("caption", "").strip()[:2200]
    want_voice = bool(f.get("want_voice")) and kind != "post"
    # تیک «عکس» و «ویدیو»: هرکدام روشن باشد همان ساخته می‌شود (پست فقط عکس دارد)
    make_image = "make_image" in f or kind == "post"
    make_video = "make_video" in f and kind != "post"
    if not (make_image or make_video):
        flash("حداقل یکی از «عکس» یا «ویدیو» را تیک بزن.", False)
        return redirect("/studio")
    image_mode = "upload" if f.get("image_mode") == "upload" else "ai"
    video_source = "upload" if f.get("video_source") == "upload" else "ai"
    img_up, vid_up, vimg_up = request.files.get("image"), request.files.get("video_file"), request.files.get("video_image")
    sound = kind != "post" and "sound" in f
    music_up = request.files.get("music")
    music_data = music_up.read() if (sound and music_up and music_up.filename) else None
    want_voice = want_voice and sound  # ویدیوی بی‌صدا ویس هم ندارد
    mode = f.get("publish_mode", "draft")
    sched = None
    if mode == "schedule":
        sched = store.parse_local(f.get("scheduled_at", ""), cfg)
        if not sched:
            flash("زمان انتشار را درست وارد کن.", False)
            return redirect("/studio")

    img_data = vid_data = vimg_data = None
    try:
        if make_image and image_mode == "upload":
            if not (img_up and img_up.filename):
                flash("تیک «عکس» روشن است ولی عکسی انتخاب نشده است.", False)
                return redirect("/studio")
            img_data = img_up.read()
            media.open_image(img_data)
        if make_video and video_source == "upload":
            if not (vid_up and vid_up.filename):
                flash("تیک «ویدیو» روشن است ولی ویدیویی انتخاب نشده است.", False)
                return redirect("/studio")
            vid_data = vid_up.read()
        if make_video and video_source == "ai" and vimg_up and vimg_up.filename:
            vimg_data = vimg_up.read()
            media.open_image(vimg_data)
    except media.MediaError as e:
        flash(str(e), False)
        return redirect("/studio")

    ai_image = make_image and image_mode == "ai"
    ai_video = make_video and video_source == "ai"
    if (ai_image or ai_video) and not (topic or caption) and not (ai_video and vimg_data):
        flash("موضوع یا کپشن را بنویس تا تصویر/ویدیو و متن ساخته شود.", False)
        return redirect("/studio")
    need = []
    if not caption or want_voice or ai_video:
        need.append("caption")
    if ai_video:
        need.append("video")
    if ai_image:
        need.append("image")
    if want_voice:
        need.append("voice")
    missing = _missing_keys(cfg, need)
    if missing:
        flash("اول کلید API این بخش‌ها را در «تنظیمات AI» وارد کن: " + "، ".join(missing), False)
        return redirect("/ai")

    def new_post(out_type, user_image, want_image):
        return store.post_create(
            kind=kind, topic=topic, caption=caption, status="generating", source="manual",
            want_image=1 if want_image else 0, want_voice=1 if want_voice else 0, user_image=1 if user_image else 0,
            out_type=out_type, scheduled_at=time.time() if mode == "now" else sched, force=1 if mode == "now" else 0,
            sound=1 if sound else 0,
        )

    pids = []
    try:
        if make_image:  # خروجی عکس
            pid = new_post("image", bool(img_data), image_mode == "ai")
            pids.append(pid)
            if img_data:
                media.save_final_image(img_data, kind, content.paths(pid)["image"])
        if make_video:  # خروجی ویدیو: یا deAPI، یا ویدیوی خود کاربر
            pid = new_post("video", bool(vimg_data), video_source == "ai")
            pids.append(pid)
            if vid_data:
                content.save_user_video(pid, vid_data)
            elif vimg_data:  # عکس برای زنده شدن با deAPI
                media.save_final_image(vimg_data, kind, content.paths(pid)["image"])
        if music_data:
            for pid in pids:
                content.save_music(pid, music_data)
    except media.MediaError as e:
        for pid in pids:
            store.post_delete(pid)
        flash(str(e), False)
        return redirect("/studio")
    for pid in pids:
        jobs.start_task(pid, "full")
    flash("ساخت محتوا شروع شد." if len(pids) == 1 else f"ساخت {len(pids)} مورد (عکس و ویدیو) شروع شد.")
    return redirect(f"/post/{pids[0]}" if len(pids) == 1 else "/posts")


# ---------- پست‌ها ----------
@app.get("/posts")
def posts():
    r = guard()
    if r:
        return r
    st = request.args.get("status", "")
    rows = store.post_list(limit=80, status=st if st in STATUS_FA else None)
    return page("posts.html", watch=("posts",), rows=rows, st=st)


def media_info(pid):
    pa = content.paths(pid)
    info = {"image": None, "video": None, "audio": None, "music": None}
    for k, path in (("image", pa["image"]), ("video", pa["video"]), ("audio", content.audio_path(pid)),
                    ("music", content.music_path(pid))):
        if path and os.path.exists(path):
            info[k] = {"name": os.path.basename(path), "v": int(os.path.getmtime(path))}
    return info


@app.get("/post/<int:pid>")
def post_page(pid):
    r = guard()
    if r:
        return r
    p = store.post_get(pid)
    if not p:
        abort(404)
    cfg = store.load_config(False)
    return page(
        "post.html", watch=(f"post:{pid}",), p=p, m=media_info(pid), editable=p["status"] in EDITABLE,
        sched_value=store.local_input_value(p["scheduled_at"], cfg),
    )


@app.get("/media/<int:pid>/<name>")
def media_file(pid, name):
    if not authed():
        abort(401)
    if name not in ("image.jpg", "video.mp4", "audio.mp3", "audio.wav", "music.audio"):
        abort(404)
    return send_from_directory(store.post_dir(pid), name, conditional=True, max_age=0)


def _load_editable(pid):
    p = store.post_get(pid)
    if not p:
        abort(404)
    return p


def _apply_form(pid):
    fields = {}
    for k, lim in (("caption", 2200), ("voice_text", 1500), ("image_prompt", 1500)):
        if k in request.form:
            fields[k] = request.form[k].strip()[:lim]
    store.post_update(pid, **fields)
    mup = request.files.get("music")
    music_new = False
    if mup and mup.filename and store.post_get(pid)["kind"] != "post":
        content.save_music(pid, mup.read())  # MediaError اگر معتبر نباشد
        store.post_update(pid, sound=1)
        music_new = True
    vup = request.files.get("video_file")
    if vup and vup.filename and store.post_get(pid)["out_type"] == "video":
        content.save_user_video(pid, vup.read())  # MediaError اگر معتبر نباشد
        return True
    up = request.files.get("image")
    if up and up.filename:
        data = up.read()
        media.open_image(data)  # MediaError اگر معتبر نباشد
        p = store.post_get(pid)
        media.save_final_image(data, p["kind"], content.paths(pid)["image"])
        store.post_update(pid, user_image=1, want_image=0)
        return True
    return music_new


@app.post("/post/<int:pid>/save")
def post_save(pid):
    r = guard()
    if r:
        return r
    p = _load_editable(pid)
    if p["status"] not in EDITABLE:
        flash("در این وضعیت قابل ویرایش نیست.", False)
        return redirect(f"/post/{pid}")
    try:
        replaced = _apply_form(pid)
    except media.MediaError as e:
        flash(str(e), False)
        return redirect(f"/post/{pid}")
    if replaced and p["kind"] != "post":
        jobs.start_task(pid, "media")
        flash("ذخیره شد؛ ویدیو با تصویر جدید دوباره ساخته می‌شود.")
    else:
        flash("ذخیره شد.")
    return redirect(f"/post/{pid}")


@app.post("/post/<int:pid>/regen/<task>")
def post_regen(pid, task):
    r = guard()
    if r:
        return r
    p = _load_editable(pid)
    if task not in ("text", "image", "video", "media") or p["status"] not in EDITABLE:
        abort(400)
    is_video = p["out_type"] == "video" and p["kind"] != "post"
    if task == "image" and is_video:
        task = "video"
    if task == "video" and not is_video:
        task = "image"
    cfg = store.load_config()
    voice_needed = ["voice"] if (p["want_voice"] and p["kind"] != "post") else []
    need = {"text": ["caption"], "image": ["image"], "video": ["video"], "media": voice_needed}[task]
    if task in ("image", "video"):
        need = (voice_needed if p["user_video"] else need + voice_needed)  # ویدیوی خود کاربر به deAPI نیاز ندارد
    missing = _missing_keys(cfg, need)
    if missing:
        flash("اول کلید API این بخش را در «تنظیمات AI» وارد کن: " + "، ".join(missing), False)
        return redirect("/ai")
    try:
        _apply_form(pid)
    except media.MediaError as e:
        flash(str(e), False)
        return redirect(f"/post/{pid}")
    if task in ("image", "video"):
        store.post_update(pid, want_image=1, user_image=0 if task == "video" and request.form.get("fresh") else p["user_image"])
    jobs.start_task(pid, task)
    return redirect(f"/post/{pid}")


@app.post("/post/<int:pid>/voice-toggle")
def post_voice_toggle(pid):
    r = guard()
    if r:
        return r
    p = _load_editable(pid)
    if p["status"] not in EDITABLE or p["kind"] == "post":
        abort(400)
    store.post_update(pid, want_voice=0 if p["want_voice"] else 1)
    return redirect(f"/post/{pid}")


@app.post("/post/<int:pid>/sound-toggle")
def post_sound_toggle(pid):
    r = guard()
    if r:
        return r
    p = _load_editable(pid)
    if p["status"] not in EDITABLE or p["kind"] == "post":
        abort(400)
    store.post_update(pid, sound=0 if p.get("sound", 1) else 1)
    if _has_source(pid):
        jobs.start_task(pid, "media")
    flash("صدای ویدیو " + ("خاموش شد؛ ویدیو بی‌صدا دوباره ساخته می‌شود." if p.get("sound", 1) else "روشن شد؛ ویدیو با صدا دوباره ساخته می‌شود."))
    return redirect(f"/post/{pid}")


@app.post("/post/<int:pid>/music-remove")
def post_music_remove(pid):
    r = guard()
    if r:
        return r
    p = _load_editable(pid)
    if p["status"] not in EDITABLE:
        abort(400)
    try:
        os.remove(content.paths(pid)["music"])
    except OSError:
        pass
    if _has_source(pid):
        jobs.start_task(pid, "media")
    flash("موزیک حذف شد.")
    return redirect(f"/post/{pid}")


def _has_source(pid):
    pa = content.paths(pid)
    p = store.post_get(pid)
    return os.path.exists(pa["clip"]) if p["out_type"] == "video" else os.path.exists(pa["image"])


def _publishable(p):
    if not store.session_files():
        return "اول در بخش «اتصال اینستاگرام» وارد شو."
    if not content.media_for_publish(p["id"], p["kind"]) or not os.path.exists(content.media_for_publish(p["id"], p["kind"])):
        return "فایل رسانه‌ی این پست آماده نیست."
    if p["kind"] != "story" and not (p["caption"] or "").strip():
        return "کپشن خالی است."
    return None


@app.post("/post/<int:pid>/publish")
def post_publish(pid):
    r = guard()
    if r:
        return r
    p = _load_editable(pid)
    if p["status"] not in EDITABLE:
        abort(400)
    try:
        _apply_form(pid)
    except media.MediaError as e:
        flash(str(e), False)
        return redirect(f"/post/{pid}")
    err = _publishable(store.post_get(pid))
    if err:
        flash(err, False)
        return redirect(f"/post/{pid}")
    jobs.schedule_now(pid, force=True)
    flash("در صف انتشار قرار گرفت؛ تا چند ثانیه‌ی دیگر ارسال می‌شود.")
    return redirect(f"/post/{pid}")


@app.post("/post/<int:pid>/schedule")
def post_schedule(pid):
    r = guard()
    if r:
        return r
    p = _load_editable(pid)
    if p["status"] not in EDITABLE:
        abort(400)
    try:
        _apply_form(pid)
    except media.MediaError as e:
        flash(str(e), False)
        return redirect(f"/post/{pid}")
    ts = store.parse_local(request.form.get("scheduled_at", ""), store.load_config(False))
    err = _publishable(store.post_get(pid)) if ts else "زمان انتشار را درست وارد کن."
    if err:
        flash(err, False)
        return redirect(f"/post/{pid}")
    store.post_update(pid, status="scheduled", scheduled_at=ts, force=0, error="")
    flash("زمان‌بندی شد.")
    return redirect(f"/post/{pid}")


@app.post("/post/<int:pid>/unschedule")
def post_unschedule(pid):
    r = guard()
    if r:
        return r
    p = _load_editable(pid)
    if p["status"] == "scheduled":
        store.post_update(pid, status="ready", scheduled_at=None, force=0)
        flash("زمان‌بندی لغو شد.")
    return redirect(f"/post/{pid}")


@app.post("/post/<int:pid>/delete")
def post_delete(pid):
    r = guard()
    if r:
        return r
    p = _load_editable(pid)
    if p["status"] in ("publishing", "generating"):
        flash("تا پایان کار فعلی نمی‌شود حذف کرد.", False)
        return redirect(f"/post/{pid}")
    store.post_delete(pid)
    shutil.rmtree(os.path.join(store.MEDIA_DIR, str(pid)), ignore_errors=True)
    flash("حذف شد.")
    return redirect("/posts")


# ---------- تنظیمات AI ----------
@app.get("/ai")
def ai_page():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    kind_of = {"voice": "voice", "image": "image", "video": "video"}
    model_defaults = {
        s: {p: ai.PROVIDERS[p].get(kind_of.get(s, "chat"), "") for p in ai.SLOT_PROVIDERS[s]} for s in store.SLOTS
    }
    # کلیدها هرگز کامل به صفحه نمی‌روند؛ فقط چهار نویسه‌ی آخر + وضعیت اخیر (سالم/مشکل‌دار)
    key_rows = {
        s: [{"tail": ai.mask_key(k), "problem": ai.key_problem(k)} for k in ai.keys_of(cfg["ai"][s])]
        for s in store.SLOTS
    }
    return page(
        "ai.html", cfg=cfg, providers=ai.PROVIDERS, slot_providers=ai.SLOT_PROVIDERS, slot_labels=ai.SLOT_LABELS,
        slots=store.SLOTS, model_defaults=model_defaults, key_rows=key_rows, multi_slots=MULTI_KEY_SLOTS,
    )


@app.post("/ai/save/<slot>")
def ai_save(slot):
    r = guard()
    if r:
        return r
    if slot not in store.SLOTS:
        abort(404)
    cfg = store.load_config()
    s = cfg["ai"][slot]
    provider = request.form.get("provider", "")
    if provider not in ai.SLOT_PROVIDERS[slot]:
        abort(400)
    s["provider"] = provider
    key = request.form.get("api_key", "").strip()
    if key:
        new = ai.split_keys(key)
        if slot in MULTI_KEY_SLOTS and provider == "deapi":
            if request.form.get("key_mode") == "add":  # به کلیدهای قبلی اضافه شود
                new = ai.split_keys("\n".join(ai.keys_of(s) + new))
        else:
            new = new[:1]  # این شرکت فقط یک کلید می‌گیرد
        s["api_key"] = "\n".join(new)
        key = s["api_key"]
    s["model"] = request.form.get("model", "").strip()[:120]
    s["base_url"] = request.form.get("base_url", "").strip()[:300]
    s["voice"] = request.form.get("voice", "").strip()[:120]
    if slot == "video":
        try:
            s["seconds"] = max(2, min(10, int(request.form.get("seconds", 5))))
        except ValueError:
            s["seconds"] = 5
    note = ""
    if key:  # همین کلید را برای بخش‌های هم‌شرکت که کلید ندارند هم بگذار
        for other in store.SLOTS:
            o = cfg["ai"][other]
            if other != slot and o["provider"] == provider and not o["api_key"] and provider not in ("custom",):
                o["api_key"] = key
                note += f" «{ai.SLOT_LABELS[other]}»"
    store.save_config(cfg)
    flash("ذخیره شد." + (f" همین کلید برای{note} هم گذاشته شد (چون همین شرکت را انتخاب کرده بودند)." if note else ""))
    if slot == "chat" and bot_running():
        stop_bot(remember=False)
        start_bot()
    return redirect("/ai#" + slot)


@app.post("/ai/key-remove/<slot>")
def ai_key_remove(slot):
    r = guard()
    if r:
        return r
    if slot not in MULTI_KEY_SLOTS:
        abort(404)
    cfg = store.load_config()
    keys = ai.keys_of(cfg["ai"][slot])
    try:
        i = int(request.form.get("idx", -1))
    except ValueError:
        i = -1
    if 0 <= i < len(keys):
        keys.pop(i)
        cfg["ai"][slot]["api_key"] = "\n".join(keys)
        store.save_config(cfg)
        flash("کلید حذف شد.")
    return redirect("/ai#" + slot)


@app.post("/ai/test/<slot>")
def ai_test(slot):
    r = guard()
    if r:
        return r
    if slot not in store.SLOTS:
        abort(404)
    ok, msg = ai.test_slot(slot, store.load_config()["ai"][slot])
    flash(f"{ai.SLOT_LABELS[slot]}: {msg}", ok)
    return redirect("/ai#" + slot)


@app.post("/ai/brand")
def ai_brand():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    cfg["brand"] = {k: request.form.get(k, "").strip()[:1500] for k in ("niche", "tone", "extra")}
    cfg["language"] = "en" if request.form.get("language") == "en" else "fa"
    tz = request.form.get("timezone", "").strip() or "Asia/Tehran"
    try:
        ZoneInfo(tz)
        cfg["timezone"] = tz
    except Exception:  # noqa: BLE001
        flash("منطقه‌ی زمانی نامعتبر است (مثال: Asia/Tehran).", False)
        return redirect("/ai#brand")
    store.save_config(cfg)
    flash("اطلاعات پیج ذخیره شد.")
    return redirect("/ai#brand")


# ---------- اتوپایلوت ----------
def _parse_times(text):
    out = []
    for tok in text.replace(",", " ").replace("،", " ").split():
        try:
            h, m = (int(x) for x in tok.split(":"))
        except ValueError:
            continue
        if 0 <= h < 24 and 0 <= m < 60 and f"{h:02d}:{m:02d}" not in out:
            out.append(f"{h:02d}:{m:02d}")
    return sorted(out)[:8]


@app.get("/autopilot")
def autopilot_page():
    r = guard()
    if r:
        return r
    cfg = store.load_config(False)
    return page("autopilot.html", cfg=cfg, ap=cfg["autopilot"], pub=cfg["publish"])


@app.post("/autopilot")
def autopilot_save():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    f = request.form
    ap = cfg["autopilot"]
    ap["enabled"] = "enabled" in f
    ap["topics"] = [t.strip() for t in f.get("topics", "").splitlines() if t.strip()][:200]
    ap["times"] = _parse_times(f.get("times", "")) or ["10:00"]
    ap["kinds"] = [k for k in ("post", "story", "reel") if k in f.getlist("kinds")] or ["post"]
    ap["approve"] = "auto" if f.get("approve") == "auto" else "review"
    ap["with_voice"] = "with_voice" in f
    ap["video"] = "video" in f
    ap["sound"] = "sound" in f
    ap["length"] = "long" if f.get("length") == "long" else "short"
    ap["long_seconds"] = 30 if f.get("long_seconds") == "30" else 20
    try:
        cfg["publish"]["min_gap_minutes"] = max(1, min(720, int(f.get("min_gap", 10))))
        cfg["publish"]["max_per_day"] = max(1, min(20, int(f.get("max_per_day", 4))))
    except ValueError:
        pass
    store.save_config(cfg)
    msg = "تنظیمات اتوپایلوت ذخیره شد."
    if ap["enabled"] and not store.session_files():
        msg += " (اتوپایلوت وقتی کار می‌کند که به اینستاگرام وصل باشی.)"
    flash(msg)
    return redirect("/autopilot")


@app.post("/autopilot/run-now")
def autopilot_run_now():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    ap = cfg["autopilot"]
    kinds = ap.get("kinds", [])
    need = ["caption"]
    if any(not jobs.ap_wants_video(ap, k) for k in kinds):
        need.append("image")
    if any(jobs.ap_wants_video(ap, k) for k in kinds):
        need.append("video")
    if any(jobs.ap_wants_voice(ap, k) for k in kinds):
        need.append("voice")
    missing = _missing_keys(cfg, need)
    if missing:
        flash("اول کلید API این بخش‌ها را وارد کن: " + "، ".join(missing), False)
        return redirect("/ai")
    pid = jobs.create_autopilot_post(cfg)
    flash("یک پست آزمایشی با تنظیمات اتوپایلوت در حال ساخت است.")
    return redirect(f"/post/{pid}")


# ---------- اینستاگرام ----------
@app.get("/instagram")
def instagram_page():
    r = guard()
    if r:
        return r
    return page(
        "instagram.html", watch=("login",), login=dict(login), has_session=bool(store.session_files()),
        username=store.meta_get("ig_username", ""), proxy_set=bool(os.getenv("IG_PROXY")),
        log=_tail(LOGIN_LOG_PATH, 15) if os.path.exists(LOGIN_LOG_PATH) else "",
    )


@app.post("/ig/login")
def ig_login():
    r = guard()
    if r:
        return r
    username = request.form.get("username", "").strip().lstrip("@")
    password = request.form.get("password", "")
    sessionid = request.form.get("sessionid", "").strip()
    if not sessionid and (not username or not password):
        flash("نام کاربری و رمز (یا sessionid) را وارد کن.", False)
        return redirect("/instagram")
    start_login(username, password, sessionid)
    return redirect("/instagram")


@app.post("/ig/code")
def ig_code():
    r = guard()
    if r:
        return r
    value = request.form.get("value", "").strip()
    if login["state"] in ("need_code", "need_password") and value and send_to_worker(value):
        set_login("verifying")
    else:
        flash("ارسال نشد؛ دوباره از اول شروع کن.", False)
    return redirect("/instagram")


@app.post("/ig/cancel")
def ig_cancel():
    r = guard()
    if r:
        return r
    cancel_login()
    return redirect("/instagram")


@app.post("/ig/reset")
def ig_reset():
    r = guard()
    if r:
        return r
    stop_bot()
    cancel_login()
    for f in store.session_files():
        try:
            os.remove(f)
        except OSError:
            pass
    store.meta_set("ig_username", "")
    flash("Session حذف شد.")
    return redirect("/instagram")


# ---------- کشف و استوری ----------
def _int(form, key, default, lo, hi):
    try:
        return max(lo, min(hi, int(float(form.get(key, default)))))
    except (TypeError, ValueError):
        return default


def _lines(text, limit=200):
    return [x.strip() for x in text.replace("،", "\n").replace(",", "\n").splitlines() if x.strip()][:limit]


def _growth_common(g, f):
    """فیلدهای مشترک فرم دو بخش (آستانه‌ها، نوع محتوا، منبع، مسدودها)."""
    g["enabled"] = "enabled" in f
    g["mode"] = "auto" if f.get("mode") == "auto" else "review"
    g["topics"] = _lines(f.get("topics", ""))
    g["sources"] = f.get("sources") if f.get("sources") in ("top", "recent", "both") else "both"
    g["types"] = [k for k in ("reel", "post") if k in f.getlist("types")] or ["reel", "post"]
    g["min_views"] = _int(f, "min_views", 0, 0, 100_000_000)
    g["min_likes"] = _int(f, "min_likes", 0, 0, 100_000_000)
    g["min_comments"] = _int(f, "min_comments", 0, 0, 100_000_000)
    g["max_age_days"] = _int(f, "max_age_days", 30, 0, 365)
    g["ai_expand"] = "ai_expand" in f
    g["blocklist"] = [x.lstrip("@") for x in _lines(f.get("blocklist", ""))]


def _growth_state(action):
    paused = growth.paused_until()
    return {
        "scanning": growth._scanning[action], "last": store.meta_get(f"growth:last:{action}", ""),
        "paused": store.fmt_ts(paused) if paused else "", "ig_ok": bool(store.session_files()),
    }


@app.get("/discover")
def discover_page():
    r = guard()
    if r:
        return r
    cfg = store.load_config(False)
    return page(
        "discover.html", watch=("growth",), cfg=cfg, g=cfg["growth"]["story"], st=_growth_state("story"),
        rows=growth.target_list("story", limit=60), G_STATUS=growth.STATUS_FA, G_KIND=growth.KIND_FA,
    )


@app.post("/discover/save")
def discover_save():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    f = request.form
    g = cfg["growth"]["story"]
    _growth_common(g, f)
    g["per_run"] = _int(f, "per_run", 1, 1, 5)
    g["times"] = _parse_times(f.get("times", "")) or ["13:00"]
    g["max_per_day"] = _int(f, "max_per_day", 3, 1, 10)
    g["min_gap_minutes"] = _int(f, "min_gap_minutes", 30, 5, 720)
    _story_look(cfg, f)
    store.save_config(cfg)
    flash("تنظیمات ذخیره شد." + (" (برای کار خودکار باید به اینستاگرام وصل باشی.)" if g["enabled"] and not store.session_files() else ""))
    return redirect("/discover")


def _story_look(cfg, f):
    """شکل استوری و «استوری خودکار پست‌های جدید»."""
    g = cfg["growth"]["story"]
    g["style"] = "card" if f.get("style") == "card" else "full"
    g["add_link"] = "add_link" in f
    growth.set_share_own(cfg, "share_own" in f)
    g["share_own_kinds"] = [k for k in ("post", "reel") if k in f.getlist("share_own_kinds")] or ["post", "reel"]
    g["share_own_delay_minutes"] = _int(f, "share_own_delay_minutes", 3, 0, 240)


@app.post("/discover/look")
def discover_look():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    _story_look(cfg, request.form)
    store.save_config(cfg)
    g = cfg["growth"]["story"]
    flash(f"ذخیره شد: استوری‌ها به شکل «{growth.STYLE_FA[g['style']]}» گذاشته می‌شوند"
          + ("؛ هر پست/ریلز جدید پیجت هم خودکار استوری می‌شود." if g["share_own"] else "."))
    return redirect("/discover#look")


@app.post("/discover/search")
def discover_search():
    r = guard()
    if r:
        return r
    topic = request.form.get("topic", "").strip()[:100]
    if not topic:
        flash("موضوع را بنویس.", False)
    elif not store.session_files():
        flash("اول در بخش «اتصال اینستاگرام» وارد شو.", False)
    elif not growth.scan_async("story", topic=topic, manual=True):
        flash("یک جست‌وجو همین الان در حال انجام است؛ چند لحظه صبر کن.", False)
    else:
        flash("جست‌وجو شروع شد؛ نتیجه تا چند ثانیه‌ی دیگر همین‌جا می‌آید.")
    return redirect("/discover")


@app.post("/discover/<int:tid>/story")
def discover_story(tid):
    r = guard()
    if r:
        return r
    ok = growth.queue_story(tid)
    flash("در صف استوری قرار گرفت؛ تا چند ثانیه‌ی دیگر گذاشته می‌شود." if ok else "این مورد قابل ارسال نیست.", ok)
    return redirect("/discover")


@app.post("/discover/<int:tid>/skip")
def discover_skip(tid):
    r = guard()
    if r:
        return r
    growth.reject(tid)
    return redirect("/discover")


# ---------- کامنت خودکار ----------
@app.get("/comments")
def comments_page():
    r = guard()
    if r:
        return r
    cfg = store.load_config(False)
    return page(
        "comments.html", watch=("growth",), cfg=cfg, g=cfg["growth"]["comment"], st=_growth_state("comment"),
        rows=growth.target_list("comment", limit=60), G_STATUS=growth.STATUS_FA, G_KIND=growth.KIND_FA,
        today_done=growth.count_done("comment", growth._day_start(cfg)),
    )


@app.post("/comments/save")
def comments_save():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    f = request.form
    g = cfg["growth"]["comment"]
    _growth_common(g, f)
    g["accounts"] = [x.lstrip("@") for x in _lines(f.get("accounts", ""), 50)]
    g["style"] = f.get("style") if f.get("style") in ("ai", "manual", "both") else "ai"
    g["ai_brief"] = f.get("ai_brief", "").strip()[:1500]
    g["manual_comments"] = [x.strip()[:300] for x in f.get("manual_comments", "").splitlines() if x.strip()][:100]
    g["per_scan"] = _int(f, "per_scan", 2, 1, 5)
    g["scan_every_minutes"] = _int(f, "scan_every_minutes", 120, 30, 1440)
    g["max_per_day"] = _int(f, "max_per_day", 10, 1, 40)       # سقف سخت برای محافظت از حساب
    g["max_per_hour"] = _int(f, "max_per_hour", 3, 1, 10)
    g["min_gap_seconds"] = _int(f, "min_gap_seconds", 300, 60, 7200)
    g["hours_from"] = _int(f, "hours_from", 9, 0, 23)
    g["hours_to"] = _int(f, "hours_to", 23, 1, 24)
    store.save_config(cfg)
    msg = "تنظیمات ذخیره شد."
    if g["style"] in ("ai", "both") and not growth.ai_slot(cfg):
        msg += " (برای کامنت AI باید کلید «نوشتن کپشن» را در تنظیمات AI وارد کنی.)"
    if g["style"] in ("manual", "both") and len(g["manual_comments"]) < 5:
        msg += " (پیشنهاد: حداقل ۵ تا ۱۰ کامنت دست‌نویس مختلف بنویس؛ کامنت تکراری اسپم حساب می‌شود.)"
    flash(msg)
    return redirect("/comments")


@app.post("/comments/scan")
def comments_scan():
    r = guard()
    if r:
        return r
    cfg = store.load_config(False)
    g = cfg["growth"]["comment"]
    if not store.session_files():
        flash("اول در بخش «اتصال اینستاگرام» وارد شو.", False)
    elif not (g["topics"] or g["accounts"]):
        flash("اول موضوع یا پیج‌ها را در تنظیمات بنویس و ذخیره کن.", False)
    elif g["style"] in ("ai", "both") and not growth.ai_slot(cfg):
        flash("برای کامنت AI اول کلید API را در «تنظیمات AI» وارد کن.", False)
    elif not growth.scan_async("comment", manual=True):
        flash("یک جست‌وجو همین الان در حال انجام است.", False)
    else:
        flash("جست‌وجو شروع شد؛ پست‌های پیدا شده با کامنت پیشنهادی همین‌جا می‌آیند.")
    return redirect("/comments")


@app.post("/comments/add")
def comments_add():
    r = guard()
    if r:
        return r
    cfg = store.load_config(False)
    urls = [u for u in _lines(request.form.get("urls", ""), 20) if "instagram.com/" in u]
    text = request.form.get("text", "").strip()[:300]
    if not urls:
        flash("لینک پست‌ها را بنویس (هر خط یک لینک اینستاگرام).", False)
    elif not store.session_files():
        flash("اول در بخش «اتصال اینستاگرام» وارد شو.", False)
    elif not text and cfg["growth"]["comment"]["style"] != "manual" and not growth.ai_slot(cfg):
        flash("متن کامنت را بنویس یا کلید AI را وارد کن.", False)
    elif not growth.scan_async("comment", urls=urls, text=text):
        flash("یک جست‌وجو همین الان در حال انجام است.", False)
    else:
        flash("لینک‌ها در حال بررسی‌اند؛ نتیجه تا چند لحظه‌ی دیگر در فهرست پایین می‌آید.")
    return redirect("/comments")


@app.post("/comments/<int:tid>/approve")
def comments_approve(tid):
    r = guard()
    if r:
        return r
    ok = growth.approve_comment(tid, request.form.get("text", ""))
    flash("تأیید شد و در صف ارسال قرار گرفت." if ok else "متن کامنت خالی است یا این مورد قابل تأیید نیست.", ok)
    return redirect("/comments")


@app.post("/comments/<int:tid>/skip")
def comments_skip(tid):
    r = guard()
    if r:
        return r
    growth.reject(tid)
    return redirect("/comments")


# ---------- دایرکت ----------
# ---------- میلیونیم ----------
@app.get("/million")
def million_page():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    snap = million.latest()
    series = million.follower_series(30)
    created = []
    if snap and (snap.get("strategy") or {}).get("created"):
        created = [p for p in (store.post_get(i) for i in snap["strategy"]["created"]) if p]
    return page(
        "million.html", watch=("million", "posts"), cfg=cfg, m=cfg["million"], snap=snap, prog=million.progress(cfg),
        series=series, pts_f=million.chart_points(series, "followers"), busy=million._busy.is_set(),
        last=store.meta_get("mil:last", ""), ig_ok=bool(store.session_files()), missing=million.needed_keys(cfg),
        created=created, share_own=cfg["growth"]["story"].get("share_own"), paused=growth.paused_until(),
    )


@app.post("/million/save")
def million_save():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    f = request.form
    m = cfg["million"]
    m["goal_views"] = _int(f, "goal_views", 1_000_000, 1000, 1_000_000_000)
    m["goal_followers"] = _int(f, "goal_followers", 10_000, 100, 100_000_000)
    m["analyze_time"] = (_parse_times(f.get("analyze_time", "")) or ["09:00"])[0]
    m["posts_per_day"] = _int(f, "posts_per_day", 2, 1, 6)
    m["kinds"] = [k for k in ("reel", "post", "story") if k in f.getlist("kinds")] or ["reel"]
    m["approve"] = "auto" if f.get("approve") == "auto" else "review"
    m["video"] = "video" in f
    m["with_voice"] = "with_voice" in f
    m["sound"] = "sound" in f
    m["competitors"] = "competitors" in f
    m["extra_hashtags"] = [x.lstrip("#") for x in _lines(f.get("extra_hashtags", ""), 10)]
    m["competitor_accounts"] = [x.lstrip("@") for x in _lines(f.get("competitor_accounts", ""), 10)]
    growth.set_share_own(cfg, "share_own" in f)
    store.save_config(cfg)
    flash("تنظیمات «میلیونیم» ذخیره شد.")
    return redirect("/million")


@app.post("/million/toggle")
def million_toggle():
    r = guard()
    if r:
        return r
    on = request.form.get("on") == "1"
    if on and not store.session_files():
        flash("اول در بخش «اتصال اینستاگرام» وارد شو.", False)
        return redirect("/million")
    if on:
        missing = _missing_keys(store.load_config(), ["caption"])
        if missing:
            flash("اول کلید API بخش «نوشتن کپشن» را در «تنظیمات AI» وارد کن؛ آنالیز و استراتژی با همان ساخته می‌شود.", False)
            return redirect("/ai")
    started = million.activate(on)
    flash("🚀 «میلیونیم» فعال شد. اولین آنالیز کامل پیج همین الان شروع شد و از این به بعد هر روز تکرار می‌شود."
          if started else ("«میلیونیم» فعال شد." if on else "«میلیونیم» غیرفعال شد؛ دیگر محتوای خودکار ساخته نمی‌شود."))
    return redirect("/million")


@app.post("/million/analyze")
def million_analyze():
    r = guard()
    if r:
        return r
    if not store.session_files():
        flash("اول در بخش «اتصال اینستاگرام» وارد شو.", False)
    elif not million.analyze_async(create=request.form.get("create") == "1"):
        flash("یک آنالیز همین الان در حال انجام است.", False)
    else:
        flash("آنالیز شروع شد؛ چند دقیقه طول می‌کشد و همین صفحه خودکار به‌روز می‌شود.")
    return redirect("/million")


@app.post("/million/create")
def million_create():
    r = guard()
    if r:
        return r
    pids, msg = million.create_from_latest()
    flash(msg, bool(pids))
    return redirect("/million")


@app.post("/million/bio")
def million_bio():
    r = guard()
    if r:
        return r
    bio = request.form.get("bio", "").strip()[:150]
    if not bio:
        flash("بیو خالی است.", False)
        return redirect("/million")
    ok, err = million.set_bio(bio)
    flash("بیو پیج عوض شد ✅" if ok else f"بیو عوض نشد: {err}", ok)
    return redirect("/million")


def read_drafts(n=15):
    try:
        with open(DRAFTS_PATH, encoding="utf-8") as f:
            lines = f.readlines()[-n:]
    except FileNotFoundError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out


def _tail(path, n=40):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8000))
            return "\n".join(f.read().decode("utf-8", "ignore").splitlines()[-n:])
    except FileNotFoundError:
        return "(هنوز لاگی نیست)"


@app.get("/dm")
def dm_page():
    r = guard()
    if r:
        return r
    cfg = store.load_config(False)
    return page("dm.html", watch=("bot",), dm=cfg["dm"], running=bot_running(), drafts=read_drafts(), log=_tail(LOG_PATH))


@app.post("/dm/save")
def dm_save():
    r = guard()
    if r:
        return r
    cfg = store.load_config()
    f = request.form
    dm = cfg["dm"]
    dm["mode"] = "auto" if f.get("mode") == "auto" else "draft"
    dm["reply_in_groups"] = "reply_in_groups" in f
    dm["disclose"] = "disclose" in f
    for key, lo, hi, default in (("max_replies_per_hour", 1, 500, 20), ("poll_seconds", 5, 600, 45)):
        try:
            dm[key] = max(lo, min(hi, int(f.get(key, default))))
        except ValueError:
            dm[key] = default
    dm["blocklist"] = [x.strip() for x in f.get("blocklist", "").replace(",", "\n").splitlines() if x.strip()]
    store.save_config(cfg)
    msg = "تنظیمات ذخیره شد."
    if bot_running():
        stop_bot(remember=False)
        start_bot()
        msg += " ربات با تنظیمات جدید دوباره راه افتاد."
    flash(msg)
    return redirect("/dm")


@app.post("/bot/start")
def bot_start():
    r = guard()
    if r:
        return r
    err = start_bot()
    flash(err or "ربات روشن شد.", err is None)
    return redirect("/dm")


@app.post("/bot/stop")
def bot_stop():
    r = guard()
    if r:
        return r
    stop_bot()
    flash("ربات خاموش شد.")
    return redirect("/dm")


def main():
    host = os.getenv("PANEL_HOST", "127.0.0.1")
    port = int(os.getenv("PORT") or os.getenv("PANEL_PORT", "8080"))
    threading.Thread(target=monitor, daemon=True).start()
    jobs.start_scheduler()
    if store.meta_get("bot_want") == "1" and store.session_files():
        start_bot()
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8, channel_timeout=300)
    except ImportError:
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
