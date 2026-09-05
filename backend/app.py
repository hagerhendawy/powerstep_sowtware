"""
PowerStep Grid — Backend Server (app.py) — نسخة هجينة (Real + Simulated)
==========================================================================
السيرفر ده بيعمل 3 حاجات:
  1. يشغّل محرك المحاكاة (simulator.py) في الخلفية، ويحدّثه كل ثانية
     (الطاقة، التخزين، درجة الحرارة، ولوحة الأحمال).
  2. يستقبل بيانات حقيقية من ESP32 فعلي عبر /api/ingest (عدد الأشخاص المكتشَف
     عن طريق التنصت على الواي فاي) — دي بقت شغالة فعليًا مش مجرد Stub.
  3. يوفّر API تقرأ منها لوحة التحكم (frontend) البيانات لحظيًا.

مهم: لو الـ ESP32 مش متصل أو مبعتش بيانات من فترة أطول من REAL_DATA_TIMEOUT_SEC
(موجودة في simulator.py)، النظام يرجع تلقائيًا لمحاكاة الإشغال بدل ما يفضل واقف
على آخر رقم — كده الداشبورد يفضل شغالة حتى لو الهاردوير اتقفل بغلط.

ملحوظة: النسخة دي من غير تسجيل بيانات CSV (wifi_dataset.csv) — لو محتاجة
تسجيل بيانات الإشغال الحقيقية لتدريب موديل لاحقًا، ده إضافة منفصلة.
"""

import threading
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from simulator import PowerStepSimulator

app = FastAPI(title="PowerStep Grid API")
# يسمح لأي صفحة ويب (حتى لو مفتوحة من ملف على جهازك، أو من الـ ESP32 نفسه)
# بالتواصل مع السيرفر من غير مشاكل CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

sim = PowerStepSimulator()


# ============================================================
# تشغيل المحاكاة في خيط منفصل (Background Thread)
# ============================================================
# السبب: عايزين المحاكاة (الطاقة + الحرارة) تتحدّث باستمرار (كل ثانية) حتى لو
# محدّش بيسأل الـ API في نفس اللحظة. الخيط ده بيشتغل طول الوقت من لحظة تشغيل
# السيرفر وهو اللي بيحرّك الأرقام. بيانات الإشغال الحقيقية (لو موجودة) بتتدمج
# جوه tick() نفسها عن طريق sim.ingest_occupancy() اللي بتستدعيها /api/ingest.

def _simulation_loop():
    while True:
        sim.tick()
        time.sleep(1.0)


@app.on_event("startup")
def start_background_simulation():
    thread = threading.Thread(target=_simulation_loop, daemon=True)
    thread.start()


# ============================================================
# API Endpoints
# ============================================================

@app.get("/api/live")
def get_live():
    """أهم نقطة اتصال — بترجع كل الأرقام اللحظية اللي الداشبورد محتاجاها."""
    return sim.snapshot()


@app.get("/api/history")
def get_history():
    """بيانات تراكمية لرسم الرسم البياني (توليد/استهلاك/حرارة على مدار اليوم)."""
    return sim.history()


@app.get("/api/status")
def get_status():
    """
    نقطة اتصال خفيفة للتأكد بسرعة إن الـ ESP32 بيوصله فعلاً بيانات حقيقية،
    من غير ما تفتحي الداشبورد كاملة. مفيدة جدًا وقت اختبار الهاردوير.
    افتحيها في المتصفح على: http://localhost:8000/api/status
    """
    s = sim.state
    age = None
    if s.last_real_update_ts > 0:
        age = round(time.time() - s.last_real_update_ts, 1)

    return {
        "occupancy_source": s.occupancy_source,          # "real_esp32" أو "simulated"
        "real_people_count_last_reported": s.real_people_count,
        "seconds_since_last_real_update": age,
        "note": (
            "لو occupancy_source بقت real_esp32 ورقم seconds_since_last_real_update "
            "بيتحدث ويفضل صغير (أقل من 15) -> الاتصال شغال تمام."
        ),
    }


@app.post("/api/ingest")
def ingest_real_reading(payload: dict):
    """
    نقطة الاتصال دي بقت مفعّلة فعليًا (مش Stub زي الأول).

    الشكل المتوقع من الـ ESP32 حاليًا (نظام Probe + CSI المدموج):
        {
          "people_count": 3,          <- الرقم النهائي المدموج (المُستخدَم في المحاكاة)
          "device_count_raw": 2,      <- عدد الأجهزة الخام من Probe (اختياري، بيتجاهَل هنا)
          "csi_variance": 0.42,       <- قيمة التذبذب الخام من CSI (اختياري، بيتجاهَل هنا)
          "csi_people_estimate": 1    <- تقدير CSI لوحده قبل الدمج (اختياري، بيتجاهَل هنا)
        }

    كل الحقول ما عدا people_count اختيارية — النسخة دي بتاخد people_count بس
    وبتتجاهل باقي الحقول (من غير تسجيلهم في أي ملف).
    """
    people_count = payload.get("people_count")

    if people_count is None:
        return {
            "status": "error",
            "message": "الحقل 'people_count' مفقود من البيانات المرسلة. راجعي كود الـ ESP32.",
        }

    try:
        people_count = int(people_count)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "message": "قيمة 'people_count' لازم تكون رقم صحيح (integer).",
        }

    sim.ingest_occupancy(people_count)

    return {
        "status": "ok",
        "received_people_count": people_count,
        "occupancy_source_now": "real_esp32",
    }


@app.post("/api/scenario")
def set_energy_scenario(payload: dict):
    """
    تبديل سيناريو جول/خطوة (رد مباشر على سؤال "الرقم ده افتراض ولا قياس
    فعلي؟"). بدل رقم واحد ثابت، عندنا 3 سيناريوهات صريحة:
        { "scenario": "pessimistic" }   -> 0.3 جول/خطوة
        { "scenario": "realistic"   }   -> 1.0 جول/خطوة (افتراضي)
        { "scenario": "optimistic"  }   -> 2.5 جول/خطوة (رقم Pavegen التسويقي)
    التغيير ده بيأثر على المحاكاة الحية فورًا (مش بس عرض توقعي).
    """
    scenario = payload.get("scenario")
    if scenario is None:
        return {"status": "error", "message": "الحقل 'scenario' مفقود."}

    try:
        sim.set_energy_scenario(scenario)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    return {"status": "ok", "energy_scenario_now": scenario}


@app.get("/api/whatif")
def what_if(num_tiles: int = 12, scenario: str = "realistic"):
    """
    What-If Projection — بيت القصيد وراء الـ Slider التفاعلي في الداشبورد.

    مش بتغيّر أي حاجة في المحاكاة الحية — بس بترجع "لو كان عدد البلاطات
    كذا وسيناريو الطاقة كذا، نسبة الاكتفاء الذاتي المتوقعة هتبقى كام؟" فورًا.
    مثال: /api/whatif?num_tiles=50&scenario=optimistic
    """
    return sim.project(num_tiles=num_tiles, energy_scenario=scenario)


# ============================================================
# تقديم ملفات لوحة التحكم (Frontend)
# ============================================================
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")