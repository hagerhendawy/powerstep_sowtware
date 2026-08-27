"""
PowerStep Grid — Simulation Engine (نسخة هجينة: بيانات حقيقية + محاكاة)
========================================================================
يحاكي هذا الملف سلوك النظام الفيزيائي: توليد الطاقة من البلاط، الاستهلاك،
شحن وحدة التخزين، درجة الحرارة، وتنبيهات الذكاء الاصطناعي.

الجديد في هذه النسخة:
  - الإشغال وعدد الأشخاص بقى ممكن يجي "حقيقي" من ESP32 فعلي (عبر ingest_occupancy)
    بدل ما يكون محاكاة دايمًا. لو معدّى أكتر من REAL_DATA_TIMEOUT_SEC من غير ما
    يوصل تحديث حقيقي، النظام يرجع تلقائيًا للمحاكاة (Fallback آمن).
  - درجة الحرارة لسه محاكاة بالكامل (مفيش حساس حرارة فعلي لحد دلوقتي)، لكن
    منطق "التبريد التلقائي" (تشغيل مروحة/حمل عند ارتفاع الحرارة) شغّال فعليًا
    على القيم المحاكاة، وجاهز يستقبل قراءة حقيقية بنفس الطريقة لاحقًا.
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
COOLING_LOAD_W = 4.0           # حمل مروحة/تبريد تجريبي (تعمل عند ارتفاع الحرارة)
SIM_SPEED = 90                 # 1 ثانية حقيقية = 90 ثانية محاكاة (يوم كامل خلال ~7 دقائق)
DAY_START_HOUR = 7.0
DAY_END_HOUR = 18.0
FAULTY_TILE_ID = 5              # بلاطة تتدهور كفاءتها تدريجيًا (لاختبار الصيانة التنبؤية)
HISTORY_MAXLEN = 600             # عدد النقاط المحفوظة لرسم الرسم البياني التراكمي

# --- إعدادات دمج البيانات الحقيقية (ESP32) ---
REAL_DATA_TIMEOUT_SEC = 15.0    # لو معدّى أكتر من كده من غير تحديث حقيقي -> نرجع للمحاكاة

# --- إعدادات محاكاة درجة الحرارة (لحد ما يتركّب حساس حقيقي) ---
TEMP_BASE_C = 24.0               # متوسط الحرارة في بداية اليوم
TEMP_DAILY_SWING_C = 5.0         # الفرق بين أعلى وأقل حرارة على مدار اليوم
TEMP_OCCUPANCY_BUMP_C = 1.5      # وجود أشخاص بيرفع الحرارة شوية (جسم + أجهزة)
TEMP_SMOOTHING = 0.25            # سرعة تغيّر القراءة نحو القيمة المستهدفة (قصور حراري واقعي)
COOLING_ON_THRESHOLD_C = 27.0    # شغّلي التبريد لو الحرارة وصلت للقيمة دي
COOLING_OFF_THRESHOLD_C = 25.5   # اقفلي التبريد لو الحرارة نزلت للقيمة دي (Hysteresis يمنع الرفرفة)
COOLING_EFFECT_C_PER_MIN = 0.06  # مقدار خفض الحرارة لكل دقيقة محاكاة أثناء التبريد


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

    # --- بيانات الإشغال/عدد الأشخاص: حقيقية أو محاكاة ---
    people_count: int = 0
    occupancy_source: str = "simulated"   # "simulated" أو "real_esp32"
    real_people_count: int = 0
    last_real_update_ts: float = 0.0

    # --- درجة الحرارة (محاكاة) ---
    temperature_c: float = TEMP_BASE_C
    cooling_active: bool = False

    tiles: list = field(default_factory=lambda: [Tile(i) for i in range(1, NUM_TILES + 1)])
    loads: dict = field(default_factory=dict)
    alerts: list = field(default_factory=list)

    history_t: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_gen: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_con: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_soc: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_footfall: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_temp: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))


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
        self._update_occupancy_and_loads(dt_sim_min)   # لازم قبل الحرارة (بتأثر على الحرارة)
        self._simulate_temperature(dt_sim_min)
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
        s.history_soc.clear(); s.history_footfall.clear(); s.history_temp.clear()

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
    def ingest_occupancy(self, people_count: int):
        """
        تُستدعى من app.py لما يوصل تحديث حقيقي من ESP32 عبر /api/ingest.
        بمجرد استدعائها، النظام يعتبر مصدر الإشغال "حقيقي" لمدة REAL_DATA_TIMEOUT_SEC
        ثانية القادمة، وبعدها يرجع تلقائيًا للمحاكاة لو معاد وصله تحديث جديد.
        """
        s = self.state
        s.real_people_count = max(0, int(people_count))
        s.last_real_update_ts = time.time()

    # -------------------------------------------------------
    def _update_occupancy_and_loads(self, dt_min):
        s = self.state

        real_data_is_fresh = (
            s.last_real_update_ts > 0
            and (time.time() - s.last_real_update_ts) < REAL_DATA_TIMEOUT_SEC
        )

        if real_data_is_fresh:
            # === في وضع البيانات الحقيقية: عدد الأشخاص جاي فعليًا من الـ ESP32 ===
            s.occupancy_source = "real_esp32"
            s.people_count = s.real_people_count
            s.occupancy = s.people_count > 0
        else:
            # === مفيش بيانات حقيقية طازة -> رجوع آمن للمحاكاة ===
            s.occupancy_source = "simulated"
            occupancy_prob = min(0.97, s.footfall_now / 25.0)
            s.occupancy = random.random() < occupancy_prob
            s.people_count = random.randint(1, 4) if s.occupancy else 0

        led_on = s.occupancy
        charging_on = s.storage_soc_wh > (STORAGE_CAPACITY_WH * 0.8)
        cooling_on = s.cooling_active

        s.dc_load_w = (
            BASE_LOAD_W
            + (LED_LOAD_W if led_on else 0)
            + (CHARGING_LOAD_W if charging_on else 0)
            + (COOLING_LOAD_W if cooling_on else 0)
        )

        energy_wh = s.dc_load_w * (dt_min / 60.0)
        s.cumulative_con_wh += energy_wh
        s.consumption_w = s.dc_load_w

        s.loads = {
            "sensors_gateway": {"name": "حساسات النظام + بوابة ESP32", "state": "ON (دائم)", "priority": "حرج"},
            "corridor_led": {"name": "إضاءة LED إرشادية بالممر", "state": "ON (تلقائي)" if led_on else "OFF (لا يوجد إشغال)", "priority": "متوسط"},
            "charging_station": {"name": "محطة شحن USB تجريبية", "state": "ON (فائض تخزين)" if charging_on else "Standby", "priority": "منخفض"},
            "cooling_fan": {"name": "مروحة/تبريد تلقائي", "state": "ON (حرارة مرتفعة)" if cooling_on else "OFF", "priority": "متوسط"},
        }

    # -------------------------------------------------------
    def _simulate_temperature(self, dt_min):
        """
        محاكاة منحنى حراري يومي واقعي + تأثير بسيط لوجود أشخاص + منطق تبريد تلقائي
        بـ Hysteresis (عتبة تشغيل مختلفة عن عتبة الإيقاف) لمنع رفرفة المروحة.
        """
        s = self.state
        if dt_min <= 0:
            return

        # منحنى جيبي: أقل حرارة في الصبح، أعلى حرارة في منتصف اليوم تقريبًا
        progress = (s.sim_hour - DAY_START_HOUR) / max(DAY_END_HOUR - DAY_START_HOUR, 1e-6)
        daily_curve = TEMP_BASE_C + TEMP_DAILY_SWING_C * math.sin(math.pi * progress)
        occupancy_bump = TEMP_OCCUPANCY_BUMP_C if s.occupancy else 0.0
        noise = random.uniform(-0.15, 0.15)
        target_temp = daily_curve + occupancy_bump + noise

        # تغيّر تدريجي نحو الهدف (قصور حراري) بدل قفزة مفاجئة
        s.temperature_c += (target_temp - s.temperature_c) * min(1.0, dt_min * TEMP_SMOOTHING)

        # منطق التبريد التلقائي (Hysteresis)
        if s.temperature_c >= COOLING_ON_THRESHOLD_C:
            s.cooling_active = True
        elif s.temperature_c <= COOLING_OFF_THRESHOLD_C:
            s.cooling_active = False

        if s.cooling_active:
            s.temperature_c -= COOLING_EFFECT_C_PER_MIN * dt_min

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
        s.history_temp.append(round(s.temperature_c, 2))

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

        if s.cooling_active:
            alerts.append({"level": "warning", "text": f"درجة الحرارة مرتفعة ({s.temperature_c:.1f}°م) — تم تشغيل التبريد تلقائيًا"})

        if s.occupancy_source == "real_esp32":
            alerts.append({"level": "success", "text": f"بيانات إشغال حقيقية من ESP32 — {s.people_count} شخص مكتشَف الآن"})

        s.alerts = alerts[:6]

    # -------------------------------------------------------
    def snapshot(self) -> dict:
        s = self.state
        self_sufficiency = (s.cumulative_gen_wh / s.cumulative_con_wh * 100) if s.cumulative_con_wh > 0 else 0.0
        hh = int(s.sim_hour)
        mm = int((s.sim_hour - hh) * 60)

        seconds_since_real_update = None
        if s.last_real_update_ts > 0:
            seconds_since_real_update = round(time.time() - s.last_real_update_ts, 1)

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
            "people_count": s.people_count,
            "occupancy_source": s.occupancy_source,          # "real_esp32" أو "simulated"
            "seconds_since_real_update": seconds_since_real_update,
            "temperature_c": round(s.temperature_c, 1),
            "cooling_active": s.cooling_active,
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
            "temp_c": list(s.history_temp),
        }