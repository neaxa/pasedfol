"""آماده‌سازی تصویر و ساخت ویدیو (تصویر + ویس) با Pillow و ffmpeg."""
import io
import json
import os
import subprocess
import tempfile

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

POST_MIN_RATIO, POST_MAX_RATIO = 0.8, 1.91  # محدوده‌ی مجاز نسبت تصویر در فید اینستاگرام
STORY_SIZE = (1080, 1920)


class MediaError(Exception):
    pass


def open_image(data):
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001
        raise MediaError("فایل تصویر معتبر نیست.") from e
    return ImageOps.exif_transpose(img).convert("RGB")


def _cover(img, size):
    return ImageOps.fit(img, size, Image.LANCZOS, centering=(0.5, 0.5))


def fit_post(img):
    """نسبت تصویر را داخل محدوده‌ی مجاز فید نگه می‌دارد و اندازه را استاندارد می‌کند."""
    w, h = img.size
    ratio = w / h
    if ratio < POST_MIN_RATIO:  # خیلی بلند → برش به ۴:۵
        img = _cover(img, (w, int(w / POST_MIN_RATIO)))
    elif ratio > POST_MAX_RATIO:  # خیلی پهن → برش به ۱.۹۱:۱
        img = _cover(img, (int(h * POST_MAX_RATIO), h))
    w, h = img.size
    if w > 1440:
        img = img.resize((1440, round(h * 1440 / w)), Image.LANCZOS)
    return img


