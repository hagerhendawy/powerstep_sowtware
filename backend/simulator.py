"""
PowerStep Grid — Simulation Engine
===================================
يحاكي هذا الملف سلوك النظام الفيزيائي بالكامل: توليد الطاقة من البلاط،
الاستهلاك، شحن وحدة التخزين، كشف الإشغال، وتنبيهات الذكاء الاصطناعي.

مهم: هذا الملف مصمَّم ليكون "مصدر بيانات" فقط. لوحة التحكم (frontend) وواجهة
الـ API (app.py) لا تعرفان أصلاً إن البيانات محاكاة — فلما تتوفر بلاطات
حقيقية وحساسات ESP32، كل اللي محتاجينه هو استبدال دالة tick() هنا بقراءة
فعلية قادمة من /api/ingest، وباقي النظام (API + Dashboard) يفضل شغال
بدون أي تعديل. راجع دالة ingest_real_reading() في app.py.
"""

import math
import random
import time
from collections import deque
from dataclasses import dataclass, field


# ============================================================
# إعدادات المحاكاة (Simulation Configuration)
# ============================================================

NUM_TILES = 12                 # عدد بلاطات التوليد في النموذج التجريبي
ENERGY_PER_STEP_J = 2.0        # جول/خطوة (افتراض بلاطة كهرومغناطيسية مهندَسة)
STORAGE_CAPACITY_WH = 3.0      # سعة وحدة التخزين (مكثفات + بطارية صغيرة)
BASE_LOAD_W = 3.0              # حمل ثابت: حساسات + بوابة ESP32 (يجب أن يعمل دائمًا)
LED_LOAD_W = 2.0               # إضاءة إرشادية LED (تعمل فقط عند وجود إشغال)
CHARGING_LOAD_W = 5.0          # محطة شحن تجريبية (تعمل فقط عند فائض تخزين)
SIM_SPEED = 90                 # 1 ثانية حقيقية = 90 ثانية محاكاة (يوم كامل خلال ~7 دقائق)
DAY_START_HOUR = 7.0
DAY_END_HOUR = 18.0
FAULTY_TILE_ID = 5              # بلاطة تتدهور كفاءتها تدريجيًا (لاختبار الصيانة التنبؤية)
HISTORY_MAXLEN = 600             # عدد النقاط المحفوظة لرسم الرسم البياني التراكمي


def footfall_rate(hour: float) -> float:
    """معدل الخطوات في الدقيقة بناءً على الساعة من اليوم (ذروات عند تبديل المحاضرات)."""
    peaks = [8, 10, 12, 14, 16]
    rate = 3.0
    for p in peaks:
        rate += 40 * math.exp(-((hour - p) ** 2) / (2 * 0.15 ** 2))
    return rate


@dataclass
class Tile:
    id: int
    efficiency: float = 1.0          # 1.0 = كفاءة كاملة
    cumulative_wh: float = 0.0

    def step_energy_j(self, base_energy_j: float) -> float:
        noise = random.uniform(0.85, 1.15)
        return base_energy_j * self.efficiency * noise


@dataclass
class SimState:
    sim_hour: float = DAY_START_HOUR
    day_number: int = 1

    generation_w: float = 0.0
    consumption_w: float = 0.0
    dc_load_w: float = 0.0

    storage_soc_wh: float = STORAGE_CAPACITY_WH * 0.5
    cumulative_gen_wh: float = 0.0
    cumulative_con_wh: float = 0.0

    footfall_now: float = 0.0
    occupancy: bool = False
    power_source: str = "harvested"   # "harvested" أو "grid_backup"

    tiles: list = field(default_factory=lambda: [Tile(i) for i in range(1, NUM_TILES + 1)])
    loads: dict = field(default_factory=dict)
    alerts: list = field(default_factory=list)

    history_t: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_gen: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_con: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_soc: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_footfall: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))


