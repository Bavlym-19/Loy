"""
Guardian server (FastAPI)

Run:
    pip install fastapi uvicorn
    uvicorn server:app --host 0.0.0.0 --port 3000

Then open  http://localhost:3000  (or http://<laptop-ip>:3000 from your phone).

Secrets (set as environment variables, or edit the defaults below):
    DEVICE_KEY   must equal DEVICE_KEY in the ESP sketch
    ADMIN_TOKEN  password for the dashboard / your Swift app

API (for the dashboard and your Swift app, send header  X-Admin-Token):
    POST /api/esp/report     ESP -> server (header X-Device-Key). Reply carries a command.
    GET  /api/state          latest readings + online flag
    GET  /api/events         event log (hash-chained, saved in events.jsonl)
    POST /api/cmd/{name}     name = clear | recalibrate | reset_wifi
"""
import hashlib
import hmac
import json
import os
import random
import time
import urllib.request
from collections import deque

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

DEVICE_KEY = os.getenv("DEVICE_KEY", "change-this-secret")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-admin-token")
ONLINE_WINDOW = 15            # seconds without a report = offline
LOG_FILE = "events.jsonl"
ALLOWED_CMDS = {"clear", "recalibrate", "reset_wifi", "open_safe"}

# WhatsApp Configuration
WA_SERVER = os.getenv("WA_SERVER", "https://server-watsapp2.onrender.com")
WA_SESSION = os.getenv("WA_SESSION", "session1")
WA_NUMBER = os.getenv("WA_NUMBER", "201274283037")
last_wa_alert = {}

# ---------- Outdoor Weather Cache (Open-Meteo, Cairo) ----------
_weather_cache = {"data": None, "ts": 0.0}
WEATHER_LAT = 30.0444
WEATHER_LON = 31.2357
WEATHER_REFRESH_SEC = 1800  # 30 minutes


def get_outdoor_weather() -> dict:
    """Fetch current weather from Open-Meteo (Cairo). Cached 30 min."""
    now = time.time()
    if _weather_cache["data"] and now - _weather_cache["ts"] < WEATHER_REFRESH_SEC:
        return _weather_cache["data"]
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
            f"&current_weather=true&hourly=relativehumidity_2m&forecast_days=1"
        )
        with urllib.request.urlopen(url, timeout=6) as resp:
            raw = json.loads(resp.read())
        cw = raw.get("current_weather", {})
        result = {
            "temp": cw.get("temperature"),
            "windspeed": cw.get("windspeed"),
            "weathercode": cw.get("weathercode", 0),
        }
        _weather_cache["data"] = result
        _weather_cache["ts"] = now
        return result
    except Exception as e:
        print(f"[Weather] Failed: {e}")
        return _weather_cache["data"] or {}


def _wmo_to_arabic(code: int) -> str:
    """Convert WMO weather code to Arabic description."""
    if code == 0:
        return "صحو وسماء صافية ☀️"
    if code in (1, 2, 3):
        return "غيوم جزئية 🌤️"
    if code in (45, 48):
        return "ضباب 🌫️"
    if code in (51, 53, 55):
        return "رذاذ خفيف 🌦️"
    if code in (61, 63, 65):
        return "أمطار 🌧️"
    if code in (71, 73, 75):
        return "ثلج 🌨️"
    if code in (80, 81, 82):
        return "أمطار متقطعة 🌦️"
    if code in (95, 96, 99):
        return "عواصف رعدية ⛈️"
    return "متغير 🌈"


def _clothing_advice(outdoor_temp: float, indoor_temp: float) -> str:
    """Generate clothing advice based on outdoor temperature."""
    diff = indoor_temp - outdoor_temp if indoor_temp and outdoor_temp else 0
    if outdoor_temp >= 36:
        base = "الجو ناري برا! البس هدوم خفيفة وفاتحة اللون وشرب ميه كتير قبل ما تخرج 🥵"
    elif outdoor_temp >= 30:
        base = "الجو حار برا، تيشيرت خفيف كفاية، بس خلي معاك ميه 💧"
    elif outdoor_temp >= 22:
        base = "الجو معتدل حلو، هدوم عادية مناسبة 🌤️"
    elif outdoor_temp >= 15:
        base = "الجو شوية بارد، البس جاكيت أو سترة خفيفة 🧥"
    elif outdoor_temp >= 8:
        base = "الجو بارد، البس كنزة وجاكيت دافي 🧣"
    else:
        base = "الجو بارد جداً! البس هدوم دافية وكوفية وجوانتي ❄️🧤"
    if diff > 8:
        base += " — ودي بالك الفرق كبير بين البيت وبرا، هتحس بالبرد مجرد ما تخرج!"
    return base


