#include <Arduino.h>
#include <SPI.h>
#include <Wire.h>
#include <RadioLib.h>
#include <SSD1306Wire.h>

// Heltec WiFi LoRa 32 V3 Pin Definitions
#define SCK 9
#define MISO 11
#define MOSI 10
#define NSS 8
#define DIO1 14
#define NRST 12
#define BUSY 13

// OLED Display & Button Pins
#define SDA_OLED 17
#define SCL_OLED 18
#define RST_OLED 21
#define VEXT_PIN 36
#define PRG_BUTTON 0

SSD1306Wire display(0x3C, SDA_OLED, SCL_OLED, GEOMETRY_128_64);

SPIClass *loraSpi = new SPIClass(FSPI);
SX1262 radio = new Module(NSS, DIO1, NRST, BUSY, *loraSpi);

#define OTA_VERSION_ID 3
#define ELRS_CRC14_POLY 0x2E57

#define FHSS_FREQ_COUNT 40
#define FHSS_SEQUENCE_LEN 240

struct ELRSRateProfile {
  const char *name;
  uint8_t sf;
  float bw_khz;
  uint8_t cr;
  uint32_t interval_us;
  uint32_t toa_us;
  uint8_t hop_interval;
  uint8_t payload_len;
};

const ELRSRateProfile RATE_TABLE[] = {
  {"50Hz",       8, 500.0, 7, 20000, 17024, 4,  8},   // Standard 915MHz 50Hz (SF8, 20ms slot, 80ms hop)
  {"25Hz",       9, 500.0, 7, 40000, 26880, 2,  8},   // Long Range 25Hz (SF9, 40ms slot, 80ms hop)
  {"100Hz",      7, 500.0, 7, 10000, 8512,  8,  8},   // Standard 100Hz 8ch (SF7, 10ms slot, 80ms hop)
  {"100Hz Full", 7, 500.0, 5, 10000, 8768,  8,  13},  // 100Hz Full Res 16ch (SF7, CR 4/5, 13-byte OTA)
  {"D50",        7, 500.0, 7, 10000, 8512,  8,  8},   // Deja Vu 50Hz (SF7, 10ms slot, 80ms hop)
  {"150Hz",      7, 500.0, 5, 6666,  4800,  12, 8},   // 150Hz 8ch (SF7, CR 4/5, 80ms hop)
  {"200Hz",      6, 500.0, 7, 5000,  3200,  16, 8},   // 200Hz 8ch (SF6, 5ms slot, 80ms hop)
  {"250Hz",      6, 500.0, 5, 4000,  2600,  20, 8},   // 250Hz 8ch (SF6, CR 4/5, 80ms hop)
  {"333Hz Full", 5, 500.0, 7, 3000,  2000,  24, 13}   // 333Hz Full Res (SF5, CR 4/7, 13-byte OTA)
};
#define RATE_COUNT (sizeof(RATE_TABLE) / sizeof(RATE_TABLE[0]))

volatile uint8_t g_current_rate_idx = 0;
volatile bool g_auto_rate_scan = true;
volatile int64_t g_last_auto_scan_us = 0;
volatile int64_t g_sync_grace_period_until = 0;

class Crc2Byte {
public:
  uint16_t _crctab[256];
  uint16_t _bitmask;
  uint8_t _bits;
  void init(uint8_t bits, uint16_t poly) {
    _bits = bits;
    _bitmask = (1 << bits) - 1;
    uint16_t highbit = 1 << (bits - 1);
    for (uint16_t i = 0; i < 256; i++) {
      uint16_t crc = (i << (bits - 8)) & _bitmask;
      for (uint8_t j = 0; j < 8; j++)
        crc = ((crc << 1) ^ ((crc & highbit) ? poly : 0)) & _bitmask;
      _crctab[i] = crc;
    }
  }
  uint16_t calc(const uint8_t *data, uint8_t len, uint16_t crc) {
    while (len--) {
      crc = (crc << 8) ^ _crctab[((crc >> (_bits - 8)) ^ (uint16_t)*data++) & 0x00FF];
    }
    return crc & _bitmask;
  }
};

Crc2Byte ota_crc;

// ZERO-KNOWLEDGE DYNAMIC UID & ENCRYPTION STATE
uint8_t discovered_UID[6] = {0, 0, 0, 0, 0, 0};
uint16_t dynamicCrcInit = 0x2156;
bool uidDiscovered = false;

