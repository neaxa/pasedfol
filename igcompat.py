"""
ابزارهای مشترک فرآیندهای اینستاگرام (ig_publish.py و ig_actions.py).
- سازگاری با moviepy نسخه‌ی ۱ و ۲ و در صورت خرابی، تحلیل ویدیو با ffprobe/ffmpeg (بدون moviepy)
- ساخت Client از روی فایل session
- دسته‌بندی خطاها: نیاز به ورود دوباره / محدودیت موقت اینستاگرام
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SESSION_FILE = "ig_session.json"
EXIT_LOGIN_REQUIRED = 3
EXIT_COOLDOWN = 4

LOGIN_ERRORS = ("LoginRequired", "ChallengeRequired")
COOLDOWN_ERRORS = ("PleaseWaitFewMinutes", "RateLimitError", "ClientThrottledError", "FeedbackRequired",
                   "ClientForbiddenError", "ActionBlocked")
COOLDOWN_HINTS = ("feedback_required", "action blocked", "try again later", "spam", "please wait a few minutes",
                  "temporarily blocked", "rate limit", "throttle")


def moviepy_compat():
    """instagrapi نسخه‌های مختلف moviepy را متفاوت صدا می‌زند (moviepy.editor در ۱.x، moviepy در ۲.x).
    این شیم هر دو را سازگار می‌کند."""
    try:
        import moviepy
    except ImportError:
        return
    if not hasattr(moviepy, "VideoFileClip"):
        try:
            from moviepy.editor import AudioFileClip, ImageClip, VideoFileClip
            moviepy.VideoFileClip, moviepy.AudioFileClip, moviepy.ImageClip = VideoFileClip, AudioFileClip, ImageClip
        except ImportError:
            pass
    try:
        import moviepy.editor  # noqa: F401
    except ImportError:
        sys.modules["moviepy.editor"] = moviepy


def _ffprobe_video(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    data = json.loads(r.stdout or "{}")
    st = (data.get("streams") or [{}])[0]
    return int(st["width"]), int(st["height"]), float((data.get("format") or {}).get("duration") or 0)


def _ffmpeg_thumb(path, out, at):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(path), "-frames:v", "1", "-q:v", "3", str(out)],
        capture_output=True, timeout=120, check=True)


def patch_video_analysis():
    """instagrapi برای ریلز/استوری ویدیویی با moviepy ابعاد و تصویر بندانگشتی را درمی‌آورد.
    اگر moviepy خراب بود (ناسازگاری نسخه)، به‌جایش ffprobe/ffmpeg استفاده می‌شود؛
    اگر سالم بود، رفتار قبلی دست‌نخورده می‌ماند."""
    try:
        import instagrapi.image_util as iu
    except Exception:  # noqa: BLE001
        return
    orig = getattr(iu, "analyze_video", None)
    if orig is None or getattr(orig, "_ffmpeg_fallback", False):
        return

    def analyze_video(path, thumbnail=None):
        try:
            return orig(path, thumbnail)
        except Exception:  # noqa: BLE001
            pass
        width, height, duration = _ffprobe_video(path)
        thumb = Path(thumbnail) if thumbnail else Path(f"{path}.jpg")
        if not thumb.exists():
            _ffmpeg_thumb(path, thumb, min(1.0, duration / 2))
        return width, height, duration, thumb

    analyze_video._ffmpeg_fallback = True
    for name, mod in list(sys.modules.items()):
        if name.startswith("instagrapi") and getattr(mod, "analyze_video", None) is orig:
            setattr(mod, "analyze_video", analyze_video)


# endpointهای «اختیاری» که اینستاگرام حذفشان کرده (404) ولی instagrapi هنوز هنگام ورود/ورود دوباره صدایشان می‌زند.
# نبودنشان هیچ ضرری ندارد؛ فقط تلاش برای «شبیه اپ بودن» است. خطای 404 این مسیرها نباید کار اصلی را خراب کند.
DEAD_ENDPOINTS = ("qe/expose", "qe/sync", "launcher/sync", "attribution/log_resurrect_attribution",
                  "accounts/contact_point_prefill", "accounts/get_prefill_candidates")


def _is_dead_endpoint(endpoint):
    ep = str(endpoint or "").lstrip("/")
    return any(ep.startswith(d) for d in DEAD_ENDPOINTS)


def patch_dead_endpoints():
    """خطای ClientNotFoundError برای qe/expose و مشابه‌ها را بی‌صدا نادیده می‌گیرد.
    این پچ در سطح private_request است، پس از هر مسیری که فراخوانی شود (login، relogin، login_by_sessionid،
    account_info، آپلود استوری/ریلز و ...) کار می‌کند. خطاهای مسیرهای دیگر دست‌نخورده می‌ماند."""
    try:
        from instagrapi import Client
    except Exception:  # noqa: BLE001
        return
    orig = getattr(Client, "private_request", None)
    if orig is not None and not getattr(orig, "_dead_endpoint_patch", False):
        def private_request(self, *args, **kwargs):
            endpoint = args[0] if args else kwargs.get("endpoint", "")
            try:
                return orig(self, *args, **kwargs)
            except Exception as e:  # noqa: BLE001
                if _is_dead_endpoint(endpoint) and type(e).__name__ in ("ClientNotFoundError", "ClientError",
                                                                          "ClientBadRequestError"):
                    return {}
                raise
        private_request._dead_endpoint_patch = True
        Client.private_request = private_request
    # راه دوم: خود متدهای اختیاری هم اگر خطا دادند، خطایشان نادیده گرفته شود
    for name in ("expose", "sync_launcher", "sync_device_features"):
        fn = getattr(Client, name, None)
        if fn is None or getattr(fn, "_dead_endpoint_patch", False):
            continue

        def make(fn):
            def safe(self, *a, **kw):
                try:
                    return fn(self, *a, **kw)
                except Exception as e:  # noqa: BLE001
                    if type(e).__name__ in ("ClientNotFoundError", "ClientError", "ClientBadRequestError"):
                        return {}
                    raise
            safe._dead_endpoint_patch = True
            return safe
        setattr(Client, name, make(fn))


def load_client():
    """Client ورودکرده از روی ig_session.json (در cwd = DATA_DIR)."""
    moviepy_compat()
    from instagrapi import Client
    patch_dead_endpoints()
    patch_video_analysis()
    cl = Client()
    cl.delay_range = [1, 3]
    if os.getenv("IG_PROXY"):
        cl.set_proxy(os.environ["IG_PROXY"])
    cl.load_settings(SESSION_FILE)
    return cl


def _classes(names):
    from instagrapi import exceptions as E
    return tuple(getattr(E, n) for n in names if hasattr(E, n))


def is_login_error(e):
    return isinstance(e, _classes(LOGIN_ERRORS))


def is_cooldown_error(e):
    if isinstance(e, _classes(COOLDOWN_ERRORS)):
        return True
    msg = f"{type(e).__name__} {e}".lower()
    return any(h in msg for h in COOLDOWN_HINTS)