def send_whatsapp(msg: str):
    try:
        url = f"{WA_SERVER}/send-message"
        payload = json.dumps({"sessionId": WA_SESSION, "number": WA_NUMBER, "message": msg}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[WA] Failed to send: {e}")


# ---------- Human-like Egyptian Arabic Message Engine ----------
def get_human_safe_opened_msg() -> str:
    templates = [
        "🔓 *يا باشا الخزنة اتفتحت بنجاح حالاً!* متقلقش خالص كل حاجة تمام وتمام التمام 🫡✨",
        "🗝️ *أمرك اتنفذ يا غالي!* إشارة الفتح وصلت للخزنة وفتحت معاك دلوقتي خلاص 💪",
        "🔓 *مساء الفل يا باشا*، الخزنة فتحت في ثانية وكل أمورك في السليم 🛡️",
        "🎉 *تمام يا كابتن!* الخزنة اتفتحت بنجاح وتقدر تستخدمها براحتك، الحارس مسجل العملية 🔐✨",
        "🫡 *تم بنجاح يا فندم!* الخزنة اتفتحت والسيستم سجل الفتح في السجل المشفر بأمان.",
    ]
    return random.choice(templates)


def get_human_safe_failed_msg(reason: str = "not_found") -> str:
    if reason == "not_found":
        templates = [
            "📡❌ *يا باشا جربت أوصل للخزنة بس مش لاقي شبكتها خالص!* اتأكد كدا إنها واصلة بالكهربا ومنورة وقريبة من الجهاز.",
            "⚠️ *معلش يا غالي*، شبكة الخزنة مش ظاهرة للجهاز خالص، شيك على الباور بتاعها وجرب تدوس فتح تاني.",
            "🔌 *يا كابتن في مشكلة في إشارة الخزنة!* الجهاز مش لاقيها، اتأكد إنها شغالة وسلك الباور سليم.",
        ]
    else:
        templates = [
            "⚠️ *يا باشا اتصلت بالخزنة بس مردتش عليا بإشارة الفتح!* جرب تدوس فتح كمان مرة.",
            "🛑 *للأسف يا غالي أمر الفتح منفذش*، الخزنة استلمت إشارة بس مفتحتش، شيك على السيرفو كده وجرب تاني.",
            "⚠️ *معلش يا كابتن حصل دروب في الاتصال مع الخزنة ومافتحتش*، جرب تفتحها تاني دلوقتي.",
        ]
    return random.choice(templates)


def get_human_status_msg(d: dict) -> str:
    if not d:
        return "⚠️ يا باشا الجهاز لسه متصلش بالسيرفر من وقت ما اشتغل!"

    greetings = [
        "🏠 *يا هلا يا باشا! البيت كله تمام وأمان الحمد لله*، وادي التقرير في ثانية:",
        "🛡️ *مساء الورد يا غالي، متقلقش خالص كل حاجة مستقرة وتحت السيطرة*، دي حالة البيت دلوقتي:",
        "🟢 *كله في السليم يا كابتن وبطمنك البيت أمان تام*، بص على القراءات كدا:",
        "🫡 *حبيبي يا باشا، الحارس صاحي ومفيش أي حركة غريبة ولا خطر*، وادي تفاصيل البيت:",
        "✨ *منور يا باشا، البيت أمان والدنيا هادية وزي الفل*، دي نظرة سريعة على الحساسات:",
    ]

    gp = d.get('gas_pct', min(100, max(0, int(((d.get('gas', 0) - d.get('gas_base', 0)) / 600.0) * 100))))
    lp = d.get('light_pct', min(100, max(0, int(((1400.0 - d.get('light', 0)) / 1280.0) * 100))))
    raw_d = d.get('dist', -1)

    if raw_d < 0:
        dist_desc = "المسار خالي تماماً"
    elif raw_d >= 100:
        dist_desc = f"{raw_d/100.0:.2f} متر (المسار ثابت)"
    else:
        dist_desc = f"{int(raw_d)} سم (المسار ثابت)"

    if gp < 15:
        gas_desc = f"{gp}% (هواء نقي ومفيش أي ريحة غاز 🟢)"
    elif gp < 40:
        gas_desc = f"{gp}% (روائح خفيفة عادية 🟡)"
    else:
        gas_desc = f"{gp}% (⚠️ في تسريب غاز خلي بالك!)"

    if lp > 60:
        light_desc = f"{lp}% (إضاءة قوية وساطعة ☀️)"
    elif lp > 20:
        light_desc = f"{lp}% (إضاءة الغرفة معتدلة 💡)"
    else:
        light_desc = f"{lp}% (المكان عتمة وهادي 🌑)"

    score = d.get('score', 0)
    score_desc = "🟢 أمان تام 100%" if score < 30 else ("🟡 في ملاحظات بسيطة" if score < 70 else "🔴 في تنبيه خطر!")

    # Outdoor weather line
    ow = get_outdoor_weather()
    outdoor_temp = ow.get("temp")
    outdoor_desc = _wmo_to_arabic(ow.get("weathercode", 0))
    indoor_temp = d.get("temp", 25)
    clothing = _clothing_advice(outdoor_temp or 25, indoor_temp)
    weather_line = (
        f"\n🌤️ *الجو برا:* {outdoor_temp}°C — {outdoor_desc}\n"
        f"👕 *نصيحة:* {clothing}"
    ) if outdoor_temp is not None else ""

    greeting = random.choice(greetings)
    return (
        f"{greeting}\n\n"
        f"🌡️ *درجة الحرارة جوا:* {d.get('temp', '-')}°C\n"
        f"💧 *نسبة الرطوبة:* {d.get('hum', '-')}%\n"
        f"💨 *مستوى الغاز:* {gas_desc}\n"
        f"💡 *شدة الإضاءة:* {light_desc}\n"
        f"📏 *حساس المسافة:* {dist_desc}\n"
        f"📶 *الواي فاي:* {d.get('wifi', '-')} ({d.get('rssi', '-')} dBm)\n"
        f"🪤 *لمسات الفخ:* {d.get('honeypot_hits', 0)} محاولة\n"
        f"🛡️ *حالة الأمان العامة:* *{score_desc}*"
        f"{weather_line}"
    )


def get_human_alert_msg(name: str, data: dict) -> str:
    alerts_map = {
        "fire": [
            "🔥 *إلحق يا باشا! في اشتباه حريق أو حرارة عالية جداً في البيت!* اتأكد من المكان فوراً وكلم حد يطمن!",
            "🚨 *يا غالي درجة الحرارة نطت فجأة بشكل خطير!* جهاز Guardian مسجل ارتفاع حرارة كبير، شيك فوراً!",
        ],
        "gas": [
            "💨 *يا باشا في تسريب غاز حالا في البيت!* خلي بالك ومتولعش أي نور واقفل محبس الغاز فوراً!",
            "🚨 *إلحق يا غالي! حساس الغاز رصد تسريب عالي جداً!* اتصرف فوراً وخلي حد يهوي المكان!",
        ],
        "motion": [
            "🏃 *يا باشا في حركة غريبة اترصدت قدام الجهاز!* في حد بيتحرك قريب من الحساس، شيك كدا!",
            "⚠️ *يا غالي الرادار لقط جسم بيقرب وبيتحرك قدام الجهاز!* خلي عينك على البيت.",
        ],
        "tamper": [
            "⚠️ *يا كابتن في حد بيلعب في الجهاز أو فتحه!* تم رصد تسريب ضوء داخل العلبة محاولة عبث بالجهاز!",
            "🚨 *يا باشا في حد بيحرك جهاز Guardian أو فكه!* اتأكد إن الجهاز في مكانه ومفيش حد بيلعب فيه.",
        ],
        "evil_twin": [
            "📡 *يا باشا في محاولة اختراق واي فاي (شبكة مزيفة)!* في جهاز بيحاول يقلد شبكة البيت.",
        ],
        "deauth": [
            "🛑 *يا غالي في هجوم فصل واي فاي (Deauth Attack)!* حد بيحاول يقطع اتصال الأجهزة بالراوتر.",
        ],
        "honeypot": [
            "🪤 *يا باشا المصيدة اصطادت متطفل!* في جهاز غريب على الشبكة حاول يدخل على السيرفر المزيف للجهاز!",
        ],
    }
    candidates = alerts_map.get(name, [f"⚠️ *تحذير أمني عاجل:* تم رصد {name}!"])
    base = random.choice(candidates)
    return (
        f"{base}\n\n"
        f"📊 *درجة الخطر:* {data.get('score', 0)}\n"
        f"🌡️ *الحرارة:* {data.get('temp', '-')}°C | 💨 *الغاز:* {data.get('gas_pct', data.get('gas', '-'))}%\n"
        f"🌐 *الجهاز:* {data.get('device', 'guardian-01')}"
    )


app = FastAPI(title="Guardian")

state = {"data": None, "seen": 0.0, "safe_status": {"status": "idle", "ts": 0}}
cmd_box = {"pending": None}
prev_alerts = {}
events = deque(maxlen=1000)


# ---------- tamper-evident event log (hash chain) ----------
def _hash(prev: str, ev: dict) -> str:
    body = json.dumps({"ts": ev["ts"], "kind": ev["kind"], "detail": ev["detail"]}, sort_keys=True)
    return hashlib.sha256((prev + body).encode()).hexdigest()


def add_event(kind: str, detail: str):
    prev = events[-1]["hash"] if events else "0" * 64
    ev = {"ts": round(time.time(), 3), "kind": kind, "detail": detail, "prev": prev}
    ev["hash"] = _hash(prev, ev)
    events.append(ev)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev) + "\n")