// TARGET PILOT FILTER
volatile bool g_target_lock_enabled = false;
volatile uint8_t g_target_uid[3] = {0, 0, 0}; // target u3, u4, u5

uint8_t FHSSsequence[FHSS_SEQUENCE_LEN];
float freq_table[FHSS_FREQ_COUNT];
uint32_t freq_regs[FHSS_FREQ_COUNT];
volatile uint8_t FHSSptr = 0;
volatile uint8_t OtaNonce = 0;
uint8_t sync_channel = 21; // Channel 21 = 916.1 MHz
volatile uint8_t g_wide_switch_idx = 0;

// Global Telemetry State for OLED Display (Core 0)
volatile float g_rssi = -100.0f;
volatile float g_snr = 0.0f;
volatile uint16_t g_ch[4] = {1500, 1500, 988, 1500};
volatile bool g_isArmed = false;
volatile uint32_t g_packetCount = 0;

// Hardware Timer and RTOS Task handles
hw_timer_t *slot_timer = NULL;
TaskHandle_t hopTaskHandle = NULL;
TaskHandle_t displayTaskHandle = NULL;

volatile bool isSynced = false;
volatile int64_t last_packet_time_us = 0;
volatile bool packetReceived = false;

// Direct SPI register operations
uint8_t readReg8(uint16_t addr) {
  digitalWrite(NSS, LOW);
  loraSpi->transfer(0x1D);
  loraSpi->transfer((addr >> 8) & 0xFF);
  loraSpi->transfer(addr & 0xFF);
  loraSpi->transfer(0x00);
  uint8_t val = loraSpi->transfer(0x00);
  digitalWrite(NSS, HIGH);
  while (digitalRead(BUSY) == HIGH);
  return val;
}

void writeReg8(uint16_t addr, uint8_t val) {
  digitalWrite(NSS, LOW);
  loraSpi->transfer(0x0D);
  loraSpi->transfer((addr >> 8) & 0xFF);
  loraSpi->transfer(addr & 0xFF);
  loraSpi->transfer(val);
  digitalWrite(NSS, HIGH);
  while (digitalRead(BUSY) == HIGH);
}

// Thread-safe fast frequency hop (<25us)
void setChannelFast(uint8_t ch) {
  uint32_t reg = freq_regs[ch];

  // 1. Standby
  digitalWrite(NSS, LOW);
  loraSpi->transfer(0x80);
  loraSpi->transfer(0x01);
  digitalWrite(NSS, HIGH);
  while (digitalRead(BUSY) == HIGH);

  // 2. Set RF Frequency
  digitalWrite(NSS, LOW);
  loraSpi->transfer(0x86);
  loraSpi->transfer((reg >> 24) & 0xFF);
  loraSpi->transfer((reg >> 16) & 0xFF);
  loraSpi->transfer((reg >> 8) & 0xFF);
  loraSpi->transfer(reg & 0xFF);
  digitalWrite(NSS, HIGH);
  while (digitalRead(BUSY) == HIGH);

  // 3. Clear IRQ
  digitalWrite(NSS, LOW);
  loraSpi->transfer(0x02);
  loraSpi->transfer(0x03);
  loraSpi->transfer(0xFF);
  digitalWrite(NSS, HIGH);
  while (digitalRead(BUSY) == HIGH);

  // 4. Set Rx Continuous
  digitalWrite(NSS, LOW);
  loraSpi->transfer(0x82);
  loraSpi->transfer(0xFF);
  loraSpi->transfer(0xFF);
  loraSpi->transfer(0xFF);
  digitalWrite(NSS, HIGH);
}

// Dedicated Real-Time Priority 24 FHSS Hopping Task (Pinned to CPU Core 1)
void fhssHopTask(void *param) {
  while (true) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    if (isSynced) {
      FHSSptr = (FHSSptr + 1) % FHSS_SEQUENCE_LEN;
      setChannelFast(FHSSsequence[FHSSptr]);
    }
  }
}

// ExpressLRS HWtimer Tock ISR: Fires at dynamic rate interval with sub-microsecond precision
void IRAM_ATTR onSlotTimerISR() {
  BaseType_t xHigherPriorityTaskWoken = pdFALSE;
  OtaNonce++;

  // Hop every N packets in lockstep with the TX rate profile
  if (isSynced && ((OtaNonce % RATE_TABLE[g_current_rate_idx].hop_interval) == 0) && (hopTaskHandle != NULL)) {
    vTaskNotifyGiveFromISR(hopTaskHandle, &xHigherPriorityTaskWoken);
    if (xHigherPriorityTaskWoken) {
      portYIELD_FROM_ISR();
    }
  }
}

