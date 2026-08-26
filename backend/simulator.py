"""
PowerStep Grid — Simulation Engine (v2)
=========================================
يحاكي هذا الملف سلوك النظام الفيزيائي بالكامل: توليد الطاقة من البلاط،
الاستهلاك، شحن وحدة التخزين، استشعار الإشغال عبر تحليل إشارة الواي فاي
(WiFi RSSI) بالذكاء الاصطناعي، التوفير المالي، والتنبيه الصوتي عند الهدر.
"""

import math
import random
import time
from collections import deque
from dataclasses import dataclass, field


NUM_TILES = 12
ENERGY_PER_STEP_J = 2.0
STORAGE_CAPACITY_WH = 3.0
BASE_LOAD_W = 3.0
LED_LOAD_W = 2.0
CHARGING_LOAD_W = 5.0
SIM_SPEED = 90
DAY_START_HOUR = 7.0
DAY_END_HOUR = 18.0
FAULTY_TILE_ID = 5
HISTORY_MAXLEN = 600

RSSI_BASELINE_DBM = -75.0
RSSI_EMPTY_STD = 0.8
RSSI_OCCUPIED_STD = 4.2
RSSI_OCCUPIED_MEAN_SHIFT = -3.0
RSSI_WINDOW = 8
RSSI_VARIANCE_THRESHOLD = 4.0

ELECTRICITY_TARIFF_EGP_PER_KWH = 2.15

# --- إعدادات وضع الهاردوير الحقيقي (Live Hardware Mode) ---
HARDWARE_CAPACITOR_FARADS = 100e-6   # قيمة المكثف المستخدَم فعليًا (100 µF) — عدّليها لو استخدمتي قيمة مختلفة
HARDWARE_STEP_VOLTAGE_THRESHOLD = 0.5  # أي قراءة فولت أعلى من ده تُحتسب كـ"خطوة" حقيقية
HARDWARE_TIMEOUT_SECONDS = 10        # لو معدّى وقت أطول من كده من غير قراءة، نرجع لوضع المحاكاة تلقائيًا
WIFI_HARDWARE_TIMEOUT_SECONDS = 10   # نفس الفكرة بس لبيانات الواي فاي الحقيقية


def footfall_rate(hour: float) -> float:
    peaks = [8, 10, 12, 14, 16]
    rate = 3.0
    for p in peaks:
        rate += 40 * math.exp(-((hour - p) ** 2) / (2 * 0.15 ** 2))
    return rate


@dataclass
class Tile:
    id: int
    efficiency: float = 1.0
    cumulative_wh: float = 0.0
    voltage_v: float = 5.0
    current_ma: float = 0.0

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
    cumulative_savings_egp: float = 0.0

    footfall_now: float = 0.0
    true_occupancy: bool = False
    classified_occupancy: bool = False
    rssi_now: float = RSSI_BASELINE_DBM
    rssi_history_short: deque = field(default_factory=lambda: deque(maxlen=RSSI_WINDOW))

    power_source: str = "harvested"
    waste_state: str = "normal"

    hardware_connected: bool = False
    hardware_voltage_now: float = 0.0
    hardware_cumulative_gen_wh: float = 0.0
    hardware_step_count: int = 0
    last_hardware_reading_time: float = 0.0

    wifi_hardware_connected: bool = False
    last_wifi_hardware_reading_time: float = 0.0

    tiles: list = field(default_factory=lambda: [Tile(i) for i in range(1, NUM_TILES + 1)])
    loads: dict = field(default_factory=dict)
    alerts: list = field(default_factory=list)

    history_t: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_gen: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_con: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_soc: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_footfall: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    history_rssi: deque = field(default_factory=lambda: deque(maxlen=60))