def chain_ok() -> bool:
    evs = list(events)
    for i, ev in enumerate(evs):
        if i > 0 and ev["prev"] != evs[i - 1]["hash"]:
            return False
        if _hash(ev["prev"], ev) != ev["hash"]:
            return False
    return True


if os.path.exists(LOG_FILE):
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass


# ---------- auth ----------
def admin(x_admin_token: str = Header(default="")):
    if not hmac.compare_digest(x_admin_token.encode(), ADMIN_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="bad admin token")


# ---------- ESP -> server ----------
@app.post("/api/esp/report")
async def esp_report(request: Request, x_device_key: str = Header(default="")):
    if not hmac.compare_digest(x_device_key.encode(), DEVICE_KEY.encode()):
        raise HTTPException(status_code=401, detail="bad device key")
    data = await request.json()
    now = time.time()

    was_online = state["data"] is not None and now - state["seen"] < ONLINE_WINDOW
    if not was_online:
        add_event("device_online", "%s on '%s'" % (data.get("device"), data.get("wifi")))

    alerts = data.get("alerts", {})
    alert_names_ar = {
        "fire": "حريق 🔥",
        "gas": "تسريب غاز 💨",
        "motion": "حركة غريبة 🏃",
        "tamper": "عبث بالجهاز ⚠️",
        "evil_twin": "شبكة واي فاي مزيفة 📡",
        "deauth": "هجوم فصل واي فاي 🛑",
        "honeypot": "اختراق ومصيدة متطفلين 🪤"
    }

    for name, val in alerts.items():
        if val and not prev_alerts.get(name):
            add_event("alert", "%s (score %s)" % (name, data.get("score")))
            # Check cooldown (send WhatsApp at most once per minute per alert)
            last_t = last_wa_alert.get(name, 0)
            if now - last_t > 60:
                last_wa_alert[name] = now
                send_whatsapp(get_human_alert_msg(name, data))
        elif not val and prev_alerts.get(name):
            add_event("cleared", name)
    prev_alerts.update(alerts)

    # Safe result tracking
    safe_res = data.get("safe_result")
    if safe_res and safe_res != "idle":
        prev_status = state.get("safe_status", {}).get("status")
        state["safe_status"] = {"status": safe_res, "ts": now}
        if prev_status == "opening":
            if safe_res == "opened":
                add_event("safe_opened", "تم فتح الخزنة بنجاح 🔓")
                send_whatsapp(get_human_safe_opened_msg())
            elif safe_res == "not_found":
                add_event("safe_not_found", "تعذر العثور على شبكة الخزنة 📡❌")
                send_whatsapp(get_human_safe_failed_msg("not_found"))
            elif safe_res == "failed":
                add_event("safe_failed", "فشل إرسال إشارة فتح الخزنة ⚠️")
                send_whatsapp(get_human_safe_failed_msg("failed"))

    state["data"] = data
    state["seen"] = now

    cmd = cmd_box["pending"]
    cmd_box["pending"] = None
    if cmd:
        add_event("command_sent", cmd)
    return {"cmd": cmd or "none"}


