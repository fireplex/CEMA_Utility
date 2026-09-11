#include <Arduino.h>
#include <SPI.h>
#include <Wire.h>
#include <RadioLib.h>
#include <SSD1306Wire.h>

// =============================================================================
// NON-BLOCKING HOT-PATH LOGGING
// Drops the line instead of stalling the demodulation loop when the UART TX FIFO
// is full (serial back-pressure was starving the RX/scan path, esp. on the Pi).
// Use LOG_HOT() for per-packet prints; keep Serial.printf/println for rare,
// important status lines that must not be dropped.
// =============================================================================
#define HOT_LOG_MIN_TXBUF 256
#define LOG_HOT(...) do { if (Serial.availableForWrite() >= HOT_LOG_MIN_TXBUF) Serial.printf(__VA_ARGS__); } while (0)

// =============================================================================
// HARDWARE TARGET SELECTOR
// (Select ONE board target according to your hardware)
// =============================================================================
// #define BOARD_LILYGO_T3_S3_SX1276     // LilyGO T3-S3 (ESP32-S3 + SX1276 868/915MHz)
#define BOARD_HELTEC_V3             // Heltec WiFi LoRa 32 V3 (ESP32-S3 + SX1262)
// #define BOARD_LILYGO_T3_S3_SX1262     // LilyGO T3-S3 (ESP32-S3 + SX1262)

// =============================================================================
// REGIONAL FREQUENCY BAND SELECTOR
// =============================================================================
// #define BAND_868_MHZ                  // EU868 Band (868.0 MHz sync channel - UK/Europe)
#define BAND_915_MHZ               // US915 Band (916.1 MHz sync channel - Americas)

#if defined(BOARD_LILYGO_T3_S3_SX1276)
  #define SCK_PIN 5
  #define MISO_PIN 3
  #define MOSI_PIN 6
  #define NSS_PIN 7
  #define NRST_PIN 8
  #define DIO0_PIN 9
  #define DIO1_PIN 33
  #define SDA_OLED 18
  #define SCL_OLED 17
  #define RST_OLED 21
  #define PRG_BUTTON 0
  #define BOARD_NAME "LILYGO T3-S3 (SX1276)"
  
  SSD1306Wire display(0x3C, SDA_OLED, SCL_OLED, GEOMETRY_128_64);
  SPIClass *loraSpi = new SPIClass(FSPI);
  SX1276 radio = new Module(NSS_PIN, DIO0_PIN, NRST_PIN, DIO1_PIN, *loraSpi);

#elif defined(BOARD_LILYGO_T3_S3_SX1262)
  #define SCK_PIN 5
  #define MISO_PIN 3
  #define MOSI_PIN 6
  #define NSS_PIN 7
  #define NRST_PIN 8
  #define DIO1_PIN 33
  #define BUSY_PIN 34
  #define SDA_OLED 18
  #define SCL_OLED 17
  #define RST_OLED 21
  #define PRG_BUTTON 0
  #define BOARD_NAME "LILYGO T3-S3 (SX1262)"
  
  SSD1306Wire display(0x3C, SDA_OLED, SCL_OLED, GEOMETRY_128_64);
  SPIClass *loraSpi = new SPIClass(FSPI);
  SX1262 radio = new Module(NSS_PIN, DIO1_PIN, NRST_PIN, BUSY_PIN, *loraSpi);

#else // Default: BOARD_HELTEC_V3
  #define SCK_PIN 9
  #define MISO_PIN 11
  #define MOSI_PIN 10
  #define NSS_PIN 8
  #define DIO1_PIN 14
  #define NRST_PIN 12
  #define BUSY_PIN 13
  #define SDA_OLED 17
  #define SCL_OLED 18
  #define RST_OLED 21
  #define VEXT_PIN 36
  #define PRG_BUTTON 0
  #define BOARD_NAME "HELTEC V3 (SX1262)"

  SSD1306Wire display(0x3C, SDA_OLED, SCL_OLED, GEOMETRY_128_64);
  SPIClass *loraSpi = new SPIClass(FSPI);
  SX1262 radio = new Module(NSS_PIN, DIO1_PIN, NRST_PIN, BUSY_PIN, *loraSpi);
#endif

// Hardware Timer and RTOS Task handles
hw_timer_t *slot_timer = NULL;
TaskHandle_t hopTaskHandle = NULL;
TaskHandle_t displayTaskHandle = NULL;

#define OTA_VERSION_ID_V3 3
#define OTA_VERSION_ID_V4 4
volatile uint8_t g_ota_version = 4; // Default to ExpressLRS 4.x
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
  {"200Hz",      6, 500.0, 7, 5000,  4380, 4,  8},   // 200Hz 8ch (SF6, CR 4/7, 4 pkts/hop, TOA 4380us)
  {"100Hz",      7, 500.0, 7, 10000, 8770,  4,  8},   // Standard 100Hz 8ch (SF7, 10ms slot, 4 pkts/hop, TOA 8770us)
  {"50Hz",       8, 500.0, 7, 20000, 18560, 4,  8},   // Standard 915MHz 50Hz (SF8, 20ms slot, 4 pkts/hop, TOA 18560us)
  {"25Hz",       9, 500.0, 7, 40000, 29950, 2,  8},   // Long Range 25Hz (SF9, 40ms slot, 2 pkts/hop, TOA 29950us)
  {"100Hz Full", 6, 500.0, 8, 10000, 6690,  4,  13},  // 100Hz Full Res 16ch (SF6, CR 4/8, 4 pkts/hop, TOA 6690us)
  {"200Hz Full", 6, 500.0, 5, 5000,  4380,  4,  13},  // 200Hz Full Res 16ch (SF6, CR 4/5, 4 pkts/hop, TOA 4380us)
  {"D50",        6, 500.0, 7, 5000,  4380,  2,  8},   // Deja Vu 50Hz (SF6, 5ms slot, 2 pkts/hop, TOA 4380us)
  {"150Hz",      7, 500.0, 5, 6666,  4800,  4,  8},   // 150Hz 8ch (SF7, CR 4/5, 4 pkts/hop, TOA 4800us)
  {"250Hz",      6, 500.0, 5, 4000,  2600,  4,  8},   // 250Hz 8ch (SF6, CR 4/5, 4 pkts/hop, TOA 2600us)
  {"333Hz Full", 5, 500.0, 7, 3000,  2000,  4,  13}   // 333Hz Full Res (SF5, CR 4/7, 4 pkts/hop, TOA 2000us)
};
#define RATE_COUNT (sizeof(RATE_TABLE) / sizeof(RATE_TABLE[0]))

// ELRS 900MHz TX radios based on the SX127x support only SF6..SF12. Our SX1262 (Heltec V3) can
// additionally run SF5, but an SX127x-based ELRS link will never transmit at SF5, so any rate
// profile that uses SF5 (e.g. "333Hz Full") is incompatible with such a link and pointless to
// scan or lock. Such profiles are flagged at boot and excluded from auto-scan / rate selection.
// Lower this to 5 to also scan SF5 (e.g. when the ELRS TX itself is SX1262/Gemini hardware).
#define ELRS_MIN_COMPAT_SF 6

// True if rate profile idx uses a spreading factor an SX127x ELRS link can actually transmit.
static inline bool isRateSFCompatible(uint8_t idx) {
  return (idx < RATE_COUNT) && (RATE_TABLE[idx].sf >= ELRS_MIN_COMPAT_SF);
}

volatile uint8_t g_current_rate_idx = 0;
volatile bool g_auto_rate_scan = false;
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

// Algebraic GF(2) Matrix Inversion for CRC-14 (Poly 0x2E57)
// Solves exact dynamicCrcInit in under 0.05 us without brute-force scanning
const uint16_t CRC14_MINV[14] = {
  0x25AD, 0x2EF7, 0x3843, 0x3086, 0x04A0, 0x0940, 0x372C,
  0x2E59, 0x1CB3, 0x1CCA, 0x1C39, 0x1DDE, 0x3BBD, 0x12D6
};