def fit_story(img):
    """بوم ۹:۱۶؛ تصویر اصلی وسط و نسخه‌ی محو‌شده‌اش پس‌زمینه."""
    bg = _cover(img, STORY_SIZE).filter(ImageFilter.GaussianBlur(28))
    fg = ImageOps.contain(img, STORY_SIZE, Image.LANCZOS)
    if fg.size == STORY_SIZE:
        return fg
    bg.paste(fg, ((STORY_SIZE[0] - fg.width) // 2, (STORY_SIZE[1] - fg.height) // 2))
    return bg


def save_final_image(data, kind, out_path):
    img = open_image(data)
    img = fit_post(img) if kind == "post" else fit_story(img)
    img.save(out_path, "JPEG", quality=92)
    return out_path


def audio_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float(r.stdout.strip())
    except ValueError as e:
        raise MediaError("فایل صوتی خوانده نشد.") from e


def _run_ffmpeg(cmd, what):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as e:
        raise MediaError("ffmpeg روی سرور نصب نیست.") from e
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"{what}: بیش از حد طول کشید.") from e
    if r.returncode != 0 or not os.path.exists(cmd[-1]):
        hint = ""
        if r.returncode in (-9, 137):
            hint = " (احتمالاً حافظه‌ی سرور کم است؛ پلن Railway را بالاتر ببر.)"
        raise MediaError(f"{what} ناموفق بود (کد خروج {r.returncode}){hint} {r.stderr[-300:]}".strip())
    return cmd[-1]


# تنظیمات کم‌مصرف برای x264 + مشخصات مورد قبول اینستاگرام (H.264 High، yuv420p، ۳۰ فریم)
_X264 = ["-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-x264-params", "rc-lookahead=8:ref=2",
         "-profile:v", "high", "-level", "4.0", "-crf", "24", "-r", "30", "-g", "60", "-pix_fmt", "yuv420p",
         # رنج رنگ «تلویزیونی» (limited) + BT.709؛ جلوی برچسب yuvj420p (full range) را می‌گیرد
         "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
# صدا همیشه AAC-LC استریو ۴۴.۱kHz (ویس Gemini تک‌کاناله و ۲۴kHz است و اینستاگرام آن را رد می‌کند)
_AAC = ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", "-movflags", "+faststart"]
# بعد از برش، رنج رنگ را صریحاً از full/JPEG به limited تبدیل می‌کنیم (in_range=auto = هر چه ورودی هست)
_FIT_916 = ("scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,setsar=1,"
            "scale=in_range=auto:out_range=tv:out_color_matrix=bt709:flags=lanczos,format=yuv420p")
AUDIO_LEAD_MS = 400  # کمی مکث قبل از شروع ویس


def prepare_audio(src_path):
    """هر فایل صوتی (mp3/wav/pcm) را به WAV استریو ۴۴.۱kHz با بلندی یکنواخت تبدیل می‌کند.
    مسیر فایل موقت را برمی‌گرداند؛ فراخواننده باید پاکش کند."""
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="voice_", dir=os.path.dirname(os.path.abspath(src_path)))
    os.close(fd)
    base = ["ffmpeg", "-y", "-loglevel", "error", "-i", src_path, "-vn"]
    tail = ["-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", out]
    try:
        _run_ffmpeg(base + ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"] + tail, "آماده‌سازی صدا")
    except MediaError:
        try:  # بعضی ساخت‌های ffmpeg loudnorm ندارند
            _run_ffmpeg(base + tail, "آماده‌سازی صدا")
        except MediaError:
            _rm_quiet(out)
            raise
    return out


def _rm_quiet(*files):
    for f in files:
        try:
            os.remove(f)
        except OSError:
            pass


def probe(path):
    """مشخصات فایل با ffprobe: {'video': {...}|None, 'audio': {...}|None, 'duration': float}"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        capture_output=True, text=True, timeout=60,
    )
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError as e:
        raise MediaError("فایل ویدیو خوانده نشد.") from e
    out = {"video": None, "audio": None, "duration": 0.0}
    for st in data.get("streams", []):
        if st.get("codec_type") in ("video", "audio") and out[st["codec_type"]] is None:
            out[st["codec_type"]] = st
    try:
        out["duration"] = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        pass
    return out


def fix_range(path):
    """ویدیوی h264 با برچسب yuvj420p (full range) را دوباره با رنج استاندارد yuv420p می‌نویسد (جایگزین فایل می‌شود)."""
    tmp = f"{path}.fix.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", "scale=in_range=auto:out_range=tv:out_color_matrix=bt709:flags=lanczos,format=yuv420p",
           *_X264, "-c:a", "copy", "-movflags", "+faststart", tmp]
    try:
        _run_ffmpeg(cmd, "اصلاح رنج رنگ ویدیو")
        os.replace(tmp, path)
    finally:
        _rm_quiet(tmp)
    return path


def validate_video(path, need_audio=False, max_seconds=60):
    """بررسی می‌کند فایل نهایی دقیقاً چیزی است که اینستاگرام می‌پذیرد؛ وگرنه MediaError با علت."""
    info = probe(path)
    v, a = info["video"], info["audio"]
    if not v:
        raise MediaError("ویدیوی ساخته‌شده تصویر ندارد.")
    if v.get("codec_name") == "h264" and v.get("pix_fmt") == "yuvj420p":  # فقط برچسب رنج است؛ یک بار خودکار اصلاح کن
        fix_range(path)
        info = probe(path)
        v, a = info["video"], info["audio"]
    if v.get("codec_name") != "h264" or v.get("pix_fmt") != "yuv420p":
        raise MediaError(f"قالب ویدیو برای اینستاگرام مناسب نیست ({v.get('codec_name')}/{v.get('pix_fmt')}).")
    if (v.get("width"), v.get("height")) != (1080, 1920):
        raise MediaError(f"ابعاد ویدیو باید ۱۰۸۰×۱۹۲۰ باشد ولی {v.get('width')}×{v.get('height')} است.")
    if need_audio:
        if not a:
            raise MediaError("ویس روی ویدیو قرار نگرفت (ویدیو صدا ندارد).")
        if a.get("codec_name") != "aac" or str(a.get("sample_rate")) not in ("44100", "48000"):
            raise MediaError(f"قالب صدا برای اینستاگرام مناسب نیست ({a.get('codec_name')}/{a.get('sample_rate')}).")
    if not (1 <= info["duration"] <= max_seconds + 1):
        raise MediaError(f"طول ویدیو ({info['duration']:.1f} ثانیه) خارج از محدوده‌ی مجاز است.")
    return info


def _mix_audio(voice_path, music_path, orig_input, dur, first_index, voice_lead=True):
    """گراف صدای خروجی را می‌سازد.
    ورودی‌ها: ویس (اختیاری)، موزیک (اختیاری، تکرار می‌شود) و صدای خود ویدیو (orig_input = «0:a» یا None).
    خروجی: (ورودی‌های اضافه‌ی ffmpeg، بخش‌های filter_complex، برچسب خروجی صدا یا None، منابع استفاده‌شده)."""
    inputs, parts, labels, used = [], [], [], []
    idx = first_index
    fmt = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
    if voice_path:
        inputs += ["-i", voice_path]
        delay = f",adelay={AUDIO_LEAD_MS}|{AUDIO_LEAD_MS}" if voice_lead else ""
        parts.append(f"[{idx}:a]{fmt}{delay},apad[av]")
        labels.append("[av]")
        used.append("voice")
        idx += 1
    if orig_input:
        vol = 0.30 if voice_path else 1.0  # زیر ویس، صدای خود ویدیو آرام‌تر می‌شود
        parts.append(f"[{orig_input}]{fmt},volume={vol},apad[ao]")
        labels.append("[ao]")
        used.append("original")
    if music_path:
        inputs += ["-stream_loop", "-1", "-i", music_path]
        vol = 0.16 if voice_path else (0.45 if orig_input else 0.9)
        fade = max(0.0, dur - 1.5)
        parts.append(f"[{idx}:a]{fmt},volume={vol},afade=t=in:st=0:d=0.6,afade=t=out:st={fade:.2f}:d=1.5[am]")
        labels.append("[am]")
        used.append("music")
        idx += 1
    if not labels:
        return inputs, parts, None, used
    if len(labels) == 1:
        parts.append(f"{labels[0]}alimiter=limit=0.95[aout]")
    else:
        parts.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
                     "alimiter=limit=0.95[aout]")
    return inputs, parts, "[aout]", used


def _encode(video_inputs, vchain, voice, music, orig_input, dur, out_path, what, max_seconds):
    """ورودی تصویر + گراف صدا → mp4 استاندارد اینستاگرام. منابع صدای استفاده‌شده را برمی‌گرداند."""
    first = sum(1 for x in video_inputs if x == "-i")
    a_in, a_parts, a_label, used = _mix_audio(voice, music, orig_input, dur, first)
    graph = [f"[0:v]{vchain}[vout]"] + a_parts
    if a_label is None:  # بی‌صدا: یک ترک صدای خالی (اینستاگرام ویدیوی بدون ترک صدا را گاهی رد می‌کند)
        a_in = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        a_label = f"{first}:a"
    else:
        a_label = a_label
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *video_inputs, *a_in, "-filter_complex", ";".join(graph),
           "-map", "[vout]", "-map", a_label, "-t", f"{dur:.2f}", *_X264, *_AAC, out_path]
    _run_ffmpeg(cmd, what)
    validate_video(out_path, need_audio=True, max_seconds=max_seconds)
    return used


def make_video(image_path, audio_path, out_path, silent_seconds=8, max_seconds=58, sound=True, music_path=None):
    """ویدیوی ۹:۱۶ از یک تصویر ثابت. sound=False: کاملاً بی‌صدا.
    با صدا: ویس و/یا موزیک پس‌زمینه (هرکدام که باشد). منابع صدای استفاده‌شده را برمی‌گرداند."""
    prepared = None
    voice = audio_path if sound else None
    music = music_path if (sound and music_path and os.path.exists(music_path)) else None
    try:
        if voice:
            prepared = prepare_audio(voice)
            dur = min(max_seconds, audio_duration(prepared) + AUDIO_LEAD_MS / 1000 + 0.8)
        else:
            dur = silent_seconds
        vin = ["-loop", "1", "-framerate", "15", "-i", image_path]
        return _encode(vin, _FIT_916, prepared, music, None, dur, out_path, "ساخت ویدیو", max_seconds)
    finally:
        if prepared:
            _rm_quiet(prepared)


def compose_clip(clip_path, audio_path, out_path, max_seconds=58, keep_audio=None, sound=True, music_path=None):
    """کلیپ ویدیویی (AI یا آپلودی) را ۱۰۸۰×۱۹۲۰ می‌کند و صدای آن را طبق انتخاب کاربر می‌سازد:
      sound=False → ویدیوی کاملاً بی‌صدا (حتی صدای خود کلیپ حذف می‌شود)
      sound=True  → صدای خود کلیپ (اگر داشته باشد، مثلاً مدل‌های LTX-2 صدا هم می‌سازند) + ویس + موزیک، هرکدام که باشد
    اگر ویس بلندتر از کلیپ باشد، کلیپ تکرار می‌شود. منابع صدای استفاده‌شده را برمی‌گرداند (لیست خالی = بی‌صدا).
    keep_audio فقط برای سازگاری با نسخه‌ی قبل است (False = صدای خود کلیپ استفاده نشود)."""
    prepared = None
    try:
        info = probe(clip_path)
        if not info["video"]:
            raise MediaError("فایل ویدیو خوانده نشد یا تصویر ندارد.")
        cdur = info["duration"] or audio_duration(clip_path)
        voice = audio_path if sound else None
        music = music_path if (sound and music_path and os.path.exists(music_path)) else None
        use_orig = sound and info["audio"] is not None and keep_audio is not False
        if voice:
            prepared = prepare_audio(voice)
            dur = min(max_seconds, max(cdur, audio_duration(prepared) + AUDIO_LEAD_MS / 1000 + 0.6))
        else:
            dur = min(max_seconds, cdur)
        loop = ["-stream_loop", "-1"] if dur > cdur + 0.05 else []
        vin = [*loop, "-i", clip_path]
        return _encode(vin, _FIT_916, prepared, music, "0:a:0" if use_orig else None, dur, out_path,
                       "آماده‌سازی ویدیو", max_seconds)
    finally:
        if prepared:
            _rm_quiet(prepared)


def extract_last_frame(video_path, out_path):
    """آخرین فریم ویدیو؛ برای شروع کلیپ بعدی در فیلم بلند (تا کاراکتر و صحنه ادامه پیدا کند)."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-0.2", "-i", video_path, "-update", "1",
           "-frames:v", "1", "-q:v", "2", out_path]
    try:
        return _run_ffmpeg(cmd, "گرفتن فریم آخر")
    except MediaError:  # بعضی فایل‌ها با sseof مشکل دارند: از روی مدت کلیپ حساب می‌کنیم
        dur = max(0.0, (probe(video_path)["duration"] or 0) - 0.15)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{dur:.2f}", "-i", video_path,
               "-frames:v", "1", "-q:v", "2", out_path]
        return _run_ffmpeg(cmd, "گرفتن فریم آخر")


