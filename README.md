# CEMA Tactical RF Intelligence & Multi-Domain Sensor System

An integrated, real-time Electronic Warfare (EW), Cyber-Electromagnetic Activities (CEMA), and Signals Intelligence (SIGINT) software platform designed for tactical air-defense and RF battlespace dominance.

---

## System Architecture & Multi-Domain Sensor Suite

The system synchronously fuses four independent sensor and processing domains:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                   CEMA TACTICAL MULTI-DOMAIN FUSION CORE                        │
└──────────────┬──────────────────┬─────────────────┬──────────────────┬──────────┘
               │                  │                 │                  │
┌──────────────▼──────────┐┌──────▼────────┐┌───────▼────────┐┌────────▼─────────┐
│ 1. HackRF One (SDR)     ││ 2. KrakenSDR  ││ 3. Heltec V3   ││ 4. RTX 3060 AI   │
│ - 1MHz to 6GHz RX/TX    ││ - 5-Ch Phase  ││ - Multi-Rate   ││ - ResNet-1D AMC  │
│ - Wideband Sweep Hunter ││   Interferom. ││   SX1262 LoRa  ││ - 4-Bit Quantized│
│ - Stare Mode (2MHz I/Q) ││ - Coherent DoA││ - Zero-Knowl.  ││   Qwen 1.5B SLM  │
│ - Native 60 FPS Video   ││ - 95% CEP     ││   ELRS Sniffer ││ - NATO SITREP /  │
│   PAL/NTSC Demodulator  ││   Geolocation ││ - Pilot Flight ││   INTSUM Gen     │
│ - Silicon CVA Fingerpr. ││ - Auto-Repair ││   Telemetry    ││ - ECM Jam Budget │
└─────────────────────────┘└───────────────┘└────────────────┘└──────────────────┘
```

---

## Key Capabilities & Operating Modes

### 1. Autonomous Hunter-Killer Engine
* **Automated Spectrum Hunting:** Sweeps user-defined tactical search bands (850–950 MHz UAV Control, 2.4 GHz ISM, 5.64–5.95 GHz FPV Video, 1.08–1.36 GHz 1.2G Video, or full 100–6000 MHz).
* **Wideband VTX & Burst Detection:** Detects spread-energy analog and digital video carriers using dynamic SNR thresholding ($\ge +10\text{ dB}$) and local plateau envelope recognition.
* **Autonomous Dwell & DoA Retune:** Automatically snaps into Stare Mode upon target detection, aligns the KrakenSDR frequency to calculate Line-of-Bearing (LoB) during the dwell window, and matches standard 48-channel FPV frequency tables (RaceBand R1–R8, FatShark F1–F8, Bands A/B/E).
* **Target Priority Queue:** Ranks threats dynamically from P1 Critical (Score 100/100) to P3 Advisory with voice synthesizer alert cues and automated tactical map injection.

### 2. High-Performance Native FPV Video Demodulator
* **Sub-Millisecond Zero-Copy Rendering:** Direct C-level frame extraction (`fpv_decoder.dll`) into Qt `Format_Indexed8` hardware color tables (Grayscale, Tactical Green, and Amber FLIR) at continuous 60 FPS.
* **Auto-Sync & Raster Recovery:** Hardware horizontal sync (64.0 µs line length) and vertical sync pulse locking with adjustable V-Hold/H-Hold for corrupted or weak RF video transmissions.
* **Detachable HUD / CRT Monitor:** Pop-out Tactical CRT window with real-time OSD channel indicators.

### 3. GPU-Accelerated Neural AMC (Automatic Modulation Classification)
* **1D Residual Convolutional Network (ResNet-1D):** Runs locally on CUDA Tensor Cores with $< 1.8\text{ ms}$ latency per buffer.
* **9 Modulation Classes:** Classifies LoRa/CSS, GFSK (Crossfire), 4-FSK (DMR), BPSK/QPSK, 16/64-QAM, OFDM (DJI OcuSync), Analog FM (FPV Video/Audio), AM (Aviation), and Continuous Wave (CW) jammers.

### 4. Tactical AI Intelligence Copilot (On-Device 4-Bit SLM)
* **Local VRAM Execution:** Quantized `Qwen/Qwen2.5-1.5B-Instruct` running entirely in 1.13 GB VRAM on NVIDIA GeForce RTX 3060 Laptop GPU.
* **Multi-Domain Synthesis:** Ingests live Station Position, Emitter Priority Queue, Heltec Pilot Telemetry, and Kraken Line-of-Bearing to answer natural language queries.
* **Automated NATO STANAG SITREP / INTSUM:** Generates formatted military intelligence summaries, kinetic flight phase classifications (Ground Staging, ISR Loiter, Ingress Transit, High-Speed Attack Dive), and Jammer-to-Signal ($J/S$) electronic countermeasure power budgets.

### 5. Multi-Rate ExpressLRS & Crossfire Sniffer (Heltec WiFi LoRa 32 V3)
* **16 MHz Hardware SPI:** SX1262 LoRa transceiver tuned for ultra-fast frequency retuning ($< 12\ \mu\text{s}$).
* **Zero-Knowledge UID Discovery:** Recovers pilot UID and dynamic CRC polynomials on sync channel 21 (916.1 MHz) and tracks 50Hz, 100Hz, 150Hz, 250Hz, 333Hz, and 500Hz FHSS hopping schedules.
* **Operator Standoff Discrepancy:** Computes Great-Circle Haversine vectors between station, drone GPS, and Kraken transmitter bearing to flag concealed ground pilots.

### 6. KrakenSDR Direction Finding & Auto-Healing WSL2 Engine
* **5-Channel Coherent MUSIC Interferometer:** Real-time spatial spectrum estimation and Line-of-Bearing (0.0° to 359.9° True) with confidence metrics.
* **Multi-Baseline Triangulation:** Solves 95% Circular Error Probable (CEP) target geolocation fixes.
* **Self-Healing Subsystem:** Background WSL2 keepalive daemon, dynamic `usbipd` path discovery, automated 5-tuner USB re-attachment, and stale shared memory cleanup (`/dev/shm`).

---

## Hardware Configuration & Tuning

| Parameter | Recommended Setting | Tactical Description |
| :--- | :--- | :--- |
| **HackRF LNA Gain** | 32 to 40 dB | Front-end RF low-noise amplifier at the antenna. Set higher for distant/weak signals. |
| **HackRF VGA Gain** | 32 to 44 dB | Baseband intermediate frequency amplifier. Adjust to avoid ADC clipping. |
| **Kraken Array Type** | Uniform Circular Array (UCA) | 5-element antenna array. Radius: 0.135m to 0.180m (depending on frequency). |
| **Heltec Sniffer Port** | `COM6` (115200 Baud) | USB-UART bridge to Heltec WiFi LoRa 32 V3 sniffer. |
| **GPU Inference** | NVIDIA CUDA (cu124) | RTX 3060 Tensor Cores with 6GB VRAM. |

---

## Flashing Heltec WiFi LoRa 32 V3 Sniffer Firmware

The complete, multi-rate sniffer source code is included in [`firmware/ELRS_Sniffer/ELRS_Sniffer.ino`](file:///C:/Users/toxic/Desktop/hackrf-utility/firmware/ELRS_Sniffer/ELRS_Sniffer.ino).

### Requirements & Arduino IDE Setup
1. **Board Package:** Install `esp32` by Espressif in Arduino Boards Manager.
   - Select Board: **Heltec WiFi LoRa 32(V3)**
   - Flash Size: **8MB (64Mb)**
   - Partition Scheme: **Default 4MB with spiffs (1.2MB APP/1.5MB SPIFFS)**
   - Upload Speed: **921600**
2. **Required Libraries:**
   - `RadioLib` (v6.0.0+) by Jan Gromes
   - `ESP8266 and ESP32 OLED driver for SSD1306 displays` (v4.4.0+) by ThingPulse
3. **Flashing Procedure:**
   - Open [`firmware/ELRS_Sniffer/ELRS_Sniffer.ino`](file:///C:/Users/toxic/Desktop/hackrf-utility/firmware/ELRS_Sniffer/ELRS_Sniffer.ino) in Arduino IDE.
   - Connect the Heltec V3 via USB-C and select the corresponding COM port.
   - Click **Upload**.
   - The on-board OLED display will initialize and report `Ready on Sync Channel (916.1 MHz)`.

---

## Standalone Deployment & Execution

### Running from Python Source
```powershell
# Activate Python 3.10 environment
python cema_app.py
```

### Running Standalone Executable
```powershell
# Launch pre-compiled standalone binary
.\CEMA_Tracker.exe
```

---

## Security & Data Sanitization Policy

* **Air-Gapped Operation:** All neural models (`tactical_amc_resnet1d.pt`, local SLM) and DSP algorithms operate strictly on local CPU/GPU resources with zero external internet dependencies.
* **Repository State:** `fingerprints.json` and `masks.json` are maintained as clean, empty default templates in version control. All tactical logs, audio debug files, and session captures are ignored via `.gitignore`.