uint16_t solveCrcInit(const uint8_t *data, uint16_t inCRC) {
  uint16_t c0 = ota_crc.calc(data, 7, 0);
  uint16_t delta = (inCRC ^ c0) & 0x3FFF;
  uint16_t init = 0;
  for (uint8_t i = 0; i < 14; i++) {
    init |= (__builtin_parity(CRC14_MINV[i] & delta) << i);
  }
  return init;
}

// ZERO-KNOWLEDGE DYNAMIC UID & ENCRYPTION STATE
uint8_t discovered_UID[6] = {0, 0, 0, 0, 0, 0};
uint16_t dynamicCrcInit = 0x2156;
bool uidDiscovered = false;

// TARGET PILOT FILTER & AIRSPACE SCAN MODE
volatile bool g_target_lock_enabled = false;
volatile uint8_t g_target_uid[3] = {0, 0, 0}; // target u3, u4, u5
volatile bool g_scan_mode = false;            // Airspace survey / all-pilots discovery mode
volatile uint8_t g_band_mode = 0;             // 0 = AUTO (v3 + v4), 4 = v4 (Ch 20 / 915.5 MHz), 3 = v3 (Ch 21 / 916.1 MHz)

uint8_t FHSSsequence[FHSS_SEQUENCE_LEN];
float freq_table[FHSS_FREQ_COUNT];
uint32_t freq_regs[FHSS_FREQ_COUNT];
volatile uint8_t FHSSptr = 0;
volatile int16_t g_pin_channel = -1;  // pin radio to this channel (0-39); -1 = normal hop. Used by the autonomous seed-solver and the manual PIN cmd.
volatile uint8_t OtaNonce = 0;

// [SEED-SOLVE] Autonomous FHSS-seed (u2,u3) brute-force for phrase-free / traditional-bound pilots.
// Sync packets only reveal u4,u5; the hop sequence is seeded by u2,u3,u4,u5. After locking a pilot
// we pin the radio to a few channels, record (FHSSptr, channel) hits where the pilot lands, then
// brute-force the two unknown bytes so sequence[ptr]==channel for every observation.
struct SeedCon { uint8_t ptr; uint8_t ch; };
SeedCon  g_seed_cons[192];
uint16_t g_seed_con_n = 0;
bool     g_seed_solved = false;      // u2,u3 confirmed for the current pilot
bool     g_seed_collecting = false;  // sweep+record in progress
uint8_t  g_seed_sweep_idx = 0;
uint32_t g_seed_dwell_until_ms = 0;
uint8_t  g_seed_pilot_u4 = 0, g_seed_pilot_u5 = 0;  // pilot the current solve belongs to
const uint8_t SEED_SWEEP_CH[] = {4, 10, 16, 26, 33, 8};  // spread of non-sync channels to pin
const uint32_t SEED_DWELL_MS = 2500;
uint8_t sync_channel = 20; // Channel 20 = 915.5 MHz (ELRS 4.x), Channel 21 = 916.1 MHz (ELRS 3.x)
volatile uint8_t g_wide_switch_idx = 0;

// Global Telemetry State for OLED Display (Core 0)
volatile float g_rssi = -100.0f;
volatile float g_snr = 0.0f;
volatile uint16_t g_ch[4] = {1500, 1500, 988, 1500};
volatile bool g_isArmed = false;
volatile uint32_t g_packetCount = 0;

volatile bool isSynced = false;
volatile int64_t last_packet_time_us = 0;
volatile bool packetReceived = false;

// Robust State Machine for FHSS Hopping and Zero-Packet-Loss Tracking
volatile bool g_link_locked = false;        // Rate, UID, and RC link confirmed
volatile bool g_hopping_locked = false;     // FHSS frequency hopping active and phase locked
volatile bool g_phase_hunting = false;      // Currently testing candidate hop frequency
volatile uint8_t g_phase_candidate = 0;     // 0..5 candidate segment on channel 21
volatile uint8_t g_test_ptr = 0;            // Candidate sequence index under test
volatile int64_t g_phase_hunt_start_us = 0; // Timestamp of candidate test start

// Direct SPI register operations (Zero-mutex fast path)
uint8_t readReg8(uint16_t addr) {
#if defined(BOARD_LILYGO_T3_S3_SX1276)
  digitalWrite(NSS_PIN, LOW);
  loraSpi->transfer(addr & 0x7F);
  uint8_t val = loraSpi->transfer(0x00);
  digitalWrite(NSS_PIN, HIGH);
  return val;
#else
  digitalWrite(NSS_PIN, LOW);
  loraSpi->transfer(0x1D);
  loraSpi->transfer((addr >> 8) & 0xFF);
  loraSpi->transfer(addr & 0xFF);
  loraSpi->transfer(0x00);
  uint8_t val = loraSpi->transfer(0x00);
  digitalWrite(NSS_PIN, HIGH);
  #ifdef BUSY_PIN
  uint32_t t0 = micros();
  while (digitalRead(BUSY_PIN) == HIGH && (micros() - t0 < 30));
  #endif
  return val;
#endif
}

void writeReg8(uint16_t addr, uint8_t val) {
#if defined(BOARD_LILYGO_T3_S3_SX1276)
  digitalWrite(NSS_PIN, LOW);
  loraSpi->transfer((addr & 0x7F) | 0x80);
  loraSpi->transfer(val);
  digitalWrite(NSS_PIN, HIGH);
#else
  digitalWrite(NSS_PIN, LOW);
  loraSpi->transfer(0x0D);
  loraSpi->transfer((addr >> 8) & 0xFF);
  loraSpi->transfer(addr & 0xFF);
  loraSpi->transfer(val);
  digitalWrite(NSS_PIN, HIGH);
  #ifdef BUSY_PIN
  uint32_t t0 = micros();
  while (digitalRead(BUSY_PIN) == HIGH && (micros() - t0 < 30));
  #endif
#endif
}

// Fast frequency hop (<15us on SX1262, <3us on SX1276)
void setChannelFast(uint8_t ch) {
#if defined(BOARD_LILYGO_T3_S3_SX1276)
  uint32_t frf = freq_regs[ch];
  digitalWrite(NSS_PIN, LOW);
  loraSpi->transfer(0x86); // Write RegFrfMsb (0x06 | 0x80)
  loraSpi->transfer((frf >> 16) & 0xFF);
  loraSpi->transfer((frf >> 8) & 0xFF);
  loraSpi->transfer(frf & 0xFF);
  digitalWrite(NSS_PIN, HIGH);
#else
  uint32_t reg = freq_regs[ch];

  // 1. Direct SetStandby in XOSC mode (0x80, 0x01)
  digitalWrite(NSS_PIN, LOW);
  loraSpi->transfer(0x80);
  loraSpi->transfer(0x01);
  digitalWrite(NSS_PIN, HIGH);

  #ifdef BUSY_PIN
  uint32_t t0 = micros();
  while (digitalRead(BUSY_PIN) == HIGH && (micros() - t0 < 30));
  #endif

  // 2. Set RF Frequency (0x86)
  digitalWrite(NSS_PIN, LOW);
  loraSpi->transfer(0x86);
  loraSpi->transfer((reg >> 24) & 0xFF);
  loraSpi->transfer((reg >> 16) & 0xFF);
  loraSpi->transfer((reg >> 8) & 0xFF);
  loraSpi->transfer(reg & 0xFF);
  digitalWrite(NSS_PIN, HIGH);

  #ifdef BUSY_PIN
  t0 = micros();
  while (digitalRead(BUSY_PIN) == HIGH && (micros() - t0 < 30));
  #endif

  // 3. Clear IRQ (0x02, 0x03, 0xFF)
  digitalWrite(NSS_PIN, LOW);
  loraSpi->transfer(0x02);
  loraSpi->transfer(0x03);
  loraSpi->transfer(0xFF);
  digitalWrite(NSS_PIN, HIGH);

  #ifdef BUSY_PIN
  t0 = micros();
  while (digitalRead(BUSY_PIN) == HIGH && (micros() - t0 < 30));
  #endif

  // 4. Immediately re-enter Continuous Rx (0x82, 0xFF, 0xFF, 0xFF)
  digitalWrite(NSS_PIN, LOW);
  loraSpi->transfer(0x82);
  loraSpi->transfer(0xFF);
  loraSpi->transfer(0xFF);
  loraSpi->transfer(0xFF);
  digitalWrite(NSS_PIN, HIGH);
#endif
}