# ---------- WhatsApp Webhook (Two-way Chat) ----------
@app.post("/api/wa/webhook")
async def wa_webhook(request: Request):
    try:
        body = await request.json()
        sender = body.get("sender", "")
        text = str(body.get("text", "")).strip().lower()

        if not text:
            return {"status": "ignored"}

        d = state.get("data")
        now = time.time()
        is_online = d is not None and now - state.get("seen", 0) < ONLINE_WINDOW

        # 1. Status query
        if any(w in text for w in ["طمني", "حالة", "status", "الحرارة", "البيت", "تقرير"]):
            if not is_online:
                reply = "⚠️ *تنبيه:* جهاز Guardian غير متصل حالياً بالسيرفر!"
            else:
                reply = get_human_status_msg(d)
            send_whatsapp(reply)

        # 1b. Weather / clothing query
        elif any(w in text for w in ["الجو", "طقس", "weather", "البس", "لبس", "برا", "خارج"]):
            w = get_outdoor_weather()
            indoor = d.get("temp") if d else None
            outdoor = w.get("temp")
            desc = _wmo_to_arabic(w.get("weathercode", 0))
            advice = _clothing_advice(outdoor or 25, indoor or 25)
            indoor_str = f"{indoor}°C" if indoor else "مش متاح"
            outdoor_str = f"{outdoor}°C" if outdoor is not None else "مش متاح"
            weather_opts = [
                f"🌤️ *الجو برا دلوقتي يا باشا:* {outdoor_str} — {desc}\n"
                f"🏠 *وجو البيت جوا:* {indoor_str}\n\n"
                f"👕 *نصيحة اللبس:*\n{advice}",
                f"☀️ *طيب يا غالي الجو برا* {outdoor_str} والوصف: {desc}\n"
                f"🌡️ *داخل البيت* عندك {indoor_str}\n\n"
                f"🧥 *إيه اللازم تلبسه:*\n{advice}",
                f"🌍 *الجو في القاهرة دلوقتي* {outdoor_str} — {desc}\n"
                f"🏠 *وجوا البيت عندك* {indoor_str}\n\n"
                f"💡 *توصيتي:*\n{advice}",
            ]
            send_whatsapp(random.choice(weather_opts))

        # 2. Clear command
        elif any(w in text for w in ["تصفير", "clear", "مسح"]):
            cmd_box["pending"] = "clear"
            add_event("command_queued", "clear (via WhatsApp)")
            send_whatsapp("✅ تم يا باشا، مسحتلك كل التنبيهات وصفرت العدادات.")

        # 3. Recalibrate command
        elif any(w in text for w in ["معايرة", "recalibrate"]):
            cmd_box["pending"] = "recalibrate"
            add_event("command_queued", "recalibrate (via WhatsApp)")
            send_whatsapp("🔄 جاري إعادة ضبط ومعايرة الحساسات على جو البيت دلوقتي...")

        # 4. Open safe command
        elif any(w in text for w in ["افتح الخزنة", "افتح الخزنه", "خزنة", "خزنه", "open safe", "فتح الخزنة", "فتح الخزنه"]):
            cmd_box["pending"] = "open_safe"
            state["safe_status"] = {"status": "opening", "ts": time.time()}
            if state.get("data"):
                state["data"]["safe_result"] = "idle"
            add_event("command_queued", "open_safe (via WhatsApp)")
            ack_options = [
                "🔓 من عيوني يا باشا! أمر فتح الخزنة اتبعت للجهاز وهيفتحهالك في ثواني معدودة.. هبلغك أول ما تفتح! 🫡",
                "🗝️ أمرك اتنفذ يا غالي، جاري إرسال إشارة الفتح للخزنة دلوقتي حالا.. ثواني وهتفتح معاك 💪",
                "✨ حاضر يا كابتن، بعت الإشارة لجهاز Guardian وهيفتح الخزنة فوراً، وهبعتلك تأكيد أول ما تفتح 🛡️",
            ]
            send_whatsapp(random.choice(ack_options))

        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ---------- dashboard / app API ----------