def concat_clips(clip_paths, out_path, sound=True):
    """چند کلیپ را پشت‌سرهم به یک ویدیوی ۹:۱۶ می‌چسباند (همه به یک مشخصات می‌رسند).
    کلیپی که صدا ندارد، صدای خالی می‌گیرد تا چسباندن به هم نخورد؛ sound=False: خروجی بی‌صدا."""
    if not clip_paths:
        raise MediaError("کلیپی برای چسباندن نیست.")
    ins, parts, labels = [], [], []
    for i, p in enumerate(clip_paths):
        info = probe(p)
        if not info["video"]:
            raise MediaError(f"کلیپ {i + 1} تصویر ندارد.")
        dur = info["duration"] or audio_duration(p)
        ins += ["-i", p]
        parts.append(f"[{i}:v]{_FIT_916},fps=30,setpts=PTS-STARTPTS[v{i}]")
        if sound and info["audio"] is not None:
            parts.append(f"[{i}:a:0]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{i}]")
        else:
            parts.append(f"anullsrc=r=44100:cl=stereo,atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    n = len(clip_paths)
    graph = ";".join(parts) + ";" + "".join(labels) + f"concat=n={n}:v=1:a=1[vout][aout]"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *ins, "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
           *_X264, *_AAC, out_path]
    _run_ffmpeg(cmd, "چسباندن کلیپ‌ها")
    return out_path