// Dedicated Real-Time Priority 24 FHSS Hopping Task (Pinned to CPU Core 1)
void fhssHopTask(void *param) {
  while (true) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    if (g_hopping_locked) {
      FHSSptr = (FHSSptr + 1) % FHSS_SEQUENCE_LEN;
      // when pinned (seed-solve/PIN), keep FHSSptr advancing (phase-locked to pilot) but hold the
      // radio on the pinned channel so each received packet yields sequence[FHSSptr]==pinCh.
      setChannelFast(g_pin_channel >= 0 ? (uint8_t)g_pin_channel : FHSSsequence[FHSSptr]);
    }
  }
}

// ExpressLRS HWtimer Tock ISR: Fires at dynamic rate interval with sub-microsecond precision
void IRAM_ATTR onSlotTimerISR() {
  BaseType_t xHigherPriorityTaskWoken = pdFALSE;
  OtaNonce++;

  // Hop every N packets in lockstep with the TX rate profile once frequency phase is locked
  if (g_hopping_locked && ((OtaNonce % RATE_TABLE[g_current_rate_idx].hop_interval) == 0) && (hopTaskHandle != NULL)) {
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
#ifdef VEXT_PIN
  pinMode(VEXT_PIN, OUTPUT);
  digitalWrite(VEXT_PIN, HIGH);
#endif

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
    if (g_hopping_locked) {
      snprintf(lineBuf, sizeof(lineBuf), "HOPPING v%u: %ddBm", g_ota_version, (int)g_rssi);
    } else if (g_link_locked || isSynced) {
      snprintf(lineBuf, sizeof(lineBuf), "LOCK v%u: %.1fMHz", g_ota_version, freq_table[sync_channel]);
    } else {
      snprintf(lineBuf, sizeof(lineBuf), "SCAN: 915.5 / 916.1");
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

// Pre-calculate frequency register values for all channels
void initFrequencyRegisters() {
#if defined(BAND_868_MHZ)
  // EU868 ELRS: 863.275 MHz to 869.575 MHz (13 channels, center 868.0 MHz, spacing 525 kHz)
  sync_channel = 6; // Channel 6 = 866.425 MHz
  for (uint8_t ch = 0; ch < 13; ch++) {
    freq_table[ch] = 863.275f + (ch * 0.525f);
    uint32_t freq_hz = (uint32_t)(freq_table[ch] * 1000000.0f);
#if defined(BOARD_LILYGO_T3_S3_SX1276)
    freq_regs[ch] = (uint32_t)(((uint64_t)freq_hz << 19) / 32000000ULL);
#else
    freq_regs[ch] = (uint32_t)(((uint64_t)freq_hz << 25) / 32000000ULL);
#endif
  }
#else
  // US915 / FCC915 ELRS: 903.5 MHz to 926.9 MHz (40 channels, center 915.0 MHz, spacing 600 kHz)
  sync_channel = (g_ota_version == 4) ? 20 : 21; // Channel 20 = 915.5 MHz (v4), Channel 21 = 916.1 MHz (v3)
  for (uint8_t ch = 0; ch < FHSS_FREQ_COUNT; ch++) {
    freq_table[ch] = 903.5f + (ch * 0.600f);
    uint32_t freq_hz = (uint32_t)(freq_table[ch] * 1000000.0f);
#if defined(BOARD_LILYGO_T3_S3_SX1276)
    // SX1276: F_RF = (F_XOSC / 2^19) * RegFrf
    freq_regs[ch] = (uint32_t)(((uint64_t)freq_hz << 19) / 32000000ULL);
#else
    // SX1262: F_RF = (F_XOSC / 2^25) * RegFrf
    freq_regs[ch] = (uint32_t)(((uint64_t)freq_hz << 25) / 32000000ULL);
#endif
  }
#endif
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
void buildDynamicFHSSSequence(uint8_t u2, uint8_t u3, uint8_t u4, uint8_t u5, uint8_t ota_ver = 4) {
  g_ota_version = ota_ver;
  uint32_t seed = ((uint32_t)u2 << 24) + ((uint32_t)u3 << 16) +
                  ((uint32_t)u4 << 8) + (u5 ^ ota_ver);
  
  rng_seed = seed;
#if defined(BAND_868_MHZ)
  uint8_t freqCount = 13;
  sync_channel = 6;
#else
  uint8_t freqCount = FHSS_FREQ_COUNT;
  sync_channel = (ota_ver == 4) ? (freqCount / 2) : ((freqCount / 2) + 1); // Ch 20 (915.5) for v4, Ch 21 (916.1) for v3
#endif

  for (uint16_t i = 0; i < FHSS_SEQUENCE_LEN; i++) {
    if (i % freqCount == 0) {
      FHSSsequence[i] = sync_channel;
    } else if (i % freqCount == sync_channel) {
      FHSSsequence[i] = 0;
    } else {
      FHSSsequence[i] = i % freqCount;
    }
  }

  for (uint16_t i = 0; i < FHSS_SEQUENCE_LEN; i++) {
    if (i % freqCount != 0) {
      uint8_t offset = (i / freqCount) * freqCount;
      uint8_t rand = elrs_rngN(freqCount - 1) + 1;
      uint8_t temp = FHSSsequence[i];
      FHSSsequence[i] = FHSSsequence[offset + rand];
      FHSSsequence[offset + rand] = temp;
    }
  }
}

// [SEED-SOLVE] Brute-force the two unknown UID seed bytes (u2,u3) from collected (ptr,ch)
// constraints. u2 bit 7 lands at seed bit 31 which the RNG masks off (% 2^31), so it never
// affects the sequence -> we only scan u2 in 0..127 (32768 candidates, <~2s on ESP32) and every
// distinct sequence is unique in that space. We score each candidate by how many constraints it
// satisfies (best-score, not all-or-nothing) so a stray phase-drift/off-by-one observation costs
// one point instead of disqualifying the true seed. Accept only when the winner clears an
// absolute floor AND beats the runner-up by a clear margin. Returns the winner + its scores.
bool solveSeedFromConstraints(uint8_t u4, uint8_t u5, uint8_t ota,
                              uint8_t &out_u2, uint8_t &out_u3,
                              uint16_t &out_best, uint16_t &out_second) {
  static uint8_t seq[FHSS_SEQUENCE_LEN];
  const uint8_t freqCount = FHSS_FREQ_COUNT;
  const uint8_t sync = (ota == 4) ? (freqCount / 2) : ((freqCount / 2) + 1);
  uint16_t best = 0, second = 0; uint8_t f2 = 0, f3 = 0;

  for (uint16_t u2 = 0; u2 < 128; u2++) {
    for (uint16_t u3 = 0; u3 < 256; u3++) {
      uint32_t rs = (((uint32_t)u2 << 24) + ((uint32_t)u3 << 16) +
                     ((uint32_t)u4 << 8) + (uint8_t)(u5 ^ ota));
      // base sequence
      for (uint16_t i = 0; i < FHSS_SEQUENCE_LEN; i++) {
        if (i % freqCount == 0)          seq[i] = sync;
        else if (i % freqCount == sync)  seq[i] = 0;
        else                             seq[i] = i % freqCount;
      }
      // in-block Fisher-Yates using the ELRS LCG (must mirror buildDynamicFHSSSequence exactly)
      for (uint16_t i = 0; i < FHSS_SEQUENCE_LEN; i++) {
        if (i % freqCount != 0) {
          rs = (214013UL * rs + 2531011UL) % 2147483648UL;
          uint8_t rnd = (uint8_t)(((rs >> 16) % (freqCount - 1)) + 1);
          uint8_t off = (i / freqCount) * freqCount;
          uint8_t t = seq[i]; seq[i] = seq[off + rnd]; seq[off + rnd] = t;
        }
      }
      uint16_t hits = 0;
      for (uint16_t k = 0; k < g_seed_con_n; k++) {
        if (seq[g_seed_cons[k].ptr] == g_seed_cons[k].ch) hits++;
      }
      if (hits > best)       { second = best; best = hits; f2 = (uint8_t)u2; f3 = (uint8_t)u3; }
      else if (hits > second) second = hits;
    }
  }
  out_u2 = f2; out_u3 = f3; out_best = best; out_second = second;
  // Accept: enough absolute agreement and a decisive lead over the next-best candidate.
  return (g_seed_con_n >= 8) && (best >= 8) && (best >= second + 4);
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
    LOG_HOT("[TLM LINK] DroneRSSI:%d | DroneLQ:%u | DroneSNR:%+d\n", droneRssi, droneLq, droneSnr);
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
      LOG_HOT("[TLM GPS] Lat:%.6f | Lon:%.6f | Alt:%u | Spd:%u | Sats:%u\n", lat, lon, alt_m, spd_kmh, sats);
    } else if (sensorType == 0x08) { // Battery Sensor
      uint16_t vbat_mv = ((uint16_t)raw[3] << 8) | raw[4];
      uint16_t curr_ma = ((uint16_t)raw[5] << 8) | raw[6];
      uint32_t cap_mah = ((uint32_t)raw[7] << 16) | ((uint32_t)raw[8] << 8) | raw[9];
      uint8_t rem_pct = raw[10];
      float vbat = vbat_mv / 10.0f;
      float curr = curr_ma / 10.0f;
      LOG_HOT("[TLM BAT] V:%.1f | I:%.1f | Cap:%lu | Batt:%u\n", vbat, curr, (unsigned long)cap_mah, rem_pct);
    } else if (sensorType == 0x1E) { // Attitude Sensor
      int16_t pitch_deg = (int16_t)(((uint16_t)raw[3] << 8) | raw[4]) / 100;
      int16_t roll_deg = (int16_t)(((uint16_t)raw[5] << 8) | raw[6]) / 100;
      int16_t yaw_deg = (int16_t)(((uint16_t)raw[7] << 8) | raw[8]) / 100;
      LOG_HOT("[TLM ATT] Pitch:%d | Roll:%d | Yaw:%d\n", pitch_deg, roll_deg, yaw_deg);
    } else if (sensorType == 0x21) { // Flight Mode Frame
      char fmode[16] = {0};
      for (uint8_t i = 0; i < 12 && (3 + i) < 16; i++) {
        fmode[i] = (char)raw[3 + i];
        if (fmode[i] == 0) break;
      }
      LOG_HOT("[TLM MODE] Mode:%s\n", fmode[0] ? fmode : "ANGLE");
    }
  }
}

// Dynamically authenticate packet (supports both ELRS 4.x and 3.x, 8B and 13B frames)
bool authenticatePacket(const byte *raw, uint8_t &pkt_type, int &matched_slot, uint8_t len = 8) {
  pkt_type = raw[0] & 0x03;
  uint8_t data_len = (len == 13) ? 11 : 7;
  byte d[13];
  memcpy(d, raw, data_len);

  if (len == 8) { // Standard 8-byte OTA4 frame (CRC14)
    uint16_t inCRC = ((uint16_t)(raw[0] >> 2) << 8) | raw[7];
    uint8_t hop_int = RATE_TABLE[g_current_rate_idx].hop_interval;

    if (pkt_type == 0b10) { // SYNC PACKET
      d[0] = 0x02; // type=2, crcHigh=0
      if (ota_crc.calc(d, 7, dynamicCrcInit) == inCRC) {
        matched_slot = 0;
        return true;
      }
      if (ota_crc.calc(d, 7, dynamicCrcInit ^ 0x80) == inCRC) {
        dynamicCrcInit ^= 0x80;
        discovered_UID[5] ^= 0x80;
        buildDynamicFHSSSequence(discovered_UID[2], discovered_UID[3], discovered_UID[4], discovered_UID[5], g_ota_version);
        matched_slot = 0;
        return true;
      }
    } else if (pkt_type == 0b00) { // RC DATA PACKET ONLY
      // 1. ELRS 4.x primary fast path: d[0] = 0x00, CRC ^= OtaNonce
      d[0] = 0x00;
      if (g_link_locked) {
        for (int8_t offset = 0; offset <= 4; offset++) {
          int8_t deltas[2] = {offset, (int8_t)-offset};
          for (uint8_t d_idx = 0; d_idx < (offset == 0 ? 1 : 2); d_idx++) {
            uint8_t testNonce = (uint8_t)(OtaNonce + deltas[d_idx]);
            if (ota_crc.calc(d, 7, dynamicCrcInit ^ testNonce) == inCRC) {
              OtaNonce = testNonce;
              matched_slot = testNonce % hop_int;
              return true;
            }
          }
        }
      } else {
        // Strict latch: Require 2 consecutive packets with consecutive nonces before locking
        static uint8_t cand_nonce = 0;
        static uint32_t cand_time_ms = 0;
        uint32_t now_ms = millis();
        for (uint16_t n = 0; n < 256; n++) {
          if (ota_crc.calc(d, 7, dynamicCrcInit ^ (uint8_t)n) == inCRC) {
            if (cand_time_ms != 0 && (now_ms - cand_time_ms < 60) && 
                ((uint8_t)(cand_nonce + 1) == (uint8_t)n || (uint8_t)(cand_nonce + 2) == (uint8_t)n)) {
              OtaNonce = (uint8_t)n;
              matched_slot = n % hop_int;
              cand_time_ms = 0;
              return true;
            } else {
              cand_nonce = (uint8_t)n;
              cand_time_ms = now_ms;
            }
            break;
          }
        }
      }

      // 2. ELRS 3.x Switch Mode Wide: d[0] = (slot + 1) << 2
      for (uint8_t slot = 0; slot < hop_int; slot++) {
        d[0] = (slot + 1) << 2;
        if (ota_crc.calc(d, 7, dynamicCrcInit) == inCRC) {
          matched_slot = slot;
          return true;
        }
        if (ota_crc.calc(d, 7, dynamicCrcInit ^ 0x80) == inCRC) {
          dynamicCrcInit ^= 0x80;
          discovered_UID[5] ^= 0x80;
          buildDynamicFHSSSequence(discovered_UID[2], discovered_UID[3], discovered_UID[4], discovered_UID[5], g_ota_version);
          matched_slot = slot;
          return true;
        }
      }

      // 3. ELRS 3.x Direct slot without offset (d[0] = slot << 2)
      for (uint8_t slot = 0; slot < hop_int; slot++) {
        d[0] = slot << 2;
        if (ota_crc.calc(d, 7, dynamicCrcInit) == inCRC) {
          matched_slot = slot;
          return true;
        }
      }

      // 4. ELRS 3.x Switch Mode Hybrid (d[0] = 0x00)
      d[0] = 0x00;
      if (ota_crc.calc(d, 7, dynamicCrcInit) == inCRC) {
        matched_slot = 0;
        return true;
      }
      if (ota_crc.calc(d, 7, dynamicCrcInit ^ 0x80) == inCRC) {
        dynamicCrcInit ^= 0x80;
        discovered_UID[5] ^= 0x80;
        buildDynamicFHSSSequence(discovered_UID[2], discovered_UID[3], discovered_UID[4], discovered_UID[5], g_ota_version);
        matched_slot = 0;
        return true;
      }
    } else if (pkt_type == 0b11) { // TELEMETRY DOWNLINK PACKET
      d[0] = 0x03;
      if (ota_crc.calc(d, 7, dynamicCrcInit) == inCRC || ota_crc.calc(d, 7, dynamicCrcInit ^ 0x80) == inCRC) {
        matched_slot = 0;
        return true;
      }
      d[0] = 0x00;
      if (ota_crc.calc(d, 7, dynamicCrcInit ^ OtaNonce) == inCRC) {
        matched_slot = 0;
        return true;
      }
    }
  } else if (len == 13) { // Full Res 13-byte OTA8 frame
    uint16_t inCRC = ((uint16_t)raw[11] << 8) | raw[12];
    if (ota_crc.calc(d, 11, dynamicCrcInit) == inCRC || ota_crc.calc(d, 11, dynamicCrcInit ^ OtaNonce) == inCRC) {
      matched_slot = 0;
      return true;
    }
    if (ota_crc.calc(d, 11, dynamicCrcInit ^ 0x80) == inCRC || ota_crc.calc(d, 11, (dynamicCrcInit ^ 0x80) ^ OtaNonce) == inCRC) {
      dynamicCrcInit ^= 0x80;
      discovered_UID[5] ^= 0x80;
      buildDynamicFHSSSequence(discovered_UID[2], discovered_UID[3], discovered_UID[4], discovered_UID[5], g_ota_version);
      matched_slot = 0;
      return true;
    }
  }
  return false;
}

void applySemtechErrataFixes() {
#if !defined(BOARD_LILYGO_T3_S3_SX1276)
  uint8_t reg0889 = readReg8(0x0889);
  writeReg8(0x0889, reg0889 & ~0x04);
  writeReg8(0x08D8, 0x09);
  radio.setRxBoostedGainMode(true);
#endif
}

void applyRateConfig(uint8_t idx, bool force = false) {
  if (idx >= RATE_COUNT) idx = 0;
  if (!isRateSFCompatible(idx)) {
    Serial.printf("[RATE SKIP] %s (SF%u) incompatible with ELRS SX127x (SF%u-12); not applied.\n",
                  RATE_TABLE[idx].name, RATE_TABLE[idx].sf, ELRS_MIN_COMPAT_SF);
    return;
  }
  if (!force && idx == g_current_rate_idx) return;

  g_current_rate_idx = idx;
  const ELRSRateProfile &p = RATE_TABLE[g_current_rate_idx];

  radio.standby();
  radio.setSpreadingFactor(p.sf);
  radio.setBandwidth(p.bw_khz);
  radio.setCodingRate(p.cr);
  radio.setPreambleLength(8);
  radio.implicitHeader(p.payload_len);
  radio.setCRC(0);
  radio.invertIQ(false);
#if !defined(BOARD_LILYGO_T3_S3_SX1276)
  applySemtechErrataFixes();
#endif

  if (slot_timer != NULL) {
    timerAlarm(slot_timer, p.interval_us, true, 0);
  }

  radio.startReceive();

  Serial.printf("[RATE LOCKED] Rate:%s | SF:%u | BW:%.0fkHz | Interval:%uus\n",
                p.name, p.sf, p.bw_khz, p.interval_us);
}

void setup() {
  Serial.setTxBufferSize(4096);
  Serial.begin(921600); // High baud reduces host-side stalls during heavy scan output (must match Pi reader)
  Serial.setTimeout(20); // Bound readStringUntil() so a partial command can't stall loop() for 1s (dropping RX packets)
  unsigned long start = millis();
  while (!Serial && (millis() - start < 2500));

  Serial.println("\n=============================================");
  Serial.println("  CEMA Sniffer: Multi-Rate Auto-Demodulator");
  Serial.printf ("  Target Board: %s\n", BOARD_NAME);
#if defined(BAND_868_MHZ)
  Serial.println("  RF Band: EU868 (863.275 - 869.575 MHz)");
#else
  Serial.println("  RF Band: US915 (903.500 - 926.900 MHz)");
#endif
  Serial.println("=============================================\n");

#ifdef VEXT_PIN
  pinMode(VEXT_PIN, OUTPUT);
  digitalWrite(VEXT_PIN, LOW);
  delay(10);
#endif

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
  display.drawString(0, 0, BOARD_NAME);
  display.drawString(0, 16, "ELRS 4.x/3.x Auto");
  display.drawString(0, 32, "Hold: Deep Sleep");
  display.display();

  ota_crc.init(14, ELRS_CRC14_POLY);
  initFrequencyRegisters();

  // Flag rate profiles whose spreading factor an SX127x-based ELRS link cannot transmit (SF < ELRS_MIN_COMPAT_SF).
  // These are excluded from auto-scan and rate selection on this SX1262 build.
  Serial.printf("[RATE COMPAT] SX127x ELRS SF range: SF%u-12. Flagging incompatible profiles:\n", ELRS_MIN_COMPAT_SF);
  for (uint8_t r = 0; r < RATE_COUNT; r++) {
    if (!isRateSFCompatible(r)) {
      Serial.printf("  [SKIP] %-10s SF%u  (unsupported by SX127x - excluded)\n", RATE_TABLE[r].name, RATE_TABLE[r].sf);
    }
  }

  // Boot placeholder sequence. u2,u3 are unknown until the autonomous seed-solver recovers
  // them per pilot; these defaults are wrong on purpose so a real lock proves the solver works.
  discovered_UID[0] = 0;
  discovered_UID[1] = 0;
  discovered_UID[2] = 0;
  discovered_UID[3] = 0;
  discovered_UID[4] = 38;
  discovered_UID[5] = 194;
  g_ota_version = 4;
  sync_channel = 20; // Channel 20 = 915.5 MHz for ELRS 4.x
  dynamicCrcInit = (((uint16_t)(38 ^ 4) << 8) | 194) & 0x3FFF; // 0x22C2 for ELRS 4.x
  buildDynamicFHSSSequence(0, 0, 38, 194, 4);

  loraSpi->begin(SCK_PIN, MISO_PIN, MOSI_PIN, NSS_PIN);
  loraSpi->setFrequency(16000000); // 16 MHz Hardware SPI

#if defined(BOARD_LILYGO_T3_S3_SX1276)
  int state = radio.begin(freq_table[sync_channel], RATE_TABLE[0].bw_khz, RATE_TABLE[0].sf, RATE_TABLE[0].cr, 0x12, 10, 10);
#else
  int state = radio.begin(freq_table[sync_channel], RATE_TABLE[0].bw_khz, RATE_TABLE[0].sf, RATE_TABLE[0].cr, 0x12, 10, 10, 1.8, false);
  radio.setDio2AsRfSwitch(true);
  applySemtechErrataFixes();
#endif

  if (state != RADIOLIB_ERR_NONE) {
    Serial.printf("[ERROR] radio.begin() failed: %d\n", state);
    while (true) delay(500);
  }

  radio.implicitHeader(RATE_TABLE[0].payload_len);
  radio.setCRC(0);
  radio.invertIQ(false);
  radio.setPacketReceivedAction(packetISR);

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

  Serial.println("[AUTODISCOVERY] Ready on Dual Sync Channels (915.5 MHz v4 / 916.1 MHz v3)...");
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
        g_link_locked = false;
        g_hopping_locked = false;
        g_phase_hunting = false;
        isSynced = false;
        g_sync_grace_period_until = 0;
        g_seed_solved = false; g_seed_collecting = false; g_seed_con_n = 0; g_pin_channel = -1;
        g_last_auto_scan_us = now;
        setChannelFast(sync_channel);
        Serial.println("[RATE AUTO] Dynamic auto-rate scanning enabled.");
      } else {
        g_auto_rate_scan = false;
        for (uint8_t r = 0; r < RATE_COUNT; r++) {
          String name = String(RATE_TABLE[r].name);
          name.toUpperCase();
          if (rate == name) {
            if (!isRateSFCompatible(r)) {
              Serial.printf("[RATE REJECT] %s (SF%u) incompatible with ELRS SX127x (SF%u-12); ignoring.\n",
                            RATE_TABLE[r].name, RATE_TABLE[r].sf, ELRS_MIN_COMPAT_SF);
              break;
            }
            g_link_locked = false;
            g_hopping_locked = false;
            g_phase_hunting = false;
            isSynced = false;
            g_sync_grace_period_until = 0;
            g_seed_solved = false; g_seed_collecting = false; g_seed_con_n = 0; g_pin_channel = -1;
            applyRateConfig(r, true);
            setChannelFast(sync_channel);
            break;
          }
        }
      }
    } else if (cmd.startsWith("SCAN:START") || cmd == "SCAN") {
      g_scan_mode = true;
      g_link_locked = false;
      g_hopping_locked = false;
      g_phase_hunting = false;
      isSynced = false;
      g_sync_grace_period_until = 0;
      g_seed_solved = false; g_seed_collecting = false; g_seed_con_n = 0; g_pin_channel = -1;
      setChannelFast(sync_channel);
      Serial.println("[SCAN MODE] Airspace survey active. Monitoring sync channels for all beacons.");
    } else if (cmd.startsWith("PIN:")) {
      // Manual diagnostic: pin the radio to a channel and print [OBS] hits (offline seed analysis). PIN:OFF to release.
      String a = cmd.substring(4); a.trim(); a.toUpperCase();
      if (a == "OFF") { g_pin_channel = -1; Serial.println("[PIN] released"); }
      else { g_pin_channel = (int16_t)a.toInt(); Serial.printf("[PIN] holding radio on Ch %d\n", g_pin_channel); }
    } else if (cmd.startsWith("SCAN:STOP")) {
      g_scan_mode = false;
      Serial.println("[SCAN MODE] Airspace survey stopped.");
    } else if (cmd.startsWith("SET_BAND:")) {
      String b = cmd.substring(9);
      b.toUpperCase();
      if (b == "V4" || b == "4" || b == "915.5") {
        g_band_mode = 4;
        sync_channel = 20;
        setChannelFast(20);
        Serial.println("[BAND LOCKED] ExpressLRS 4.x (915.5 MHz / Ch 20).");
      } else if (b == "V3" || b == "3" || b == "916.1") {
        g_band_mode = 3;
        sync_channel = 21;
        setChannelFast(21);
        Serial.println("[BAND LOCKED] ExpressLRS 3.x (916.1 MHz / Ch 21).");
      } else {
        g_band_mode = 0;
        Serial.println("[BAND AUTO] Dual-band alternation (915.5 & 916.1 MHz).");
      }
    } else if (cmd.startsWith("LOCK_PILOT:")) {
      g_scan_mode = false;
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

          bool already_on_target = (discovered_UID[4] == u4 && discovered_UID[5] == u5 && g_link_locked && isSynced);

          g_target_uid[0] = u3;
          g_target_uid[1] = u4;
          g_target_uid[2] = u5;
          g_target_lock_enabled = true;

          if (already_on_target) {
            Serial.printf("[PILOT TARGET] Locked filter to current active pilot %u:%u:%u (Hopping preserved seamlessly).\n", u3, u4, u5);
          } else {
            g_link_locked = false;
            g_hopping_locked = false;
            g_phase_hunting = false;
            isSynced = false;
            g_sync_grace_period_until = now + 15000000;
            setChannelFast(sync_channel);
            Serial.printf("[PILOT TARGET] Locked filter to UID %u:%u:%u. Re-acquiring sync...\n", u3, u4, u5);
          }
        }
      }
    }
  }

  // Dual-frequency autodiscovery: alternate listening between Ch 20 (915.5 MHz v4) and Ch 21
  // (916.1 MHz v3) every 1.5s until we get a real SYNC. We deliberately gate on !g_hopping_locked
  // (set only by a genuine sync), NOT !g_link_locked: an RC packet can latch g_link_locked while
  // parked on the WRONG sync channel (e.g. Ch 21 for a v4 pilot), which would freeze the hunt on
  // that channel so no v4 sync is ever heard and the seed-solve never triggers. Keep hunting both
  // channels through an RC-only lock until the correct sync channel is found.
  static int64_t last_disc_alt_us = 0;
  static uint8_t disc_scan_ch = 20;
  if (!g_hopping_locked && g_pin_channel < 0 && (now - last_disc_alt_us > 1500000)) {
    last_disc_alt_us = now;
    if (g_band_mode == 0) {
      disc_scan_ch = (disc_scan_ch == 20) ? 21 : 20;
    } else if (g_band_mode == 4) {
      disc_scan_ch = 20;
    } else if (g_band_mode == 3) {
      disc_scan_ch = 21;
    }
    setChannelFast(disc_scan_ch);
  }

  // 2. Auto-Rate Discovery Engine: If not link-locked, rotate rates
  uint32_t auto_rate_period_us = g_scan_mode ? 3000000 : 12000000;
  if (!g_link_locked && g_auto_rate_scan && (now > g_sync_grace_period_until) && (now - g_last_auto_scan_us > auto_rate_period_us)) {
    g_last_auto_scan_us = now;
    // Advance to the next SX127x-compatible rate profile, skipping incompatible (SF5) ones
    uint8_t next_idx = g_current_rate_idx;
    for (uint8_t k = 0; k < RATE_COUNT; k++) {
      next_idx = (next_idx + 1) % RATE_COUNT;
      if (isRateSFCompatible(next_idx)) break;
    }
    applyRateConfig(next_idx, true);
    setChannelFast(disc_scan_ch);
  }

  // 3. Phase Hunt Timeout: If candidate hop receives no packet within 15ms, immediately return to sync channel!
  if (g_phase_hunting && (now - g_phase_hunt_start_us > 15000)) {
    g_phase_hunting = false;
    setChannelFast(sync_channel);
    g_phase_candidate = (g_phase_candidate + 1) % 6;
  }

  // [SEED-SOLVE] Autonomous sweep: pin each survey channel in turn (looping over the list for
  // several passes), recording strong RC hits. After each full pass, try to brute-force the
  // unknown seed bytes (u2,u3); accept the first decisive solution and resume full hop-following.
  if (g_seed_collecting) {
    const uint8_t nch = (uint8_t)(sizeof(SEED_SWEEP_CH));
    const uint8_t MAX_PASSES = 4;
    uint32_t ms = millis();
    if (g_seed_dwell_until_ms == 0) {                 // begin sweep
      g_seed_sweep_idx = 0;
      g_pin_channel = SEED_SWEEP_CH[0];
      g_seed_dwell_until_ms = ms + SEED_DWELL_MS;
      Serial.printf("[SEED] collecting FHSS constraints (pin Ch %u)...\n", (unsigned)SEED_SWEEP_CH[0]);
    } else if (ms >= g_seed_dwell_until_ms) {
      g_seed_sweep_idx++;
      bool pass_done = (g_seed_sweep_idx % nch) == 0;
      if (!pass_done) {
        g_pin_channel = SEED_SWEEP_CH[g_seed_sweep_idx % nch];
        g_seed_dwell_until_ms = ms + SEED_DWELL_MS;
      } else {
        // full pass complete -> attempt a solve from everything gathered so far
        uint8_t pass_no = g_seed_sweep_idx / nch;
        g_pin_channel = -1;
        uint8_t su2, su3; uint16_t best, second;
        Serial.printf("[SEED] pass %u: solving from %u constraints...\n", pass_no, g_seed_con_n);
        uint32_t t0 = millis();
        bool ok = solveSeedFromConstraints(discovered_UID[4], discovered_UID[5], g_ota_version,
                                           su2, su3, best, second);
        last_packet_time_us = esp_timer_get_time();   // fresh grace so no watchdog re-park after the blocking solve
        if (ok) {
          discovered_UID[2] = su2;
          discovered_UID[3] = su3;
          buildDynamicFHSSSequence(su2, su3, discovered_UID[4], discovered_UID[5], g_ota_version);
          g_seed_solved = true;
          g_seed_collecting = false;
          g_hopping_locked = true;
          Serial.printf("[SEED SOLVED] u2=%u u3=%u (best %u/%u vs %u, %lums) -> full FHSS hop-following active\n",
                        su2, su3, best, g_seed_con_n, second, (unsigned long)(millis() - t0));
        } else if (pass_no >= MAX_PASSES) {
          g_seed_collecting = false;   // give up gracefully; stay locked on sync channel
          Serial.printf("[SEED] no decisive solution after %u passes (best %u/%u vs %u); staying parked.\n",
                        pass_no, best, g_seed_con_n, second);
        } else {
          // keep the constraints, run another pass for more coverage
          Serial.printf("[SEED] pass %u inconclusive (best %u/%u vs %u); another pass...\n",
                        pass_no, best, g_seed_con_n, second);
          if (g_seed_con_n > 170) g_seed_con_n = 170;   // clamp against overflow across passes
          g_pin_channel = SEED_SWEEP_CH[0];
          g_seed_dwell_until_ms = millis() + SEED_DWELL_MS;
        }
      }
    }
  }

  // 4. Loss of Hopping Watchdog (600ms timeout during active hopping)
  if (g_pin_channel < 0 && !g_seed_collecting && g_hopping_locked && (now - last_packet_time_us > 600000)) {
    g_hopping_locked = false;
    g_phase_hunting = false;
    setChannelFast(sync_channel);
    Serial.printf("[FHSS RE-PARK] Hopping lost. Parked on %.1f MHz (Ch %u)...\n", freq_table[sync_channel], sync_channel);
  }

  // 5. Total Loss of Link Watchdog (5.0s timeout while parked on sync channel)
  if (g_link_locked && !g_hopping_locked && !g_phase_hunting && (now - last_packet_time_us > 5000000)) {
    g_link_locked = false;
    isSynced = false;
    setChannelFast(sync_channel);
    g_last_auto_scan_us = now;
    Serial.println("[!] Link lost. Scanning 915.5 / 916.1 MHz for re-acquisition...");
  }

  // 6. Ingest Received Packets
  if (packetReceived) {
    packetReceived = false;

    byte raw[16] = {0};
    uint8_t plen = RATE_TABLE[g_current_rate_idx].payload_len;
    float rssi = -100.0f;
    float snr = 0.0f;

    int state = radio.readData(raw, plen);
    if (state != RADIOLIB_ERR_NONE) {
      radio.startReceive();
      return;
    }
    rssi = radio.getRSSI();
    snr = radio.getSNR();
    radio.startReceive();

    // Throttled diagnostic (max once every 1000ms, or every 200ms on strong signal)
    static uint32_t last_diag_ms = 0;
    uint32_t now_ms = millis();
    if (!g_link_locked && ((rssi > -60.0f && now_ms - last_diag_ms >= 200) || (now_ms - last_diag_ms >= 1000))) {
      last_diag_ms = now_ms;
      LOG_HOT("[RX-DIAG %s @ %.1fMHz] RSSI=%.0f RAW: %02X %02X %02X %02X %02X %02X %02X %02X\n",
                    RATE_TABLE[g_current_rate_idx].name, freq_table[disc_scan_ch], rssi,
                    raw[0], raw[1], raw[2], raw[3], raw[4], raw[5], raw[6], raw[7]);
    }

    uint8_t pkt_type = raw[0] & 0x03;

    // While pinned, a received packet means the pilot is on the pinned channel at the current
    // FHSSptr -> hard constraint sequence[FHSSptr] == pinCh. During autonomous seed-solving we
    // record strong RC hits; the manual PIN command prints them for offline analysis.
    if (g_pin_channel >= 0) {
      if (g_seed_collecting) {
        if (pkt_type == 0b00 && rssi >= -80.0f && g_seed_con_n < (uint16_t)(sizeof(g_seed_cons)/sizeof(g_seed_cons[0]))) {
          g_seed_cons[g_seed_con_n].ptr = FHSSptr;
          g_seed_cons[g_seed_con_n].ch  = (uint8_t)g_pin_channel;
          g_seed_con_n++;
        }
      } else {
        Serial.printf("[OBS ptr=%u pinCh=%d nonce=%u type=%u rssi=%.0f]\n",
                      FHSSptr, g_pin_channel, OtaNonce, pkt_type, rssi);
      }
    }

    // 1. SYNC PACKET DISCOVERY
    if (pkt_type == 0b10) {
      uint8_t fhssIdx = raw[1];
      uint8_t nonce = raw[2];
      uint8_t data_len = (plen == 13) ? 11 : 7;
      uint16_t inCRC = ((uint16_t)(raw[0] >> 2) << 8) | raw[7];
      byte d[13];
      memcpy(d, raw, data_len);
      d[0] = 0x02;

      // Plausibility check: valid sequence index and a signal above the deep-noise floor.
      // The real validity gate is the 14-bit CRC match below (v4/v3_crc_matched) - false-CRC
      // "sync" garbage sits at -100..-120 dBm - so the RSSI floor only needs to reject deep
      // noise, not real (sometimes weak) beacons. A -80 floor here used to drop borderline
      // syncs, letting the pilot lock via RC only and never anchoring FHSSptr, so the
      // autonomous seed-solve never triggered ("[SEED] collecting" intermittently missing).
      bool plausibility_ok = (fhssIdx < FHSS_SEQUENCE_LEN) && (rssi > -92.0f);

      if (plausibility_ok) {
        uint16_t solvedCrc = solveCrcInit(d, inCRC);

        Serial.printf("[SYNC-DETECT @ %.1fMHz] RSSI:%.0f RAW: %02X %02X %02X %02X %02X %02X %02X %02X | solved=0x%04X (u4=%u, u5=%u)\n",
                      freq_table[sync_channel], rssi, raw[0], raw[1], raw[2], raw[3], raw[4], raw[5], raw[6], raw[7],
                      solvedCrc, (solvedCrc >> 8) ^ 4, solvedCrc & 0xFF);

        // First check ELRS 4.x:
        uint8_t u4_v4 = (solvedCrc >> 8) ^ 4;
        uint8_t u5_v4 = solvedCrc & 0xFF;
        bool v4_crc_matched = (u4_v4 == raw[5]) && (((u5_v4 ^ raw[6]) & ~0x3F) == 0);

        // Second check ELRS 3.x:
        // The 14-bit CRC init only encodes the LOW 6 bits of UID4 (top 2 bits are lost
        // in the 0x3FFF mask), so compare only those 6 bits against the sync payload's
        // full UID4 (raw[5]); u5 is fully recoverable. Previously this used an exact
        // u4 match, which could never pass for a pilot whose UID4 >= 64.
        uint8_t u4_v3 = (solvedCrc >> 8);
        uint8_t u5_v3 = (solvedCrc & 0xFF) ^ 3;
        bool v3_crc_matched = (((u4_v3 ^ raw[5]) & 0x3F) == 0) && (u5_v3 == raw[6]);
        uint8_t u3_v3 = raw[4];

        if (v4_crc_matched || v3_crc_matched) {
          // Resolve the reported pilot identity locally (no state mutation yet)
          uint8_t rep_ver = v4_crc_matched ? 4 : 3;
          uint8_t rep_ch  = v4_crc_matched ? 20 : 21;
          uint8_t rep_u4  = v4_crc_matched ? u4_v4 : raw[5];  // v3: report the full UID4 from the sync payload
          uint8_t rep_u5  = v4_crc_matched ? u5_v4 : u5_v3;

          // AIRSPACE SURVEY: observe & report only. Do NOT rewrite the demodulator
          // lock state (dynamicCrcInit / discovered_UID / sync_channel / FHSS) — otherwise
          // every beacon from every pilot thrashes the lock and the survey parks on the
          // sync channel returning only ~1-2 beacons/sec. Just catalogue and keep listening.
          if (g_scan_mode) {
            static uint32_t last_survey_ms = 0;
            uint32_t survey_ms = millis();
            if (survey_ms - last_survey_ms >= 100) {
              last_survey_ms = survey_ms;
              if (v4_crc_matched) {
                LOG_HOT("[PILOT DISCOVERED] v4 | UID4:%u UID5:%u | CRC:0x%04X | RSSI:%.0f | Rate:%s | Ch:20\n",
                        rep_u4, rep_u5, solvedCrc, rssi, RATE_TABLE[g_current_rate_idx].name);
              } else {
                LOG_HOT("[PILOT DISCOVERED] v3 | UID3:%u UID4:%u UID5:%u | CRC:0x%04X | RSSI:%.0f | Rate:%s | Ch:21\n",
                        u3_v3, rep_u4, rep_u5, solvedCrc, rssi, RATE_TABLE[g_current_rate_idx].name);
              }
            }
            radio.startReceive();
            return;
          }

          // TARGET LOCK FILTER: when a specific pilot is selected, ignore other pilots'
          // syncs so we actually lock the chosen one (this filter was previously never
          // enforced, so locks would bounce between pilots and stall the RC feed).
          if (g_target_lock_enabled && (rep_u4 != g_target_uid[1] || rep_u5 != g_target_uid[2])) {
            radio.startReceive();
            return;
          }

          uint8_t target_rate_idx = g_current_rate_idx;

          if (v4_crc_matched) {
            // ExpressLRS 4.x protocol
            g_ota_version = 4;
            sync_channel = 20;
            dynamicCrcInit = solvedCrc;
            discovered_UID[4] = u4_v4;
            discovered_UID[5] = u5_v4;

            // Rate resolution for ELRS 4.x
            uint8_t rfRateEnum = raw[3];
            if (rfRateEnum == 5 || rfRateEnum == 6 || rfRateEnum == 0) target_rate_idx = 0;      // 200Hz
            else if (rfRateEnum == 2 || rfRateEnum == 3) target_rate_idx = 1; // 100Hz
            else if (rfRateEnum == 1) target_rate_idx = 2;                    // 50Hz
            else if (rfRateEnum == 4) target_rate_idx = 3;                    // 25Hz

            // u2,u3 are NOT carried in the sync packet; the autonomous seed-solver (below)
            // recovers them by brute force. Build a placeholder sequence for now so the radio
            // has something to hop and FHSSptr can anchor — the solver overwrites it once solved.
            buildDynamicFHSSSequence(discovered_UID[2], discovered_UID[3], u4_v4, u5_v4, 4);
          } else {
            // ExpressLRS 3.x protocol
            g_ota_version = 3;
            sync_channel = 21;
            dynamicCrcInit = solvedCrc;
            discovered_UID[3] = u3_v3;
            discovered_UID[4] = raw[5];   // full UID4 from sync payload (CRC only carries low 6 bits)
            discovered_UID[5] = u5_v3;

            // Keep the current rate: we only decode a v3 sync when already at the pilot's
            // SF/rate, so re-deriving the rate from raw[3] (which mis-mapped to 50Hz) is
            // both unnecessary and harmful. target_rate_idx stays = g_current_rate_idx.

            buildDynamicFHSSSequence(discovered_UID[2], u3_v3, raw[5], u5_v3, 3);
          }

          static uint32_t last_sync_print_ms = 0;
          uint32_t sync_now_ms = millis();
          if (sync_now_ms - last_sync_print_ms >= 250) {
            last_sync_print_ms = sync_now_ms;
            Serial.printf("[PILOT DISCOVERED] v%u | UID4:%u UID5:%u | CRC:0x%04X | RSSI:%.0f | Rate:%s | Ch:%u\n",
                          g_ota_version, discovered_UID[4], discovered_UID[5], dynamicCrcInit, rssi, RATE_TABLE[g_current_rate_idx].name, sync_channel);
            Serial.printf("[SYNC VERIFIED] HopIdx:%u Nonce:%u | CRC:0x%04X\n", fhssIdx, nonce, dynamicCrcInit);
          }

          // Ignore syncs whose rate uses an SF an SX127x ELRS link can't transmit (e.g. SF5).
          // Such a link is SX1262/Gemini hardware, not our target, so don't lock onto it.
          if (!isRateSFCompatible(target_rate_idx)) {
            Serial.printf("[SYNC IGNORE] %s (SF%u) not SX127x-compatible; skipping lock.\n",
                          RATE_TABLE[target_rate_idx].name, RATE_TABLE[target_rate_idx].sf);
            radio.startReceive();
            return;
          }

          if (target_rate_idx < RATE_COUNT && target_rate_idx != g_current_rate_idx) {
            applyRateConfig(target_rate_idx, true);
          }

          FHSSptr = fhssIdx;
          OtaNonce = nonce;
          g_wide_switch_idx = nonce % 8;
          timerWrite(slot_timer, RATE_TABLE[g_current_rate_idx].toa_us);

          g_link_locked = true;
          isSynced = true;
          g_hopping_locked = true;
          g_phase_hunting = false;
          last_packet_time_us = now;

          g_rssi = rssi;
          g_snr = snr;

          // [SEED-SOLVE] Sync only reveals u4,u5. If we haven't solved this pilot's seed bytes
          // (u2,u3) yet, start the autonomous sweep now that FHSSptr is anchored and hopping is
          // locked. A different pilot (u4/u5 changed) invalidates any prior solution.
          if (!g_seed_collecting && (!g_seed_solved || rep_u4 != g_seed_pilot_u4 || rep_u5 != g_seed_pilot_u5)) {
            g_seed_solved = false;
            g_seed_pilot_u4 = rep_u4;
            g_seed_pilot_u5 = rep_u5;
            g_seed_con_n = 0;
            g_seed_dwell_until_ms = 0;
            g_seed_collecting = true;
          }
        }
      }
    }
    // 2. RC DATA PACKET
    else if (pkt_type == 0b00) {
      int matched_slot = -1;
      bool authenticated = authenticatePacket(raw, pkt_type, matched_slot, plen);

      if (authenticated) {
        if (!g_link_locked && !g_scan_mode) {
          g_link_locked = true;
          isSynced = true;
          Serial.printf("[LINK LOCKED via RC] RSSI:%.0f | Rate:%s\n", rssi, RATE_TABLE[g_current_rate_idx].name);
        }
        last_packet_time_us = now;
        timerWrite(slot_timer, RATE_TABLE[g_current_rate_idx].toa_us);

        uint8_t hop_int = RATE_TABLE[g_current_rate_idx].hop_interval;
        if (matched_slot >= 0) {
          OtaNonce = (OtaNonce & ~(hop_int - 1)) | (uint8_t)(matched_slot % hop_int);
        }

        // Canonical ExpressLRS HybridWideNonceToSwitchIndex
        uint8_t sw_idx = ((OtaNonce & 0b111) + ((OtaNonce >> 3) & 0b1)) % 8;
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

        // Rate-limit serial logging to ~25Hz (every 40ms) to prevent UART buffer blocking
        static uint32_t last_serial_emit_ms = 0;
        uint32_t now_ms = millis();
        if (now_ms - last_serial_emit_ms >= 40) {
          last_serial_emit_ms = now_ms;
          LOG_HOT("[RC %s] RSSI:%4.0f dBm | SNR:%+5.1f dB | CH1:%4u | CH2:%4u | CH3:%4u | CH4:%4u | CH5:%4u | CH6:%4u | CH7:%4u | CH8:%4u | CH9:%4u | CH10:%4u | CH11:%4u | CH12:%4u | CH13:%4u | CH14:%4u | CH15:%4u | CH16:%4u | ARM:%s\n",
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
        timerWrite(slot_timer, RATE_TABLE[g_current_rate_idx].toa_us);
        parseDownlinkTelemetry(raw);
      }
    }
  }
}