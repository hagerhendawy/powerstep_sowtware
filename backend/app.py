"""
PowerStep Grid — Backend Server (app.py)
==========================================
السيرفر ده بيعمل حاجتين بس:
  1. يشغّل محرك المحاكاة (simulator.py) في الخلفية، ويحدّثه كل ثانية.
  2. يوفّر API (نقاط اتصال) تقرأ منها لوحة التحكم (frontend) البيانات لحظيًا.

لما نيجي نستخدم بلاط حقيقي وESP32 فعلي بدل المحاكاة، هنضيف مسار POST
اسمه /api/ingest يستقبل قراءات حقيقية من الأجهزة، ونوقف المحاكاة، وباقي
الكود (كل الـ API endpoints + لوحة التحكم) هيفضل شغال زي ما هو بالظبط.
"""

import asyncio
import threading
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from simulator import PowerStepSimulator

app = FastAPI(title="PowerStep Grid API")
# يسمح لأي صفحة ويب (حتى لو مفتوحة من ملف على جهازك) بالتواصل مع السيرفر
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
# السبب: عايزين المحاكاة تتحدّث باستمرار (كل ثانية) حتى لو محدّش بيسأل
# الـ API في نفس اللحظة. الخيط المنفصل ده بيشتغل طول الوقت من لحظة تشغيل
# السيرفر وهو اللي بيحرّك الأرقام.

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
    """بيانات تراكمية لرسم الرسم البياني (توليد مقابل استهلاك على مدار اليوم)."""
    return sim.history()


@app.post("/api/ingest")
def ingest_real_reading(payload: dict):
    """
    نقطة اتصال مستقبلية لاستقبال قراءات حقيقية من ESP32 فعلي.
    شكل البيانات المتوقع (مثال):
    {
        "tile_id": 3,
        "voltage": 4.2,
        "current_ma": 12.5,
        "rssi": -63
    }
    حاليًا الدالة دي مجرد "هيكل جاهز" (Stub) — لسه مش متفعّلة عمليًا لأننا
    في مرحلة السيميوليشن. لما يجهز الهاردوير، هنملأ الدالة دي بمنطق يحدّث
    حالة sim.state مباشرة من القراءة الحقيقية بدل المعادلات الرياضية.
    """
    return {"status": "received_but_not_yet_active", "payload": payload}


# ============================================================
# تقديم ملفات لوحة التحكم (Frontend)
# ============================================================
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
