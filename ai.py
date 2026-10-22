"""
لایه‌ی یکپارچه‌ی هوش مصنوعی: متن، تصویر و صدا از شرکت‌های مختلف.
هر «بخش» (slot) تنظیم مستقل خودش را دارد: provider + api_key + model (+ base_url / voice).
فقط به requests وابسته است.
"""
import base64
import io
import json
import logging
import random
import re
import threading
import time
import wave

import requests

TIMEOUT = 120
GEMINI_ROOT = "https://generativelanguage.googleapis.com"
DEAPI_ROOT = "https://api.deapi.ai"
DEAPI_POLL_SECONDS = 5
DEAPI_TIMEOUT = 15 * 60

PROVIDERS = {
    "openai": {
        "label": "OpenAI", "base": "https://api.openai.com/v1",
        "chat": "gpt-4.1-mini", "image": "gpt-image-1", "voice": "gpt-4o-mini-tts", "voice_name": "alloy",
    },
    "gemini": {
        "label": "Google Gemini", "base": GEMINI_ROOT + "/v1beta/openai",
        "chat": "gemini-2.5-flash", "image": "gemini-2.5-flash-image", "voice": "gemini-3.8-flash-tts",
        "voice_name": "Kore",
    },
    "openrouter": {
        "label": "OpenRouter", "base": "https://openrouter.ai/api/v1",
        "chat": "openai/gpt-4.1-mini", "image": "google/gemini-2.5-flash-image",
    },
    "pollinations": {
        "label": "Pollinations", "base": "https://gen.pollinations.ai/v1",
        "chat": "openai/gpt-5.4-nano", "image": "black-forest-labs/flux.1-schnell", "voice": "tts-1",
        "voice_name": "alloy",
    },
    "elevenlabs": {
        "label": "ElevenLabs", "base": "https://api.elevenlabs.io/v1",
        "voice": "eleven_multilingual_v2", "voice_name": "21m00Tcm4TlvDq8ikWAM",
    },
    "deapi": {
        "label": "deAPI", "base": DEAPI_ROOT, "image": "Flux1schnell", "video": "Ltx2_5_22B_Dist_INT8",
    },
    "custom": {"label": "سازگار با OpenAI (آدرس دلخواه: Groq، DeepSeek، Together، ...)", "base": ""},
}

SLOT_PROVIDERS = {
    "chat": ["openai", "gemini", "openrouter", "pollinations", "custom"],
    "caption": ["openai", "gemini", "openrouter", "pollinations", "custom"],
    "image": ["openai", "gemini", "openrouter", "pollinations", "deapi", "custom"],
    "video": ["deapi"],
    "voice": ["openai", "gemini", "elevenlabs", "pollinations", "custom"],
}

SLOT_LABELS = {
    "chat": "پاسخ دایرکت (چت)",
    "caption": "نوشتن کپشن و متن محتوا",
    "image": "ساخت تصویر",
    "video": "ساخت ویدیو با AI",
    "voice": "ساخت ویس (متن به گفتار)",
}


log = logging.getLogger("ai")


class AIError(Exception):
    pass


class KeyProblem(AIError):
    """مشکل خودِ کلید/حساب (معلق، نامعتبر، بدون اعتبار)؛ با کلید دیگر می‌شود ادامه داد."""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


# ---------- چند کلید برای یک بخش ----------
KEY_BAD_TTL = 30 * 60      # کلید خراب را تا این مدت اول امتحان نمی‌کنیم
KEY_OK_TTL = 10 * 60       # کلیدی که تازه سالم بوده، دوباره پینگ نمی‌شود
KEY_STATUSES = (401, 402, 403)  # نامعتبر / نیاز به پرداخت (اعتبار تمام) / معلق
_key_lock = threading.Lock()
_bad_keys = {}   # key -> (تا چه زمانی، دلیل)
_ok_keys = {}    # key -> زمان آخرین تست سالم