class PowerStepSimulator:
    """محرك المحاكاة الرئيسي — استدعِ tick() كل ثانية تقريبًا."""

    def __init__(self):
        self.state = SimState()
        self._last_real_time = time.time()
        self._elapsed_sim_seconds_today = 0.0

    # -------------------------------------------------------
    def tick(self):
        now = time.time()
        dt_real = now - self._last_real_time
        self._last_real_time = now
        dt_sim_sec = dt_real * SIM_SPEED
        dt_sim_min = dt_sim_sec / 60.0

        s = self.state
        self._elapsed_sim_seconds_today += dt_sim_sec
        s.sim_hour = DAY_START_HOUR + (self._elapsed_sim_seconds_today / 3600.0)

        if s.sim_hour >= DAY_END_HOUR:
            self._start_new_day()
            return

        self._simulate_tile_degradation(dt_sim_min)
        self._simulate_generation(dt_sim_min)
        self._simulate_occupancy_and_loads(dt_sim_min)
        self._simulate_storage(dt_sim_min)
        self._update_history()
        self._update_alerts()

    # -------------------------------------------------------
    def _start_new_day(self):
        s = self.state
        s.day_number += 1
        s.sim_hour = DAY_START_HOUR
        self._elapsed_sim_seconds_today = 0.0
        s.cumulative_gen_wh = 0.0
        s.cumulative_con_wh = 0.0
        s.history_t.clear(); s.history_gen.clear(); s.history_con.clear()
        s.history_soc.clear(); s.history_footfall.clear()

    # -------------------------------------------------------
    def _simulate_tile_degradation(self, dt_min):
        """بلاطة واحدة تفقد كفاءتها تدريجيًا لمحاكاة عطل حقيقي يكتشفه الذكاء الاصطناعي."""
        for tile in self.state.tiles:
            if tile.id == FAULTY_TILE_ID:
                progress = min(self._elapsed_sim_seconds_today / (3600 * 6), 1.0)
                tile.efficiency = max(0.55, 1.0 - progress * 0.45)

    # -------------------------------------------------------
    def _simulate_generation(self, dt_min):
        s = self.state
        rate = footfall_rate(s.sim_hour)
        steps_this_tick = max(0, random.gauss(rate * dt_min, math.sqrt(max(rate * dt_min, 0.01))))
        s.footfall_now = steps_this_tick / max(dt_min, 1e-6)  # steps/min تقريبي للعرض

        total_energy_j = 0.0
        for tile in s.tiles:
            share = steps_this_tick / NUM_TILES
            total_energy_j += tile.step_energy_j(ENERGY_PER_STEP_J) * share
            tile.cumulative_wh += (tile.step_energy_j(ENERGY_PER_STEP_J) * share) / 3600.0

        energy_wh = total_energy_j / 3600.0
        s.generation_w = (energy_wh / max(dt_min / 60.0, 1e-9)) if dt_min > 0 else 0.0
        s.cumulative_gen_wh += energy_wh

    # -------------------------------------------------------
    def _simulate_occupancy_and_loads(self, dt_min):
        s = self.state
        # استشعار الإشغال عبر محاكاة تذبذب إشارة الواي فاي (RSSI) — احتمالية مبنية على كثافة الحركة
        occupancy_prob = min(0.97, s.footfall_now / 25.0)
        s.occupancy = random.random() < occupancy_prob

        led_on = s.occupancy
        charging_on = s.storage_soc_wh > (STORAGE_CAPACITY_WH * 0.8)

        s.dc_load_w = BASE_LOAD_W + (LED_LOAD_W if led_on else 0) + (CHARGING_LOAD_W if charging_on else 0)

        energy_wh = s.dc_load_w * (dt_min / 60.0)
        s.cumulative_con_wh += energy_wh
        s.consumption_w = s.dc_load_w

        s.loads = {
            "sensors_gateway": {"name": "حساسات النظام + بوابة ESP32", "state": "ON (دائم)", "priority": "حرج"},
            "corridor_led": {"name": "إضاءة LED إرشادية بالممر", "state": "ON (تلقائي)" if led_on else "OFF (لا يوجد إشغال)", "priority": "متوسط"},
            "charging_station": {"name": "محطة شحن USB تجريبية", "state": "ON (فائض تخزين)" if charging_on else "Standby", "priority": "منخفض"},
        }

    # -------------------------------------------------------
    def _simulate_storage(self, dt_min):
        s = self.state
        gen_wh = s.generation_w * (dt_min / 60.0)
        con_wh = s.dc_load_w * (dt_min / 60.0)
        net_wh = gen_wh - con_wh
        s.storage_soc_wh = max(0.0, min(STORAGE_CAPACITY_WH, s.storage_soc_wh + net_wh))

        if s.storage_soc_wh <= STORAGE_CAPACITY_WH * 0.05 and s.dc_load_w > 0:
            s.power_source = "grid_backup"
        elif s.storage_soc_wh > STORAGE_CAPACITY_WH * 0.15:
            s.power_source = "harvested"

    # -------------------------------------------------------
    def _update_history(self):
        s = self.state
        s.history_t.append(round(s.sim_hour, 3))
        s.history_gen.append(round(s.cumulative_gen_wh, 4))
        s.history_con.append(round(s.cumulative_con_wh, 4))
        s.history_soc.append(round(s.storage_soc_wh, 4))
        s.history_footfall.append(round(s.footfall_now, 1))

    # -------------------------------------------------------
    def _update_alerts(self):
        s = self.state
        alerts = []

        for tile in s.tiles:
            if tile.efficiency < 0.80:
                drop = round((1 - tile.efficiency) * 100)
                alerts.append({"level": "warning", "text": f"بلاطة #{tile.id}: تراجع في الأداء (-{drop}%) — يُنصح بالفحص"})

        if s.storage_soc_wh > STORAGE_CAPACITY_WH * 0.9:
            alerts.append({"level": "info", "text": "وحدة التخزين قاربت على الامتلاء الكامل"})

        for peak in [8, 10, 12, 14, 16]:
            if 0 < (peak - s.sim_hour) * 60 <= 20:
                alerts.append({"level": "success", "text": f"نافذة فائض طاقة متوقعة الساعة {peak}:00"})

        if s.footfall_now > 35:
            alerts.append({"level": "warning", "text": "كثافة حركة عالية عند المدخل الآن"})

        if s.power_source == "grid_backup":
            alerts.append({"level": "danger", "text": "التخزين منخفض — تم التحويل للشبكة الاحتياطية"})

        s.alerts = alerts[:6]

    # -------------------------------------------------------
    def snapshot(self) -> dict:
        s = self.state
        self_sufficiency = (s.cumulative_gen_wh / s.cumulative_con_wh * 100) if s.cumulative_con_wh > 0 else 0.0
        hh = int(s.sim_hour)
        mm = int((s.sim_hour - hh) * 60)
        return {
            "day": s.day_number,
            "sim_time": f"{hh:02d}:{mm:02d}",
            "generation_w": round(s.generation_w, 2),
            "consumption_w": round(s.consumption_w, 2),
            "self_sufficiency_pct": round(min(self_sufficiency, 100), 1),
            "storage_soc_pct": round((s.storage_soc_wh / STORAGE_CAPACITY_WH) * 100, 1),
            "cumulative_gen_wh": round(s.cumulative_gen_wh, 3),
            "cumulative_con_wh": round(s.cumulative_con_wh, 3),
            "footfall": round(s.footfall_now, 1),
            "occupancy": s.occupancy,
            "power_source": s.power_source,
            "loads": s.loads,
            "alerts": s.alerts,
            "tiles": [{"id": t.id, "efficiency_pct": round(t.efficiency * 100, 1),
                       "cumulative_wh": round(t.cumulative_wh, 4)} for t in s.tiles],
        }

    def history(self) -> dict:
        s = self.state
        return {
            "t": list(s.history_t),
            "gen_wh": list(s.history_gen),
            "con_wh": list(s.history_con),
            "soc_wh": list(s.history_soc),
            "footfall": list(s.history_footfall),
        }
