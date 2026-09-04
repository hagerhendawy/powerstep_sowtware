"""
csi_udp_logger.py
==================
يستقبل حزم CSI من الـ ESP32 عبر UDP (بروتوكول عالي الأداء، بدون overhead
الـ Serial أو HTTP) ويسجلها بنفس شكل ملف RSSI اللي زميلك شغال عليه بالظبط:
عمود source، عمود data (نص JSON)، وعمود ts — عشان يبقى متوافق مباشرة مع
نفس خط أنابيب تدريب الموديل بتاعه من غير ما يحتاج يغيّر حاجة في الكود بتاعه.

اللابل (مشغول/فاضي) بيتحدد من الـ ESP32 نفسه — بالأمر اللي بتبعتيه في صندوق
الإرسال بـ Arduino Serial Monitor ('1' أو '0')، وبييجي جوه كل حزمة تلقائيًا.
يعني: سيبي Serial Monitor مفتوح في نافذة، وشغّلي السكريبت ده في نافذة تانية
(Command Prompt)، واكتبي 1/0 في الـ Serial Monitor وانتي بتجمعي البيانات.

التشغيل:
    python csi_udp_logger.py

(مفيش مكتبات إضافية مطلوبة — socket و csv و json و struct كلها built-in في بايثون)
"""

import socket
import struct
import csv
import json
from datetime import datetime

UDP_PORT = 5005
NODE_ID = "corridor_node_1"   # 🔧 غيّريه لو البلاطة/العقدة دي اسمها مختلف عند زميلك
CSV_FILENAME = f"csi_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

# ⚠️ تنسيق الحزمة لازم يطابق الـ struct CsiPacket بتاع كود الـ ESP32 بالظبط:
#   uint32_t timestamp_ms | int8_t rssi | uint16_t num_subcarriers |
#   float mean_amp | float std_amp | uint8_t label
# "<" = little-endian بدون padding (مطابق لـ __attribute__((packed)) في الكود)
PACKET_FORMAT = "<IbHffB"
PACKET_SIZE = struct.calcsize(PACKET_FORMAT)


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", UDP_PORT))

    print(f"📡 في انتظار بيانات CSI على المنفذ {UDP_PORT} ...")
    print(f"💾 هيتسجل بنفس شكل ملف RSSI (source, data, ts) في: {CSV_FILENAME}")
    print("تأكدي إن Serial Monitor مفتوح واكتبي 1 (مشغول) أو 0 (فاضي) وانتي بتجمعي.")
    print("اضغطي Ctrl+C هنا لإيقاف التسجيل.\n")

    count = 0
    label_counts = {0: 0, 1: 0}

    # encoding="utf-8-sig" يضيف BOM في أول الملف — بالظبط زي ملف RSSI الأصلي
    with open(CSV_FILENAME, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["source", "data", "ts"])

        try:
            while True:
                raw, addr = sock.recvfrom(1024)

                if len(raw) != PACKET_SIZE:
                    # حزمة بحجم غير متوقع (تلف أثناء النقل) — نتجاهلها
                    continue

                _esp_ts_ms, rssi, num_subcarriers, mean_amp, std_amp, label = struct.unpack(PACKET_FORMAT, raw)

                # ⚠️ الـ ESP32 مبعتش وقت حقيقي (millis() بس عداد تشغيل)، فالـ "ts"
                # جوه الـ JSON وبره بيتسجلوا بتوقيت اللابتوب لحظة الاستقبال —
                # بالظبط زي ما ملف RSSI الأصلي عامل (فيه فرق بسيط بين الاتنين
                # لأنهم بيتسجلوا في لحظتين متتاليتين، مش نفس اللحظة بالظبط)
                inner_ts = datetime.now().isoformat()
                data_obj = {
                    "node_id": NODE_ID,
                    "rssi": round(float(rssi), 1),
                    "mean_amp": round(float(mean_amp), 3),
                    "std_amp": round(float(std_amp), 3),
                    "num_subcarriers": int(num_subcarriers),
                    "label": int(label),
                    "ts": inner_ts,
                }
                outer_ts = datetime.now().isoformat()

                writer.writerow(["csi", json.dumps(data_obj), outer_ts])

                count += 1
                label_counts[label] = label_counts.get(label, 0) + 1

                if count % 50 == 0:
                    label_txt = "مشغول" if label == 1 else "فاضي"
                    print(f"✅ اتسجل {count} عينة | فاضي={label_counts[0]} مشغول={label_counts[1]} | آخر لابل: {label_txt}")

        except KeyboardInterrupt:
            print(f"\n🛑 اتوقف التسجيل.")
            print(f"إجمالي العينات: {count}  (فاضي={label_counts[0]}, مشغول={label_counts[1]})")
            print(f"الملف: {CSV_FILENAME}")


if __name__ == "__main__":
    main()