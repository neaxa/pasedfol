"""ساخت محتوا: کپشن، تصویر، ویس و ویدیو برای هر پست."""
import hashlib
import json
import logging
import os

import ai
import media
import store

log = logging.getLogger("content")

KIND_HINT = {
    "post": "پست فید: کپشن کامل و ارزشمند، با شروع جذاب (خط اول قلاب باشد) و یک دعوت به اقدام در پایان.",
    "story": "استوری: خیلی کوتاه، صمیمی و سریع؛ فقط یکی دو جمله.",
    "reel": "ریلز: خط اول یک قلاب قوی برای نگه‌داشتن مخاطب، کپشن کوتاه و پرانرژی.",
}
LANG = {"fa": "فارسی روان و طبیعی (نه ترجمه‌ای)", "en": "English"}


def _ensure_dir(pid):
    return store.post_dir(pid)


def paths(pid):
    d = _ensure_dir(pid)
    return {
        "image": os.path.join(d, "image.jpg"),
        "video": os.path.join(d, "video.mp4"),
        "clip": os.path.join(d, "clip.mp4"),
        "audio_mp3": os.path.join(d, "audio.mp3"),
        "audio_wav": os.path.join(d, "audio.wav"),
        "music": os.path.join(d, "music.audio"),  # موزیک پس‌زمینه‌ای که کاربر آپلود کرده (هر فرمتی)
    }


def music_path(pid):
    m = paths(pid)["music"]
    return m if os.path.exists(m) else None


def save_music(pid, data):
    """موزیک پس‌زمینه‌ی این پست را ذخیره می‌کند (mp3/m4a/wav/...)؛ اگر فایل صوتی معتبر نباشد MediaError."""
    pa = paths(pid)
    tmp = pa["music"] + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
    info = media.probe(tmp)
    if not info["audio"] or (info["duration"] or 0) < 1:
        _rm(tmp)
        raise media.MediaError("فایل موزیک معتبر نیست (صدای قابل خواندن ندارد).")
    os.replace(tmp, pa["music"])
    _rm(pa["video"])  # ویدیو باید با موزیک جدید دوباره ساخته شود


def audio_path(pid):
    p = paths(pid)
    for k in ("audio_mp3", "audio_wav"):
        if os.path.exists(p[k]):
            return p[k]
    return None


def _rm(*files):
    for f in files:
        try:
            os.remove(f)
        except OSError:
            pass


def _brand_block(cfg):
    b = cfg["brand"]
    lines = []
    if b.get("niche"):
        lines.append(f"موضوع و حوزه‌ی پیج: {b['niche']}")
    if b.get("tone"):
        lines.append(f"لحن برند: {b['tone']}")
    if b.get("extra"):
        lines.append(f"توضیحات بیشتر: {b['extra']}")
    return "\n".join(lines) or "(توضیحی برای پیج وارد نشده؛ بر اساس موضوع پست بنویس.)"


def recent_topics(n=15):
    rows = store.post_list(limit=n, where="topic != ''")
    return [r["topic"] for r in rows]


def suggest_topic(cfg, recent):
    slot = cfg["ai"]["caption"]
    avoid = "\n".join(f"- {t}" for t in recent) or "(چیزی نیست)"
    msg = (
        f"{_brand_block(cfg)}\n\nموضوع‌هایی که اخیراً کار شده و نباید تکرار شوند:\n{avoid}\n\n"
        "یک ایده‌ی تازه و جذاب برای پست بعدی این پیج بده. فقط خودِ موضوع را در یک خط بنویس، بدون توضیح."
    )
    return ai.chat(slot, [{"role": "user", "content": msg}]).strip().strip('"«»')[:200]


CLIP_SECONDS = 10  # طول هر کلیپ در فیلم بلند


def long_parts(long_secs):
    """فیلم ۲۰ ثانیه‌ای = ۲ کلیپ، ۳۰ ثانیه‌ای = ۳ کلیپ ۱۰ ثانیه‌ای."""
    return max(2, min(3, int(long_secs) // CLIP_SECONDS)) if long_secs else 0


def parse_storyboard(text):
    try:
        d = json.loads(text or "")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def write_content(cfg, topic, kind, want_voice, want_image_prompt, image_bytes=None, keep_caption=None, video=False,
                  brief="", long_secs=0):
    slot = cfg["ai"]["caption"]
    lang = LANG.get(cfg.get("language"), LANG["fa"])
    keys = ['"caption": متن کپشن (حداکثر ۱۵۰۰ کاراکتر، بدون هشتگ' + (" — همان کپشن موجود را بدون تغییر بنویس" if keep_caption else "") + ")",
            '"hashtags": آرایه‌ی ۶ تا ۱۰ هشتگ مرتبط (بدون علامت #)']
    if want_voice:
        if long_secs:
            lo = int(long_secs) * 2 - 6
            words = f"{lo} تا {lo + 10} کلمه‌ای (حدود {int(long_secs) - 3} ثانیه گفتار پیوسته، یک راوی با یک صدا از اول تا آخر)"
        else:
            words = "۱۰ تا ۲۰ کلمه‌ای (حدود ۵ تا ۸ ثانیه)" if video else "۲۰ تا ۴۵ کلمه‌ای"
        keys.append(f'"voice_script": متن گفتاری {words} برای ویس؛ طبیعی و خودمانی، بدون ایموجی و هشتگ، قابل خواندن با صدای بلند')
    n_parts = long_parts(long_secs) if video else 0
    if want_image_prompt and n_parts:
        keys.append('"character": توصیف دقیق و ثابت کاراکتر(های) اصلی به انگلیسی (سن، چهره، مو، لباس، رنگ‌ها)؛ در همه‌ی صحنه‌ها عیناً همین کاراکتر و همین ظاهر تکرار می‌شود')
        keys.append(f'"scenes": آرایه‌ی دقیقاً {n_parts} رشته؛ هر رشته توصیف سینمایی یک صحنه‌ی {CLIP_SECONDS} ثانیه‌ای به انگلیسی '
                    '(حرکت سوژه، حرکت دوربین، نور، مکان). صحنه‌ها یک داستان پیوسته‌اند: هر صحنه ادامه‌ی قبلی است، '
                    'با همان کاراکتر، همان لباس و همان فضا؛ هیچ متن نوشتاری داخل ویدیو نباشد')
    elif want_image_prompt and video:
        keys.append('"image_prompt": توصیف سینمایی صحنه‌ی ویدیوی ۵ ثانیه‌ای به انگلیسی (سوژه، حرکت سوژه، حرکت دوربین، نور، سبک)؛ هیچ متن نوشتاری داخل ویدیو نباشد')
    elif want_image_prompt:
        keys.append('"image_prompt": توصیف دقیق و سینمایی تصویر به انگلیسی (سوژه، نور، سبک، ترکیب‌بندی)؛ هیچ متن نوشتاری داخل تصویر نباشد')
    system = (
        "تو مدیر شبکه‌های اجتماعی حرفه‌ای این پیج اینستاگرام هستی.\n"
        f"{_brand_block(cfg)}\nزبان خروجی: {lang}\n"
        f"نوع محتوا: {KIND_HINT[kind]}\n\n"
        "فقط و فقط یک JSON معتبر برگردان (بدون توضیح و بدون ```) با این کلیدها:\n" + "\n".join(f"- {k}" for k in keys)
    )
    user_text = f"موضوع: {topic}" if topic else "موضوع را از روی تصویر پیدا کن."
    if brief:
        user_text += ("\n\nدستور استراتژی رشد پیج (دقیقاً رعایت کن؛ قلاب خط اول، زاویه و سبک همین باشد):\n" + brief[:1500])
    if keep_caption:
        user_text += f"\n\nکپشن موجود: {keep_caption}"
    msgs = [{"role": "system", "content": system}]
    if image_bytes:
        msgs.append(ai.image_message(user_text + "\n(تصویر پیوست همان تصویری است که منتشر می‌شود.)", image_bytes))
    else:
        msgs.append({"role": "user", "content": user_text})
    try:
        raw = ai.chat(slot, msgs)
    except ai.AIError:
        if not image_bytes:
            raise
        # مدلی که تصویر نمی‌فهمد: بدون تصویر دوباره تلاش کن
        msgs[-1] = {"role": "user", "content": user_text or "یک پست جذاب بنویس."}
        raw = ai.chat(slot, msgs)
    data = ai.extract_json(raw)
    if not isinstance(data, dict):
        data = {"caption": raw}
    caption = (keep_caption or str(data.get("caption", ""))).strip()
    tags = data.get("hashtags") or []
    if isinstance(tags, str):
        tags = tags.replace("#", " ").split()
    tags = [str(t).strip().lstrip("#").replace(" ", "_") for t in tags if str(t).strip()][:12]
    if tags and not keep_caption:
        caption += "\n\n" + " ".join("#" + t for t in tags)
    out = {
        "caption": caption[:2200],
        "voice_text": str(data.get("voice_script", "")).strip(),
        "image_prompt": str(data.get("image_prompt", "")).strip(),
        "storyboard": "",
    }
    if n_parts and want_image_prompt:
        scenes = data.get("scenes") or []
        if isinstance(scenes, str):
            scenes = [scenes]
        scenes = [str(x).strip() for x in scenes if str(x).strip()]
        if not scenes:
            scenes = [str(data.get("image_prompt", "")).strip() or topic]
        while len(scenes) < n_parts:  # AI کمتر از تعداد خواسته‌شده داد: آخرین صحنه ادامه پیدا می‌کند
            scenes.append(scenes[-1] + " (continue the same story, same character and setting)")
        scenes = scenes[:n_parts]
        out["storyboard"] = json.dumps({"character": str(data.get("character", "")).strip()[:600], "scenes": scenes},
                                       ensure_ascii=False)
        out["image_prompt"] = scenes[0]
    return out


# ---------- مراحل ----------
def _step(pid, text):
    store.post_update(pid, step=text)


def gen_text(pid, cfg, keep_caption=None):
    p = store.post_get(pid)
    topic = p["topic"]
    if not topic and not p["user_image"]:
        _step(pid, "پیدا کردن ایده‌ی موضوع…")
        topic = suggest_topic(cfg, recent_topics())
        store.post_update(pid, topic=topic)
    _step(pid, "نوشتن کپشن…")
    img_bytes = None
    if p["user_image"] and os.path.exists(paths(pid)["image"]):
        with open(paths(pid)["image"], "rb") as f:
            img_bytes = f.read()
    want_voice = bool(p["want_voice"]) and p["kind"] != "post"
    is_video = p["out_type"] == "video"
    want_prompt = (bool(p["want_image"]) and not p["user_image"]) or (is_video and not p["user_video"])
    long_secs = int(p.get("long_secs") or 0) if (is_video and not p["user_video"]) else 0
    out = write_content(cfg, topic, p["kind"], want_voice, want_prompt, img_bytes, keep_caption, video=is_video,
                        brief=p.get("brief") or "", long_secs=long_secs)
    fields = {"caption": out["caption"]}
    if want_voice:
        fields["voice_text"] = out["voice_text"] or out["caption"][:300]
    if want_prompt:
        fields["image_prompt"] = out["image_prompt"] or topic
        if long_secs:
            fields["storyboard"] = out["storyboard"]
    store.post_update(pid, **fields)


def save_user_video(pid, data):
    """ویدیوی آپلودی کاربر را به‌عنوان کلیپ این پست ذخیره می‌کند و فریم اولش را برای پیش‌نمایش/فهم AI می‌گیرد."""
    pa = paths(pid)
    with open(pa["clip"], "wb") as f:
        f.write(data)
    try:
        info = media.probe(pa["clip"])
        if not info["video"] or info["duration"] <= 0:
            raise media.MediaError("فایل ویدیو معتبر نیست.")
        tmp = pa["image"] + ".frame.jpg"
        media.extract_frame(pa["clip"], tmp)
        with open(tmp, "rb") as f:
            media.save_final_image(f.read(), "story", pa["image"])
        _rm(tmp)
    except Exception:
        _rm(pa["clip"])
        raise
    _rm(pa["video"])  # نسخه‌ی قبلی منتشرشدنی باید دوباره ساخته شود
    store.post_update(pid, user_video=1, user_image=1, out_type="video")


def gen_image(pid, cfg):
    p = store.post_get(pid)
    prompt = (p["image_prompt"] or p["topic"] or "").strip()
    if not prompt:
        raise ai.AIError("توصیف تصویر خالی است؛ آن را بنویس یا اول متن بساز.")
    _step(pid, "ساخت تصویر…")
    aspect = "4:5" if p["kind"] == "post" else "9:16"
    data = ai.image(cfg["ai"]["image"], prompt, aspect)
    media.save_final_image(data, p["kind"], paths(pid)["image"])
    store.post_update(pid, user_image=0)


def gen_video(pid, cfg):
    """ویدیو با AI (deAPI): اگر کاربر عکس داده باشد همان زنده می‌شود، وگرنه از روی متن ساخته می‌شود."""
    p = store.post_get(pid)
    if p["user_video"]:  # ویدیوی خود کاربر است؛ فقط دوباره آماده‌سازی می‌شود، با AI عوض نمی‌شود
        if not os.path.exists(paths(pid)["clip"]):
            raise media.MediaError("ویدیوی آپلودشده پیدا نشد؛ دوباره آپلودش کن.")
        return
    if int(p.get("long_secs") or 0):
        return gen_long_video(pid, cfg, p)
    prompt = (p["image_prompt"] or p["topic"] or "").strip()
    if not prompt:
        raise ai.AIError("توصیف ویدیو خالی است؛ آن را بنویس یا اول متن بساز.")
    pa = paths(pid)
    img_bytes = None
    if p["user_image"] and os.path.exists(pa["image"]):
        with open(pa["image"], "rb") as f:
            img_bytes = f.read()
    _step(pid, "ساخت ویدیو با AI (چند دقیقه طول می‌کشد)…")
    seconds = cfg["ai"]["video"].get("seconds") or 5
    data = ai.video(cfg["ai"]["video"], prompt, img_bytes, seconds, with_audio=bool(p.get("sound", 1)))
    with open(pa["clip"], "wb") as f:
        f.write(data)
    if not p["user_image"]:  # فریم اول برای پیش‌نمایش
        tmp = pa["image"] + ".frame.jpg"
        media.extract_frame(pa["clip"], tmp)
        with open(tmp, "rb") as f:
            media.save_final_image(f.read(), "story", pa["image"])
        _rm(tmp)


def gen_long_video(pid, cfg, p):
    """فیلم بلند: چند کلیپ ۱۰ ثانیه‌ای با صدا. کاراکتر با دو روش ثابت می‌ماند:
    ۱) توصیف یکسان کاراکتر در پرامپت همه‌ی کلیپ‌ها، ۲) هر کلیپ از آخرین فریم کلیپ قبلی شروع می‌شود.
    بعد همه به هم چسبانده می‌شوند. کلیپ‌های ساخته‌شده نگه داشته می‌شوند تا اگر وسط کار خطا شد، دوباره پول‌شان خرج نشود."""
    pa = paths(pid)
    d = os.path.dirname(pa["clip"])
    n = long_parts(p["long_secs"])
    sb = parse_storyboard(p.get("storyboard"))
    scenes = [s for s in (sb.get("scenes") or []) if str(s).strip()]
    if not scenes:
        base = (p["image_prompt"] or p["topic"] or "").strip()
        if not base:
            raise ai.AIError("توصیف صحنه‌ها خالی است؛ اول متن بساز.")
        scenes = [base]
    while len(scenes) < n:
        scenes.append(scenes[-1] + " (continue the same story, same character and setting)")
    scenes = scenes[:n]
    character = str(sb.get("character", "")).strip()
    sig = hashlib.sha1(json.dumps([character, scenes, bool(p.get("sound", 1))],
                                  ensure_ascii=False).encode()).hexdigest()[:12]
    sig_file = os.path.join(d, "segments.sig")
    seg = [os.path.join(d, f"seg{i + 1}.mp4") for i in range(n)]
    try:
        same = open(sig_file).read().strip() == sig
    except OSError:
        same = False
    if not same:
        _rm(*seg)
    seconds = cfg["ai"]["video"].get("seconds") or CLIP_SECONDS
    seconds = CLIP_SECONDS if seconds < CLIP_SECONDS else seconds
    first_img = None
    if p["user_image"] and os.path.exists(pa["image"]):
        with open(pa["image"], "rb") as f:
            first_img = f.read()
    for i in range(n):
        if os.path.exists(seg[i]) and (media.probe(seg[i])["duration"] or 0) > 1:
            continue
        _step(pid, f"ساخت کلیپ {i + 1} از {n} (هر کلیپ چند دقیقه طول می‌کشد)…")
        img = first_img
        if i > 0:
            tmp = os.path.join(d, f"last{i}.jpg")
            media.extract_last_frame(seg[i - 1], tmp)
            with open(tmp, "rb") as f:
                img = f.read()
            _rm(tmp)
        prompt = (f"{character}. " if character else "") + scenes[i]
        if i > 0:
            prompt += " Continue seamlessly from the previous shot with the exact same character, outfit and setting."
        data = ai.video(cfg["ai"]["video"], prompt, img, CLIP_SECONDS, with_audio=bool(p.get("sound", 1)))
        with open(seg[i], "wb") as f:
            f.write(data)
        with open(sig_file, "w") as f:
            f.write(sig)
    _step(pid, "چسباندن کلیپ‌ها…")
    media.concat_clips(seg, pa["clip"], sound=bool(p.get("sound", 1)))
    if not p["user_image"]:  # فریم اول برای پیش‌نمایش (عکس خود کاربر دست‌نخورده می‌ماند)
        tmp = pa["image"] + ".frame.jpg"
        media.extract_frame(pa["clip"], tmp)
        with open(tmp, "rb") as f:
            media.save_final_image(f.read(), "story", pa["image"])
        _rm(tmp)


def _make_voice(pid, cfg, p, pa):
    if not (p["want_voice"] and p.get("sound", 1) and (p["voice_text"] or "").strip()):
        return None
    _step(pid, "ساخت ویس…")
    data, ext = ai.tts(cfg["ai"]["voice"], p["voice_text"].strip())
    audio = pa["audio_" + ext]
    with open(audio, "wb") as f:
        f.write(data)
    return audio


def _sound_note(p, used, is_clip):
    """توضیح کوتاه درباره‌ی صدای ویدیوی ساخته‌شده (در صفحه‌ی پست نشان داده می‌شود)."""
    if not p.get("sound", 1):
        return "🔇 ویدیو بی‌صدا ساخته شد (صدا را خودت خاموش کرده‌ای)."
    if not used:
        why = "کلیپ ویدیو صدای خودش را ندارد" if is_clip else "این ویدیو از عکس ساخته شده"
        return f"⚠️ صدا روشن است ولی {why} و ویس/موزیک هم انتخاب نشده؛ ویدیو بی‌صدا ماند. ویس را روشن کن یا موزیک آپلود کن."
    names = {"voice": "ویس", "original": "صدای خود ویدیو", "music": "موزیک"}
    return "🔊 صدای ویدیو: " + " + ".join(names[u] for u in used)


def build_media(pid, cfg):
    p = store.post_get(pid)
    pa = paths(pid)
    kind = p["kind"]
    sound = bool(p.get("sound", 1))
    music = music_path(pid) if sound else None
    if kind != "post" and p["out_type"] == "video":
        if not os.path.exists(pa["clip"]):
            raise media.MediaError("ویدیوی AI هنوز ساخته نشده است.")
        _rm(pa["audio_mp3"], pa["audio_wav"], pa["video"])
        audio = _make_voice(pid, cfg, p, pa)
        _step(pid, "آماده‌سازی ویدیو…")
        own = bool(p["user_video"])
        used = media.compose_clip(pa["clip"], audio, pa["video"], max_seconds=90 if (own and kind == "reel") else 58,
                                  sound=sound, music_path=music)
        store.post_update(pid, note=_sound_note(p, used, True))
        return
    if not os.path.exists(pa["image"]):
        raise media.MediaError("هنوز تصویری برای این پست نیست.")
    if kind == "post":
        _rm(pa["video"], pa["audio_mp3"], pa["audio_wav"])
        store.post_update(pid, note="")
        return
    _rm(pa["audio_mp3"], pa["audio_wav"], pa["video"])
    audio = _make_voice(pid, cfg, p, pa)
    if kind == "reel" or audio or music:
        _step(pid, "ساخت ویدیو…")
        used = media.make_video(pa["image"], audio, pa["video"], sound=sound, music_path=music)
        store.post_update(pid, note=_sound_note(p, used, False))
    else:
        store.post_update(pid, note="")


def media_for_publish(pid, kind):
    """مسیر فایلی که باید منتشر شود."""
    pa = paths(pid)
    if kind == "post":
        return pa["image"]
    if os.path.exists(pa["video"]):
        return pa["video"]
    if kind == "reel":
        return None
    return pa["image"]


def run_task(pid, task="full"):
    """task: full | text | image | media — در یک thread اجرا می‌شود."""
    try:
        cfg = store.load_config()
        p = store.post_get(pid)
        if not p:
            return
        store.post_update(pid, status="generating", error="", step="شروع…")
        pa = paths(pid)
        has_image = os.path.exists(pa["image"])
        is_video = p["out_type"] == "video" and p["kind"] != "post"
        if task == "full":
            keep = p["caption"].strip() or None
            need_ai = (not keep) or (p["want_voice"] and p["kind"] != "post" and not p["voice_text"].strip()) \
                or (is_video and not p["user_video"] and not p["image_prompt"].strip()) \
                or (is_video and not p["user_video"] and int(p.get("long_secs") or 0) and not (p.get("storyboard") or "").strip()) \
                or (p["want_image"] and not p["user_image"] and not p["image_prompt"].strip())
            if need_ai:
                gen_text(pid, cfg, keep)
            if is_video:
                gen_video(pid, cfg)
            elif not has_image:
                if not p["want_image"]:
                    raise ai.AIError("تصویری وجود ندارد؛ یک تصویر آپلود کن یا ساخت تصویر با AI را روشن کن.")
                gen_image(pid, cfg)
            build_media(pid, cfg)
        elif task == "text":
            gen_text(pid, cfg)
            if store.post_get(pid)["want_voice"] and (os.path.exists(pa["clip"]) if is_video else has_image):
                build_media(pid, cfg)
        elif task in ("image", "video"):
            if is_video:
                gen_video(pid, cfg)
            else:
                gen_image(pid, cfg)
            build_media(pid, cfg)
        elif task == "media":
            build_media(pid, cfg)
        fresh = store.post_get(pid)
        done = "scheduled" if (task == "full" and fresh and fresh["scheduled_at"]) else "ready"
        store.post_update(pid, status=done, step="", error="")
    except (ai.AIError, media.MediaError) as e:
        log.warning("task %s failed for post %s: %s", task, pid, e)
        store.post_update(pid, status="failed", step="", error=str(e)[:500])
    except Exception as e:  # noqa: BLE001
        log.exception("task %s crashed for post %s", task, pid)
        store.post_update(pid, status="failed", step="", error=f"{type(e).__name__}: {str(e)[:300]}")