@app.get("/api/state")
def get_state(_=Depends(admin)):
    now = time.time()
    has = state["data"] is not None
    return {
        "online": has and now - state["seen"] < ONLINE_WINDOW,
        "age": round(now - state["seen"], 1) if has else None,
        "data": state["data"],
        "pending_cmd": cmd_box["pending"],
        "safe_status": state.get("safe_status", {"status": "idle", "ts": 0}),
        "weather": get_outdoor_weather(),
    }


@app.get("/api/weather")
def get_weather(_=Depends(admin)):
    w = get_outdoor_weather()
    return {
        "temp": w.get("temp"),
        "windspeed": w.get("windspeed"),
        "weathercode": w.get("weathercode", 0),
        "description": _wmo_to_arabic(w.get("weathercode", 0)),
        "indoor_temp": (state["data"] or {}).get("temp"),
        "clothing_advice": _clothing_advice(w.get("temp", 25), (state["data"] or {}).get("temp", 25)),
    }


@app.get("/api/events")
def get_events(_=Depends(admin)):
    return {"chain_ok": chain_ok(), "events": list(events)[-50:]}


@app.post("/api/cmd/{name}")
def send_cmd(name: str, _=Depends(admin)):
    if name not in ALLOWED_CMDS:
        raise HTTPException(status_code=400, detail="unknown command")
    cmd_box["pending"] = name
    if name == "open_safe":
        state["safe_status"] = {"status": "opening", "ts": time.time()}
        if state.get("data"):
            state["data"]["safe_result"] = "idle"
    add_event("command_queued", name)
    return {"queued": name}


