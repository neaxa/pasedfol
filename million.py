"""
«میلیونیم»: هدف = یک پست/ریلز با ۱ میلیون بازدید و ۱۰ هزار فالوور.

هر روز (در ساعتی که تعیین می‌کنی) و هر بار که خودت بزنی:
  ۱) کل پیج را می‌خواند: موضوع، بیو، فالوور، و بازدید/لایک/کامنت تک‌تک پست‌ها و ریلزها
  ۲) روند روزانه را بررسی می‌کند: کدام پست‌ها از دیروز رشد کرده‌اند، بازدید پست‌های اخیر بالا رفته یا پایین آمده
  ۳) ریلز/پست‌های پربازدید حوزه‌ی تو (هشتگ‌ها و پیج‌های رقیب) را آنالیز می‌کند: قلاب خط اول، طول ویدیو، هشتگ‌ها، نوع محتوا
  ۴) همه را به AI می‌دهد تا استراتژی و برنامه‌ی محتوای امروز را بنویسد (قلاب، زاویه، فرمت، ساعت انتشار، یک «شرط ویرال»)
  ۵) اگر «فعال» باشد، همان برنامه را می‌سازد (کپشن، تصویر/ویدیو، ویس) و یا منتظر تأیید تو می‌ماند یا سر ساعت منتشر می‌کند
"""
import json
import logging
import re
import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import ai
import growth
import jobs
import store

log = logging.getLogger("million")

_busy = threading.Event()
WEEKDAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


# ---------- ابزارها ----------
def hashtags_of(text):
    return [t.lower() for t in re.findall(r"#([\w\u200c]+)", text or "")]


def hook_of(caption):
    for line in (caption or "").splitlines():
        line = re.sub(r"#[\w\u200c]+", "", line).strip()
        if line:
            return line[:140]
    return ""


def reach(it):
    """معیار عملکرد: بازدید (برای ریلز)؛ برای عکس که بازدید عمومی ندارد، تخمین از لایک و کامنت."""
    v = int(it.get("views") or 0)
    return v if v > 0 else int(it.get("likes", 0)) * 12 + int(it.get("comments", 0)) * 40


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs)) if xs else 0


def _median(xs):
    return round(statistics.median(xs)) if xs else 0


def _bucket(value, edges):
    for lo, hi, label in edges:
        if lo <= value < hi:
            return label
    return edges[-1][2]


DUR_EDGES = [(0, 8, "زیر ۸ ثانیه"), (8, 15, "۸ تا ۱۵ ثانیه"), (15, 30, "۱۵ تا ۳۰ ثانیه"), (30, 60, "۳۰ تا ۶۰ ثانیه"),
             (60, 10**9, "بیش از ۱ دقیقه")]
CAP_EDGES = [(0, 60, "خیلی کوتاه"), (60, 250, "کوتاه"), (250, 800, "متوسط"), (800, 10**9, "بلند")]


def _local(ts, tz):
    return datetime.fromtimestamp(ts, tz)


