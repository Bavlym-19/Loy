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


def send_whatsapp(msg: str):
    try:
        url = f"{WA_SERVER}/send-message"
        payload = json.dumps({"sessionId": WA_SESSION, "number": WA_NUMBER, "message": msg}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[WA] Failed to send: {e}")


app = FastAPI(title="Guardian")

state = {"data": None, "seen": 0.0}
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
                ar_title = alert_names_ar.get(name, name)
                wa_msg = (
                    f"🚨 *تحذير أمني عاجل من Guardian!* 🚨\n\n"
                    f"⚠️ الخطر: *{ar_title}*\n"
                    f"📊 درجة الخطر: *{data.get('score', 0)}*\n\n"
                    f"🌡️ الحرارة: {data.get('temp', '-')}°C\n"
                    f"💧 الرطوبة: {data.get('hum', '-') }%\n"
                    f"📏 المسافة: {data.get('dist', '-')} سم\n"
                    f"🌐 الجهاز: {data.get('device', 'guardian')}"
                )
                send_whatsapp(wa_msg)
        elif not val and prev_alerts.get(name):
            add_event("cleared", name)
    prev_alerts.update(alerts)

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
                reply = (
                    "🏠 *تقرير حالة البيت الآن:*\n\n"
                    f"🌡️ الحرارة: {d.get('temp', '-')}°C\n"
                    f"💧 الرطوبة: {d.get('hum', '-') }%\n"
                    f"💨 الغاز: {d.get('gas', '-')} (القاعدة: {d.get('gas_base', '-')})\n"
                    f"📏 المسافة: {d.get('dist', '-')} سم\n"
                    f"📶 الواي فاي: {d.get('wifi', '-')} ({d.get('rssi', '-')} dBm)\n"
                    f"🪤 لمسات الفخ: {d.get('honeypot_hits', 0)}\n"
                    f"🛡️ درجة الخطر: *{d.get('score', 0)}* {'🟢 أمان تام' if d.get('score', 0) == 0 else '🔴 تحذير'}"
                )
            send_whatsapp(reply)

        # 2. Clear command
        elif any(w in text for w in ["تصفير", "clear", "مسح"]):
            cmd_box["pending"] = "clear"
            add_event("command_queued", "clear (via WhatsApp)")
            send_whatsapp("✅ تم إرسال أمر تصفير التنبيهات للجهاز بنجاح.")

        # 3. Recalibrate command
        elif any(w in text for w in ["معايرة", "recalibrate"]):
            cmd_box["pending"] = "recalibrate"
            add_event("command_queued", "recalibrate (via WhatsApp)")
            send_whatsapp("🔄 جاري إعادة معايرة الحساسات...")

        # 4. Open safe command
        elif any(w in text for w in ["افتح الخزنة", "افتح الخزنه", "خزنة", "خزنه", "open safe", "فتح الخزنة", "فتح الخزنه"]):
            cmd_box["pending"] = "open_safe"
            add_event("command_queued", "open_safe (via WhatsApp)")
            send_whatsapp("🔓 جاري إرسال أمر فتح الخزنة... سيصل الأمر للجهاز خلال ثواني.")

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
    }


@app.get("/api/events")
def get_events(_=Depends(admin)):
    return {"chain_ok": chain_ok(), "events": list(events)[-50:]}


@app.post("/api/cmd/{name}")
def send_cmd(name: str, _=Depends(admin)):
    if name not in ALLOWED_CMDS:
        raise HTTPException(status_code=400, detail="unknown command")
    cmd_box["pending"] = name
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
      set('temp',d.temp+' C');set('hum',d.hum+' %');set('gas',d.gas+' ('+d.gas_base+')');
      set('light',d.light+' ('+d.light_base+')');set('dist',d.dist<0?'-':d.dist+' cm');
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
