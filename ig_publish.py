"""
انتشار یک پست/استوری/ریلز در اینستاگرام (فرآیند جدا؛ توسط jobs.py اجرا می‌شود).
استفاده: ig_publish.py <post|story|reel> <مسیر فایل رسانه> <مسیر فایل کپشن یا ->
خروجی: یک خط JSON. کد خروج ۳ یعنی session منقضی شده و باید دوباره وارد شد.
"""
import json
import os
import sys
from pathlib import Path

os.chdir(os.getenv("DATA_DIR", "."))
_out = sys.stdout
sys.stdout = sys.stderr  # هر print دیگری به لاگ برود، نه پروتکل

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import igcompat  # noqa: E402

SESSION_FILE = igcompat.SESSION_FILE
EXIT_LOGIN_REQUIRED = igcompat.EXIT_LOGIN_REQUIRED


def emit(**kw):
    _out.write(json.dumps(kw, ensure_ascii=False) + "\n")
    _out.flush()


def _thumb(video_path):
    """تصویر بندانگشتی را خودمان با ffmpeg می‌سازیم تا به moviepy وابسته نباشیم."""
    out = Path(f"{video_path}.thumb.jpg")
    try:
        igcompat._ffmpeg_thumb(video_path, out, 1.0)
        return out
    except Exception:  # noqa: BLE001
        return None


def main():
    if len(sys.argv) < 4:
        emit(ok=False, error="آرگومان کافی نیست.")
        return 1
    kind, media_path, cap_file = sys.argv[1:4]
    caption = ""
    if cap_file != "-":
        with open(cap_file, encoding="utf-8") as f:
            caption = f.read().strip()
    if not os.path.exists(SESSION_FILE):
        emit(ok=False, login_required=True, error="به اینستاگرام وصل نیستی؛ از بخش «اتصال اینستاگرام» وارد شو.")
        return EXIT_LOGIN_REQUIRED

    cl = igcompat.load_client()
    from instagrapi.exceptions import ChallengeRequired, LoginRequired

    path = Path(media_path)
    try:
        cl.account_info()
        url, info = "", {}
        if kind == "post":
            m = cl.photo_upload(path, caption)
            url = f"https://www.instagram.com/p/{m.code}/"
        elif kind == "reel":
            m = cl.clip_upload(path, caption, thumbnail=_thumb(path))
            url = f"https://www.instagram.com/reel/{m.code}/"
        elif kind == "story":
            if path.suffix.lower() == ".mp4":
                cl.video_upload_to_story(path, thumbnail=_thumb(path))
            else:
                cl.photo_upload_to_story(path)
        else:
            emit(ok=False, error=f"نوع ناشناخته: {kind}")
            return 1
        try:
            cl.dump_settings(SESSION_FILE)
        except Exception:  # noqa: BLE001
            pass
        if kind in ("post", "reel"):
            info = {"pk": str(getattr(m, "pk", "") or ""), "code": getattr(m, "code", "") or "",
                    "author": getattr(getattr(m, "user", None), "username", "") or ""}
        emit(ok=True, url=url, **info)
        return 0
    except (LoginRequired, ChallengeRequired) as e:
        emit(ok=False, login_required=True, error=f"session منقضی شده یا اینستاگرام تأیید هویت خواسته ({type(e).__name__}).")
        return EXIT_LOGIN_REQUIRED
    except Exception as e:  # noqa: BLE001
        hint = ""
        if kind in ("story", "reel") and path.suffix.lower() == ".mp4" and "transcode" in str(e).lower():
            hint = " (اینستاگرام ویدیو را نپذیرفت؛ «ساخت دوباره‌ی ویدیو» را بزن و دوباره امتحان کن.)"
        emit(ok=False, error=f"{type(e).__name__}: {str(e)[:300]}{hint}", cooldown=igcompat.is_cooldown_error(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
