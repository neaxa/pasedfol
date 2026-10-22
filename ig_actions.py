"""
عملیات اینستاگرام برای بخش «کشف و استوری» و «کامنت خودکار» (فرآیند جدا؛ توسط growth.py اجرا می‌شود).
استفاده: ig_actions.py <search|story|comment|mine|insights|set_bio> <مسیر فایل JSON پارامترها>
خروجی: یک خط JSON. کد خروج ۳ = session منقضی، ۴ = اینستاگرام محدودیت موقت داده (باید مدتی صبر کرد).

search : هشتگ‌ها / پیج‌ها / لینک پست‌ها را می‌گیرد و فهرست پست‌ها با آمار (بازدید، لایک، کامنت) برمی‌گرداند.
story  : یک پست/ریلز را در استوری حساب خودت می‌گذارد (حالت «کامل» یا «کارت پیش‌نمایش»).
mine   : تازه‌ترین پست‌های خود حساب (برای استوری خودکار پست‌های جدید).
insights: پروفایل، پست‌های خودت و پست‌های پربازدید حوزه‌ات (برای بخش «میلیونیم»).
set_bio: بیو پیج را عوض می‌کند.
comment: زیر یک پست کامنت می‌گذارد.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.chdir(os.getenv("DATA_DIR", "."))
_out = sys.stdout
sys.stdout = sys.stderr  # هر print دیگری به لاگ برود، نه پروتکل

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import igcompat  # noqa: E402


def emit(**kw):
    _out.write(json.dumps(kw, ensure_ascii=False) + "\n")
    _out.flush()


def _num(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _thumb_of(m):
    for obj in [m] + list(getattr(m, "resources", None) or []):
        u = getattr(obj, "thumbnail_url", None)
        if u:
            return str(u)
    return ""


def media_item(m, topic=""):
    """مدل Media را به یک dict ساده تبدیل می‌کند (فیلتر و امتیازدهی سمت پنل انجام می‌شود)."""
    ptype = getattr(m, "product_type", "") or ""
    mtype = _num(getattr(m, "media_type", 0))
    taken = getattr(m, "taken_at", None)
    user = getattr(m, "user", None)
    return {
        "pk": str(m.pk),
        "code": getattr(m, "code", "") or "",
        "author": getattr(user, "username", "") or "",
        "author_pk": str(getattr(user, "pk", "") or ""),
        "kind": "reel" if (ptype == "clips" or mtype == 2) else "post",
        "views": max(_num(getattr(m, "play_count", 0)), _num(getattr(m, "view_count", 0))),
        "likes": _num(getattr(m, "like_count", 0)),
        "comments": _num(getattr(m, "comment_count", 0)),
        "taken_at": taken.timestamp() if hasattr(taken, "timestamp") else 0,
        "thumb": _thumb_of(m),
        "caption": (getattr(m, "caption_text", "") or "")[:600],
        "comments_disabled": bool(getattr(m, "comments_disabled", False)),
        "topic": topic,
    }


def _hashtag_fetchers(source):
    order = []
    if source in ("top", "both"):
        order += ["hashtag_medias_top", "hashtag_medias_top_v1"]
    if source in ("recent", "both"):
        order += ["hashtag_medias_recent", "hashtag_medias_recent_v1"]
    return order


def _try_fetch(cl, names, *args, **kw):
    """اولین متدی را که جواب بدهد امتحان می‌کند (متدهای هشتگ بین نسخه‌های instagrapi فرق دارند)."""
    last = None
    for n in names:
        fn = getattr(cl, n, None)
        if not fn:
            continue
        try:
            return fn(*args, **kw)
        except Exception as e:  # noqa: BLE001
            if igcompat.is_login_error(e) or igcompat.is_cooldown_error(e):
                raise
            last = e
    if last:
        raise last
    return []


def do_search(cl, p):
    amount = max(5, min(60, int(p.get("amount", 24))))
    items, errors, seen = [], [], set()

    def add(m, topic):
        if str(m.pk) in seen:
            return
        seen.add(str(m.pk))
        items.append(media_item(m, topic))

    for tag in p.get("hashtags", []):
        try:
            if p.get("source") == "both":  # هر منبع سهم خودش را می‌گیرد
                for src in ("top", "recent"):
                    for m in _try_fetch(cl, _hashtag_fetchers(src), tag, amount=max(5, amount // 2)):
                        add(m, tag)
            else:
                for m in _try_fetch(cl, _hashtag_fetchers(p.get("source", "top")), tag, amount=amount):
                    add(m, tag)
        except Exception as e:  # noqa: BLE001
            if igcompat.is_login_error(e) or igcompat.is_cooldown_error(e):
                raise
            errors.append(f"#{tag}: {type(e).__name__}: {str(e)[:120]}")
    for user in p.get("accounts", []):
        try:
            uid = cl.user_id_from_username(user.lstrip("@"))
            for m in cl.user_medias(uid, amount=min(amount, 12)):
                add(m, "@" + user.lstrip("@"))
        except Exception as e:  # noqa: BLE001
            if igcompat.is_login_error(e) or igcompat.is_cooldown_error(e):
                raise
            errors.append(f"@{user}: {type(e).__name__}: {str(e)[:120]}")
    for url in p.get("urls", []):
        try:
            pk = cl.media_pk_from_url(url)
            add(cl.media_info(pk), "link")
        except Exception as e:  # noqa: BLE001
            if igcompat.is_login_error(e) or igcompat.is_cooldown_error(e):
                raise
            errors.append(f"{url[:60]}: {type(e).__name__}: {str(e)[:120]}")
    me = str(getattr(cl, "user_id", "") or "")
    for it in items:
        it["is_mine"] = bool(me) and it["author_pk"] == me
    return {"ok": True, "items": items, "errors": errors}


def _download(url, proxy=None):
    if url.startswith("file://") or os.path.isabs(url):  # فقط برای تست‌های بدون اینترنت
        with open(url[7:] if url.startswith("file://") else url, "rb") as f:
            return f.read()
    import requests
    r = requests.get(url, timeout=60, proxies={"http": proxy, "https": proxy} if proxy else None,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.content


def _try_download(url):
    if not url:
        return None
    try:
        return _download(str(url), os.getenv("IG_PROXY"))
    except Exception:  # noqa: BLE001
        return None


def _main_resource(info):
    """برای پست چندتایی (آلبوم) اولین اسلاید؛ وگرنه خود پست."""
    res = list(getattr(info, "resources", None) or [])
    if _num(getattr(info, "media_type", 0)) == 8 and res:
        return res[0]
    return info


def _is_video(obj):
    return _num(getattr(obj, "media_type", 0)) == 2 or bool(getattr(obj, "video_url", None))


def _extras(info, link=True, mention=True, area=(0.5, 0.9)):
    """استیکر لینک پست اصلی و منشن صاحب پست (اگر نسخه‌ی instagrapi پشتیبانی کند)."""
    kw = {}
    try:
        from instagrapi import types as T
    except Exception:  # noqa: BLE001
        return kw
    code = getattr(info, "code", "") or ""
    is_reel = (getattr(info, "product_type", "") == "clips")
    if link and code and hasattr(T, "StoryLink"):
        try:
            url = f"https://www.instagram.com/{'reel' if is_reel else 'p'}/{code}/"
            kw["links"] = [T.StoryLink(webUri=url, x=area[0], y=area[1], width=0.5, height=0.06)]
        except Exception:  # noqa: BLE001
            pass
    user = getattr(info, "user", None)
    if mention and user is not None and hasattr(T, "StoryMention") and hasattr(T, "UserShort"):
        try:
            us = T.UserShort(pk=str(getattr(user, "pk", "")), username=getattr(user, "username", ""))
            kw["mentions"] = [T.StoryMention(user=us, x=0.5, y=0.11, width=0.4, height=0.05)]
        except Exception:  # noqa: BLE001
            pass
    return kw


def _upload_story(fn, path, kw, **base):
    """آپلود با استیکرهای اضافه؛ اگر نسخه‌ی instagrapi پارامتری را نشناخت (قبل از آپلود)، بدون آن تلاش می‌کند."""
    try:
        return fn(Path(path), **base, **kw)
    except TypeError:
        return fn(Path(path), **base)


def _story_full(cl, info, p, tmpdir):
    """حالت «کامل»: خود عکس یا ویدیو داخل استوری دیده و پخش می‌شود."""
    import media as media_mod
    author = getattr(getattr(info, "user", None), "username", "") or p.get("author", "")
    src = _main_resource(info)
    kw = _extras(info, link=p.get("add_link", True))
    if _is_video(src):
        data = _try_download(getattr(src, "video_url", None) or getattr(info, "video_url", None))
        if not data:
            return {"ok": False, "error": "فایل ویدیوی این پست دانلود نشد (شاید خصوصی یا حذف شده باشد)."}
        raw = os.path.join(tmpdir, "src.mp4")
        with open(raw, "wb") as f:
            f.write(data)
        out = os.path.join(tmpdir, "story.mp4")
        media_mod.story_full_video(raw, out, author, sound=p.get("sound", True))
        thumb = os.path.join(tmpdir, "story.mp4.jpg")
        try:
            igcompat._ffmpeg_thumb(out, thumb, 0.5)
        except Exception:  # noqa: BLE001
            thumb = None
        _upload_story(cl.video_upload_to_story, out, kw, caption="", thumbnail=Path(thumb) if thumb else None)
        return {"ok": True, "style": "full", "media": "video"}
    data = _try_download(_thumb_of(src) or _thumb_of(info))
    if not data:
        return {"ok": False, "error": "عکس این پست دانلود نشد (شاید خصوصی یا حذف شده باشد)."}
    out = os.path.join(tmpdir, "story.jpg")
    media_mod.story_full_image(data, author, out)
    _upload_story(cl.photo_upload_to_story, out, kw, caption="")
    return {"ok": True, "style": "full", "media": "photo"}


def _story_card(cl, info, p, tmpdir):
    """حالت «کارت»: کارت پیش‌نمایش (کاملاً دیده می‌شود) + استیکر پست روی همان کارت که با زدن، پست اصلی باز شود."""
    from instagrapi.types import StoryMedia
    import media as media_mod
    pk = str(p["pk"])
    user = getattr(info, "user", None)
    author = getattr(user, "username", "") or p.get("author", "")
    is_reel = (getattr(info, "product_type", "") == "clips") or _num(getattr(info, "media_type", 0)) == 2
    thumb = _try_download(_thumb_of(info))
    avatar = _try_download(getattr(user, "profile_pic_url", None))
    out = os.path.join(tmpdir, "card.jpg")
    x, y, w, h = media_mod.story_card(thumb, author, is_reel, out, avatar)
    sticker = StoryMedia(media_pk=int(pk), x=x, y=y, width=w, height=h)
    kw = _extras(info, link=p.get("add_link", True), mention=False, area=(0.5, min(0.95, y + h / 2 + 0.035)))
    try:
        _upload_story(cl.photo_upload_to_story, out, kw, caption="", medias=[sticker])
    except TypeError as e:
        return {"ok": False, "error": f"نسخه‌ی instagrapi قدیمی است و استیکر پست را پشتیبانی نمی‌کند؛ آن را آپدیت کن ({e})."}
    return {"ok": True, "style": "card"}


def do_story(cl, p):
    """پست/ریلز را در استوری حساب خودت می‌گذارد؛ به یکی از دو شکل:
      full : کل عکس/ویدیو داخل خود استوری (همان‌جا دیده می‌شود) + نام و لینک صاحب پست
      card : کارت پیش‌نمایش؛ با زدن روی آن پست اصلی باز می‌شود"""
    import shutil
    info = cl.media_info(str(p["pk"]))  # اگر پست حذف/خصوصی شده باشد همین‌جا خطا می‌دهد
    tmpdir = tempfile.mkdtemp(prefix="story_")
    try:
        if p.get("style") == "card":
            return _story_card(cl, info, p, tmpdir)
        return _story_full(cl, info, p, tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def do_mine(cl, p):
    """تازه‌ترین پست‌ها/ریلزهای خود حساب (برای «هر پست جدید را استوری کن»)."""
    me = str(getattr(cl, "user_id", "") or "")
    if not me:
        return {"ok": False, "error": "شناسه‌ی حساب پیدا نشد."}
    items = []
    for m in cl.user_medias(me, amount=max(1, min(20, int(p.get("amount", 6))))):
        it = media_item(m, "own")
        it["is_mine"] = True
        items.append(it)
    return {"ok": True, "items": items}


def _rich_item(m, topic=""):
    it = media_item(m, topic)
    it["duration"] = float(getattr(m, "video_duration", 0) or 0)
    it["caption"] = (getattr(m, "caption_text", "") or "")[:1200]
    return it


def do_insights(cl, p):
    """داده‌ی خام بخش «میلیونیم»: پروفایل خودت، همه‌ی پست‌های اخیرت با آمار، و پست‌های پربازدید حوزه‌ات."""
    me = str(getattr(cl, "user_id", "") or "")
    prof, errors = {}, []
    try:
        u = cl.user_info(me)
        prof = {
            "username": getattr(u, "username", "") or "", "full_name": getattr(u, "full_name", "") or "",
            "bio": getattr(u, "biography", "") or "", "followers": _num(getattr(u, "follower_count", 0)),
            "following": _num(getattr(u, "following_count", 0)), "media_count": _num(getattr(u, "media_count", 0)),
            "category": getattr(u, "category_name", "") or getattr(u, "category", "") or "",
            "external_url": str(getattr(u, "external_url", "") or ""),
            "is_business": bool(getattr(u, "is_business", False)),
        }
    except Exception as e:  # noqa: BLE001
        if igcompat.is_login_error(e) or igcompat.is_cooldown_error(e):
            raise
        errors.append(f"profile: {type(e).__name__}: {str(e)[:120]}")
    mine = []
    try:
        for m in cl.user_medias(me, amount=max(6, min(60, int(p.get("amount", 30))))):
            it = _rich_item(m, "own")
            it["is_mine"] = True
            mine.append(it)
    except Exception as e:  # noqa: BLE001
        if igcompat.is_login_error(e) or igcompat.is_cooldown_error(e):
            raise
        errors.append(f"my posts: {type(e).__name__}: {str(e)[:120]}")
    niche, seen = [], set()
    per = max(5, min(30, int(p.get("niche_amount", 18))))
    for tag in p.get("hashtags", [])[:4]:
        try:
            for m in _try_fetch(cl, _hashtag_fetchers("top"), tag, amount=per):
                if str(m.pk) not in seen:
                    seen.add(str(m.pk))
                    niche.append(_rich_item(m, tag))
        except Exception as e:  # noqa: BLE001
            if igcompat.is_login_error(e) or igcompat.is_cooldown_error(e):
                raise
            errors.append(f"#{tag}: {type(e).__name__}: {str(e)[:120]}")
    for user in p.get("accounts", [])[:4]:
        try:
            uid = cl.user_id_from_username(user.lstrip("@"))
            for m in cl.user_medias(uid, amount=12):
                if str(m.pk) not in seen:
                    seen.add(str(m.pk))
                    niche.append(_rich_item(m, "@" + user.lstrip("@")))
        except Exception as e:  # noqa: BLE001
            if igcompat.is_login_error(e) or igcompat.is_cooldown_error(e):
                raise
            errors.append(f"@{user}: {type(e).__name__}: {str(e)[:120]}")
    for it in niche:
        it["is_mine"] = bool(me) and it["author_pk"] == me
    niche = [it for it in niche if not it["is_mine"]]
    return {"ok": True, "profile": prof, "mine": mine, "niche": niche, "errors": errors}


def do_set_bio(cl, p):
    bio = (p.get("bio") or "").strip()[:150]
    if not bio:
        return {"ok": False, "error": "بیو خالی است."}
    cl.account_edit(biography=bio)
    return {"ok": True}


def do_comment(cl, p):
    text = (p.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "متن کامنت خالی است."}
    pk = str(p["pk"])
    info = cl.media_info(pk)
    if getattr(info, "comments_disabled", False):
        return {"ok": False, "error": "کامنت این پست بسته است.", "skip": True}
    mid = cl.media_id(pk)
    c = cl.media_comment(mid, text)
    return {"ok": True, "comment_pk": str(getattr(c, "pk", "") or "")}


COMMANDS = {"search": do_search, "story": do_story, "comment": do_comment, "mine": do_mine,
            "insights": do_insights, "set_bio": do_set_bio}


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in COMMANDS:
        emit(ok=False, error="دستور یا پارامتر نامعتبر.")
        return 1
    cmd = sys.argv[1]
    with open(sys.argv[2], encoding="utf-8") as f:
        params = json.load(f)
    if not os.path.exists(igcompat.SESSION_FILE):
        emit(ok=False, login_required=True, error="به اینستاگرام وصل نیستی؛ از بخش «اتصال اینستاگرام» وارد شو.")
        return igcompat.EXIT_LOGIN_REQUIRED
    cl = igcompat.load_client()
    try:
        cl.account_info()
        res = COMMANDS[cmd](cl, params)
        try:
            cl.dump_settings(igcompat.SESSION_FILE)
        except Exception:  # noqa: BLE001
            pass
        emit(**res)
        return 0 if res.get("ok") else 1
    except Exception as e:  # noqa: BLE001
        if igcompat.is_login_error(e):
            emit(ok=False, login_required=True,
                 error=f"session منقضی شده یا اینستاگرام تأیید هویت خواسته ({type(e).__name__}).")
            return igcompat.EXIT_LOGIN_REQUIRED
        if igcompat.is_cooldown_error(e):
            emit(ok=False, cooldown=True, error=f"اینستاگرام محدودیت موقت داده ({type(e).__name__}: {str(e)[:160]}).")
            return igcompat.EXIT_COOLDOWN
        emit(ok=False, error=f"{type(e).__name__}: {str(e)[:300]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