# ---------- آنالیز پیج خودت ----------
def own_stats(mine, tz, prev_mine=None):
    mine = [m for m in mine if m.get("pk")]
    out = {"count": len(mine)}
    if not mine:
        return out
    by_kind = defaultdict(list)
    for m in mine:
        by_kind[m.get("kind", "post")].append(m)
    out["by_kind"] = {k: {"count": len(v), "avg": _avg([reach(x) for x in v]), "median": _median([reach(x) for x in v]),
                          "avg_likes": _avg([x.get("likes", 0) for x in v]), "avg_comments": _avg([x.get("comments", 0) for x in v])}
                      for k, v in by_kind.items()}
    ranked = sorted(mine, key=reach, reverse=True)
    slim = lambda m: {"pk": m["pk"], "code": m.get("code", ""), "kind": m.get("kind"), "views": m.get("views", 0),  # noqa: E731
                      "likes": m.get("likes", 0), "comments": m.get("comments", 0), "reach": reach(m),
                      "hook": hook_of(m.get("caption")), "duration": round(m.get("duration") or 0, 1),
                      "taken_at": m.get("taken_at", 0), "thumb": m.get("thumb", "")}
    out["top"] = [slim(m) for m in ranked[:6]]
    out["bottom"] = [slim(m) for m in ranked[-3:]] if len(ranked) > 6 else []
    out["best_views"] = max(int(m.get("views") or 0) for m in mine)
    out["total_views"] = sum(int(m.get("views") or 0) for m in mine)
    # ساعت و روز انتشار
    hours, days = defaultdict(list), defaultdict(list)
    for m in mine:
        if m.get("taken_at"):
            d = _local(m["taken_at"], tz)
            hours[d.hour].append(reach(m))
            days[d.weekday()].append(reach(m))
    out["by_hour"] = sorted(({"hour": h, "avg": _avg(v), "n": len(v)} for h, v in hours.items()), key=lambda x: -x["avg"])[:6]
    out["by_day"] = sorted(({"day": WEEKDAYS_FA[d], "avg": _avg(v), "n": len(v)} for d, v in days.items()), key=lambda x: -x["avg"])
    # طول ریلز و طول کپشن
    durs = defaultdict(list)
    for m in by_kind.get("reel", []):
        if m.get("duration"):
            durs[_bucket(m["duration"], DUR_EDGES)].append(reach(m))
    out["by_duration"] = sorted(({"bucket": b, "avg": _avg(v), "n": len(v)} for b, v in durs.items()), key=lambda x: -x["avg"])
    caps = defaultdict(list)
    for m in mine:
        caps[_bucket(len(m.get("caption") or ""), CAP_EDGES)].append(reach(m))
    out["by_caption"] = sorted(({"bucket": b, "avg": _avg(v), "n": len(v)} for b, v in caps.items()), key=lambda x: -x["avg"])
    # تعداد هشتگ در بهترین‌ها و بدترین‌ها
    half = max(1, len(ranked) // 2)
    out["hashtags_top_avg"] = round(sum(len(hashtags_of(m.get("caption"))) for m in ranked[:half]) / half, 1)
    out["hashtags_used"] = [t for t, _ in Counter(t for m in ranked[:half] for t in hashtags_of(m.get("caption"))).most_common(10)]
    # تعداد انتشار در ۳۰ روز اخیر
    now = time.time()
    out["posts_30d"] = sum(1 for m in mine if m.get("taken_at") and now - m["taken_at"] < 30 * 86400)
    # روند: ۵ پست آخر در برابر ۵ پست قبل
    chron = sorted((m for m in mine if m.get("taken_at")), key=lambda m: m["taken_at"], reverse=True)
    last5, prev5 = [reach(m) for m in chron[:5]], [reach(m) for m in chron[5:10]]
    if last5 and prev5 and _avg(prev5):
        out["trend_pct"] = round((_avg(last5) - _avg(prev5)) * 100 / _avg(prev5))
    # رشد هر پست از عکس فوری قبلی (کدام پست‌ها هنوز دارند بازدید می‌گیرند)
    if prev_mine:
        old = {str(m["pk"]): m for m in prev_mine}
        risers = []
        for m in mine:
            o = old.get(str(m["pk"]))
            if o:
                gain = reach(m) - reach(o)
                if gain > 0:
                    risers.append(dict(slim(m), gain=gain))
        out["risers"] = sorted(risers, key=lambda x: -x["gain"])[:5]
    return out


# ---------- آنالیز ویدیوهای پربازدید حوزه ----------
def niche_stats(niche):
    items = sorted((n for n in niche if n.get("pk")), key=reach, reverse=True)
    out = {"count": len(items)}
    if not items:
        return out
    top = items[:12]
    out["avg_views_top"] = _avg([reach(x) for x in top])
    out["median_views"] = _median([reach(x) for x in items])
    out["reel_share"] = round(100 * sum(1 for x in top if x.get("kind") == "reel") / len(top))
    reel_d = [x["duration"] for x in top if x.get("kind") == "reel" and x.get("duration")]
    out["reel_duration_avg"] = round(sum(reel_d) / len(reel_d), 1) if reel_d else 0
    out["hashtags"] = [t for t, _ in Counter(t for x in top for t in hashtags_of(x.get("caption"))).most_common(15)]
    hooks = [hook_of(x.get("caption")) for x in top]
    out["question_hooks"] = round(100 * sum(1 for h in hooks if "?" in h or "؟" in h) / len(top))
    out["number_hooks"] = round(100 * sum(1 for h in hooks if re.search(r"[0-9۰-۹]", h)) / len(top))
    out["caption_len_avg"] = _avg([len(x.get("caption") or "") for x in top])
    out["top"] = [{"pk": x["pk"], "code": x.get("code", ""), "author": x.get("author", ""), "kind": x.get("kind"),
                   "views": x.get("views", 0), "likes": x.get("likes", 0), "comments": x.get("comments", 0),
                   "reach": reach(x), "duration": round(x.get("duration") or 0, 1), "hook": hook_of(x.get("caption")),
                   "hashtags": hashtags_of(x.get("caption"))[:6], "thumb": x.get("thumb", ""), "topic": x.get("topic", "")}
                  for x in top]
    return out


# ---------- عکس‌های فوری ----------
def snapshots(limit=60):
    with store.db() as c:
        rows = c.execute("SELECT * FROM mil_snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def latest():
    rows = snapshots(1)
    if not rows:
        return None
    r = rows[0]
    for k in ("data", "strategy"):
        try:
            r[k] = json.loads(r[k] or "{}")
        except ValueError:
            r[k] = {}
    return r


def _save_snapshot(prof, own, data, strategy):
    with store.db() as c:
        cur = c.execute(
            "INSERT INTO mil_snapshots (ts, followers, following, media_count, best_views, total_views, data, strategy)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), int(prof.get("followers") or 0), int(prof.get("following") or 0), int(prof.get("media_count") or 0),
             int(own.get("best_views") or 0), int(own.get("total_views") or 0), json.dumps(data, ensure_ascii=False),
             json.dumps(strategy, ensure_ascii=False)))
        return cur.lastrowid


def _update_strategy(sid, strategy):
    with store.db() as c:
        c.execute("UPDATE mil_snapshots SET strategy=? WHERE id=?", (json.dumps(strategy, ensure_ascii=False), sid))


def follower_series(days=30):
    """یک نقطه برای هر روز (آخرین عکس فوری همان روز)."""
    tz = store.tz_of()
    per_day = {}
    for r in reversed(snapshots(400)):
        per_day[_local(r["ts"], tz).strftime("%Y-%m-%d")] = (r["followers"], r["best_views"])
    keys = sorted(per_day)[-days:]
    return [{"day": k, "followers": per_day[k][0], "best_views": per_day[k][1]} for k in keys]


def progress(cfg=None):
    cfg = cfg or store.load_config(False)
    m = cfg["million"]
    snap = latest()
    gv, gf = int(m.get("goal_views") or 1_000_000), int(m.get("goal_followers") or 10_000)
    out = {"goal_views": gv, "goal_followers": gf, "followers": 0, "best_views": 0, "has_data": bool(snap)}
    if not snap:
        return out
    out["followers"], out["best_views"] = snap["followers"], snap["best_views"]
    out["views_pct"] = min(100, round(100 * snap["best_views"] / gv, 1)) if gv else 0
    out["followers_pct"] = min(100, round(100 * snap["followers"] / gf, 1)) if gf else 0
    series = follower_series(14)
    if len(series) >= 2:
        d0, d1 = series[0], series[-1]
        days = max(1, (datetime.strptime(d1["day"], "%Y-%m-%d") - datetime.strptime(d0["day"], "%Y-%m-%d")).days)
        per_day = (d1["followers"] - d0["followers"]) / days
        out["per_day"] = round(per_day, 1)
        if per_day > 0 and snap["followers"] < gf:
            out["eta_days"] = int((gf - snap["followers"]) / per_day) + 1
    out["done_views"] = snap["best_views"] >= gv
    out["done_followers"] = snap["followers"] >= gf
    return out


def chart_points(series, key, w=600, h=120):
    """نقاط SVG برای نمودار ساده."""
    if len(series) < 2:
        return ""
    vals = [s[key] for s in series]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    step = w / (len(vals) - 1)
    return " ".join(f"{i * step:.1f},{h - 8 - (v - lo) * (h - 16) / span:.1f}" for i, v in enumerate(vals))


# ---------- AI ----------
def _ai_strategy(cfg, prof, own, nic, prev_strategy):
    slot = growth.ai_slot(cfg)
    if not slot:
        raise ai.AIError("برای آنالیز «میلیونیم» اول کلید API بخش «نوشتن کپشن» را در تنظیمات AI بگذار.")
    m = cfg["million"]
    b = cfg["brand"]
    n_plan = max(1, int(m.get("posts_per_day", 2)))
    kinds = [k for k in m.get("kinds", ["reel", "post"]) if k in ("reel", "post", "story")] or ["reel"]
    lang = "فارسی روان و محاوره‌ای" if cfg.get("language") != "en" else "English"
    system = (
        "تو یک استراتژیست رشد اینستاگرام در سطح جهانی هستی که پیج‌های کوچک را ویرال کرده‌ای. "
        "با داده‌های واقعی این پیج و پربازدیدترین پست‌های حوزه‌اش کار می‌کنی، نه حدس کلی. "
        f"هدف‌ها: (۱) یکی از ریلزها/پست‌های این پیج به {m.get('goal_views', 1_000_000):,} بازدید برسد "
        f"(۲) پیج به {m.get('goal_followers', 10_000):,} فالوور برسد.\n"
        "قواعد: الگوهای موفق را از داده بیرون بکش (قلاب خط اول، طول ریلز، نوع محتوا، ساعت انتشار، هشتگ‌ها) و ترکیبشان کن. "
        "هر ایده باید کاملاً مشخص و قابل ساخت باشد (نه «محتوای ارزشمند بساز»). قلاب‌ها کوتاه و کنجکاوی‌برانگیز باشند. "
        "بدون کپی مستقیم از رقبا؛ الهام از الگو. ایده‌ها با کارهای قبلی پیج تکراری نباشند.\n"
        f"زبان همه‌ی متن‌ها: {lang}. فقط و فقط یک JSON معتبر برگردان (بدون ``` و توضیح)."
    )
    data = {
        "profile": prof, "brand": {"niche": b.get("niche", ""), "tone": b.get("tone", "")},
        "my_page": {k: own.get(k) for k in ("count", "by_kind", "top", "bottom", "best_views", "by_hour", "by_day",
                                             "by_duration", "by_caption", "hashtags_top_avg", "hashtags_used", "posts_30d",
                                             "trend_pct", "risers") if own.get(k) not in (None, [], {})},
        "viral_in_niche": {k: nic.get(k) for k in ("count", "avg_views_top", "median_views", "reel_share", "reel_duration_avg",
                                                    "hashtags", "question_hooks", "number_hooks", "caption_len_avg", "top")
                           if nic.get(k) not in (None, [], {})},
        "yesterday_plan": [p.get("topic") for p in (prev_strategy or {}).get("plan", [])][:6],
    }
    for t in data["viral_in_niche"].get("top", []):
        t.pop("thumb", None)
    for t in data["my_page"].get("top", []) + data["my_page"].get("bottom", []) + data["my_page"].get("risers", []):
        t.pop("thumb", None)
    schema = (
        '{"score": عدد ۰ تا ۱۰۰ (آمادگی پیج برای ویرال شدن),'
        ' "niche": "موضوع واقعی پیج در یک خط",'
        ' "diagnosis": "۲ تا ۴ جمله: وضعیت فعلی و مهم‌ترین دلیل اینکه هنوز ویرال نشده",'
        ' "trend": "یک جمله درباره‌ی روند روزهای اخیر",'
        ' "bio_suggestion": "بیو پیشنهادی حداکثر ۱۵۰ کاراکتر که فالو گرفتن را راحت کند",'
        ' "winning_patterns": ["۳ تا ۶ الگوی مشخص که در پست‌های پربازدید (خودت و حوزه) جواب داده"],'
        ' "stop_doing": ["۲ تا ۴ کاری که باید کنار بگذارد"],'
        ' "hooks": ["۵ قلاب خط اول آماده"],'
        ' "best_times": ["HH:MM", "..."] (۲ یا ۳ ساعت بر اساس داده؛ اگر داده نبود ساعت‌های پرمخاطب معمول),'
        ' "reel_length": "طول پیشنهادی ریلز",'
        ' "hashtags": ["۸ تا ۱۲ هشتگ بدون #"],'
        ' "viral_bet": {"topic": "...", "hook": "...", "format": "...", "why": "..."},'
        f' "plan": [دقیقاً {n_plan} مورد: {{"kind": یکی از {kinds}, "topic": "موضوع دقیق", "hook": "قلاب خط اول/ثانیه‌ی اول",'
        ' "angle": "زاویه و ساختار محتوا قدم به قدم", "format": "فرمت تصویری (مثلاً متن روی تصویر، قبل/بعد، لیست ۳تایی)",'
        ' "cta": "دعوت به اقدام برای فالو/سیو/شیر", "hashtags": ["..."], "viral": true برای موردی که شرط ویرال است}],'
        ' "followers_plan": ["۳ تا ۵ کار مشخص برای رسیدن به هدف فالوور"],'
        ' "today_focus": "یک جمله: مهم‌ترین کار امروز"}'
    )
    user = "داده‌ها:\n" + json.dumps(data, ensure_ascii=False)[:14000] + "\n\nخروجی با این ساختار:\n" + schema
    raw = ai.chat(slot, [{"role": "system", "content": system}, {"role": "user", "content": user}])
    st = ai.extract_json(raw)
    if not isinstance(st, dict) or not st.get("plan"):
        raise ai.AIError("AI استراتژی معتبری برنگرداند؛ دوباره «آنالیز» را بزن.")
    plan = []
    for p in st.get("plan", [])[: n_plan]:
        if not isinstance(p, dict) or not str(p.get("topic", "")).strip():
            continue
        k = p.get("kind") if p.get("kind") in kinds else kinds[len(plan) % len(kinds)]
        plan.append({"kind": k, "topic": str(p["topic"]).strip()[:200], "hook": str(p.get("hook", "")).strip()[:200],
                     "angle": str(p.get("angle", "")).strip()[:600], "format": str(p.get("format", "")).strip()[:200],
                     "cta": str(p.get("cta", "")).strip()[:200], "viral": bool(p.get("viral")),
                     "hashtags": [str(h).lstrip("#").replace(" ", "_") for h in (p.get("hashtags") or [])][:12]})
    if not plan:
        raise ai.AIError("AI برنامه‌ی محتوای قابل ساختی نداد؛ دوباره امتحان کن.")
    st["plan"] = plan
    st["best_times"] = [t for t in (st.get("best_times") or []) if re.fullmatch(r"\d{1,2}:\d{2}", str(t))][:4]
    st["bio_suggestion"] = str(st.get("bio_suggestion", "")).strip()[:150]
    return st


# ---------- ساخت محتوا از برنامه ----------
def _brief(item, st):
    lines = []
    if item.get("hook"):
        lines.append(f"قلاب خط اول (دقیقاً با همین شروع کن یا خیلی نزدیک به آن): {item['hook']}")
    if item.get("angle"):
        lines.append(f"زاویه و ساختار: {item['angle']}")
    if item.get("format"):
        lines.append(f"فرمت: {item['format']}")
    if item.get("cta"):
        lines.append(f"دعوت به اقدام پایان کپشن: {item['cta']}")
    pats = [str(x) for x in (st.get("winning_patterns") or [])][:4]
    if pats:
        lines.append("الگوهای اثبات‌شده‌ی این حوزه: " + "؛ ".join(pats))
    if item.get("kind") == "reel" and st.get("reel_length"):
        lines.append(f"طول ریلز/متن گفتاری متناسب با: {st['reel_length']}")
    tags = item.get("hashtags") or st.get("hashtags") or []
    if tags:
        lines.append("از این هشتگ‌ها استفاده کن: " + " ".join("#" + str(t).lstrip("#") for t in tags[:12]))
    if item.get("viral"):
        lines.append("این «شرط ویرال» امروز است: قلاب باید در ۱ ثانیه‌ی اول میخکوب کند و محتوا ارزش سیو/شیر داشته باشد.")
    return "\n".join(lines)


def _next_time(hhmm, tz, after=None):
    after = after or datetime.now(tz)
    try:
        hh, mm = (int(x) for x in hhmm.split(":"))
        slot = after.replace(hour=hh % 24, minute=mm % 60, second=0, microsecond=0)
    except ValueError:
        return None
    if slot <= after + timedelta(minutes=20):
        slot += timedelta(days=1)
    return slot.timestamp()


def needed_keys(cfg):
    m = cfg["million"]
    kinds = m.get("kinds", [])
    need = ["caption"]
    if (not m.get("video")) or "post" in kinds:
        need.append("image")
    if m.get("video") and any(k != "post" for k in kinds):
        need.append("video")
    if any(jobs.ap_wants_voice(m, k) for k in kinds) and m.get("sound", True):
        need.append("voice")
    out = []
    for s in need:
        slot = cfg["ai"][s]
        if not slot.get("api_key") and slot.get("provider") != "custom":
            out.append(ai.SLOT_LABELS[s])
    return out


def create_posts(cfg, st, force=False):
    """برنامه‌ی امروز را می‌سازد (تا سقف «پست در روز»). شناسه‌ی پست‌های ساخته‌شده را برمی‌گرداند."""
    m = cfg["million"]
    tz = store.tz_of(cfg)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    key = f"mil:created:{today}"
    done = int(store.meta_get(key, "0"))
    want = max(1, int(m.get("posts_per_day", 2)))
    if done >= want and not force:
        return []
    times = st.get("best_times") or ["12:30", "20:30"]
    auto = m.get("approve") == "auto"
    pids, used_ts = [], []
    for i, item in enumerate(st.get("plan", [])[: (want if force else want - done)]):
        kind = item["kind"]
        when = _next_time(times[i % len(times)], tz)
        while when and any(abs(when - t) < 45 * 60 for t in used_ts):  # دو پست هم‌زمان نشوند
            when += 50 * 60
        if when:
            used_ts.append(when)
        pid = store.post_create(
            kind=kind, topic=item["topic"], status="generating", source="million", want_image=1,
            out_type="video" if (m.get("video") and kind != "post") else "image",
            want_voice=1 if jobs.ap_wants_voice(m, kind) else 0, sound=1 if m.get("sound", True) else 0,
            brief=_brief(item, st), scheduled_at=when if auto else None,
            note=("" if auto else f"⏰ زمان پیشنهادی انتشار: {store.fmt_ts(when, cfg)}") if when else "",
        )
        jobs.start_task(pid, "full")
        pids.append(pid)
    store.meta_set(key, done + len(pids))
    return pids


# ---------- اجرای آنالیز ----------
def _set_last(msg):
    store.meta_set("mil:last", msg)


def _niche_tags(cfg, prev):
    m = cfg["million"]
    tags = [t.lstrip("#").strip().lower() for t in m.get("extra_hashtags", []) if t.strip()]
    niche = (cfg["brand"].get("niche") or "").strip() or ((prev or {}).get("strategy") or {}).get("niche", "")
    if niche:
        tags += growth.build_hashtags(cfg, {"ai_expand": True}, niche[:60], max_tags=4)
    if prev:
        tags += ((prev.get("data") or {}).get("own") or {}).get("hashtags_used", [])[:2]
    return list(dict.fromkeys(t for t in tags if t))[:4]


def analyze(create=False):
    """یک دور کامل: خواندن پیج و رقبا ← آمار ← استراتژی AI ← (اختیاری) ساخت محتوا. (موفق؟، پیام) را برمی‌گرداند."""
    cfg = store.load_config()
    m = cfg["million"]
    prev = latest()
    _set_last("در حال خواندن پیج و پست‌ها از اینستاگرام…")
    params = {"amount": 30, "niche_amount": 18, "hashtags": _niche_tags(cfg, prev) if m.get("competitors", True) else [],
              "accounts": [a.lstrip("@") for a in m.get("competitor_accounts", []) if a.strip()][:4]}
    res, code = growth.run_worker("insights", params, timeout=1500)
    if not res.get("ok"):
        growth._handle_failure(res, code)
        return False, res.get("error", "خواندن اطلاعات پیج ناموفق بود.")
    prof, mine, niche = res.get("profile") or {}, res.get("mine") or [], res.get("niche") or []
    if not params["hashtags"] and m.get("competitors", True) and mine:  # اولین بار: هشتگ‌ها را از پست‌های خودت بردار
        tags = [t for t, _ in Counter(t for x in mine for t in hashtags_of(x.get("caption"))).most_common(3)]
        if tags:
            r2, _ = growth.run_worker("search", {"source": "top", "amount": 18, "hashtags": tags, "accounts": [], "urls": []})
            niche = [x for x in r2.get("items", []) if not x.get("is_mine")] if r2.get("ok") else []
    tz = store.tz_of(cfg)
    prev_mine = ((prev or {}).get("data") or {}).get("mine_raw") or []
    own = own_stats(mine, tz, prev_mine)
    nic = niche_stats(niche)
    mine_raw = [{k: x.get(k) for k in ("pk", "views", "likes", "comments", "kind")} for x in mine]
    data = {"profile": prof, "own": own, "niche": nic, "mine_raw": mine_raw, "errors": res.get("errors", [])[:4],
            "hashtags_scanned": params["hashtags"]}
    sid = _save_snapshot(prof, own, data, {})
    _set_last("در حال نوشتن استراتژی با AI…")
    try:
        st = _ai_strategy(cfg, prof, own, nic, (prev or {}).get("strategy"))
    except ai.AIError as e:
        _update_strategy(sid, {"error": str(e)[:400]})
        return False, f"اطلاعات پیج خوانده شد ولی استراتژی ساخته نشد: {e}"
    msg = "آنالیز کامل شد."
    if create and m.get("enabled"):
        missing = needed_keys(cfg)
        if missing:
            msg += " محتوا ساخته نشد چون کلید این بخش‌ها نیست: " + "، ".join(missing)
        else:
            pids = create_posts(cfg, st)
            st["created"] = pids
            if pids:
                msg += f" {len(pids)} محتوای جدید طبق برنامه در حال ساخت است" + (
                    " و سر ساعت پیشنهادی منتشر می‌شود." if m.get("approve") == "auto" else "؛ بعد از ساخت منتظر تأیید توست.")
            else:
                msg += " سهمیه‌ی محتوای امروز قبلاً ساخته شده است."
    _update_strategy(sid, st)
    return True, msg


def analyze_async(create=False):
    if _busy.is_set():
        return False
    _busy.set()

    def run():
        try:
            ok, msg = analyze(create)
        except Exception as e:  # noqa: BLE001
            log.exception("million analyze failed")
            msg = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            _busy.clear()
        _set_last(msg)
        store.meta_set("mil:last_ts", time.time())

    threading.Thread(target=run, daemon=True).start()
    return True


def create_from_latest():
    """ساخت دستی برنامه‌ی آخرین آنالیز (حتی اگر سهمیه‌ی امروز پر شده باشد)."""
    cfg = store.load_config()
    snap = latest()
    st = (snap or {}).get("strategy") or {}
    if not st.get("plan"):
        return [], "اول یک بار «آنالیز» را بزن."
    missing = needed_keys(cfg)
    if missing:
        return [], "اول کلید API این بخش‌ها را وارد کن: " + "، ".join(missing)
    pids = create_posts(cfg, st, force=True)
    st["created"] = (st.get("created") or []) + pids
    _update_strategy(snap["id"], st)
    return pids, f"{len(pids)} محتوا طبق برنامه در حال ساخت است."


def activate(on):
    cfg = store.load_config()
    cfg["million"]["enabled"] = bool(on)
    store.save_config(cfg)
    if on and store.session_files():
        tz = store.tz_of(cfg)
        store.meta_set(f"mil:{datetime.now(tz).strftime('%Y-%m-%d')}", "1")  # آنالیز امروز همین الان انجام می‌شود
        analyze_async(create=True)
        return True
    return False


def tick(cfg):
    """هر ۲۰ ثانیه از زمان‌بند صدا زده می‌شود: روزی یک بار در ساعت تعیین‌شده آنالیز + ساخت برنامه."""
    m = cfg["million"]
    if not m.get("enabled") or not store.session_files() or _busy.is_set():
        return
    if time.time() < float(store.meta_get("growth:pause_until", "0")):
        return
    tz = store.tz_of(cfg)
    now = datetime.now(tz)
    try:
        hh, mm = (int(x) for x in str(m.get("analyze_time", "09:00")).split(":"))
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError:
        return
    key = f"mil:{now.strftime('%Y-%m-%d')}"
    if now >= slot and (now - slot) < timedelta(hours=8) and store.meta_get(key) is None:
        store.meta_set(key, "1")
        analyze_async(create=True)


def set_bio(bio):
    res, code = growth.run_worker("set_bio", {"bio": bio})
    if not res.get("ok"):
        growth._handle_failure(res, code)
    return bool(res.get("ok")), res.get("error", "")


def sig():
    with store.db() as c:
        n = c.execute("SELECT COUNT(*) FROM mil_snapshots").fetchone()[0]
    return f"{_busy.is_set()}|{n}|{(store.meta_get('mil:last', '') or '')[:40]}"