def split_keys(text):
    """کلیدها را از متن چندخطی (یا جداشده با ویرگول/فاصله) درمی‌آورد؛ بدون تکرار و با حفظ ترتیب."""
    out = []
    for k in re.split(r"[\s,;]+", text or ""):
        k = k.strip()
        if k and k not in out:
            out.append(k)
    return out


def keys_of(slot):
    return split_keys(slot.get("api_key"))


def mask_key(k):
    return "…" + k[-4:] if len(k) > 4 else "…"


def key_problem(k):
    """اگر کلید اخیراً خراب بوده، دلیلش؛ وگرنه None."""
    with _key_lock:
        v = _bad_keys.get(k)
    if v and v[0] > time.time():
        return v[1]
    return None


def _mark_bad(k, reason):
    with _key_lock:
        _bad_keys[k] = (time.time() + KEY_BAD_TTL, reason)
        _ok_keys.pop(k, None)


def _mark_ok(k):
    with _key_lock:
        _bad_keys.pop(k, None)
        _ok_keys[k] = time.time()


# ---------- ابزارهای داخلی ----------
def _prov(slot):
    p = (slot.get("provider") or "openai").strip()
    return p, PROVIDERS.get(p, PROVIDERS["custom"])


def _key(slot, required=True):
    ks = keys_of(slot)  # بخش‌هایی که چند کلید را پشتیبانی نمی‌کنند از اولین کلید استفاده می‌کنند
    k = ks[0] if ks else ""
    if required and not k and slot.get("provider") != "custom":
        raise AIError("کلید API برای این بخش وارد نشده است.")
    return k


def _base(slot):
    _, info = _prov(slot)
    base = (slot.get("base_url") or "").strip().rstrip("/") or info["base"]
    if not base:
        raise AIError("برای حالت «آدرس دلخواه» باید آدرس API (base URL) را وارد کنی.")
    return base


def _model(slot, kind):
    _, info = _prov(slot)
    m = (slot.get("model") or "").strip() or info.get(kind, "")
    if not m:
        raise AIError("نام مدل را وارد کن.")
    return m