void IRAM_ATTR packetISR() {
  packetReceived = true;
}

// Power Down / Deep Sleep Sequence triggered by Long Press (>1.0s)
void enterDeepSleep() {
  display.clear();
  display.setFont(ArialMT_Plain_16);
  display.setTextAlignment(TEXT_ALIGN_CENTER);
  display.drawString(64, 16, "POWER OFF");
  display.setFont(ArialMT_Plain_10);
  display.drawString(64, 38, "Entering Deep Sleep...");
  display.display();
  delay(600);

  display.displayOff();
  pinMode(VEXT_PIN, OUTPUT);
  digitalWrite(VEXT_PIN, HIGH);

  radio.sleep();
  esp_sleep_enable_ext0_wakeup((gpio_num_t)PRG_BUTTON, 0);

  while (digitalRead(PRG_BUTTON) == LOW) {
    delay(10);
  }
  delay(100);

  Serial.println("[POWER] Entering Deep Sleep. Press PRG button to wake up.");
  Serial.flush();
  esp_deep_sleep_start();
}

// Dual 2D Gimbal Crosshairs Display Task on Core 0 (Runs at 15Hz)
// Basic High-Contrast Tactical Status Display Task on Core 0
void displayTask(void *param) {
  pinMode(PRG_BUTTON, INPUT_PULLUP);

  while (true) {
    // PRG Button Long Press (>1.0s) enters Deep Sleep
    if (digitalRead(PRG_BUTTON) == LOW) {
      unsigned long pressStart = millis();
      while (digitalRead(PRG_BUTTON) == LOW) {
        if (millis() - pressStart > 1000) {
          enterDeepSleep();
          break;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
      }
    }

    display.clear();
    display.setFont(ArialMT_Plain_10);
    display.setTextAlignment(TEXT_ALIGN_LEFT);

    // Line 1: Header (Bridge Mode & Armed status)
    display.drawString(0, 0, "CEMA SERIAL BRIDGE");
    if (g_isArmed) {
      display.fillRect(96, 0, 32, 11);
      display.setColor(BLACK);
      display.drawString(100, 0, "ARM");
      display.setColor(WHITE);
    } else {
      display.drawRect(96, 0, 32, 11);
      display.drawString(100, 0, "DIS");
    }

    display.drawLine(0, 13, 128, 13);

    // Line 2: Rate Profile
    char lineBuf[32];
    snprintf(lineBuf, sizeof(lineBuf), "RATE: %-10s", RATE_TABLE[g_current_rate_idx].name);
    display.drawString(0, 16, lineBuf);

    // Line 3: Sync State & RSSI
    if (isSynced) {
      snprintf(lineBuf, sizeof(lineBuf), "SYNC: LOCKED (%ddBm)", (int)g_rssi);
    } else {
      snprintf(lineBuf, sizeof(lineBuf), "SYNC: SCANNING 916.1");
    }
    display.drawString(0, 28, lineBuf);

    // Line 4: Active Target Pilot UID
    if (g_target_lock_enabled) {
      snprintf(lineBuf, sizeof(lineBuf), "TRGT: %u:%u:%u (LOCK)", g_target_uid[0], g_target_uid[1], g_target_uid[2]);
    } else if (isSynced) {
      snprintf(lineBuf, sizeof(lineBuf), "TRGT: %u:%u:%u (AUTO)", discovered_UID[3], discovered_UID[4], discovered_UID[5]);
    } else {
      snprintf(lineBuf, sizeof(lineBuf), "TRGT: AUTO / ANY");
    }
    display.drawString(0, 40, lineBuf);

    // Line 5: Packet Count & Signal-to-Noise Ratio
    snprintf(lineBuf, sizeof(lineBuf), "PKTS: %lu | SNR:%+ddB", (unsigned long)g_packetCount, (int)g_snr);
    display.drawString(0, 52, lineBuf);

    display.display();
    vTaskDelay(pdMS_TO_TICKS(100)); // 10 Hz refresh
  }
}

// Pre-calculate SX1262 frequency register values for all 40 channels
void initFrequencyRegisters() {
  for (uint8_t ch = 0; ch < FHSS_FREQ_COUNT; ch++) {
    freq_table[ch] = 903.5f + (ch * 0.6f);
    uint32_t freq_hz = (uint32_t)(freq_table[ch] * 1000000.0f);
    freq_regs[ch] = (uint32_t)(((uint64_t)freq_hz << 25) / 32000000ULL);
  }
}

// Deterministic PRNG
static uint32_t rng_seed = 0;
uint16_t elrs_rng(void) {
  const uint32_t m = 2147483648;
  const uint32_t a = 214013;
  const uint32_t c = 2531011;
  rng_seed = (a * rng_seed + c) % m;
  return rng_seed >> 16;
}

uint8_t elrs_rngN(const uint8_t max_val) {
  return elrs_rng() % max_val;
}

// Build 240-hop sequence dynamically from discovered UID bytes
void buildDynamicFHSSSequence(uint8_t u2, uint8_t u3, uint8_t u4, uint8_t u5) {
  uint32_t seed = ((uint32_t)u2 << 24) + ((uint32_t)u3 << 16) +
                  ((uint32_t)u4 << 8) + (u5 ^ OTA_VERSION_ID);
  
  rng_seed = seed;
  sync_channel = (FHSS_FREQ_COUNT / 2) + 1; // Channel 21 = 916.1 MHz

  for (uint16_t i = 0; i < FHSS_SEQUENCE_LEN; i++) {
    if (i % FHSS_FREQ_COUNT == 0) {
      FHSSsequence[i] = sync_channel;
    } else if (i % FHSS_FREQ_COUNT == sync_channel) {
      FHSSsequence[i] = 0;
    } else {
      FHSSsequence[i] = i % FHSS_FREQ_COUNT;
    }
  }

  for (uint16_t i = 0; i < FHSS_SEQUENCE_LEN; i++) {
    if (i % FHSS_FREQ_COUNT != 0) {
      uint8_t offset = (i / FHSS_FREQ_COUNT) * FHSS_FREQ_COUNT;
      uint8_t rand = elrs_rngN(FHSS_FREQ_COUNT - 1) + 1;
      uint8_t temp = FHSSsequence[i];
      FHSSsequence[i] = FHSSsequence[offset + rand];
      FHSSsequence[offset + rand] = temp;
    }
  }
}

// Persistent channel state (prevents non-active round-robin switches from resetting/flickering)
uint16_t g_unpacked_rc[16] = {1500, 1500, 988, 1500, 1000, 1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500};

// Betaflight/ExpressLRS analog channel unpacker (supports standard 8B Wide 8ch and Full Res 13B)
void unpackChannels(const byte *b, uint16_t *rc, uint8_t len = 8, uint8_t sw_idx = 0) {
  uint32_t val = 0;
  uint8_t bits = 0;
  uint8_t idx = 0;
  
  // Channels 1..4 (Roll, Pitch, Throttle, Yaw) - 10-bit resolution (Always present in every packet)
  for (uint8_t n = 0; n < 4; n++) {
    while (bits < 10 && idx < len) {
      val |= ((uint32_t)b[idx++]) << bits;
      bits += 8;
    }
    g_unpacked_rc[n] = 988 + (val & 0x03FF);
    val >>= 10;
    bits -= 10;
  }

  // Standard 8B Wide 8ch Switch Unpacking (CH5 is 1-bit ARM; CH6..CH12 are 7-bit switches updated per FHSS hop)
  if (len >= 6) {
    const uint8_t switchByte = b[5]; // Byte 6 in OTA frame

    // AUX1 (CH5) - Low latency 1-bit Arm switch (Bit 7)
    g_unpacked_rc[4] = (switchByte & 0x80) ? 2000 : 1000;

    // Wide 8ch switch decoding (sw_idx is (OtaNonce / hop_interval) % 8)
    if (sw_idx < 7) {
      uint8_t val7 = switchByte & 0x7F;
      uint16_t us_val;
      if (val7 <= 2) {
        us_val = 1000;
      } else if (val7 >= 125) {
        us_val = 2000;
      } else if (val7 >= 58 && val7 <= 70) {
        us_val = 1500;
      } else {
        us_val = 988 + (uint16_t)(((uint32_t)val7 * 1024) / 127);
      }
      g_unpacked_rc[5 + sw_idx] = us_val;
    }
  }

  // Full Res 13B mode (All channels updated directly without round-robin)
  if (len >= 13) {
    for (uint8_t n = 4; n < 16 && idx < len; n++) {
      while (bits < 10 && idx < len) {
        val |= ((uint32_t)b[idx++]) << bits;
        bits += 8;
      }
      g_unpacked_rc[n] = 988 + (val & 0x03FF);
      val >>= 10;
      bits -= 10;
    }
  }

  // Copy persistent state to output buffer
  for (uint8_t i = 0; i < 16; i++) {
    rc[i] = g_unpacked_rc[i];
  }
}

// Parse Downlink Telemetry (Link Quality, Battery, GPS, Attitude, Flight Mode)
void parseDownlinkTelemetry(const byte *raw) {
  uint8_t tlmType = (raw[1] >> 4) & 0x07;

  if (tlmType == 0) { // Link Statistics
    int8_t droneRssi = -(raw[2] & 0x7F);
    uint8_t droneLq = raw[4] & 0x7F;
    int8_t droneSnr = (int8_t)raw[5];
    Serial.printf("[TLM LINK] DroneRSSI:%d | DroneLQ:%u | DroneSNR:%+d\n", droneRssi, droneLq, droneSnr);
  } else {
    // CRSF Sensor Frame
    uint8_t sensorType = raw[2];
    if (sensorType == 0x02) { // GPS Frame
      int32_t raw_lat = ((int32_t)raw[3] << 24) | ((int32_t)raw[4] << 16) | ((int32_t)raw[5] << 8) | raw[6];
      int32_t raw_lon = ((int32_t)raw[7] << 24) | ((int32_t)raw[8] << 16) | ((int32_t)raw[9] << 8) | raw[10];
      float lat = raw_lat / 10000000.0f;
      float lon = raw_lon / 10000000.0f;
      uint16_t spd_kmh = (((uint16_t)raw[11] << 8) | raw[12]) / 10;
      uint16_t alt_m = (((uint16_t)raw[13] << 8) | raw[14]) - 1000;
      uint8_t sats = raw[15];
      Serial.printf("[TLM GPS] Lat:%.6f | Lon:%.6f | Alt:%u | Spd:%u | Sats:%u\n", lat, lon, alt_m, spd_kmh, sats);
    } else if (sensorType == 0x08) { // Battery Sensor
      uint16_t vbat_mv = ((uint16_t)raw[3] << 8) | raw[4];
      uint16_t curr_ma = ((uint16_t)raw[5] << 8) | raw[6];
      uint32_t cap_mah = ((uint32_t)raw[7] << 16) | ((uint32_t)raw[8] << 8) | raw[9];
      uint8_t rem_pct = raw[10];
      float vbat = vbat_mv / 10.0f;
      float curr = curr_ma / 10.0f;
      Serial.printf("[TLM BAT] V:%.1f | I:%.1f | Cap:%lu | Batt:%u\n", vbat, curr, (unsigned long)cap_mah, rem_pct);
    } else if (sensorType == 0x1E) { // Attitude Sensor
      int16_t pitch_deg = (int16_t)(((uint16_t)raw[3] << 8) | raw[4]) / 100;
      int16_t roll_deg = (int16_t)(((uint16_t)raw[5] << 8) | raw[6]) / 100;
      int16_t yaw_deg = (int16_t)(((uint16_t)raw[7] << 8) | raw[8]) / 100;
      Serial.printf("[TLM ATT] Pitch:%d | Roll:%d | Yaw:%d\n", pitch_deg, roll_deg, yaw_deg);
    } else if (sensorType == 0x21) { // Flight Mode Frame
      char fmode[16] = {0};
      for (uint8_t i = 0; i < 12 && (3 + i) < 16; i++) {
        fmode[i] = (char)raw[3 + i];
        if (fmode[i] == 0) break;
      }
      Serial.printf("[TLM MODE] Mode:%s\n", fmode[0] ? fmode : "ANGLE");
    }
  }
}

// Dynamically authenticate packet (supports both 8B and 13B frames)
bool authenticatePacket(const byte *raw, uint8_t &pkt_type, int &matched_slot, uint8_t len = 8) {
  uint8_t crc_idx = (len == 13) ? 12 : 7;
  uint8_t data_len = crc_idx;
  uint16_t inCRC = ((uint16_t)(raw[0] >> 2) << 8) | raw[crc_idx];
  pkt_type = raw[0] & 0x03;
  byte d[13];
  memcpy(d, raw, data_len);

  if (pkt_type == 0b10) { // SYNC PACKET
    d[0] = 0x02; // type=2, crcHigh=0
    if (ota_crc.calc(d, data_len, dynamicCrcInit) == inCRC) {
      matched_slot = 0;
      return true;
    }
  } else if (pkt_type == 0b00) { // RC DATA PACKET
    for (uint8_t slot = 0; slot < 4; slot++) {
      d[0] = (slot + 1) << 2;
      if (ota_crc.calc(d, data_len, dynamicCrcInit) == inCRC) {
        matched_slot = slot;
        return true;
      }
    }
  } else if (pkt_type == 0b11) { // TELEMETRY DOWNLINK PACKET
    d[0] = 0x03; // type=3, crcHigh=0
    if (ota_crc.calc(d, data_len, dynamicCrcInit) == inCRC) {
      matched_slot = 0;
      return true;
    }
  }
  return false;
}

void applySemtechErrataFixes() {
  uint8_t reg0889 = readReg8(0x0889);
  writeReg8(0x0889, reg0889 & ~0x04);
  writeReg8(0x08D8, 0x09);
  radio.setRxBoostedGainMode(true);
}

void applyRateConfig(uint8_t idx, bool force = false) {
  if (idx >= RATE_COUNT) idx = 0;
  if (!force && idx == g_current_rate_idx) return;

  g_current_rate_idx = idx;
  const ELRSRateProfile &p = RATE_TABLE[g_current_rate_idx];

  radio.standby();
  radio.setSpreadingFactor(p.sf);
  radio.setBandwidth(p.bw_khz);
  radio.setCodingRate(p.cr);
  radio.implicitHeader(p.payload_len);
  radio.setCRC(0);
  radio.invertIQ(false);
  applySemtechErrataFixes();

  if (slot_timer != NULL) {
    timerAlarm(slot_timer, p.interval_us, true, 0);
  }

  radio.startReceive();

  Serial.printf("[RATE LOCKED] Rate:%s | SF:%u | BW:%.0fkHz | Interval:%uus\n",
                p.name, p.sf, p.bw_khz, p.interval_us);
}

void setup() {
  Serial.begin(115200);
  unsigned long start = millis();
  while (!Serial && (millis() - start < 2500));

  Serial.println("\n=============================================");
  Serial.println("  Heltec Sniffer: Multi-Rate Auto-Demodulator");
  Serial.println("  Heltec WiFi LoRa 32 V3 / ExpressLRS 915MHz");
  Serial.println("=============================================\n");

  pinMode(VEXT_PIN, OUTPUT);
  digitalWrite(VEXT_PIN, LOW);
  delay(10);

  pinMode(RST_OLED, OUTPUT);
  digitalWrite(RST_OLED, HIGH);
  delay(1);
  digitalWrite(RST_OLED, LOW);
  delay(20);
  digitalWrite(RST_OLED, HIGH);
  delay(10);

  display.init();
  display.flipScreenVertically();
  display.setFont(ArialMT_Plain_10);
  display.drawString(0, 0, "ELRS Sniffer V3");
  display.drawString(0, 16, "Multi-Rate Auto");
  display.drawString(0, 32, "Hold: Deep Sleep");
  display.display();

  ota_crc.init(14, ELRS_CRC14_POLY);
  initFrequencyRegisters();

  dynamicCrcInit = 0x2156;
  buildDynamicFHSSSequence(253, 130, 33, 85);

  loraSpi->begin(SCK, MISO, MOSI, NSS);
  loraSpi->setFrequency(16000000); // 16 MHz Hardware SPI for ultra-fast register and packet transfers

  int state = radio.begin(903.5 + (sync_channel * 0.6), RATE_TABLE[0].bw_khz, RATE_TABLE[0].sf, RATE_TABLE[0].cr, 0x12, 10, 10, 1.8, false);
  if (state != RADIOLIB_ERR_NONE) {
    Serial.printf("[ERROR] radio.begin() failed: %d\n", state);
    while (true) delay(500);
  }

  radio.setDio2AsRfSwitch(true);
  radio.implicitHeader(RATE_TABLE[0].payload_len);
  radio.setCRC(0);
  radio.invertIQ(false);
  radio.setPacketReceivedAction(packetISR);

  applySemtechErrataFixes();

  xTaskCreatePinnedToCore(
    fhssHopTask,
    "fhssHop",
    4096,
    NULL,
    configMAX_PRIORITIES - 1,
    &hopTaskHandle,
    1
  );

  xTaskCreatePinnedToCore(
    displayTask,
    "displayTask",
    4096,
    NULL,
    1,
    &displayTaskHandle,
    0
  );

  slot_timer = timerBegin(1000000);
  timerAttachInterrupt(slot_timer, &onSlotTimerISR);
  timerAlarm(slot_timer, RATE_TABLE[0].interval_us, true, 0);

  Serial.println("[AUTODISCOVERY] Ready on Sync Channel (916.1 MHz)...");
  radio.startReceive();
}

void loop() {
  int64_t now = esp_timer_get_time();

  // 1. Process Host Serial Commands from CEMA Tracker (e.g. SET_RATE:100HZ / LOCK_PILOT:130,33,85)
  while (Serial.available() > 0) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd.startsWith("SET_RATE:")) {
      String rate = cmd.substring(9);
      rate.toUpperCase();
      if (rate == "AUTO") {
        g_auto_rate_scan = true;
        isSynced = false;
        g_sync_grace_period_until = 0;
        g_last_auto_scan_us = now;
        setChannelFast(sync_channel);
        Serial.println("[RATE AUTO] Dynamic auto-rate scanning enabled.");
      } else {
        g_auto_rate_scan = false;
        for (uint8_t r = 0; r < RATE_COUNT; r++) {
          String name = String(RATE_TABLE[r].name);
          name.toUpperCase();
          if (rate == name) {
            isSynced = false;
            g_sync_grace_period_until = 0;
            applyRateConfig(r, true);
            setChannelFast(sync_channel);
            break;
          }
        }
      }
    } else if (cmd.startsWith("LOCK_PILOT:")) {
      String arg = cmd.substring(11);
      arg.trim();
      arg.toUpperCase();
      if (arg == "AUTO" || arg == "ANY") {
        g_target_lock_enabled = false;
        Serial.println("[PILOT TARGET] Tracking any active pilot (Auto).");
      } else {
        int first_sep = arg.indexOf(':');
        if (first_sep < 0) first_sep = arg.indexOf(',');
        int second_sep = (first_sep >= 0) ? arg.indexOf(':', first_sep + 1) : -1;
        if (second_sep < 0 && first_sep >= 0) second_sep = arg.indexOf(',', first_sep + 1);

        if (first_sep > 0 && second_sep > first_sep) {
          uint8_t u3 = arg.substring(0, first_sep).toInt();
          uint8_t u4 = arg.substring(first_sep + 1, second_sep).toInt();
          uint8_t u5 = arg.substring(second_sep + 1).toInt();
          g_target_uid[0] = u3;
          g_target_uid[1] = u4;
          g_target_uid[2] = u5;
          g_target_lock_enabled = true;
          isSynced = false;
          g_sync_grace_period_until = 0;
          setChannelFast(sync_channel);
          Serial.printf("[PILOT TARGET] Locked filter to UID %u:%u:%u. Re-acquiring sync...\n", u3, u4, u5);
        }
      }
    }
  }

  // 2. Auto-Rate Discovery Engine: If not synced and NOT in grace lock, rotate rates every 4.0s
  if (!isSynced && g_auto_rate_scan && (now > g_sync_grace_period_until) && (now - g_last_auto_scan_us > 4000000)) {
    g_last_auto_scan_us = now;
    uint8_t next_idx = (g_current_rate_idx + 1) % RATE_COUNT;
    applyRateConfig(next_idx, true);
    setChannelFast(sync_channel);
  }

  // 3. Loss of Synchronization Watchdog (5.0s timeout)
  if (isSynced && (now - last_packet_time_us > 5000000)) {
    isSynced = false;
    setChannelFast(sync_channel);
    g_last_auto_scan_us = now;
    Serial.println("[!] Sync lost. Re-parking on 916.1 MHz for discovery...");
  }

  // 4. Ingest Received Packets
  if (packetReceived) {
    packetReceived = false;

    byte raw[16];
    uint8_t plen = RATE_TABLE[g_current_rate_idx].payload_len;
    int state = radio.readData(raw, plen);

    if (state == RADIOLIB_ERR_NONE) {
      float rssi = radio.getRSSI();
      float snr = radio.getSNR();
      uint8_t pkt_type = raw[0] & 0x03;

      // 1. SYNC PACKET DISCOVERY
      if (pkt_type == 0b10) {
        uint8_t fhssIdx = raw[1];
        uint8_t nonce = raw[2];
        uint8_t u3 = raw[4];
        uint8_t u4 = raw[5];
        uint8_t u5 = raw[6];

        uint16_t testCrcInit = ((uint16_t)u4 << 8) | u5;
        testCrcInit ^= OTA_VERSION_ID;

        uint8_t crc_idx = (plen == 13) ? 12 : 7;
        uint8_t data_len = crc_idx;
        uint16_t inCRC = ((uint16_t)(raw[0] >> 2) << 8) | raw[crc_idx];
        byte d[13];
        memcpy(d, raw, data_len);
        d[0] = 0x02;

        if (ota_crc.calc(d, data_len, testCrcInit) == inCRC) {
          // Always emit discovered pilot notification
          Serial.printf("[PILOT DISCOVERED] UID3:%u UID4:%u UID5:%u | CRC:0x%04X | RSSI:%.0f | Rate:%s\n",
                        u3, u4, u5, testCrcInit, rssi, RATE_TABLE[g_current_rate_idx].name);

          // If target pilot lock is active, verify matching UID
          if (g_target_lock_enabled) {
            if (u3 != g_target_uid[0] || u4 != g_target_uid[1] || u5 != g_target_uid[2]) {
              radio.startReceive();
              return; // Reject untargeted pilot
            }
          }

          dynamicCrcInit = testCrcInit;
          discovered_UID[3] = u3;
          discovered_UID[4] = u4;
          discovered_UID[5] = u5;
          buildDynamicFHSSSequence(253, u3, u4, u5);

          FHSSptr = fhssIdx;
          OtaNonce = nonce;
          g_wide_switch_idx = nonce % 8;
          timerWrite(slot_timer, RATE_TABLE[g_current_rate_idx].toa_us);
          isSynced = true;
          g_sync_grace_period_until = now + 8000000; // 8-second grace lock!
          last_packet_time_us = now;

          g_rssi = rssi;
          g_snr = snr;

          Serial.printf("\n[SYNC VERIFIED] UID3:%u UID4:%u UID5:%u | CRC: 0x%04X | HopIdx:%u Nonce:%u | Rate:%s\n",
                        u3, u4, dynamicCrcInit, fhssIdx, nonce, RATE_TABLE[g_current_rate_idx].name);
        }
      }
      // 2. RC DATA PACKET
      else if (pkt_type == 0b00) {
        int matched_slot = -1;
        if (authenticatePacket(raw, pkt_type, matched_slot, plen)) {
          last_packet_time_us = now;
          g_sync_grace_period_until = now + 8000000; // 8-second grace lock!

          uint8_t hop_int = RATE_TABLE[g_current_rate_idx].hop_interval;
          OtaNonce = ((OtaNonce / hop_int) * hop_int) + (uint8_t)matched_slot;
          timerWrite(slot_timer, RATE_TABLE[g_current_rate_idx].toa_us);
          isSynced = true;

          uint8_t sw_idx = (OtaNonce / hop_int) % 8;
          uint16_t ch[16];
          unpackChannels(&raw[1], ch, plen, sw_idx);
          bool isArmed = (ch[4] > 1500);

          g_rssi = rssi;
          g_snr = snr;
          g_ch[0] = ch[0];
          g_ch[1] = ch[1];
          g_ch[2] = ch[2];
          g_ch[3] = ch[3];
          g_isArmed = isArmed;
          g_packetCount++;

          // Rate-limit serial logging to ~25Hz (every 38ms) to prevent UART buffer blocking on CPU Core 1
          static uint32_t last_serial_emit_ms = 0;
          uint32_t now_ms = millis();
          if (now_ms - last_serial_emit_ms >= 38) {
            last_serial_emit_ms = now_ms;
            Serial.printf("[RC %s] RSSI:%4.0f dBm | SNR:%+5.1f dB | CH1:%4u | CH2:%4u | CH3:%4u | CH4:%4u | CH5:%4u | CH6:%4u | CH7:%4u | CH8:%4u | CH9:%4u | CH10:%4u | CH11:%4u | CH12:%4u | CH13:%4u | CH14:%4u | CH15:%4u | CH16:%4u | ARM:%s\n",
                          RATE_TABLE[g_current_rate_idx].name, rssi, snr, 
                          ch[0], ch[1], ch[2], ch[3], ch[4], ch[5], ch[6], ch[7],
                          ch[8], ch[9], ch[10], ch[11], ch[12], ch[13], ch[14], ch[15],
                          isArmed ? "ON " : "OFF");
          }
        }
      }
      // 3. TELEMETRY DOWNLINK PACKET
      else if (pkt_type == 0b11) {
        int matched_slot = -1;
        if (authenticatePacket(raw, pkt_type, matched_slot, plen)) {
          last_packet_time_us = now;
          g_sync_grace_period_until = now + 8000000; // 8-second grace lock!
          timerWrite(slot_timer, RATE_TABLE[g_current_rate_idx].toa_us);
          isSynced = true;
          parseDownlinkTelemetry(raw);
        }
      }
    }
    radio.startReceive();
  }
}