class PowerStepSimulator:
    """محرك المحاكاة الرئيسي — استدعِ tick() كل ثانية تقريبًا."""

    def __init__(self):
        self.state = SimState()
        self._last_real_time = time.time()
        self._elapsed_sim_seconds_today = 0.0

    # -------------------------------------------------------
    def ingest_wifi_reading(self, rssi: float):
        """
        تُستدعى من app.py لما توصل قراءة RSSI حقيقية من ESP32 مخصَّص لاستشعار
        الواي فاي. بتغذّي نفس نافذة التذبذب (rssi_history_short) اللي بيحسب
        عليها النموذج قرار "مشغولة/فاضية" — لكن دلوقتي ببيانات حقيقية 100%.
        """
        s = self.state

        # لو ده أول قراءة حقيقية بعد ما كنا في وضع محاكاة، لازم نفضّي البافر
        # الأول عشان مانخلطش قراءات محاكاة وهمية مع قراءات حقيقية في نفس الحساب
        if not s.wifi_hardware_connected:
            s.rssi_history_short.clear()

        s.rssi_now = round(rssi, 2)
        s.rssi_history_short.append(s.rssi_now)
        s.history_rssi.append(s.rssi_now)
        s.last_wifi_hardware_reading_time = time.time()
        s.wifi_hardware_connected = True

        if len(s.rssi_history_short) >= 4:
            mean_r = sum(s.rssi_history_short) / len(s.rssi_history_short)
            variance = sum((x - mean_r) ** 2 for x in s.rssi_history_short) / len(s.rssi_history_short)
            s.classified_occupancy = variance > RSSI_VARIANCE_THRESHOLD

    # -------------------------------------------------------
    def _check_wifi_hardware_timeout(self):
        s = self.state
        if s.wifi_hardware_connected and (time.time() - s.last_wifi_hardware_reading_time) > WIFI_HARDWARE_TIMEOUT_SECONDS:
            s.wifi_hardware_connected = False
            s.rssi_history_short.clear()

    # -------------------------------------------------------
    def ingest_hardware_reading(self, voltage: float, tile_id: int = 1):
        """
        تُستدعى مباشرة من app.py لما توصل قراءة حقيقية من ESP32 عبر /api/ingest.
        بتحسب الطاقة التقريبية من الجهد المقاس فعليًا على المكثف، باستخدام
        قانون طاقة المكثف: E = 0.5 × C × V²
        """
        s = self.state
        s.hardware_voltage_now = round(voltage, 3)
        s.last_hardware_reading_time = time.time()
        s.hardware_connected = True

        if voltage > HARDWARE_STEP_VOLTAGE_THRESHOLD:
            energy_j = 0.5 * HARDWARE_CAPACITOR_FARADS * (voltage ** 2)
            energy_wh = energy_j / 3600.0
            s.hardware_cumulative_gen_wh += energy_wh
            s.hardware_step_count += 1

    # -------------------------------------------------------
    def _check_hardware_timeout(self):
        s = self.state
        if s.hardware_connected and (time.time() - s.last_hardware_reading_time) > HARDWARE_TIMEOUT_SECONDS:
            s.hardware_connected = False

    # -------------------------------------------------------
    def reset(self):
        """
        إعادة تشغيل المحاكاة من الصفر فورًا (يوم 1، الساعة 7 صباحًا).
        """
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

        self._check_hardware_timeout()
        self._check_wifi_hardware_timeout()
        self._simulate_tile_degradation(dt_sim_min)
        self._simulate_generation(dt_sim_min)
        self._simulate_wifi_sensing()
        self._simulate_loads_and_consumption(dt_sim_min)
        self._simulate_storage(dt_sim_min)
        self._update_savings(dt_sim_min)
        self._update_history()
        self._update_alerts_and_waste_state()

    # -------------------------------------------------------
    def _start_new_day(self):
        s = self.state
        s.day_number += 1
        s.sim_hour = DAY_START_HOUR
        self._elapsed_sim_seconds_today = 0.0
        s.cumulative_gen_wh = 0.0
        s.cumulative_con_wh = 0.0
        s.cumulative_savings_egp = 0.0
        s.history_t.clear(); s.history_gen.clear(); s.history_con.clear()
        s.history_soc.clear(); s.history_footfall.clear(); s.history_rssi.clear()

    # -------------------------------------------------------
    def _simulate_tile_degradation(self, dt_min):
        for tile in self.state.tiles:
            if tile.id == FAULTY_TILE_ID:
                progress = min(self._elapsed_sim_seconds_today / (3600 * 6), 1.0)
                tile.efficiency = max(0.55, 1.0 - progress * 0.45)

    # -------------------------------------------------------
    def _simulate_generation(self, dt_min):
        s = self.state
        rate = footfall_rate(s.sim_hour)
        steps_this_tick = max(0, random.gauss(rate * dt_min, math.sqrt(max(rate * dt_min, 0.01))))
        s.footfall_now = steps_this_tick / max(dt_min, 1e-6)

        total_energy_j = 0.0
        total_efficiency = sum(t.efficiency for t in s.tiles) or 1.0
        for tile in s.tiles:
            share = steps_this_tick / NUM_TILES
            tile_energy_j = tile.step_energy_j(ENERGY_PER_STEP_J) * share
            total_energy_j += tile_energy_j
            tile.cumulative_wh += tile_energy_j / 3600.0

        energy_wh = total_energy_j / 3600.0
        s.generation_w = (energy_wh / max(dt_min / 60.0, 1e-9)) if dt_min > 0 else 0.0
        s.cumulative_gen_wh += energy_wh

        for tile in s.tiles:
            tile_power_w = s.generation_w * (tile.efficiency / total_efficiency)
            tile.voltage_v = round(5.0 + random.uniform(-0.15, 0.15), 2)
            tile.current_ma = round((tile_power_w / tile.voltage_v) * 1000, 1)

    # -------------------------------------------------------
    def _simulate_wifi_sensing(self):
        s = self.state

        if s.wifi_hardware_connected:
            # فيه بيانات واي فاي حقيقية واصلة دلوقتي — سيبي التصنيف زي ما اتحسب
            # بالفعل جوه ingest_wifi_reading()، ومتحطيش true_occupancy وهمية
            # لأننا مالناش "حقيقة فعلية" نقارن بيها في وضع الهاردوير الحقيقي.
            return

        occupancy_prob = min(0.97, s.footfall_now / 25.0)
        s.true_occupancy = random.random() < occupancy_prob

        if s.true_occupancy:
            rssi = random.gauss(RSSI_BASELINE_DBM + RSSI_OCCUPIED_MEAN_SHIFT, RSSI_OCCUPIED_STD)
        else:
            rssi = random.gauss(RSSI_BASELINE_DBM, RSSI_EMPTY_STD)

        s.rssi_now = round(rssi, 2)
        s.rssi_history_short.append(s.rssi_now)
        s.history_rssi.append(s.rssi_now)

        if len(s.rssi_history_short) >= 4:
            mean_r = sum(s.rssi_history_short) / len(s.rssi_history_short)
            variance = sum((x - mean_r) ** 2 for x in s.rssi_history_short) / len(s.rssi_history_short)
            s.classified_occupancy = variance > RSSI_VARIANCE_THRESHOLD
        else:
            s.classified_occupancy = False

    # -------------------------------------------------------
    def _simulate_loads_and_consumption(self, dt_min):
        s = self.state

        led_on = s.classified_occupancy
        charging_on = s.storage_soc_wh > (STORAGE_CAPACITY_WH * 0.8)

        s.dc_load_w = BASE_LOAD_W + (LED_LOAD_W if led_on else 0) + (CHARGING_LOAD_W if charging_on else 0)

        energy_wh = s.dc_load_w * (dt_min / 60.0)
        s.cumulative_con_wh += energy_wh
        s.consumption_w = s.dc_load_w

        s.loads = {
            "sensors_gateway": {"name": "حساسات النظام + بوابة ESP32", "state": "ON (دائم)", "priority": "حرج"},
            "corridor_led": {"name": "إضاءة LED إرشادية بالممر", "state": "ON (تصنيف: مشغولة)" if led_on else "OFF (تصنيف: فاضية)", "priority": "متوسط"},
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
    def _update_savings(self, dt_min):
        s = self.state
        gen_for_savings = s.hardware_cumulative_gen_wh if s.hardware_connected else s.cumulative_gen_wh
        offset_wh = min(gen_for_savings, s.cumulative_con_wh)
        s.cumulative_savings_egp = (offset_wh / 1000.0) * ELECTRICITY_TARIFF_EGP_PER_KWH

    # -------------------------------------------------------
    def _update_history(self):
        s = self.state
        s.history_t.append(round(s.sim_hour, 3))
        s.history_gen.append(round(s.cumulative_gen_wh, 4))
        s.history_con.append(round(s.cumulative_con_wh, 4))
        s.history_soc.append(round(s.storage_soc_wh, 4))
        s.history_footfall.append(round(s.footfall_now, 1))

    # -------------------------------------------------------
    def _update_alerts_and_waste_state(self):
        s = self.state
        alerts = []
        waste_triggered = False

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
            waste_triggered = True

        if (not s.wifi_hardware_connected) and s.classified_occupancy and not s.true_occupancy:
            alerts.append({"level": "warning", "text": "تنبيه: إضاءة LED تعمل رغم عدم وجود إشغال فعلي (خطأ تصنيف مؤقت في مستشعر الواي فاي)"})
            waste_triggered = True

        s.waste_state = "waste" if waste_triggered else "normal"
        s.alerts = alerts[:6]

    # -------------------------------------------------------
    def snapshot(self) -> dict:
        s = self.state

        if s.hardware_connected:
            effective_gen_wh = s.hardware_cumulative_gen_wh
        else:
            effective_gen_wh = s.cumulative_gen_wh

        self_sufficiency = (effective_gen_wh / s.cumulative_con_wh * 100) if s.cumulative_con_wh > 0 else 0.0
        hh = int(s.sim_hour)
        mm = int((s.sim_hour - hh) * 60)
        return {
            "day": s.day_number,
            "sim_time": f"{hh:02d}:{mm:02d}",
            "data_source": "hardware" if s.hardware_connected else "simulated",
            "wifi_data_source": "hardware" if s.wifi_hardware_connected else "simulated",
            "hardware_voltage_now": s.hardware_voltage_now,
            "hardware_step_count": s.hardware_step_count,
            "generation_w": round(s.generation_w, 2),
            "consumption_w": round(s.consumption_w, 2),
            "self_sufficiency_pct": round(min(self_sufficiency, 100), 1),
            "storage_soc_pct": round((s.storage_soc_wh / STORAGE_CAPACITY_WH) * 100, 1),
            "cumulative_gen_wh": round(effective_gen_wh, 3),
            "cumulative_con_wh": round(s.cumulative_con_wh, 3),
            "savings_egp": round(s.cumulative_savings_egp, 3),
            "tariff_egp_per_kwh": ELECTRICITY_TARIFF_EGP_PER_KWH,
            "footfall": round(s.footfall_now, 1),
            "true_occupancy": s.true_occupancy,
            "classified_occupancy": s.classified_occupancy,
            "rssi_now": s.rssi_now,
            "rssi_recent": list(s.history_rssi)[-30:],
            "power_source": s.power_source,
            "waste_state": s.waste_state,
            "loads": s.loads,
            "alerts": s.alerts,
            "tiles": [{"id": t.id, "efficiency_pct": round(t.efficiency * 100, 1),
                       "cumulative_wh": round(t.cumulative_wh, 4),
                       "voltage_v": t.voltage_v, "current_ma": t.current_ma} for t in s.tiles],
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