DASHBOARD = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Guardian</title>
<style>
:root{--bg:#f4f4f2;--card:#fff;--tx:#1f1f1d;--mut:#6b6b66;--ok:#3B6D11;--warn:#BA7517;--bad:#A32D2D;--line:#e2e1db}
@media(prefers-color-scheme:dark){:root{--bg:#1c1c1a;--card:#2a2a27;--tx:#ecebe5;--mut:#a3a29a;--line:#3d3d39;--ok:#97C459;--warn:#EF9F27;--bad:#F09595}}
body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,sans-serif}
.w{max-width:780px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.row{display:flex;gap:14px;flex-wrap:wrap}.k{flex:1;min-width:120px}
.k small{color:var(--mut);display:block}.k b{font-size:19px;font-weight:500}
#score{font-size:60px;font-weight:500;line-height:1}
.chip{display:inline-block;padding:4px 10px;border-radius:999px;border:1px solid var(--line);margin:3px;font-size:14px;color:var(--mut)}
.chip.on{background:var(--bad);color:#fff;border-color:var(--bad)}
button,input{padding:10px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--tx);font-size:15px}
button{cursor:pointer}button.danger{border-color:var(--bad);color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px;border-bottom:1px solid var(--line);text-align:start}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-left:6px;background:var(--bad)}.dot.on{background:var(--ok)}
.mut{color:var(--mut);font-size:14px}
</style></head><body><div class="w">
<div class="card" id="tokcard" style="display:none"><b>ادخل كلمة سر الداشبورد (ADMIN_TOKEN)</b><br><br>
<input id="tokin" type="password" style="direction:ltr"> <button id="tokbtn">دخول</button></div>
<div class="card"><span class="dot" id="dot"></span><b id="onl">...</b>
<span class="mut" id="pend"></span>
<div class="row" style="margin-top:10px">
<div class="k"><small>الجهاز</small><b id="dev">-</b></div>
<div class="k"><small>الشبكة</small><b id="wifi">-</b></div>
<div class="k"><small>IP</small><b id="ip">-</b></div>
<div class="k"><small>الإشارة</small><b id="rssi">-</b></div></div></div>
<div class="card"><div class="row"><div class="k"><small>درجة الخطر</small><div id="score">-</div></div>
<div class="k" style="flex:3"><small>التنبيهات</small><div id="alerts"></div></div></div></div>
<div class="card"><div class="row">
<div class="k"><small>الحرارة</small><b id="temp">-</b></div>
<div class="k"><small>الرطوبة</small><b id="hum">-</b></div>
<div class="k"><small>الغاز (القاعدة)</small><b id="gas">-</b></div>
<div class="k"><small>الضوء (القاعدة)</small><b id="light">-</b></div>
<div class="k"><small>المسافة</small><b id="dist">-</b></div>
<div class="k"><small>فخ: عدد اللمسات</small><b id="hp">-</b></div></div></div>
<div class="card"><b>تحكم</b><br><br>
<button onclick="cmd('clear')">تصفير التنبيهات</button>
<button onclick="cmd('recalibrate')">إعادة المعايرة</button>
<button style="background:#E67E22;color:#fff;border-color:#E67E22" onclick="cmd('open_safe','سيتصل جهاز Guardian بشبكة الخزنة ويفتحها. متأكد؟')">🔓 فتح الخزنة</button>
<button class="danger" onclick="cmd('reset_wifi','الجهاز هيفصل من الشبكة وهيفتح شبكة الإعداد بتاعته. بعدها السيرفر مش هيشوفه لحد ما تعمله إعداد من الموبايل. متأكد؟')">فصل الجهاز عن الشبكة</button>
<div class="mut" style="margin-top:8px">الأمر بيوصل للجهاز في خلال 5 ثواني تقريبا.</div></div>
<div class="card"><b>سجل الأحداث</b> <span id="chain" class="mut"></span>
<table><thead><tr><th>الوقت</th><th>النوع</th><th>التفاصيل</th></tr></thead><tbody id="ev"></tbody></table></div>
</div><script>
const $=id=>document.getElementById(id);
let tok=localStorage.getItem('gtok')||'';
const hdr=()=>({'X-Admin-Token':tok});
const AL=[['fire','حريق'],['gas','غاز'],['motion','حركة'],['tamper','عبث'],['evil_twin','شبكة مزيفة'],['deauth','هجوم فصل'],['honeypot','فخ']];
function set(id,v){$(id).textContent=(v===undefined||v===null||v==='')?'-':v;}
$('tokbtn').onclick=()=>{tok=$('tokin').value.trim();localStorage.setItem('gtok',tok);tick();};
async function tick(){
  try{
    const r=await fetch('/api/state',{headers:hdr()});
    if(r.status===401){$('tokcard').style.display='block';return;}
    $('tokcard').style.display='none';
    const s=await r.json(),d=s.data;
    $('dot').className='dot'+(s.online?' on':'');
    set('onl',s.online?'متصل':(d?'غير متصل (آخر ظهور من '+s.age+' ثانية)':'لسه مفيش جهاز اتصل'));
    set('pend',s.pending_cmd?'  أمر في الانتظار: '+s.pending_cmd:'');
    if(d){
      set('dev',d.device);set('wifi',d.wifi);set('ip',d.ip);set('rssi',d.rssi+' dBm');
      set('temp',d.temp+' C');set('hum',d.hum+' %');
      let gp = d.gas_pct !== undefined ? d.gas_pct : Math.min(100, Math.max(0, Math.round(((d.gas - d.gas_base)/600)*100)));
      let lp = d.light_pct !== undefined ? d.light_pct : Math.min(100, Math.max(0, Math.round(((1400 - d.light)/1280)*100)));
      set('gas', gp + '%');
      set('light', lp + '%');
      let dst = d.dist < 0 ? 'المسار خالي' : (d.dist >= 100 ? (d.dist/100).toFixed(2) + ' م' : Math.round(d.dist) + ' سم');
      set('dist', dst);
      set('hp',d.honeypot_hits+(d.honeypot_ip?' ('+d.honeypot_ip+')':''));
      $('score').textContent=d.score;
      $('score').style.color=d.level===2?'var(--bad)':(d.level===1?'var(--warn)':'var(--ok)');
      const box=$('alerts');box.textContent='';
      AL.forEach(a=>{const c=document.createElement('span');c.className='chip'+(d.alerts[a[0]]?' on':'');c.textContent=a[1];box.append(c);});
    }
    const er=await fetch('/api/events',{headers:hdr()}),e=await er.json();
    $('chain').textContent=e.chain_ok?'(السجل سليم)':'(تحذير: السجل اتلعب فيه!)';
    const tb=$('ev');tb.textContent='';
    e.events.slice().reverse().slice(0,30).forEach(x=>{
      const tr=document.createElement('tr');
      [new Date(x.ts*1000).toLocaleTimeString(),x.kind,x.detail].forEach(t=>{const td=document.createElement('td');td.textContent=t;tr.append(td);});
      tb.append(tr);
    });
  }catch(x){set('onl','السيرفر مش بيرد');}
}
async function cmd(n,ask){
  if(ask&&!confirm(ask))return;
  await fetch('/api/cmd/'+n,{method:'POST',headers:hdr()});tick();
}
setInterval(tick,2000);tick();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD
