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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6I_7jC0Uzt6KNfIwnhlMUu59C76NcM-kQrpNtNJe3r72A")
ONLINE_WINDOW = 15            # seconds without a report = offline
LOG_FILE = "events.jsonl"
ALLOWED_CMDS = {"clear", "recalibrate", "reset_wifi", "open_safe"}

# WhatsApp Configuration
WA_SERVER = os.getenv("WA_SERVER", "https://server-watsapp2.onrender.com")
WA_SESSION = os.getenv("WA_SESSION", "session1")
WA_NUMBER = os.getenv("WA_NUMBER", "201274283037")
last_wa_alert = {}

# ---------- Outdoor Weather Cache (Open-Meteo) ----------
_weather_cache = {}  # key: (lat_round, lon_round) -> {"data": dict, "ts": float}
WEATHER_LAT_DEFAULT = 30.0444
WEATHER_LON_DEFAULT = 31.2357
WEATHER_REFRESH_SEC = 900  # 15 minutes


def get_outdoor_weather(lat: float = None, lon: float = None) -> dict:
    """Fetch current weather from Open-Meteo for given or default coordinates."""
    now = time.time()
    lat = float(lat) if lat is not None else WEATHER_LAT_DEFAULT
    lon = float(lon) if lon is not None else WEATHER_LON_DEFAULT
    cache_key = (round(lat, 2), round(lon, 2))

    cached = _weather_cache.get(cache_key)
    if cached and now - cached["ts"] < WEATHER_REFRESH_SEC:
        return cached["data"]

    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current_weather=true&hourly=relativehumidity_2m&forecast_days=1"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "GuardianSecurity/2.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        cw = raw.get("current_weather", {})
        result = {
            "temp": cw.get("temperature"),
            "windspeed": cw.get("windspeed"),
            "weathercode": cw.get("weathercode", 0),
            "is_day": cw.get("is_day", 1),
            "lat": lat,
            "lon": lon,
        }
        _weather_cache[cache_key] = {"data": result, "ts": now}
        return result
    except Exception as e:
        print(f"[Weather] Failed for ({lat},{lon}): {e}")
        if cached:
            return cached["data"]
        return {"temp": 25.0, "windspeed": 10.0, "weathercode": 0, "is_day": 1, "lat": lat, "lon": lon}


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
    return "طقس معتدل 🌤️"


def _clothing_advice(outdoor_temp: float, indoor_temp: float) -> str:
    """Generate clothing advice based on outdoor temperature and indoor difference."""
    diff = (indoor_temp or 25) - (outdoor_temp or 25)
    if outdoor_temp >= 36:
        base = "الجو ناري وشديد الحرارة برا! البس هدوم قطنية خفيفة وفاتحة اللون، واشرب ميه كتير عشان تتجنب ضربات الشمس 🥵💧."
    elif outdoor_temp >= 30:
        base = "الجو حر برا، تيشيرت خفيف هيكون ممتاز، بس خلي معاك زجاجة ميه تحسباً للعطش 👕☀️."
    elif outdoor_temp >= 23:
        base = "الجو جميل ومعتدل جداً برا، لبس عادي مريح مناسب للخروج اليوم 🌤️."
    elif outdoor_temp >= 16:
        base = "في نسمة برد خفيفة برا، يُفضل تلبس جاكيت خفيف أو سترة مريحة فوق الهدوم 🧥."
    elif outdoor_temp >= 10:
        base = "الجو بارد برا! البس كنزة تقيلة وجاكيت دافي عشان متلقطش برد 🧣."
    else:
        base = "الجو قارس البرودة! لازم تتقل كويس جداً وتلبس جاكيت تقيل وكوفية قبل ما تخرج ❄️🧤."

    if diff >= 6:
        base += f" وخلي بالك البيت جوا أدفى بـ {int(diff)} درجات، هتحس بلسعة برد أول ما تخرج!"
    elif diff <= -6:
        base += f" والبيت جوا أبرد بـ {int(abs(diff))} درجات، برا هتلاقي الجو أحر بكتير."
    return base


def _eye_protection_warning(indoor_light_pct: int, is_day: int, outdoor_code: int) -> str:
    """Eye health protection: warns when leaving a dark home into bright outdoor daylight."""
    if is_day == 1 and indoor_light_pct < 45:
        return (
            "🕶️ *تحذير صحي لحماية العين:* البيت عندك مظلم نسبياً وبرا في شمس ونور نهار ساطع! "
            "الخروج المفاجئ للشمس بيرهق حدقة العين ويسبب انقباض وإجهاد فوري. "
            "يُنصح بلبس نظارة شمسية 🕶️ أو تشغيل إضاءة الغرفة تدريجياً لدقيقة قبل الخروج لحماية نظرك."
        )
    return ""


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


def call_gemini_llm(user_msg: str, system_prompt: str) -> str:
    """Call Google Gemini LLM using user API key with fallback."""
    if not GEMINI_API_KEY:
        return ""
    models = ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.5-flash"]
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        payload = json.dumps({
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_msg}]}],
            "generationConfig": {"temperature": 0.75, "maxOutputTokens": 450}
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=9) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                if text and text.strip():
                    return text.strip()
        except Exception as e:
            print(f"[Gemini] {model} fail: {e}")
            continue
    return ""


# ---------- Guardian AI Assistant Engine ----------
def guardian_ai_brain(message: str, location_name: str = "", lat: float = None, lon: float = None) -> dict:
    """Intelligent context-aware assistant for Guardian smart home security."""
    d = state.get("data") or {}
    text = (message or "").strip().lower()

    # Telemetry
    gas_pct = d.get("gas_pct", min(100, max(0, int(((d.get("gas", 0) - d.get("gas_base", 0)) / 600.0) * 100))))
    light_pct = d.get("light_pct", min(100, max(0, int(((1400.0 - d.get("light", 0)) / 1280.0) * 100))))
    indoor_temp = d.get("temp", 25.0)
    indoor_hum = d.get("hum", 50.0)
    score = d.get("score", 0)
    alerts = d.get("alerts", {})
    safe_status = state.get("safe_status", {}).get("status", "idle")

    # Outdoor Weather & Eye Protection
    ow = get_outdoor_weather(lat, lon)
    outdoor_temp = ow.get("temp", 25.0)
    is_day = ow.get("is_day", 1)
    weather_desc = _wmo_to_arabic(ow.get("weathercode", 0))
    clothing = _clothing_advice(outdoor_temp, indoor_temp)
    eye_warning = _eye_protection_warning(light_pct, is_day, ow.get("weathercode", 0))

    loc_str = location_name if location_name else "موقعك الحالي"

    # 1. Primary: True Gemini Generative AI Call
    system_prompt = (
        f"أنت 'الحارس الذكي' (Guardian AI) لنظام أمن وحماية المنزل الذكي Guardian.\n"
        f"تتحدث بلهجة مصرية ودية وطبيعية جداً وبشرية 100% (زي: يا باشا، يا غالي، متقلقش، كله في السليم، عينيا الاتنين).\n"
        f"بيانات وحساسات البيت لايف في هذه اللحظة بالملّي:\n"
        f"- شدة الإضاءة والنور: {light_pct}% ({'المكان منور 💡' if light_pct >= 50 else 'النور مطفي والمكان مظلم 🌑'})\n"
        f"- الغاز والأدخنة: {gas_pct}% ({'هواء نقي ومستقر وطبيعي 🟢' if gas_pct < 20 else 'في مؤشر غاز أو تسريب 🚨'})\n"
        f"- حرارة البيت جوا: {indoor_temp}°C (رطوبة {indoor_hum}%)\n"
        f"- حالة الخزنة: {safe_status} ({'مقفولة ومؤمنة تماماً' if safe_status != 'opened' else 'مفتوحة حالياً'})\n"
        f"- رادار الحركة والمسافة: {d.get('dist', -1)} سم ({'المسار خالي ومفيش اقتراب' if alerts.get('motion', 0) == 0 else 'تم رصد حركة واقتراب!'})\n"
        f"- مؤشر الخطر الأمني: {score}/100\n"
        f"- موقع المستخدم برا في الشارع: {loc_str}\n"
        f"- طقس الشارع في موقع المستخدم: {outdoor_temp}°C ({weather_desc})\n"
        f"- نصيحة اللبس المحسوبة لطقسه: {clothing}\n"
        f"- قاعدة حماية العين: {'البيت مظلم وبرا نهار شمس ساطعة، انصحه يلبس نظارة شمس 🕶️ أو يشغل النور تدريجياً لدقيقة قبل ما يخرج لحماية نظره وشبكية عينه من الإبهار المفاجئ' if (is_day == 1 and light_pct < 45) else 'طبيعي'}\n\n"
        f"قواعد الإجابة:\n"
        f"1. لو المستخدم سألك: 'هو أنا نسيت النور منور؟' أو أي سؤال عن النور، جاوبه مباشرة وبدقة وبشرية عن حالة النور والأرقام وطمنه.\n"
        f"2. لو قالك: 'أنا خارج برا خلي بالك من البيت' أو رايح مشوار، طمنه إن وضع الحراسة نشط، اديله تقرير سريع ومطمئن عن البيت (النور مطفي/منور، الغاز سليم، الخزنة مقفولة)، وانصحه باللبس المناسب لطقس موقعه مع نصيحة النظارة الشمسية لو البيت مظلم وبرا نهار.\n"
        f"3. خلي إجابتك ذكية وسريعة ومفيدة ومكتوبة بالمصري الطبيعي بدون تكلف مع إيموجي خفيفة معبرة."
    )

    llm_reply = call_gemini_llm(message, system_prompt)
    if llm_reply:
        return {
            "reply": llm_reply,
            "weather": ow,
            "location": loc_str,
            "eye_warning": eye_warning,
            "indoor_temp": indoor_temp,
            "outdoor_temp": outdoor_temp,
            "light_pct": light_pct,
            "gas_pct": gas_pct,
            "safe_status": safe_status,
        }

    # Intent 1: Did I leave the lights on? / Light status
    if any(k in text for k in ["نور", "الضوء", "اللمبة", "منور", "مطفي", "اضاءة", "إضاءة"]):
        if light_pct >= 50:
            reply = (
                f"💡 *أيوة يا باشا، النور منور حالياً في المكان!* شدة الإضاءة واصلة لـ {light_pct}%.\n"
                f"لو أنت برا أو مش محتاج النور، افتكر تطفيه لتوفير الطاقة وحماية الأجهزة 🔌."
            )
        else:
            reply = (
                f"🌑 *لا يا غالي متقلقش خالص! النور مطفي والغرفة عتمة تماماً* (شدة الإضاءة {light_pct}% بس).\n"
                f"البيت هادي ومفيش أي نور شغال على الفاضي، متطمن تماماً 🫡✨."
            )

    # Intent 2: Going out / Watch the house
    elif any(k in text for k in ["خارج", "برا البيت", "رايح", "مسافر", "خلي بالك", "احرس", "مش في البيت", "هخرج"]):
        reply = (
            f"🫡 *تروح وترجع بألف سلامة يا باشا! البيت في أمان تام وتحت حراستي المشددة.* 🛡️\n\n"
            f"📋 *فحص سريع قبل ما تتحرك:*\n"
            f"• 💡 النور: {'منور (' + str(light_pct) + '%)' if light_pct >= 50 else 'مطفي تماماً ومظلم (' + str(light_pct) + '%) 🟢'}\n"
            f"• 💨 الغاز والأدخنة: {'هواء نقي وطبيعي 🟢' if gas_pct < 20 else '⚠️ في مؤشر روائح/غاز (' + str(gas_pct) + '%)'}\n"
            f"• 🔐 الخزنة: {'مقفولة ومؤمنة في السليم 🔐' if safe_status != 'opened' else 'مفتوحة حالياً 🔓'}\n"
            f"• 🌡️ حرارة البيت جوا: {indoor_temp}°C (رطوبة {indoor_hum}%)\n\n"
            f"📍 *الطقس في {loc_str}:* {outdoor_temp}°C — {weather_desc}\n"
            f"👕 *نصيحة الخروج:* {clothing}\n"
        )
        if eye_warning:
            reply += f"\n{eye_warning}\n"
        reply += "\nعين الرادار مش هتغفل ثانية، وهبعتلك فوري على الموبايل والواتساب لو حصل أي طارئ! 💪✨"

    # Intent 3: Weather, clothing & eye protection advice
    elif any(k in text for k in ["الجو", "طقس", "البس", "لبس", "عين", "نظارة", "حرارة برا", "الشارع"]):
        reply = (
            f"🌤️ *حالة الطقس لـ {loc_str}:*\n"
            f"• درجة الحرارة برا: *{outdoor_temp}°C* ({weather_desc})\n"
            f"• حرارة البيت عندك جوا: *{indoor_temp}°C*\n"
            f"• الفرق: البيت {'أدفى' if indoor_temp > outdoor_temp else 'أبرد'} بـ {abs(round(indoor_temp - outdoor_temp, 1))}°C.\n\n"
            f"👕 *نصيحة اللبس والخروج:*\n{clothing}\n"
        )
        if eye_warning:
            reply += f"\n{eye_warning}"

    # Intent 4: Gas or smoke check
    elif any(k in text for k in ["غاز", "تسريب", "ريحة", "دخان", "حريق"]):
        if gas_pct >= 40:
            reply = f"🚨 *تحذير عاجل!* حساس الغاز مسجل نسبة تسريب {gas_pct}%! اتصرف فوراً وماتشغلش أي نور واقفل محبس الغاز."
        elif gas_pct >= 15:
            reply = f"🟡 في روائح خفيفة مسجلة بنسبة {gas_pct}%، لكن مفيش خطر مباشر. الجهاز بيراقب باستمرار."
        else:
            reply = f"🟢 *الهواء نقي تماماً ومفيش أي تسريب غاز يا باشا!* نسبة الغاز {gas_pct}% وكل شيء سليم 100%."

    # Intent 5: Safe box status
    elif any(k in text for k in ["خزنة", "خزنه", "قفل", "سيرفو"]):
        reply = (
            f"🔐 *حالة الخزنة دلوقتي:* *{safe_status}*\n"
            f"جهاز الحارس جاهز لتنفيذ أمر الفتح في أي لحظة تضغط فيها على زر فتح الخزنة 🗝️."
        )

    # Intent 6: Overall security & movement check
    elif any(k in text for k in ["طمني", "حالة", "أمان", "حركة", "في حد", "خطر", "حرامي"]):
        dist_val = d.get("dist", -1)
        dist_text = "المسار خالي ومفيش اقتراب" if dist_val < 0 or dist_val > 80 else f"في جسم قريب على بعد {int(dist_val)} سم"
        reply = (
            f"🛡️ *تقرير الحارس الذكي الشامل:*\n"
            f"• مؤشر الخطر الأمني: *{score}/100* ({'🟢 آمن تماماً' if score < 30 else '🟡 في ملاحظات' if score < 70 else '🔴 خطر'})\n"
            f"• حركة الرادار: {dist_text}\n"
            f"• الغاز: {gas_pct}% | الإضاءة: {light_pct}%\n"
            f"• حرارة البيت: {indoor_temp}°C\n"
            f"• الشبكة: {d.get('wifi', '-')} متصلة ومحمية من هجمات الـ Evil Twin.\n"
            f"كل حاجة تحت الملاحظة ومفيش أي قلق يا فندم! 🫡"
        )

    # General AI conversation fallback
    else:
        reply = (
            f"أهلاً بيك يا باشا! أنا المساعد الذكي لنظام Guardian الأمني 🤖🛡️.\n"
            f"البيت دلوقتي {'في أمان تام ومستقر 🟢' if score < 30 else 'تحت المراقبة والملاحظة'}.\n"
            f"تقدر تسألني عن: إضاءة البيت (هل نسيت النور منور؟)، تسريب الغاز، حالة الخزنة، "
            f"أو تقولي إنك خارج برا عشان أديك نصيحة اللبس والطقس لموقعك وحماية عينك من شمس النهار."
        )

    return {
        "reply": reply,
        "weather": ow,
        "location": loc_str,
        "eye_warning": eye_warning,
        "indoor_temp": indoor_temp,
        "outdoor_temp": outdoor_temp,
        "light_pct": light_pct,
        "gas_pct": gas_pct,
        "safe_status": safe_status,
    }


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

        # 5. AI Assistant Intelligent Fallback (handles "نسيت النور", "خارج برا", etc.)
        else:
            ai_res = guardian_ai_brain(message=text)
            send_whatsapp(ai_res["reply"])

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
def get_weather(lat: float = None, lon: float = None, _=Depends(admin)):
    w = get_outdoor_weather(lat, lon)
    indoor = (state["data"] or {}).get("temp", 25.0)
    outdoor = w.get("temp", 25.0)
    light_pct = (state["data"] or {}).get("light_pct", 50)
    is_day = w.get("is_day", 1)
    eye_warn = _eye_protection_warning(light_pct, is_day, w.get("weathercode", 0))

    return {
        "temp": outdoor,
        "windspeed": w.get("windspeed"),
        "weathercode": w.get("weathercode", 0),
        "is_day": is_day,
        "description": _wmo_to_arabic(w.get("weathercode", 0)),
        "indoor_temp": indoor,
        "difference": round(indoor - outdoor, 1),
        "clothing_advice": _clothing_advice(outdoor, indoor),
        "eye_warning": eye_warn,
        "lat": w.get("lat"),
        "lon": w.get("lon"),
    }


@app.post("/api/ai/chat")
async def ai_chat(request: Request, _=Depends(admin)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = body.get("message", "")
    location = body.get("location", "")
    lat = body.get("lat")
    lon = body.get("lon")

    result = guardian_ai_brain(message=message, location_name=location, lat=lat, lon=lon)
    return result


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
