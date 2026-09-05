/*
  ============================================================
  PowerStep Grid - Counter (Probe + CSI)
  ============================================================
  - Probe Requests: counts actual devices
  - CSI: detects motion (fallback for silent devices)
  - Real CSI data (device_count_raw, csi_variance, csi_people_estimate)
    is sent with every event for wifi_dataset.csv logging.
  ============================================================
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <string.h>
#include <math.h>

extern "C" {
  #include "esp_wifi.h"
  #include "esp_wifi_types.h"
}

// ================== Network Settings ==================
const char* WIFI_SSID     = "WE9A3A17";
const char* WIFI_PASSWORD = "aa1c04e2";
const char* SERVER_IP   = "192.168.1.16";
const int   SERVER_PORT = 8000;
// ====================================================

// ================== CSI Settings ==================
#define CSI_BUFFER_SIZE    30
#define MOVEMENT_THRESHOLD 0.15

struct CSISample {
  float amplitude;
  unsigned long timestamp;
};

CSISample csiBuffer[CSI_BUFFER_SIZE];
int bufferIndex = 0;
int sampleCount = 0;
float baseline = 0.0;
bool calibrated = false;

// ================== CSI Callback ==================
void csi_rx_cb(void *ctx, wifi_csi_info_t *info) {
  if (!info || !info->buf) return;
  
  float sum = 0.0;
  int count = 0;
  
  for (int i = 0; i < info->len; i += 2) {
    int16_t real = info->buf[i];
    int16_t imag = info->buf[i+1];
    float amplitude = sqrt(real*real + imag*imag);
    sum += amplitude;
    count++;
  }
  
  if (count > 0) {
    float avgAmplitude = sum / count;
    csiBuffer[bufferIndex].amplitude = avgAmplitude;
    csiBuffer[bufferIndex].timestamp = millis();
    bufferIndex = (bufferIndex + 1) % CSI_BUFFER_SIZE;
    if (sampleCount < CSI_BUFFER_SIZE) sampleCount++;
  }
}

// Calculate Variance
float calculateVariance() {
  if (sampleCount < 10) return 0.0;
  
  float mean = 0.0;
  for (int i = 0; i < sampleCount; i++) {
    mean += csiBuffer[i].amplitude;
  }
  mean /= sampleCount;
  
  float variance = 0.0;
  for (int i = 0; i < sampleCount; i++) {
    float diff = csiBuffer[i].amplitude - mean;
    variance += diff * diff;
  }
  return variance / sampleCount;
}

// Enable CSI
void setupCSI() {
  wifi_csi_config_t csi_config = {
    .lltf_en = true,
    .htltf_en = true,
    .stbc_htltf2_en = true,
    .ltf_merge_en = true,
    .channel_filter_en = false,
    .manu_scale = false,
    .shift = 0
  };
  
  esp_wifi_set_csi_config(&csi_config);
  esp_wifi_set_csi_rx_cb(&csi_rx_cb, NULL);
  esp_wifi_set_csi(true);
}

// ================== Device Counting (Probe) ==================
#define MAX_DEVICES 100
#define DEVICE_TIMEOUT_MS 60000

struct SeenDevice {
  uint8_t mac[6];
  unsigned long lastSeen;
};

SeenDevice seenDevices[MAX_DEVICES];
int deviceCount = 0;
unsigned long lastSendTime = 0;

void wifi_sniffer_packet_handler(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT) return;
  
  wifi_promiscuous_pkt_t *pkt = (wifi_promiscuous_pkt_t *)buf;
  uint8_t *payload = pkt->payload;
  
  uint8_t frameSubType = (payload[0] & 0xF0) >> 4;
  if (frameSubType != 0x4) return;
  
  uint8_t *srcMac = &payload[10];
  
  for (int i = 0; i < deviceCount; i++) {
    if (memcmp(seenDevices[i].mac, srcMac, 6) == 0) {
      seenDevices[i].lastSeen = millis();
      return;
    }
  }
  
  if (deviceCount < MAX_DEVICES) {
    memcpy(seenDevices[deviceCount].mac, srcMac, 6);
    seenDevices[deviceCount].lastSeen = millis();
    deviceCount++;
  }
}

int getActiveDeviceCount() {
  unsigned long now = millis();
  int writeIndex = 0;
  for (int i = 0; i < deviceCount; i++) {
    if (now - seenDevices[i].lastSeen <= DEVICE_TIMEOUT_MS) {
      if (writeIndex != i) seenDevices[writeIndex] = seenDevices[i];
      writeIndex++;
    }
  }
  deviceCount = writeIndex;
  return deviceCount;
}

// ================== Send Data ==================
void sendDataToServer(int deviceCount_, int csiPeople, float variance) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("No WiFi connection");
    return;
  }

  HTTPClient http;
  String url = "http://" + String(SERVER_IP) + ":" + String(SERVER_PORT) + "/api/ingest";
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(4000);

  // ---------- Fusion Logic ----------
  int finalCount = deviceCount_;  // Start with device count
  
  // If there is motion (CSI) and device count is 0 -> at least 1 person
  if (csiPeople > 0 && deviceCount_ == 0) {
    finalCount = 1;
  }
  
  // If there is motion and device count is > 0 -> use the bigger number
  if (csiPeople > deviceCount_ && csiPeople > 0) {
    finalCount = csiPeople;
  }
  // ------------------------------

  // Send actual CSI data for later training
  String jsonPayload = "{\"people_count\": " + String(finalCount) +
                        ", \"device_count_raw\": " + String(deviceCount_) +
                        ", \"csi_variance\": " + String(variance, 4) +
                        ", \"csi_people_estimate\": " + String(csiPeople) + "}";
  int httpCode = http.POST(jsonPayload);

  if (httpCode > 0) {
    Serial.print("Sent - Code: ");
    Serial.print(httpCode);
    Serial.print(" | Devices: ");
    Serial.print(deviceCount_);
    Serial.print(" | CSI Var: ");
    Serial.print(variance);
    Serial.print(" | CSI Est: ");
    Serial.print(csiPeople);
    Serial.print(" | Final: ");
    Serial.println(finalCount);
  } else {
    Serial.print("Failed: ");
    Serial.println(http.errorToString(httpCode));
  }
  http.end();
}

// ================== Setup ==================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("=========================================");
  Serial.println("  PowerStep Grid - Counter (Probe + CSI)");
  Serial.println("=========================================");

  // WiFi
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  Serial.print("Connecting");
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi connection failed");
    while (true) delay(1000);
  }

  Serial.print("IP: ");
  Serial.println(WiFi.localIP());

  // Enable Sniffer
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_promiscuous_rx_cb(&wifi_sniffer_packet_handler);

  // Enable CSI
  setupCSI();
  
  Serial.println("CSI enabled, calculating baseline for 10 seconds...");
  delay(10000);
  
  float variance = calculateVariance();
  baseline = variance * 1.5;
  calibrated = true;
  
  Serial.print("Baseline = ");
  Serial.println(baseline);

  lastSendTime = millis();
}

// ================== Loop ==================
void loop() {
  if (millis() - lastSendTime >= 5000) {
    lastSendTime = millis();

    // 1. Count devices
    int deviceCount_ = getActiveDeviceCount();
    
    // 2. Measure CSI
    float variance = calculateVariance();
    int csiPeople = 0;
    if (calibrated && variance > baseline) {
      // Rough estimation based on variance ratio
      float ratio = variance / baseline;
      if (ratio < 2.0) csiPeople = 1;
      else if (ratio < 4.0) csiPeople = 2;
      else csiPeople = 3;  // Max 3 from CSI
    }
    
    // 3. Serial output
    Serial.println("---------------------");
    Serial.print("Devices: "); Serial.print(deviceCount_);
    Serial.print(" | CSI Var: "); Serial.print(variance);
    Serial.print(" (Motion: ");
    Serial.print(csiPeople > 0 ? "YES" : "NO");
    Serial.print(") | CSI Est: ");
    Serial.println(csiPeople);

    // 4. Send to server
    sendDataToServer(deviceCount_, csiPeople, variance);
  }
}