def _headers(slot, key):
    h = {"Content-Type": "application/json", "User-Agent": "ig-assistant/2.0"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    if slot.get("provider") == "openrouter":
        h["X-Title"] = "Instagram Assistant"
    return h


def _http_error(r):
    return AIError(f"HTTP {r.status_code}: {r.text[:300]}")


def _post(url, headers, payload, tries=2):
    last = None
    for i in range(tries):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            last = AIError(f"خطای شبکه: {type(e).__name__}")
            time.sleep(2)
            continue
        if r.status_code in (429, 500, 502, 503, 504) and i + 1 < tries:
            last = _http_error(r)
            time.sleep(3 * (i + 1))
            continue
        if r.status_code >= 400:
            raise _http_error(r)
        return r
    raise last or AIError("خطای نامشخص")


def _download(url):
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        return r.content
    except requests.RequestException as e:
        raise AIError(f"دانلود تصویر ناموفق بود: {type(e).__name__}") from e


def _data_url_bytes(url):
    m = re.match(r"data:[^;]+;base64,(.+)", url, re.S)
    return base64.b64decode(m.group(1)) if m else _download(url)


def extract_json(text):
    """از جواب مدل یک JSON بیرون می‌کشد (حتی اگر داخل ``` باشد)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except json.JSONDecodeError:
            return None
    return None


# ---------- متن ----------
def chat(slot, messages):
    """messages به فرمت OpenAI. متن جواب را برمی‌گرداند."""
    key = _key(slot)
    url = _base(slot) + "/chat/completions"
    payload = {"model": _model(slot, "chat"), "messages": messages}
    data = _post(url, _headers(slot, key), payload).json()
    try:
        content = data["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError) as e:
        raise AIError(f"جواب نامعتبر از مدل: {str(data)[:200]}") from e
    if isinstance(content, list):
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return (content or "").strip()


def image_message(text, image_bytes, mime="image/jpeg"):
    url = f"data:{mime};base64," + base64.b64encode(image_bytes).decode()
    return {"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": url}},
    ]}


# ---------- تصویر ----------
def _interaction_media(data, kind):
    found = None
    for step in data.get("steps", []) or []:
        if step.get("type") == "model_output":
            for c in step.get("content", []) or []:
                if c.get("type") == kind and c.get("data"):
                    found = base64.b64decode(c["data"])
    return found


def _gemini_headers(key):
    return {"Content-Type": "application/json", "x-goog-api-key": key, "User-Agent": "ig-assistant/2.0"}


def _gemini_image(slot, prompt, aspect):
    key = _key(slot)
    model = _model(slot, "image")
    try:
        data = _post(f"{GEMINI_ROOT}/v1beta/interactions", _gemini_headers(key), {
            "model": model, "input": prompt,
            "response_format": {"type": "image", "mime_type": "image/jpeg", "aspect_ratio": aspect},
        }).json()
        img = _interaction_media(data, "image")
        if img:
            return img
    except AIError as e:
        if "HTTP 401" in str(e) or "HTTP 403" in str(e) or "HTTP 429" in str(e):
            raise
    data = _post(f"{GEMINI_ROOT}/v1beta/models/{model}:generateContent", _gemini_headers(key), {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }).json()
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"])
    raise AIError("Gemini تصویری برنگرداند.")


def _openrouter_image(slot, prompt):
    key = _key(slot)
    data = _post(_base(slot) + "/chat/completions", _headers(slot, key), {
        "model": _model(slot, "image"),
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
    }).json()
    try:
        images = data["choices"][0]["message"].get("images") or []
        return _data_url_bytes(images[0]["image_url"]["url"])
    except (KeyError, IndexError, TypeError) as e:
        raise AIError("OpenRouter تصویری برنگرداند؛ مدل باید خروجی تصویر پشتیبانی کند.") from e


def _openai_image(slot, prompt, aspect):
    key = _key(slot)
    size = "1024x1024" if aspect == "1:1" else "1024x1536"
    data = _post(_base(slot) + "/images/generations", _headers(slot, key), {
        "model": _model(slot, "image"), "prompt": prompt, "size": size, "n": 1,
    }).json()
    try:
        item = data["data"][0]
    except (KeyError, IndexError, TypeError) as e:
        raise AIError(f"جواب نامعتبر از API تصویر: {str(data)[:200]}") from e
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        return _download(item["url"])
    raise AIError("API تصویری برنگرداند.")


def image(slot, prompt, aspect="1:1"):
    """aspect: 1:1 | 4:5 | 9:16 ؛ بایت‌های تصویر را برمی‌گرداند."""
    p, _ = _prov(slot)
    if p == "gemini":
        return _gemini_image(slot, prompt, aspect)
    if p == "openrouter":
        return _openrouter_image(slot, prompt)
    if p == "deapi":
        return _deapi_image(slot, prompt, aspect)
    return _openai_image(slot, prompt, aspect)



# ---------- deAPI (تصویر و ویدیو) ----------
def _deapi_root(slot):
    return (slot.get("base_url") or "").strip().rstrip("/") or DEAPI_ROOT


def _deapi_headers(key):
    return {"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "ig-assistant/2.0"}


def _deapi_error(r):
    try:
        j = r.json()
        parts = [str(j.get("message", ""))]
        for field, msgs in (j.get("errors") or {}).items():
            parts.append(f"{field}: {' '.join(msgs) if isinstance(msgs, list) else msgs}")
        text = " | ".join(p for p in parts if p)
    except ValueError:
        text = r.text[:300]
    msg = f"deAPI HTTP {r.status_code}: {text[:400]}"
    if r.status_code in KEY_STATUSES:
        return KeyProblem(msg, r.status_code)
    return AIError(msg)


def _deapi_model_info(slot, key, slug, inference_type):
    """محدودیت‌ها و پیش‌فرض‌های مدل را از خود deAPI می‌گیرد (اندازه، تعداد فریم، fps، steps)."""
    try:
        r = requests.get(
            _deapi_root(slot) + "/api/v2/models", headers=_deapi_headers(key), timeout=30,
            params={"limit": 100, "filter[inference_types]": inference_type},
        )
        if r.status_code in (401, 403):
            raise _deapi_error(r)
        for m in r.json().get("data", []):
            if m.get("slug") == slug:
                return m.get("info") or {}
    except AIError:
        raise
    except (requests.RequestException, ValueError, AttributeError):
        pass
    return {}


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _deapi_dims(info, aw, ah, long_side):
    """اندازه‌ی مجاز نزدیک به نسبت aw:ah با رعایت محدودیت‌های مدل."""
    lim = info.get("limits") or {}
    step = int(_num(lim.get("dimension_multiple")) or _num(lim.get("resolution_step")) or 32)
    portrait = ah >= aw
    h = float(min(long_side, _num(lim.get("max_height"), long_side) if portrait else long_side))
    w = h * aw / ah
    max_w = _num(lim.get("max_width"))
    if max_w and w > max_w:
        w, h = max_w, max_w * ah / aw
    max_px = _num(lim.get("max_pixels"))
    if max_px and w * h > max_px:
        k = (max_px / (w * h)) ** 0.5
        w, h = w * k, h * k
    w, h = max(step, int(w) // step * step), max(step, int(h) // step * step)
    min_w, min_h = _num(lim.get("min_width"), 0), _num(lim.get("min_height"), 0)
    while w < min_w:
        w += step
    while h < min_h:
        h += step
    return int(w), int(h)


def _clamp(v, lo, hi):
    if lo is not None:
        v = max(v, lo)
    if hi is not None:
        v = min(v, hi)
    return v


def _deapi_fields(info, model, prompt, w, h):
    lim, dfl = info.get("limits") or {}, info.get("defaults") or {}
    steps = int(_clamp(_num(dfl.get("steps")) or _num(lim.get("min_steps")) or 1,
                       _num(lim.get("min_steps")), _num(lim.get("max_steps"))))
    guidance = _clamp(_num(dfl.get("guidance")) or 5.0, _num(lim.get("min_guidance")), _num(lim.get("max_guidance")))
    return {"prompt": prompt, "model": model, "width": w, "height": h, "seed": random.randint(1, 2**31 - 1),
            "steps": steps, "guidance": guidance}


def _deapi_submit(slot, key, path, fields, files=None):
    url = _deapi_root(slot) + path
    r = None
    for attempt in range(2):
        try:
            if files:
                r = requests.post(url, headers=_deapi_headers(key), data={k: (("1" if v else "0") if isinstance(v, bool) else str(v)) for k, v in fields.items()},
                                  files=files, timeout=TIMEOUT)
            else:
                r = requests.post(url, headers=_deapi_headers(key), json=fields, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise AIError(f"خطای شبکه با deAPI: {type(e).__name__}") from e
        if r.status_code == 429 and attempt == 0:
            time.sleep(min(30, _num(r.headers.get("Retry-After"), 5)))
            continue
        break
    if r.status_code >= 400:
        raise _deapi_error(r)
    try:
        return r.json()["data"]["request_id"]
    except (KeyError, TypeError, ValueError) as e:
        raise AIError(f"جواب نامعتبر از deAPI: {r.text[:200]}") from e


def _deapi_wait(slot, key, request_id):
    """تا آماده شدن کار صبر می‌کند و فایل نتیجه را برمی‌گرداند."""
    deadline = time.time() + DEAPI_TIMEOUT
    net_errors = 0
    while time.time() < deadline:
        try:
            r = requests.get(f"{_deapi_root(slot)}/api/v2/jobs/{request_id}", headers=_deapi_headers(key), timeout=30)
            net_errors = 0
        except requests.RequestException:
            net_errors += 1
            if net_errors > 5:
                raise AIError("ارتباط با deAPI هنگام انتظار قطع شد.")
            time.sleep(DEAPI_POLL_SECONDS)
            continue
        if r.status_code >= 400 and r.status_code != 429:
            raise _deapi_error(r)
        data = (r.json() or {}).get("data") or {} if r.status_code < 400 else {}
        status = data.get("status")
        if status == "done":
            url = data.get("result_url")
            if not url:
                raise AIError("deAPI نتیجه‌ای (result_url) برنگرداند.")
            return _download_big(url)
        if status == "error":
            hint = " (می‌شود دوباره امتحان کرد)" if data.get("retryable") else ""
            raise AIError(f"deAPI: ساخت ناموفق بود — {data.get('error_code') or ''} {data.get('error_reason') or ''}{hint}".strip())
        time.sleep(DEAPI_POLL_SECONDS)
    raise AIError("deAPI بیش از ۱۵ دقیقه طول کشید؛ بعداً دوباره امتحان کن.")


def _download_big(url):
    try:
        r = requests.get(url, timeout=180)
        r.raise_for_status()
        return r.content
    except requests.RequestException as e:
        raise AIError(f"دانلود نتیجه از deAPI ناموفق بود: {type(e).__name__}") from e


def _deapi_ping(slot, key):
    """تست سریع و رایگان کلید: فهرست مدل‌ها را می‌خواند. فقط مشکل خودِ کلید را خطا می‌دهد."""
    try:
        r = requests.get(_deapi_root(slot) + "/api/v2/models", headers=_deapi_headers(key), timeout=20,
                         params={"limit": 1})
    except requests.RequestException:
        return  # مشکل شبکه است نه کلید؛ بگذار خود درخواست اصلی تصمیم بگیرد
    if r.status_code in KEY_STATUSES:
        raise _deapi_error(r)


def deapi_run(slot, fn):
    """fn(key) را با اولین کلید سالم اجرا می‌کند. هر کلید اول تست می‌شود؛ اگر معلق/نامعتبر/بی‌اعتبار بود
    (مثلاً «HTTP 403: Client account is suspended») کنار گذاشته می‌شود و کلید بعدی امتحان می‌شود."""
    keys = keys_of(slot)
    if not keys:
        raise AIError("کلید API برای این بخش وارد نشده است.")
    live = [k for k in keys if not key_problem(k)]
    cand = live or keys  # اگر همه خراب علامت خورده‌اند، همه را دوباره امتحان کن (شاید شارژ شده باشند)
    fails = [f"کلید {keys.index(k) + 1} ({mask_key(k)}): {key_problem(k)}" for k in keys if k not in cand]
    for k in cand:
        n = keys.index(k) + 1
        try:
            if time.time() - _ok_keys.get(k, 0) > KEY_OK_TTL:
                _deapi_ping(slot, k)
            out = fn(k)
        except KeyProblem as e:
            _mark_bad(k, str(e))
            fails.append(f"کلید {n} ({mask_key(k)}): {e}")
            log.warning("deAPI key %d (%s) rejected: %s", n, mask_key(k), e)
            continue
        _mark_ok(k)
        if len(keys) > 1:
            log.info("deAPI: با کلید %d از %d ساخته شد", n, len(keys))
        return out
    if len(keys) == 1:
        raise AIError(fails[-1].split(": ", 1)[1])
    raise AIError(f"هیچ‌کدام از {len(keys)} کلید deAPI سالم نبود → " + " | ".join(fails))


def _deapi_image(slot, prompt, aspect):
    return deapi_run(slot, lambda key: _deapi_image_with(slot, key, prompt, aspect))


def _deapi_image_with(slot, key, prompt, aspect):
    model = _model(slot, "image")
    info = _deapi_model_info(slot, key, model, "txt2img")
    aw, ah = (int(x) for x in aspect.split(":"))
    w, h = _deapi_dims(info, aw, ah, 1024)
    rid = _deapi_submit(slot, key, "/api/v2/images/generations", _deapi_fields(info, model, prompt, w, h))
    return _deapi_wait(slot, key, rid)


AUDIO_FLAG_KEYS = ("generate_audio", "with_audio", "audio", "enable_audio", "sound")


def video(slot, prompt, image_bytes=None, seconds=5, with_audio=None):
    """ویدیوی ۹:۱۶ می‌سازد. اگر image_bytes داده شود همان عکس زنده می‌شود (img2video)، وگرنه متن به ویدیو.
    with_audio: اگر مدل deAPI گزینه‌ی ساخت صدا داشته باشد (مثل LTX-2)، روشن/خاموشش می‌کند.
    با چند کلید: اولین کلید سالم استفاده می‌شود (deapi_run)."""
    return deapi_run(slot, lambda key: _video_with(slot, key, prompt, image_bytes, seconds, with_audio))


def _video_with(slot, key, prompt, image_bytes, seconds, with_audio):
    model = _model(slot, "video")
    itype = "img2video" if image_bytes else "txt2video"
    info = _deapi_model_info(slot, key, model, itype)
    lim, dfl = info.get("limits") or {}, info.get("defaults") or {}
    w, h = _deapi_dims(info, 9, 16, 1024)
    min_fps, max_fps = _num(lim.get("min_fps")), _num(lim.get("max_fps"))
    fps = int(min_fps) if (min_fps and min_fps == max_fps) else int(_num(dfl.get("fps")) or min_fps or 24)
    lo, hi = _num(lim.get("min_frames"), 24), _num(lim.get("max_frames"), 241)
    frames = int(_clamp(round(float(seconds) * fps), lo, hi))
    if (int(lo) - 1) % 8 == 0 and frames > 1:  # بعضی مدل‌ها (LTX) تعداد فریم 8k+1 می‌خواهند
        frames = int(_clamp(round((frames - 1) / 8) * 8 + 1, lo, hi))
    fields = _deapi_fields(info, model, prompt, w, h)
    fields.update(frames=frames, fps=fps)
    if with_audio is not None:  # فقط اگر خود مدل چنین پارامتری اعلام کرده باشد فرستاده می‌شود
        for k in AUDIO_FLAG_KEYS:
            if k in dfl or k in lim:
                fields[k] = bool(with_audio)
                break
    if image_bytes:
        rid = _deapi_submit(slot, key, "/api/v2/videos/animations", fields,
                            files={"first_frame_image": ("frame.jpg", image_bytes, "image/jpeg")})
    else:
        rid = _deapi_submit(slot, key, "/api/v2/videos/generations", fields)
    return _deapi_wait(slot, key, rid)


def _deapi_check_one(slot, key, inference_type):
    model = _model(slot, "video" if inference_type.endswith("video") else "image")
    try:
        r = requests.get(_deapi_root(slot) + "/api/v2/models", headers=_deapi_headers(key), timeout=30,
                         params={"limit": 100, "filter[inference_types]": inference_type})
    except requests.RequestException as e:
        raise AIError(f"خطای شبکه با deAPI: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise _deapi_error(r)
    slugs = [m.get("slug") for m in r.json().get("data", [])]
    if model not in slugs:
        raise AIError(f"کلید درست است ولی مدل «{model}» در deAPI پیدا نشد. نمونه‌ی مدل‌های موجود: {', '.join(slugs[:6])}")
    return model


def deapi_check(slot, inference_type):
    """همه‌ی کلیدها را جداگانه تست می‌کند. (مدل، [(شماره، کلید، سالم؟، پیام)]) برمی‌گرداند؛
    اگر هیچ کلید سالمی نباشد AIError."""
    keys = keys_of(slot)
    if not keys:
        raise AIError("کلید API برای این بخش وارد نشده است.")
    rows, model = [], None
    for i, k in enumerate(keys, 1):
        try:
            model = _deapi_check_one(slot, k, inference_type)
            _mark_ok(k)
            rows.append((i, k, True, "سالم"))
        except KeyProblem as e:
            _mark_bad(k, str(e))
            rows.append((i, k, False, str(e)))
        except AIError as e:
            rows.append((i, k, False, str(e)))
    if not any(r[2] for r in rows):
        raise AIError(" | ".join(f"کلید {i} ({mask_key(k)}): {m}" for i, k, _, m in rows))
    return model, rows


# ---------- صدا ----------
def _pcm_to_wav(pcm, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _gemini_tts(slot, text):
    key = _key(slot)
    model = _model(slot, "voice")
    voice = (slot.get("voice") or "").strip() or PROVIDERS["gemini"]["voice_name"]
    try:
        data = _post(f"{GEMINI_ROOT}/v1beta/interactions", _gemini_headers(key), {
            "model": model,
            "input": [{"type": "user_input", "content": [{"type": "text", "text": text}]}],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": voice}]},
        }).json()
        audio = _interaction_media(data, "audio")
        if audio:
            return audio, "wav"
    except AIError as e:
        if "HTTP 401" in str(e) or "HTTP 403" in str(e) or "HTTP 429" in str(e):
            raise
    data = _post(f"{GEMINI_ROOT}/v1beta/models/{model}:generateContent", _gemini_headers(key), {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }).json()
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                raw = base64.b64decode(blob["data"])
                return (raw if raw[:4] == b"RIFF" else _pcm_to_wav(raw)), "wav"
    raise AIError("Gemini صدایی برنگرداند.")


def _elevenlabs_tts(slot, text):
    key = _key(slot)
    voice_id = (slot.get("voice") or "").strip() or PROVIDERS["elevenlabs"]["voice_name"]
    r = _post(
        f"{_base(slot)}/text-to-speech/{voice_id}?output_format=mp3_44100_128",
        {"Content-Type": "application/json", "xi-api-key": key, "User-Agent": "ig-assistant/2.0"},
        {"text": text, "model_id": _model(slot, "voice")},
    )
    return r.content, "mp3"


def tts(slot, text):
    """(بایت‌های صدا، پسوند) را برمی‌گرداند."""
    p, info = _prov(slot)
    if p == "gemini":
        return _gemini_tts(slot, text)
    if p == "elevenlabs":
        return _elevenlabs_tts(slot, text)
    key = _key(slot)
    voice = (slot.get("voice") or "").strip() or info.get("voice_name", "alloy")
    r = _post(_base(slot) + "/audio/speech", _headers(slot, key), {
        "model": (slot.get("model") or "").strip() or info.get("voice") or "tts-1",
        "input": text, "voice": voice, "response_format": "mp3",
    })
    if "json" in r.headers.get("content-type", ""):
        raise AIError(f"جواب نامعتبر از API صدا: {r.text[:200]}")
    return r.content, "mp3"


# ---------- تست ----------
def _rows_text(rows):
    ok = sum(1 for r in rows if r[2])
    head = f"{ok} کلید سالم از {len(rows)}." if len(rows) > 1 else "کلید سالم است."
    if len(rows) == 1:
        return head
    return head + " " + " | ".join(f"کلید {i} ({mask_key(k)}): {'✓ سالم' if good else '✗ ' + msg}" for i, k, good, msg in rows)


def test_slot(name, slot):
    try:
        if name in ("chat", "caption"):
            out = chat(slot, [{"role": "user", "content": "یک جمله‌ی خیلی کوتاه فارسی بگو."}])
            return True, f"اتصال موفق بود. جواب مدل: {out or '(خالی)'}"
        if name == "video":
            model, rows = deapi_check(slot, "txt2video")
            return True, f"مدل «{model}» موجود است. {_rows_text(rows)} (برای کم نکردن اعتبار، ویدیوی واقعی ساخته نشد.)"
        if name == "image" and slot.get("provider") == "deapi":
            model, rows = deapi_check(slot, "txt2img")
            return True, f"مدل «{model}» موجود است. {_rows_text(rows)}"
        if name == "image":
            img = image(slot, "a small red apple on a white table, minimal, no text", "1:1")
            return True, f"اتصال موفق بود. تصویر ساخته شد ({len(img) // 1024} کیلوبایت)."
        if name == "voice":
            audio, ext = tts(slot, "سلام، این یک تست صدا است.")
            return True, f"اتصال موفق بود. صدا ساخته شد ({len(audio) // 1024} کیلوبایت، {ext})."
    except AIError as e:
        return False, f"خطا: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"خطا: {type(e).__name__}: {e}"
    return False, "بخش نامعتبر"