def has_audible_sound(path, threshold_db=-55.0):
    """آیا فایل صدای شنیدنی دارد؟ (ترک صدای کاملاً ساکت = False)"""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0?", "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    for line in r.stderr.splitlines():
        if "max_volume:" in line:
            try:
                return float(line.split("max_volume:")[1].split("dB")[0]) > threshold_db
            except ValueError:
                return False
    return False


def story_backdrop(data, out_path):
    """پس‌زمینه‌ی ۹:۱۶ برای استوریِ «اشتراک‌گذاری پست»: تصویر بندانگشتی محو و تیره؛ بدون تصویر، رنگ ساده."""
    if data:
        try:
            bg = _cover(open_image(data), STORY_SIZE).filter(ImageFilter.GaussianBlur(40))
            bg = ImageEnhance.Brightness(bg).enhance(0.55)
            bg.save(out_path, "JPEG", quality=90)
            return out_path
        except MediaError:
            pass
    Image.new("RGB", STORY_SIZE, (24, 26, 40)).save(out_path, "JPEG", quality=90)
    return out_path


def extract_frame(video_path, out_path):
    """اولین فریم ویدیو (برای پیش‌نمایش و تصویر بندانگشتی)."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-frames:v", "1", "-q:v", "2", out_path]
    return _run_ffmpeg(cmd, "گرفتن فریم")


# ---------- استوری از پست دیگران (بخش «کشف و استوری») ----------
_FONT_PATHS = (
    "/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)
_PERSIAN_FONTS = _FONT_PATHS[:3]


def _font(size):
    from PIL import ImageFont
    for p in _FONT_PATHS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow قدیمی
        return ImageFont.load_default()


def _rtl_ok():
    """متن فارسی فقط وقتی درست کشیده می‌شود که هم فونت فارسی روی سرور باشد هم Pillow با raqm ساخته شده باشد."""
    if not any(os.path.exists(p) for p in _PERSIAN_FONTS):
        return False
    try:
        from PIL import features
        return bool(features.check("raqm"))
    except Exception:  # noqa: BLE001
        return False


def _text_w(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def credit_badge(author):
    """برچسب «@نام‌کاربری» (برای اعتبار صاحب پست) به‌صورت PNG شفاف."""
    from PIL import ImageDraw
    font = _font(40)
    text = f"@{author}" if author else "@instagram"
    tmp = Image.new("RGBA", (10, 10))
    tw, th = _text_w(ImageDraw.Draw(tmp), text, font)
    w, h = tw + 64, th + 36
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=(0, 0, 0, 150))
    d.text((32, 18 - 6), text, font=font, fill=(255, 255, 255, 255))
    return img


def _paste_badge(img, author, y=170):
    badge = credit_badge(author)
    img = img.convert("RGBA")
    img.alpha_composite(badge, ((img.width - badge.width) // 2, y))
    return img.convert("RGB")


def story_full_image(data, author, out_path):
    """حالت «کامل»: خود عکس پست داخل استوری (وسط، با پس‌زمینه‌ی محو) + نام صاحب پست."""
    img = fit_story(open_image(data))
    _paste_badge(img, author).save(out_path, "JPEG", quality=92)
    return out_path


def story_full_video(src_path, out_path, author, max_seconds=59, sound=True):
    """حالت «کامل» برای ریلز/ویدیو: کل ویدیو داخل استوری پخش می‌شود (۹:۱۶، پس‌زمینه‌ی محو اگر نسبت فرق کند)
    + نام صاحب پست. صدای خود ویدیو حفظ می‌شود (مگر sound=False). استوری حداکثر ۶۰ ثانیه است."""
    info = probe(src_path)
    if not info["video"]:
        raise MediaError("ویدیوی پست خوانده نشد.")
    dur = min(max_seconds, info["duration"] or max_seconds)
    if dur < 1:
        raise MediaError("ویدیوی پست خیلی کوتاه است.")
    fd, badge_path = tempfile.mkstemp(suffix=".png", prefix="badge_")
    os.close(fd)
    try:
        credit_badge(author).save(badge_path, "PNG")
        vchain = (
            "split[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2,eq=brightness=-0.12[bg];"
            "[b]scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
            "[base][1:v]overlay=(W-w)/2:170,setsar=1,"
            "scale=in_range=auto:out_range=tv:out_color_matrix=bt709:flags=lanczos,format=yuv420p"
        )
        vin = ["-i", src_path, "-loop", "1", "-i", badge_path]
        orig = "0:a:0" if (sound and info["audio"]) else None
        return _encode(vin, vchain, None, None, orig, dur, out_path, "ساخت استوری ویدیویی", max_seconds)
    finally:
        _rm_quiet(badge_path)


def _ig_gradient(size):
    """پس‌زمینه‌ی گرادیانی شبیه رنگ‌های اینستاگرام (وقتی تصویر بندانگشتی نداریم)."""
    w, h = size
    top, bot = (91, 79, 245), (238, 88, 98)
    img = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        img.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    return img.resize((w, h))


def story_card(thumb_data, author, is_reel, out_path, avatar_data=None):
    """حالت «کارت»: پیش‌نمایش پست به‌شکل یک کارت وسط استوری (عکس کاور + نام صاحب پست + «برای دیدن بزن»).
    استیکر پست اینستاگرام دقیقاً روی همین کارت گذاشته می‌شود تا با زدن روی آن، پست اصلی باز شود.
    خروجی: (x, y, width, height) نرمال‌شده‌ی محل کارت برای استیکر."""
    from PIL import ImageDraw
    W, H = STORY_SIZE
    thumb = None
    if thumb_data:
        try:
            thumb = open_image(thumb_data)
        except MediaError:
            thumb = None
    if thumb is not None:
        bg = _cover(thumb, STORY_SIZE).filter(ImageFilter.GaussianBlur(40))
        bg = ImageEnhance.Brightness(bg).enhance(0.5)
    else:
        bg = _ig_gradient(STORY_SIZE)
    cw = 820
    head, foot = 130, 140
    mh = 1090 if is_reel else 1025  # ریلز بلندتر، پست تقریباً ۴:۵
    ch = head + mh + foot
    cx, cy = (W - cw) // 2, (H - ch) // 2
    canvas = bg.convert("RGBA")
    # سایه
    shadow = Image.new("RGBA", STORY_SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((cx + 6, cy + 18, cx + cw + 6, cy + ch + 18), radius=44, fill=(0, 0, 0, 110))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))
    card = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    d.rounded_rectangle((0, 0, cw - 1, ch - 1), radius=40, fill=(255, 255, 255, 255))
    # سربرگ: آواتار + نام کاربری
    av = 76
    ax, ay = 34, (head - av) // 2
    mask = Image.new("L", (av, av), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, av - 1, av - 1), fill=255)
    if avatar_data:
        try:
            a_img = _cover(open_image(avatar_data), (av, av))
        except MediaError:
            a_img = _ig_gradient((av, av))
    else:
        a_img = _ig_gradient((av, av))
    ring = Image.new("RGBA", (av + 10, av + 10), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((0, 0, av + 9, av + 9), fill=(225, 48, 108, 255))
    card.alpha_composite(ring, (ax - 5, ay - 5))
    card.paste(a_img, (ax, ay), mask)
    name_font = _font(40)
    d.text((ax + av + 24, head // 2 - 26), f"@{author or 'instagram'}"[:26], font=name_font, fill=(20, 21, 26, 255))
    # تصویر پست
    m = _cover(thumb, (cw, mh)) if thumb is not None else _ig_gradient((cw, mh))
    card.paste(m, (0, head))
    if is_reel:  # نشانه‌ی پخش ویدیو
        ov = Image.new("RGBA", (cw, mh), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        r = 74
        mx, my = cw // 2, mh // 2
        od.ellipse((mx - r, my - r, mx + r, my + r), fill=(0, 0, 0, 120))
        od.polygon([(mx - 24, my - 38), (mx - 24, my + 38), (mx + 40, my)], fill=(255, 255, 255, 235))
        card.alpha_composite(ov, (0, head))
    # پانویس: «برای دیدن پست بزن»
    label = ("برای دیدن پست اصلی بزن" if _rtl_ok() else "Tap to view the post") + ("  ▶" if is_reel else "")
    f2 = _font(38)
    tw, th = _text_w(d, label, f2)
    pill_w, pill_h = min(cw - 80, tw + 80), 82
    px, py = (cw - pill_w) // 2, head + mh + (foot - pill_h) // 2
    d.rounded_rectangle((px, py, px + pill_w, py + pill_h), radius=pill_h // 2, fill=(91, 79, 245, 255))
    d.text(((cw - tw) // 2, py + (pill_h - th) // 2 - 8), label, font=f2, fill=(255, 255, 255, 255))
    # گوشه‌های گرد
    cmask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(cmask).rounded_rectangle((0, 0, cw - 1, ch - 1), radius=40, fill=255)
    canvas.paste(card, (cx, cy), cmask)
    canvas.convert("RGB").save(out_path, "JPEG", quality=92)
    return (0.5, (cy + ch / 2) / H, cw / W, ch / H)
