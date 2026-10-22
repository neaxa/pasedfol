"""
فرآیند لاگین اینستاگرام؛ توسط panel.py اجرا می‌شود.
دو روش:
  ۱) نام کاربری + رمز (IG_USERNAME / IG_PASSWORD) — اگر کد دومرحله‌ای یا تأیید خواست، به پنل خبر می‌دهیم.
  ۲) sessionid کوکی مرورگر (IG_SESSIONID) — وقتی اینستاگرام لاگین از IP سرور را رد می‌کند، راه مطمئن‌تر است.
پروتکل: هر خط خروجی یک JSON است؛ پاسخ‌ها خط‌به‌خط از stdin می‌آید.
رمز و sessionid هیچ‌جا ذخیره نمی‌شود؛ فقط فایل session (ig_session.json) ذخیره می‌شود.
"""
import json
import os
import sys

os.chdir(os.getenv("DATA_DIR", "."))

_out = sys.stdout
sys.stdout = sys.stderr  # هر print دیگری به لاگ می‌رود، نه به پروتکل پنل


def emit(**kw):
    _out.write(json.dumps(kw, ensure_ascii=False) + "\n")
    _out.flush()


def ask(kind):
    emit(event="need", kind=kind)
    line = sys.stdin.readline()
    if not line:
        raise EOFError("panel closed")
    return line.strip()


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import igcompat  # noqa: E402
from instagrapi import Client  # noqa: E402
from instagrapi.exceptions import TwoFactorRequired  # noqa: E402

igcompat.patch_dead_endpoints()  # qe/expose دیگر وجود ندارد (404)؛ نباید ورود را خراب کند

SESSION_FILE = "ig_session.json"


def main():
    username = os.environ.get("IG_USERNAME", "").strip()
    password = os.environ.get("IG_PASSWORD", "")
    sessionid = os.environ.get("IG_SESSIONID", "").strip()
    if not sessionid and (not username or not password):
        emit(event="error", message="نام کاربری و رمز (یا sessionid) را وارد کن.")
        return
    cl = Client()
    cl.delay_range = [1, 3]
    if os.getenv("IG_PROXY"):
        cl.set_proxy(os.environ["IG_PROXY"])
    cl.challenge_code_handler = lambda _u, _choice: ask("code")
    try:
        if sessionid:
            cl.login_by_sessionid(sessionid)
        else:
            try:
                cl.login(username, password)
            except TwoFactorRequired:
                cl.login(username, password, verification_code=ask("code"))
        cl.dump_settings(SESSION_FILE)
        try:
            os.chmod(SESSION_FILE, 0o600)
        except OSError:
            pass
        name = username
        try:
            name = cl.account_info().username
        except Exception:  # noqa: BLE001
            pass
        emit(event="done", username=name or "", guid=str(cl.user_id))
    except Exception as e:  # noqa: BLE001
        emit(event="error", message=f"{type(e).__name__}: {str(e)[:300]}")


main()
