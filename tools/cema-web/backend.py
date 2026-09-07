"""
CEMA Tactical Web Backend - Raspberry Pi 5 & Mobile PWA
Autonomous Tactical Intelligence, LoRa Telemetry Sniffer & SDR Engine
"""

import os
import sys
import io
import time
import json
import re
import math
import queue
import ctypes
import asyncio
import threading
import subprocess
import urllib.request
from collections import deque
from typing import Dict, List, Optional, Set

import serial
import serial.tools.list_ports
import numpy as np

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

try:
    from PIL import Image
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

app = FastAPI(title="CEMA Tactical Suite", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Global State & Buffers
# -----------------------------------------------------------------------------
class TacticalState:
    def __init__(self):
        self.lock = threading.RLock()
        
        # Heltec / LoRa Node Status
        self.serial_connected = False
        self.active_port = "None"
        self.available_ports: List[str] = []
        self.user_selected_port: Optional[str] = None
        self.last_serial_line = ""
        self.serial_rx_count = 0
        self.active_rate_name = "AUTO"
        self.active_sf = 7
        self.active_bw_khz = 500.0
        self.active_interval_us = 10000
        self.active_target_pilot = "AUTO"
        
        # Airspace Survey / Recon Scan Mode
        self.scan_mode = False
        self.scan_rate = "50HZ"
        self.scan_band = "AUTO"
        
        # RC Channels (16 channels, standard center 1500, min 988, max 2012)
        self.channels = [1500] * 16
        self.channels[2] = 988   # CH3 Throttle idle
        self.channels[4] = 1000  # CH5 AUX1 Disarmed
        self.is_armed = False
        self.last_rc_timestamp = 0.0
        
        # Link & RF Metrics
        self.sniffer_rssi = -115.0
        self.sniffer_snr = 0.0
        self.packet_rate = "UNKNOWN"
        self.drone_lq = 0
        self.drone_rssi1 = -120.0
        self.drone_rssi2 = -120.0
        self.drone_snr = 0.0
        self.drone_tx_power = 0
        
        # Flight Dynamics & Telemetry
        self.pitch = 0.0
        self.roll = 0.0
        self.yaw = 0.0
        self.flight_mode = "STANDBY"
        
        # Battery
        self.battery_volt = 0.0
        self.battery_curr = 0.0
        self.battery_mah = 0
        self.battery_pct = 0.0
        
        # GPS
        self.gps_lat = 0.0
        self.gps_lon = 0.0
        self.gps_alt = 0.0
        self.gps_spd = 0.0
        self.gps_sats = 0
        self.has_gps = False
        
        # Maneuver Classifier
        self.maneuver_name = "DISARMED / MOTOR SHUTDOWN"
        self.maneuver_detail = "Motors Idle | Throttle: 0% | Cyclic Rate: 0 us/s"
        self.prev_stick_sample = [1500, 1500, 988, 1500]
        self.prev_stick_time = time.time()
        
        # Multi-Pilot Airspace Registry {uid_str: dict}
        self.pilots: Dict[str, dict] = {}
        
        # Event / SITREP Log (ring buffer max 300)
        self.sitrep_log: deque = deque(maxlen=300)
        
        # Raw Serial Console Log (ring buffer max 100)
        self.serial_log: deque = deque(maxlen=100)
        
        # System Health (Pi 5)
        self.sys_cpu_temp = 0.0
        self.sys_load = "0.00"
        self.sys_ram_pct = 0.0
        self.sys_wifi_clients = 0
        
        # HackRF SDR State
        self.hackrf_available = False
        self.hackrf_running = False
        self.hackrf_devices: List[dict] = []
        self.hackrf_selected_serial: Optional[str] = None
        self.hackrf_active_serial: Optional[str] = None
        self.hackrf_board_name: str = "None"
        self.hackrf_firmware: str = "None"
        self.hackrf_revision: str = "None"
        self.hackrf_freq_mhz = 915.0
        self.hackrf_lna = 32
        self.hackrf_vga = 30
        self.latest_fft: List[float] = []

        # KrakenSDR Direction-Finding (DoA) State
        self.kraken_enabled = False          # DoA acquisition worker actively polling
        self.kraken_mode = "KRAKEN_POLL"     # "KRAKEN_POLL" (live) | "SIMULATOR"
        self.kraken_host = "127.0.0.1"
        self.kraken_doa_port = 8081          # DOA_value.html fast stream
        self.kraken_api_port = 8080          # krakensdr_doa web GUI / API
        self.kraken_freq_mhz = 915.0
        self.kraken_gain_db = 30.0
        self.kraken_array = "UCA"            # "UCA" | "ULA"
        self.kraken_spacing_m = 0.135        # element spacing / array radius (meters)
        self.kraken_online = False           # DOA stream currently reachable
        self.kraken_tuners = 0               # RTL-SDR coherent tuners detected (0..5)
        self.kraken_health = "OFFLINE"       # OFFLINE | DEGRADED | INITIALIZING | ONLINE | SIMULATOR
        self.kraken_bearing_deg = 0.0        # latest Direction-of-Arrival bearing
        self.kraken_confidence = 0.0         # 0..100 %
        self.kraken_snr_db = 0.0             # peak power / SNR proxy
        self.kraken_locked = False           # confidence above lock threshold
        self.kraken_last_update = 0.0        # epoch of last valid bearing
        self.kraken_spectrum: List[float] = []  # 360-degree MUSIC pseudo-spectrum (0..1)

        # Analog FPV VTX Video Decoding (HackRF -> fpv_decoder.so)
        self.vtx_running = False
        self.vtx_freq_mhz = 5800.0
        self.vtx_channel = "F4"          # channel label
        self.vtx_standard = "PAL"        # PAL | NTSC
        self.vtx_lna = 32
        self.vtx_vga = 30
        self.vtx_invert = False
        # Video sync / centering / picture tuning (applied live, no HackRF restart)
        self.vtx_auto_hsync = True       # H-Sync PLL lock on/off
        self.vtx_line_len = 640.0        # manual line length px (620..660) when auto off
        self.vtx_v_hold = 0              # vertical hold offset, lines (0..625)
        self.vtx_h_hold = 0              # horizontal hold offset, px (-320..320)
        self.vtx_brightness = 0.0        # -50..50
        self.vtx_contrast = 1.0          # 0.5..3.0
        self.vtx_sync_locked = False
        self.vtx_fps = 0.0
        self.vtx_error = ""

tactical_state = TacticalState()

# WebSocket active client pool (managed purely in async loop, completely lock-free)
connected_clients: Set[WebSocket] = set()

# Command Queue for outgoing serial writes to Heltec
serial_cmd_queue = queue.Queue()

# Queue for pending SITREP entries to be broadcast
pending_sitrep_queue = queue.Queue()


def log_sitrep(level: str, category: str, message: str, voice: bool = False):
    """Adds a structured tactical entry to SITREP log."""
    ts = time.strftime("%H:%M:%S")
    entry = {
        "timestamp": ts,
        "time_epoch": time.time(),
        "level": level,         # "CRIT", "WARN", "INFO", "SUCCESS"
        "category": category,   # "LORA", "PILOT", "ARM", "SDR", "SYS"
        "message": message,
        "voice": voice          # If true, speech synth will announce on mobile client
    }
    with tactical_state.lock:
        tactical_state.sitrep_log.append(entry)
    
    # Non-blocking enqueue for async broadcaster
    pending_sitrep_queue.put(entry)


def update_maneuver_classifier(ch1: int, ch2: int, ch3: int, ch4: int, armed: bool):
    """
    Tactical Flight Dynamics & Maneuver Classifier
    Mode 2: CH1=Yaw, CH2=Pitch, CH3=Throttle, CH4=Roll
    """
    now = time.time()
    dt = max(0.01, now - tactical_state.prev_stick_time)
    
    # Throttle percentage (988us -> 0%, 2012us -> 100%)
    thr_pct = max(0.0, min(100.0, (ch3 - 988.0) / 10.24))
    
    # Cyclic rate: change in Pitch and Roll per second
    d_pit = abs(ch2 - tactical_state.prev_stick_sample[1]) / dt
    d_rol = abs(ch4 - tactical_state.prev_stick_sample[3]) / dt
    d_yaw = abs(ch1 - tactical_state.prev_stick_sample[0]) / dt
    cyclic_rate = max(d_pit, d_rol, d_yaw)
    
    tactical_state.prev_stick_sample = [ch1, ch2, ch3, ch4]
    tactical_state.prev_stick_time = now
    
    prev_armed = tactical_state.is_armed
    if armed != prev_armed:
        if armed:
            log_sitrep("CRIT", "ARM", "ALERT: TARGET DRONE ARMED - MOTORS ACTIVE", voice=True)
        else:
            log_sitrep("WARN", "ARM", "NOTICE: TARGET DRONE DISARMED - MOTORS INACTIVE", voice=True)
            
    if not armed:
        name = "DISARMED / MOTOR SHUTDOWN"
        detail = f"Motors Idle | Throttle: {thr_pct:.0f}% | Cyclic: {cyclic_rate:.0f} us/s"
    elif thr_pct < 5.0:
        name = "ARMED / ZERO THROTTLE GLIDE"
        detail = f"Free Flight | Throttle: {thr_pct:.0f}% | Cyclic: {cyclic_rate:.0f} us/s"
    elif cyclic_rate > 350.0:
        name = "RAPID AEROBATIC MANEUVER"
        detail = f"High Cyclic Rate ({cyclic_rate:.0f} us/s) | Throttle: {thr_pct:.0f}%"
    elif thr_pct > 80.0:
        name = "FULL THROTTLE PUNCHOUT"
        detail = f"Vertical Climb | Throttle: {thr_pct:.0f}% | High G-Force"
    elif ch2 < 1250:
        name = "AGGRESSIVE NOSE-DOWN DIVE"
        detail = f"Forward Penetration | Pitch: {ch2} us | Throttle: {thr_pct:.0f}%"
    elif ch2 > 1750:
        name = "PULL-UP / FLARE"
        detail = f"Airspeed Braking | Pitch: {ch2} us | Throttle: {thr_pct:.0f}%"
    elif abs(ch4 - 1500) > 280:
        name = "HIGH-BANK KNIFE EDGE TURN"
        detail = f"Coordinated Roll: {ch4} us | Yaw: {ch1} us"
    else:
        name = "STABLE CRUISE / HOVER"
        detail = f"Level Attitude | Throttle: {thr_pct:.0f}% | Trim Centered"
        
    tactical_state.maneuver_name = name
    tactical_state.maneuver_detail = detail


# -----------------------------------------------------------------------------
# Serial LoRa Sniffer Bridge (Heltec V3 / LilyGO T3-S3)
# -----------------------------------------------------------------------------
def get_candidate_serial_ports() -> List[str]:
    """Finds all viable USB serial ports on Linux/Pi 5 or Windows, strictly excluding internal hardware UARTs."""
    candidates = []
    
    # 1. On Linux, check USB serial device paths specifically
    if os.name == 'posix':
        # Check /dev/serial/by-id first (real USB adapters)
        if os.path.exists("/dev/serial/by-id"):
            try:
                for f in sorted(os.listdir("/dev/serial/by-id")):
                    full = os.path.join("/dev/serial/by-id", f)
                    if os.path.islink(full) or os.path.exists(full):
                        real = os.path.realpath(full)
                        if real not in candidates and not real.startswith("/dev/ttyAMA"):
                            candidates.append(real)
            except Exception:
                pass

        # Explicit /dev/ttyUSB* (CP2102/CP210x, CH340, FTDI)
        for i in range(8):
            p = f"/dev/ttyUSB{i}"
            if os.path.exists(p) and p not in candidates:
                candidates.append(p)
                
        # Explicit /dev/ttyACM* (CDC-ACM native USB)
        for i in range(8):
            p = f"/dev/ttyACM{i}"
            if os.path.exists(p) and p not in candidates:
                candidates.append(p)

    # 2. Query pyserial list_ports with strict exclusion of motherboard UARTs
    try:
        ports = serial.tools.list_ports.comports()
        for p in ports:
            dev = p.device
            # Strictly blacklist any internal Raspberry Pi hardware UARTs
            if any(dev.startswith(bad) for bad in ["/dev/ttyAMA", "/dev/ttyS", "/dev/ttyprintk", "/dev/tty0", "/dev/tty1"]):
                continue
            # On Linux, only allow true USB devices
            if os.name == 'posix' and not (dev.startswith("/dev/ttyUSB") or dev.startswith("/dev/ttyACM")):
                continue
            if dev not in candidates:
                candidates.append(dev)
    except Exception:
        pass
        
    return candidates


HELTEC_BAUD = 921600  # must match ELRS_Sniffer.ino Serial.begin()


def open_heltec_serial(port: str, baud: int = HELTEC_BAUD) -> serial.Serial:
    """
    Opens the Heltec serial WITHOUT pulsing DTR/RTS, so connecting/reconnecting does
    not auto-reset the ESP32-S3 (which would drop the sniffer's lock). On Linux we also
    clear hupcl so closing the port doesn't reset it either.
    """
    if os.name == "posix":
        try:
            subprocess.run(["stty", "-F", port, "-hupcl"], capture_output=True, timeout=2)
        except Exception:
            pass
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.1
    ser.dsrdtr = False
    ser.rtscts = False
    # Do NOT assign ser.dtr/ser.rts: on the Heltec V3's CP210x auto-reset circuit ANY
    # DTR/RTS line edge reboots the board. Empirically, clearing hupcl (above) and
    # leaving the modem lines untouched opens the port without resetting the ESP32-S3.
    ser.open()
    return ser


def serial_reader_thread():
    """Background worker continuously reading and parsing Heltec V3 serial frames."""
    ser: Optional[serial.Serial] = None
    current_port: Optional[str] = None
    
    log_sitrep("INFO", "LORA", "Heltec LoRa serial subsystem initialized.")
    
    while True:
        # Process outgoing command queue first
        while not serial_cmd_queue.empty():
            try:
                cmd = serial_cmd_queue.get_nowait()
                if ser and ser.is_open:
                    ser.write((cmd + "\n").encode('utf-8'))
                    ser.flush()
                    log_sitrep("INFO", "LORA", f"Transmitted command: {cmd}")
            except Exception as e:
                print(f"[SERIAL] Write error: {e}")
                
        # Check if user requested switching to a specific port
        with tactical_state.lock:
            forced_port = tactical_state.user_selected_port
            
        if forced_port and current_port != forced_port:
            if ser and ser.is_open:
                try:
                    ser.close()
                except Exception:
                    pass
            ser = None
            current_port = None

        # Maintain serial connection
        if ser is None or not ser.is_open:
            ports = get_candidate_serial_ports()
            with tactical_state.lock:
                tactical_state.available_ports = ports
                tactical_state.serial_connected = False
                tactical_state.active_port = "Searching..."
                
            if not ports:
                time.sleep(1.0)
                continue
                
            target_candidates = [forced_port] if forced_port in ports else ports
            for candidate in target_candidates:
                try:
                    ser = open_heltec_serial(candidate, HELTEC_BAUD)
                    current_port = candidate
                    with tactical_state.lock:
                        tactical_state.serial_connected = True
                        tactical_state.active_port = candidate
                    log_sitrep("SUCCESS", "LORA", f"Connected to LoRa sniffer on {candidate}", voice=True)
                    break
                except Exception:
                    ser = None
                    continue
                    
            if ser is None:
                time.sleep(1.0)
                continue

        # Drain outgoing serial commands to Heltec hardware
        while not serial_cmd_queue.empty():
            try:
                cmd_out = serial_cmd_queue.get_nowait()
                ser.write(f"{cmd_out}\n".encode('utf-8'))
                ser.flush()
                print(f"[SERIAL TX] Sent to sniffer: {cmd_out}")
            except Exception as e:
                print(f"[SERIAL TX ERR] {e}")

        # Read line
        try:
            raw_line = ser.readline().decode('utf-8', errors='ignore').strip()
        except Exception as e:
            print(f"[SERIAL] Read exception on {current_port}: {e}")
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(1.0)
            continue
            
        if not raw_line:
            continue
            
        # Log to raw serial terminal
        with tactical_state.lock:
            tactical_state.serial_rx_count += 1
            tactical_state.last_serial_line = raw_line
            tactical_state.serial_log.append({
                "time": time.strftime("%H:%M:%S"),
                "line": raw_line
            })

        try:
            # 1. [RATE LOCKED] or [RATE AUTO] Rate:50Hz | SF:8 | BW:500kHz | Interval:20000us
            rate_match = re.search(r'\[RATE (?:LOCKED|AUTO)\]\s*Rate:([\w\s]+?)\s*\|\s*SF:(\d+)\s*\|\s*BW:([\d\.]+)kHz\s*\|\s*Interval:(\d+)us', raw_line)
            if rate_match:
                r_name = rate_match.group(1).strip()
                sf = int(rate_match.group(2))
                bw = float(rate_match.group(3))
                inv = int(rate_match.group(4))
                with tactical_state.lock:
                    tactical_state.active_rate_name = r_name
                    tactical_state.active_sf = sf
                    tactical_state.active_bw_khz = bw
                    tactical_state.active_interval_us = inv
                    tactical_state.packet_rate = r_name
                log_sitrep("INFO", "LORA", f"Sniffer demod locked: {r_name} (SF{sf} / {bw}kHz)", voice=True)
                continue

            # 2. [RC 50Hz] RSSI: -11 dBm | SNR:+10.8 dB | CH1:1500 ... ARM:OFF
            if raw_line.startswith("[RC"):
                rc_meta = re.search(r'\[RC(?:\s+([\d\w\s]+))?\]\s*RSSI:\s*([-\d\.\+]+).*?SNR:\s*([-\d\.\+]+)', raw_line)
                if rc_meta:
                    prate = rc_meta.group(1).strip() if rc_meta.group(1) else "50Hz"
                    rssi = float(rc_meta.group(2))
                    snr = float(rc_meta.group(3))
                    
                    ch_matches = re.findall(r'CH(\d+):(\d+)', raw_line)
                    with tactical_state.lock:
                        tactical_state.sniffer_rssi = rssi
                        tactical_state.sniffer_snr = snr
                        tactical_state.packet_rate = prate
                        tactical_state.last_rc_timestamp = time.time()
                        
                        for ch_str, val_str in ch_matches:
                            idx = int(ch_str) - 1
                            if 0 <= idx < 16:
                                tactical_state.channels[idx] = int(val_str)
                                
                        arm_match = re.search(r'ARM:(ON|OFF)', raw_line)
                        if arm_match:
                            armed = (arm_match.group(1) == "ON")
                        else:
                            armed = (tactical_state.channels[4] > 1500)
                            
                        ch1 = tactical_state.channels[0]
                        ch2 = tactical_state.channels[1]
                        ch3 = tactical_state.channels[2]
                        ch4 = tactical_state.channels[3]
                        tactical_state.is_armed = armed

                        # Active pilot tracking: if pilot table is empty, auto-register active transmitter
                        if not tactical_state.pilots:
                            tactical_state.pilots["27:26:C2"] = {
                                "uid": "27:26:C2",
                                "u3": 39, "u4": 38, "u5": 194,
                                "crc": "22C2",
                                "rssi": rssi,
                                "rate": prate,
                                "first_seen": time.time(),
                                "last_seen": time.time(),
                                "count": 1
                            }
                        else:
                            for p in tactical_state.pilots.values():
                                if time.time() - p.get("last_seen", 0) < 5.0:
                                    p["rssi"] = rssi
                                    p["rate"] = prate
                                    p["last_seen"] = time.time()
                                    p["count"] += 1
                                    break
                    update_maneuver_classifier(ch1, ch2, ch3, ch4, armed)
                    continue

            # 3. [PILOT DISCOVERED] v4 | UID4:38 UID5:194 | CRC:0x22C2 | RSSI:-29 | Rate:50Hz | Ch:20
            if "[PILOT DISCOVERED]" in raw_line:
                proto_m = re.search(r'\bv(\d+)\b', raw_line)
                u4_m = re.search(r'UID4:(\d+)', raw_line)
                u5_m = re.search(r'UID5:(\d+)', raw_line)
                u3_m = re.search(r'UID3:(\d+)', raw_line)
                crc_m = re.search(r'CRC:0x([0-9A-Fa-f]+)', raw_line)
                rssi_m = re.search(r'RSSI:([-\d\.\+]+)', raw_line)
                rate_m = re.search(r'Rate:([^|\n\r]+)', raw_line)
                if u4_m and u5_m:
                    proto = f"v{proto_m.group(1)}" if proto_m else "v4"
                    u4 = int(u4_m.group(1))
                    u5 = int(u5_m.group(1))
                    if u4 == 38 and u5 == 194:
                        u3 = 39
                    elif u4 == 33 and u5 == 85:
                        u3 = 130
                    else:
                        u3 = int(u3_m.group(1)) if u3_m else 130
                    crc_hex = crc_m.group(1).upper() if crc_m else "22C2"
                    rssi = float(rssi_m.group(1)) if rssi_m else -50.0
                    rate = rate_m.group(1).strip() if rate_m else tactical_state.packet_rate
                    uid_str = f"{u3:02X}:{u4:02X}:{u5:02X}"
                    
                    with tactical_state.lock:
                        # Remove placeholder ACTIVE-01 once true hardware UID is confirmed
                        tactical_state.pilots.pop("ACTIVE-01", None)
                        if uid_str not in tactical_state.pilots:
                            is_new = True
                            tactical_state.pilots[uid_str] = {
                                "uid": uid_str,
                                "proto": proto,
                                "u3": u3, "u4": u4, "u5": u5,
                                "crc": crc_hex,
                                "rssi": rssi,
                                "rate": rate,
                                "first_seen": time.time(),
                                "last_seen": time.time(),
                                "count": 1
                            }
                        else:
                            is_new = False
                            tactical_state.pilots[uid_str]["proto"] = proto
                            tactical_state.pilots[uid_str]["rssi"] = rssi
                            tactical_state.pilots[uid_str]["rate"] = rate
                            tactical_state.pilots[uid_str]["last_seen"] = time.time()
                            tactical_state.pilots[uid_str]["count"] += 1
                            
                    if is_new:
                        log_sitrep("CRIT", "PILOT", f"NEW AIRSPACE TRANSMITTER: UID {uid_str} ({rate}) at {rssi:.0f}dBm", voice=True)
                    continue

            # 4. [PILOT TARGET] feedback
            if "[PILOT TARGET]" in raw_line:
                msg_target = raw_line.replace("[PILOT TARGET]", "").strip()
                log_sitrep("INFO", "PILOT", f"Sniffer Hardware: {msg_target}", voice=False)
                continue
        except Exception:
            pass

        # 4. [SYNC VERIFIED] HopIdx:12 Nonce:84 | CRC:0x2156
        if "[SYNC VERIFIED]" in raw_line:
            with tactical_state.lock:
                for p in tactical_state.pilots.values():
                    p["last_seen"] = time.time()
            continue

        # 5. [TLM GPS] Lat:51.50742 Lon:-0.12781 Alt:124m Spd:48km/h Sats:14
        gps_m = re.search(r'\[TLM GPS\]\s*Lat:([-\d\.]+)\s+Lon:([-\d\.]+)\s+Alt:([-\d\.]+)m\s+Spd:([-\d\.]+)km/h\s+Sats:(\d+)', raw_line)
        if gps_m:
            with tactical_state.lock:
                tactical_state.gps_lat = float(gps_m.group(1))
                tactical_state.gps_lon = float(gps_m.group(2))
                tactical_state.gps_alt = float(gps_m.group(3))
                tactical_state.gps_spd = float(gps_m.group(4))
                tactical_state.gps_sats = int(gps_m.group(5))
                tactical_state.has_gps = True
            continue

        # 6. [TLM BATTERY] Volt:15.8V Curr:12.4A mAh:680
        bat_m = re.search(r'\[TLM BATTERY\]\s*Volt:([-\d\.]+)V\s+Curr:([-\d\.]+)A\s+mAh:(\d+)', raw_line)
        if bat_m:
            v = float(bat_m.group(1))
            c = float(bat_m.group(2))
            mah = int(bat_m.group(3))
            cells = max(1, round(v / 3.8))
            cell_v = v / cells
            pct = max(0.0, min(100.0, (cell_v - 3.5) / 0.7 * 100.0))
            with tactical_state.lock:
                tactical_state.battery_volt = v
                tactical_state.battery_curr = c
                tactical_state.battery_mah = mah
                tactical_state.battery_pct = round(pct, 1)
            continue

        # 7. [TLM ATTITUDE] Pitch:+12.4deg Roll:-4.1deg Yaw:182.0deg
        att_m = re.search(r'\[TLM ATTITUDE\]\s*Pitch:([-\d\.\+]+)deg\s+Roll:([-\d\.\+]+)deg\s+Yaw:([-\d\.\+]+)deg', raw_line)
        if att_m:
            with tactical_state.lock:
                tactical_state.pitch = float(att_m.group(1))
                tactical_state.roll = float(att_m.group(2))
                tactical_state.yaw = float(att_m.group(3))
            continue

        # 8. [TLM MODE] Mode:ACRO
        mode_m = re.search(r'\[TLM MODE\]\s*Mode:(\w+)', raw_line)
        if mode_m:
            with tactical_state.lock:
                tactical_state.flight_mode = mode_m.group(1)
            continue

        # 9. [TLM LINK] Link:100% RSSI1:-45dBm RSSI2:-48dBm SNR:+11dB Power:25mW
        link_m = re.search(r'\[TLM LINK\]\s*Link:(\d+)%\s+RSSI1:([-\d\.]+)dBm\s+RSSI2:([-\d\.]+)dBm\s+SNR:([-\d\.\+]+)dB\s+Power:(\d+)mW', raw_line)
        if link_m:
            with tactical_state.lock:
                tactical_state.drone_lq = int(link_m.group(1))
                tactical_state.drone_rssi1 = float(link_m.group(2))
                tactical_state.drone_rssi2 = float(link_m.group(3))
                tactical_state.drone_snr = float(link_m.group(4))
                tactical_state.drone_tx_power = int(link_m.group(5))
            continue


# -----------------------------------------------------------------------------
# System Health Poller (Raspberry Pi 5)
# -----------------------------------------------------------------------------
def system_monitor_thread():
    """Monitors Pi 5 CPU temperature, load, RAM, and Wi-Fi hotspot stations."""
    while True:
        cpu_temp = 0.0
        if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
            try:
                with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                    cpu_temp = round(int(f.read().strip()) / 1000.0, 1)
            except Exception:
                pass
                
        load_str = "0.00"
        if os.path.exists("/proc/loadavg"):
            try:
                with open("/proc/loadavg", "r") as f:
                    parts = f.read().split()
                    load_str = parts[0]
            except Exception:
                pass
                
        ram_pct = 0.0
        if os.path.exists("/proc/meminfo"):
            try:
                mem_total = 0
                mem_avail = 0
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            mem_total = int(line.split()[1])
                        elif line.startswith("MemAvailable:"):
                            mem_avail = int(line.split()[1])
                if mem_total > 0:
                    ram_pct = round(((mem_total - mem_avail) / mem_total) * 100.0, 1)
            except Exception:
                pass
                
        wifi_clients = 0
        try:
            res = subprocess.run(["iw", "dev", "wlan0", "station", "dump"], capture_output=True, text=True, timeout=1)
            wifi_clients = res.stdout.count("Station ")
        except Exception:
            pass
            
        with tactical_state.lock:
            tactical_state.sys_cpu_temp = cpu_temp
            tactical_state.sys_load = load_str
            tactical_state.sys_ram_pct = ram_pct
            tactical_state.sys_wifi_clients = wifi_clients
            
        time.sleep(2.0)


# -----------------------------------------------------------------------------
# HackRF SDR Device Detection & Stare/Sweep Engine
# -----------------------------------------------------------------------------
hackrf_stop_event = threading.Event()

def get_candidate_hackrf_devices() -> List[dict]:
    """Runs hackrf_info and parses all connected HackRF units."""
    devices = []
    try:
        res = subprocess.run(["hackrf_info"], capture_output=True, text=True, timeout=2)
        text = res.stdout or ""
        blocks = text.split("Found HackRF")
        for blk in blocks[1:]:
            dev = {}
            idx_m = re.search(r'Index:\s*(\d+)', blk)
            dev["index"] = int(idx_m.group(1)) if idx_m else len(devices)
            
            serial_m = re.search(r'Serial number:\s*([0-9a-fA-F]+)', blk)
            full_serial = serial_m.group(1).lower() if serial_m else "Unknown"
            dev["serial"] = full_serial
            dev["short_serial"] = full_serial[-8:] if len(full_serial) >= 8 else full_serial
            
            board_m = re.search(r'Board ID Number:\s*\d+\s*\((.*?)\)', blk)
            dev["board"] = board_m.group(1) if board_m else "HackRF One"
            
            fw_m = re.search(r'Firmware Version:\s*([^\n\r\(]+)', blk)
            dev["firmware"] = fw_m.group(1).strip() if fw_m else "Unknown"
            
            rev_m = re.search(r'Hardware Revision:\s*(\w+)', blk)
            dev["revision"] = rev_m.group(1) if rev_m else "Unknown"
            
            dev["label"] = f"{dev['board']} (Rev {dev['revision']}) - ...{dev['short_serial']}"
            devices.append(dev)
    except Exception:
        pass
        
    return devices


def hackrf_dsp_worker():
    """Monitors HackRF hardware attachment and streams real-time FFT spectrum."""
    global hackrf_stop_event
    last_detect_time = 0.0
    prev_dev_count = -1

    while True:
        now = time.time()
        
        # Periodically scan for HackRF devices when not streaming
        if not tactical_state.hackrf_running and not tactical_state.vtx_running and (now - last_detect_time > 3.0):
            last_detect_time = now
            devs = get_candidate_hackrf_devices()
            with tactical_state.lock:
                tactical_state.hackrf_devices = devs
                tactical_state.hackrf_available = (len(devs) > 0)
                if devs:
                    serials = [d["serial"] for d in devs]
                    if not tactical_state.hackrf_selected_serial or tactical_state.hackrf_selected_serial not in serials:
                        tactical_state.hackrf_selected_serial = devs[0]["serial"]
                    
                    cur = next((d for d in devs if d["serial"] == tactical_state.hackrf_selected_serial), devs[0])
                    tactical_state.hackrf_board_name = cur["board"]
                    tactical_state.hackrf_firmware = cur["firmware"]
                    tactical_state.hackrf_revision = cur["revision"]
                else:
                    tactical_state.hackrf_board_name = "None"
                    tactical_state.hackrf_firmware = "None"
                    tactical_state.hackrf_revision = "None"
                    
            if len(devs) != prev_dev_count:
                if devs:
                    d0 = devs[0]
                    log_sitrep("SUCCESS", "SDR", f"HackRF One detected: Serial ...{d0['short_serial']} (Rev {d0['revision']})", voice=False)
                elif prev_dev_count > 0:
                    log_sitrep("WARN", "SDR", "HackRF One disconnected.", voice=False)
                prev_dev_count = len(devs)

        if not tactical_state.hackrf_running or not tactical_state.hackrf_available or tactical_state.vtx_running:
            time.sleep(0.5)
            continue
            
        freq_hz = int(tactical_state.hackrf_freq_mhz * 1e6)
        lna = tactical_state.hackrf_lna
        vga = tactical_state.hackrf_vga
        target_serial = tactical_state.hackrf_selected_serial
        
        cmd = [
            "hackrf_transfer",
            "-r", "-",
            "-f", str(freq_hz),
            "-s", "20000000",
            "-l", str(lna),
            "-g", str(vga),
            "-a", "1"
        ]
        if target_serial and target_serial != "Unknown":
            cmd.extend(["-d", target_serial])
            
        with tactical_state.lock:
            tactical_state.hackrf_active_serial = target_serial
            
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=32768)
            short_s = target_serial[-8:] if target_serial else "Default"
            log_sitrep("INFO", "SDR", f"HackRF Stare tuned to {tactical_state.hackrf_freq_mhz} MHz (Dev ...{short_s})")
            
            CHUNK_SIZE = 32768
            fft_size = 512
            window = np.hanning(fft_size)
            bytes_needed = fft_size * 2
            last_fft_time = 0.0
            
            while tactical_state.hackrf_running and not hackrf_stop_event.is_set():
                raw = proc.stdout.read(CHUNK_SIZE)
                if not raw or len(raw) < bytes_needed:
                    time.sleep(0.005)
                    continue
                
                # Rate-limit FFT calculation to ~25Hz (every 40ms) to conserve CPU
                now = time.time()
                if now - last_fft_time < 0.04:
                    continue
                last_fft_time = now
                
                # Take the freshest chunk of bytes for FFT
                raw_fft = raw[-bytes_needed:]
                iq_raw = np.frombuffer(raw_fft, dtype=np.int8).astype(np.float32)
                i_data = iq_raw[0::2]
                q_data = iq_raw[1::2]
                
                # Zero-mean removal to kill DC hardware bias
                i_data = i_data - np.mean(i_data)
                q_data = q_data - np.mean(q_data)
                
                # Normalize raw int8 [-128, 127] to [-1.0, 1.0] and apply window
                iq_complex = ((i_data + 1j * q_data) / 128.0) * window
                
                # Compute FFT
                fft_complex = np.fft.fftshift(np.fft.fft(iq_complex, fft_size))
                
                # Coherent gain normalization (Hanning window sum = fft_size / 2)
                mag = np.abs(fft_complex) / (fft_size / 2.0)
                
                # LO Leakage Spike Notch Filter (central 5 bins around DC center)
                center_bin = fft_size // 2
                bg_val = (np.median(mag[max(0, center_bin-10):center_bin-2]) + 
                          np.median(mag[center_bin+3:min(fft_size, center_bin+11)])) / 2.0
                mag[center_bin-2:center_bin+3] = bg_val
                
                # Decibels full scale (dBFS: -100 to 0)
                mag_db = 20.0 * np.log10(np.maximum(mag, 1e-5))
                
                with tactical_state.lock:
                    tactical_state.latest_fft = [round(float(v), 1) for v in mag_db]
                
        except Exception as e:
            print(f"[HACKRF] DSP worker exception: {e}")
        finally:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
            hackrf_stop_event.clear()
            with tactical_state.lock:
                tactical_state.hackrf_active_serial = None
            time.sleep(1.0)


# -----------------------------------------------------------------------------
# KrakenSDR Direction Finding (DoA) Engine
#
# Mirrors the desktop CEMA Tracker (cema_app.KrakenDoAThread): consumes the
# krakensdr_doa "DOA_value.html" fast stream (CSV) either from the local Pi
# filesystem (when the DAQ runs on this host) or over HTTP from a remote/WSL
# Kraken host, parses bearing + the 360-degree MUSIC pseudo-spectrum, and folds
# it into the tactical state for the mobile PWA. A SIMULATOR mode is included
# for demos / bench testing without hardware.
# -----------------------------------------------------------------------------
KRAKEN_LOCK_CONF = 40.0          # confidence % above which a bearing counts as "locked"
KRAKEN_LOCAL_DOA_FILES = [       # DOA_value.html locations when the DAQ runs on this Pi
    os.path.expanduser("~/krakensdr_doa/krakensdr_doa/_share/DOA_value.html"),
    "/home/feeka/krakensdr_doa/krakensdr_doa/_share/DOA_value.html",
]
KRAKEN_LOCAL_SETTINGS = [        # settings.json locations for local retune
    os.path.expanduser("~/krakensdr_doa/krakensdr_doa/_share/settings.json"),
    "/home/feeka/krakensdr_doa/krakensdr_doa/_share/settings.json",
]

kraken_stop_event = threading.Event()
_kraken_sim_angle = 135.0


def parse_doa_value_line(raw_text: str) -> Optional[dict]:
    """Parses a krakensdr_doa DOA_value.html CSV line into a bearing dict, or None."""
    if not raw_text or "," not in raw_text:
        return None
    parts = [p.strip() for p in raw_text.split(",") if p.strip()]
    if len(parts) < 6:
        return None
    try:
        ts = float(parts[0])
        bearing = float(parts[1])
        conf_raw = float(parts[2])
        pwr = float(parts[3])
        freq_val = float(parts[4])
    except (ValueError, IndexError):
        return None

    f_mhz = freq_val / 1e6 if freq_val > 1e5 else freq_val

    # 360-degree MUSIC pseudo-spectrum lives at parts[17:17+360]; normalise to 0..1
    spectrum: List[float] = []
    if len(parts) >= 17 + 360:
        try:
            spec_raw = [float(x) for x in parts[17:17 + 360]]
            max_s = max(spec_raw)
            min_s = min(spec_raw)
            rng = (max_s - min_s) if max_s > min_s else 1.0
            spectrum = [round((x - min_s) / rng, 4) for x in spec_raw]
        except ValueError:
            spectrum = []

    # Kraken reports confidence either as a small ratio (<10) or already a 0..100 value
    conf = min(100.0, max(0.0, conf_raw * 18.0)) if conf_raw < 10.0 else min(100.0, conf_raw)

    return {
        "ts": ts,
        "bearing": bearing % 360.0,
        "confidence": conf,
        "snr_db": pwr,
        "freq_mhz": f_mhz,
        "spectrum": spectrum,
    }


def _fetch_doa_raw(host: str, port: int) -> Optional[str]:
    """Reads the DOA_value.html stream: local file fast-path, else HTTP GET."""
    # Fast path: DAQ running on this Pi -> read the shared file directly
    if host in ("127.0.0.1", "localhost", "::1"):
        for p in KRAKEN_LOCAL_DOA_FILES:
            try:
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        txt = f.read().strip()
                    if txt:
                        return txt
            except Exception:
                pass
    # Network path (remote Kraken host, or local web server)
    try:
        url = f"http://{host}:{port}/DOA_value.html"
        req = urllib.request.Request(url, headers={"User-Agent": "CEMA-Web/2.0"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="ignore").strip()
    except Exception:
        pass
    return None


def _count_kraken_tuners() -> int:
    """Counts attached RTL-SDR (0bda:2838) coherent tuners on this Linux host."""
    if os.name != "posix":
        return 0
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=2)
        return out.stdout.count("0bda:2838")
    except Exception:
        return 0


def push_kraken_settings(freq_mhz: float, gain_db: float, array: str, spacing_m: float) -> bool:
    """Best-effort retune of a locally-running krakensdr_doa via its settings.json."""
    target = None
    for p in KRAKEN_LOCAL_SETTINGS:
        if os.path.exists(p):
            target = p
            break
    if not target:
        return False
    try:
        with open(target, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    cfg["center_freq"] = float(freq_mhz)
    cfg["uniform_gain"] = float(gain_db)
    cfg["en_doa"] = True
    cfg["ant_arrangement"] = "UCA" if array.upper() == "UCA" else "ULA"
    cfg["ant_spacing_meters"] = float(spacing_m)
    cfg["doa_method"] = "MUSIC"
    cfg["vfo_freq_0"] = float(freq_mhz * 1e6)
    cfg["ext_upd_flag"] = True
    try:
        with open(target, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        # Nudge inotify so the DAQ hw_controller reloads immediately
        try:
            os.utime(target, None)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _kraken_simulate(freq_mhz: float) -> dict:
    """Generates a plausible synthetic bearing + MUSIC spectrum for demo mode."""
    global _kraken_sim_angle
    import random
    _kraken_sim_angle = (_kraken_sim_angle + 0.4 + random.uniform(-0.35, 0.35)) % 360.0
    conf = min(98.5, max(65.0, 92.0 + random.uniform(-4.0, 4.0)))
    snr = min(35.0, max(12.0, 26.0 + random.uniform(-1.5, 1.5)))
    main_deg = int(_kraken_sim_angle)
    multipath_deg = (main_deg + 85) % 360
    spectrum = [0.0] * 360
    for deg in range(360):
        d_main = min(abs(deg - main_deg), 360 - abs(deg - main_deg))
        d_multi = min(abs(deg - multipath_deg), 360 - abs(deg - multipath_deg))
        p_main = max(0.0, 1.0 - (d_main / 22.0) ** 2)
        p_multi = 0.35 * max(0.0, 1.0 - (d_multi / 32.0) ** 2)
        spectrum[deg] = round(max(0.0, min(1.0, p_main + p_multi + random.uniform(0.02, 0.07))), 4)
    return {
        "bearing": _kraken_sim_angle,
        "confidence": conf,
        "snr_db": snr,
        "freq_mhz": freq_mhz,
        "spectrum": spectrum,
    }


def _cardinal(bearing: float) -> str:
    cards = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return cards[int((bearing + 11.25) / 22.5) % 16]


def kraken_doa_worker():
    """Background thread: acquires KrakenSDR DoA bearings and updates tactical state."""
    last_ts = 0.0
    last_health_check = 0.0
    last_lock_sitrep = 0.0
    was_locked = False

    while True:
        now = time.time()

        # Periodic hardware/stream health check (cheap, every ~3s) regardless of run state
        if now - last_health_check > 3.0:
            last_health_check = now
            host = tactical_state.kraken_host
            doa_port = tactical_state.kraken_doa_port
            tuners = _count_kraken_tuners()
            stream_ok = _fetch_doa_raw(host, doa_port) is not None
            with tactical_state.lock:
                if tactical_state.kraken_mode == "SIMULATOR" and tactical_state.kraken_enabled:
                    tactical_state.kraken_health = "SIMULATOR"
                    tactical_state.kraken_online = True
                elif stream_ok:
                    tactical_state.kraken_health = "ONLINE"
                    tactical_state.kraken_online = True
                elif tuners == 5:
                    tactical_state.kraken_health = "INITIALIZING"
                    tactical_state.kraken_online = False
                elif tuners > 0:
                    tactical_state.kraken_health = "DEGRADED"
                    tactical_state.kraken_online = False
                else:
                    tactical_state.kraken_health = "OFFLINE"
                    tactical_state.kraken_online = False
                tactical_state.kraken_tuners = tuners

        if not tactical_state.kraken_enabled:
            was_locked = False
            time.sleep(0.2)
            continue

        with tactical_state.lock:
            mode = tactical_state.kraken_mode
            host = tactical_state.kraken_host
            doa_port = tactical_state.kraken_doa_port
            freq_mhz = tactical_state.kraken_freq_mhz

        result = None
        if mode == "SIMULATOR":
            result = _kraken_simulate(freq_mhz)
            time.sleep(0.08)
        else:
            raw = _fetch_doa_raw(host, doa_port)
            parsed = parse_doa_value_line(raw) if raw else None
            if parsed and parsed["ts"] != last_ts:
                last_ts = parsed["ts"]
                result = parsed
            time.sleep(0.05)  # 20 Hz poll

        if not result:
            continue

        bearing = result["bearing"]
        conf = result["confidence"]
        locked = conf >= KRAKEN_LOCK_CONF

        with tactical_state.lock:
            tactical_state.kraken_bearing_deg = round(bearing, 1)
            tactical_state.kraken_confidence = round(conf, 1)
            tactical_state.kraken_snr_db = round(result["snr_db"], 1)
            tactical_state.kraken_locked = locked
            tactical_state.kraken_last_update = now
            if result["spectrum"]:
                tactical_state.kraken_spectrum = result["spectrum"]
            if mode != "SIMULATOR" and result.get("freq_mhz"):
                tactical_state.kraken_freq_mhz = round(result["freq_mhz"], 3)

        # SITREP on lock acquisition, and throttled while held
        if locked and (not was_locked or now - last_lock_sitrep > 8.0):
            last_lock_sitrep = now
            log_sitrep("SUCCESS", "SDR",
                       f"DoA LOCK: {bearing:05.1f}° {_cardinal(bearing)} | CONF {conf:.0f}% | SNR {result['snr_db']:.0f}dB",
                       voice=(not was_locked))
        was_locked = locked


# -----------------------------------------------------------------------------
# Analog FPV VTX Video Decoder (HackRF -> fpv_decoder.so -> MJPEG)
#
# Ports the desktop CEMA Tracker's NativeHackRFVideoThread to the Pi: the native
# C decoder (fpv_decoder.so, cross-compiled from fpv_decoder.c) FM-demods the
# analog 5.8/1.2 GHz VTX from HackRF IQ at 10 MSPS into 640x480 grayscale frames,
# which are JPEG-encoded and served as an MJPEG stream to the PWA.
# -----------------------------------------------------------------------------
VTX_W, VTX_H = 640, 480

# Common analog FPV video channels (MHz). Grouped by band; label -> freq.
VTX_CHANNELS = {
    "R1": 5658, "R2": 5695, "R3": 5732, "R4": 5769, "R5": 5806, "R6": 5843, "R7": 5880, "R8": 5917,
    "F1": 5740, "F2": 5760, "F3": 5780, "F4": 5800, "F5": 5820, "F6": 5840, "F7": 5860, "F8": 5880,
    "A1": 5865, "A2": 5845, "A3": 5825, "A4": 5805, "A5": 5785, "A6": 5765, "A7": 5745, "A8": 5725,
    "B1": 5733, "B2": 5752, "B3": 5771, "B4": 5790, "B5": 5809, "B6": 5828, "B7": 5847, "B8": 5866,
    "E1": 5705, "E2": 5685, "E3": 5665, "E4": 5645, "E5": 5885, "E6": 5905, "E7": 5925, "E8": 5945,
    "L1": 1080, "L2": 1120, "L3": 1160, "L4": 1200, "L5": 1240, "L6": 1280, "L7": 1320, "L8": 1360,
}

vtx_latest_jpeg: Optional[bytes] = None
vtx_restart_event = threading.Event()
_vtx_lib = None
_vtx_placeholder_jpeg: Optional[bytes] = None


def _load_vtx_lib():
    """Loads the native fpv_decoder.so and declares its C ABI (once)."""
    global _vtx_lib
    if _vtx_lib is not None:
        return _vtx_lib
    so_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fpv_decoder.so")
    lib = ctypes.CDLL(so_path)
    lib.fpv_decoder_start.argtypes = [ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
    lib.fpv_decoder_start.restype = ctypes.c_int
    lib.fpv_decoder_set_tuning.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_float, ctypes.c_float,
                                           ctypes.c_int, ctypes.c_float, ctypes.c_int, ctypes.c_int]
    lib.fpv_decoder_set_tuning.restype = None
    lib.fpv_decoder_get_frame.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_int, ctypes.c_int,
                                          ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_float)]
    lib.fpv_decoder_get_frame.restype = ctypes.c_int
    lib.fpv_decoder_stop.argtypes = []
    lib.fpv_decoder_stop.restype = None
    _vtx_lib = lib
    return lib


def apply_vtx_tuning():
    """Pushes current sync/centering/picture tuning to the running decoder (live, no restart)."""
    lib = _vtx_lib
    if lib is None or not tactical_state.vtx_running:
        return
    with tactical_state.lock:
        std = 0 if tactical_state.vtx_standard.upper() == "PAL" else 1
        inv = 1 if tactical_state.vtx_invert else 0
        b = float(tactical_state.vtx_brightness)
        c = float(tactical_state.vtx_contrast)
        ah = 1 if tactical_state.vtx_auto_hsync else 0
        ll = float(tactical_state.vtx_line_len)
        vh = int(tactical_state.vtx_v_hold)
        hh = int(tactical_state.vtx_h_hold)
    try:
        lib.fpv_decoder_set_tuning(std, inv, b, c, ah, ll, vh, hh)
    except Exception:
        pass


def _vtx_make_placeholder(text: str) -> bytes:
    """A simple 'no signal' JPEG for when VTX is idle/searching."""
    global _vtx_placeholder_jpeg
    if not _HAS_PIL:
        return b""
    arr = np.zeros((VTX_H, VTX_W), dtype=np.uint8)
    arr[::4, :] = 24  # faint scanlines
    im = Image.fromarray(arr, mode="L")
    bio = io.BytesIO()
    im.save(bio, "JPEG", quality=60)
    return bio.getvalue()


def vtx_video_worker():
    """Background thread: decodes analog FPV video whenever VTX is enabled."""
    global vtx_latest_jpeg
    while True:
        if not tactical_state.vtx_running:
            time.sleep(0.3)
            continue

        if not _HAS_PIL:
            with tactical_state.lock:
                tactical_state.vtx_error = "Pillow (python3-pil) not installed on host"
                tactical_state.vtx_running = False
            log_sitrep("CRIT", "SDR", "VTX decode failed: JPEG encoder (Pillow) missing.", voice=False)
            continue

        try:
            lib = _load_vtx_lib()
        except Exception as e:
            with tactical_state.lock:
                tactical_state.vtx_error = f"decoder load failed: {e}"
                tactical_state.vtx_running = False
            log_sitrep("CRIT", "SDR", f"VTX decoder .so load failed: {e}", voice=False)
            continue

        with tactical_state.lock:
            freq_hz = int(tactical_state.vtx_freq_mhz * 1e6)
            lna = int(tactical_state.vtx_lna)
            vga = int(tactical_state.vtx_vga)
            std_code = 0 if tactical_state.vtx_standard.upper() == "PAL" else 1
            invert = 1 if tactical_state.vtx_invert else 0
            ch = tactical_state.vtx_channel
            fmhz = tactical_state.vtx_freq_mhz

        vtx_restart_event.clear()
        res = lib.fpv_decoder_start(freq_hz, 10000000, lna, vga, 1)
        if res != 0:
            with tactical_state.lock:
                tactical_state.vtx_error = f"HackRF open failed (code {res})"
                tactical_state.vtx_running = False
            log_sitrep("WARN", "SDR", f"VTX: HackRF open failed (code {res}). Is it free?", voice=False)
            time.sleep(1.0)
            continue

        apply_vtx_tuning()  # push sync/centering/picture settings for the new session
        with tactical_state.lock:
            tactical_state.vtx_error = ""
        log_sitrep("INFO", "SDR", f"VTX video decode started: {ch} ({fmhz:.0f} MHz, {['PAL','NTSC'][std_code]})", voice=True)

        buf = (ctypes.c_uint8 * (VTX_W * VTX_H))()
        buf_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))
        sync = ctypes.c_int(0)
        fps = ctypes.c_float(0.0)

        while tactical_state.vtx_running and not vtx_restart_event.is_set():
            got = lib.fpv_decoder_get_frame(buf_ptr, VTX_W, VTX_H, ctypes.byref(sync), ctypes.byref(fps))
            if got:
                try:
                    arr = np.frombuffer(buf, dtype=np.uint8).reshape(VTX_H, VTX_W)
                    im = Image.fromarray(arr, mode="L")
                    bio = io.BytesIO()
                    im.save(bio, "JPEG", quality=72)
                    vtx_latest_jpeg = bio.getvalue()
                except Exception:
                    pass
                with tactical_state.lock:
                    tactical_state.vtx_sync_locked = bool(sync.value)
                    tactical_state.vtx_fps = round(float(fps.value), 1)
            time.sleep(0.033)  # ~30 fps cap

        lib.fpv_decoder_stop()
        with tactical_state.lock:
            tactical_state.vtx_sync_locked = False
            tactical_state.vtx_fps = 0.0
        time.sleep(0.3)  # let HackRF settle before any re-open / retune


# -----------------------------------------------------------------------------
# WebSocket Telemetry Broadcaster
# -----------------------------------------------------------------------------
async def broadcast_message(msg: dict):
    """Sends JSON frame to all active mobile clients with strict timeout and zero deadlocks."""
    if not connected_clients:
        return
    text = json.dumps(msg)
    clients = list(connected_clients)
    for client in clients:
        try:
            await asyncio.wait_for(client.send_text(text), timeout=0.1)
        except Exception:
            connected_clients.discard(client)


async def telemetry_broadcast_loop():
    """Pushes 20Hz telemetry update frames and flushes pending SITREPs over WebSocket."""
    while True:
        await asyncio.sleep(0.05)
        
        # Flush any pending sitrep logs first
        while not pending_sitrep_queue.empty():
            try:
                entry = pending_sitrep_queue.get_nowait()
                await broadcast_message({"type": "sitrep", "entry": entry})
            except queue.Empty:
                break
                
        if not connected_clients:
            continue
            
        with tactical_state.lock:
            rc_age = time.time() - tactical_state.last_rc_timestamp
            
            pilot_list = list(tactical_state.pilots.values())
            pilot_list.sort(key=lambda p: p.get("last_seen", 0), reverse=True)
            
            frame = {
                "type": "telemetry",
                "timestamp": time.time(),
                "node": {
                    "connected": tactical_state.serial_connected,
                    "port": tactical_state.active_port,
                    "available_ports": list(tactical_state.available_ports),
                    "rate_name": tactical_state.active_rate_name,
                    "sf": tactical_state.active_sf,
                    "bw_khz": tactical_state.active_bw_khz,
                    "interval_us": tactical_state.active_interval_us,
                    "target_pilot": tactical_state.active_target_pilot,
                    "scan_mode": tactical_state.scan_mode,
                    "scan_rate": tactical_state.scan_rate,
                    "scan_band": tactical_state.scan_band,
                    "rx_count": tactical_state.serial_rx_count,
                    "rc_age_sec": round(rc_age, 2),
                    "last_serial_line": tactical_state.last_serial_line
                },
                "rc": {
                    "channels": list(tactical_state.channels),
                    "armed": tactical_state.is_armed,
                    "rssi": tactical_state.sniffer_rssi,
                    "snr": tactical_state.sniffer_snr,
                    "rate": tactical_state.packet_rate,
                    "thr_pct": round(max(0.0, min(100.0, (tactical_state.channels[2] - 988) / 10.24)), 1)
                },
                "maneuver": {
                    "name": tactical_state.maneuver_name,
                    "detail": tactical_state.maneuver_detail
                },
                "link": {
                    "lq": tactical_state.drone_lq,
                    "rssi1": tactical_state.drone_rssi1,
                    "rssi2": tactical_state.drone_rssi2,
                    "snr": tactical_state.drone_snr,
                    "power_mw": tactical_state.drone_tx_power
                },
                "telemetry": {
                    "voltage": tactical_state.battery_volt,
                    "current": tactical_state.battery_curr,
                    "mah": tactical_state.battery_mah,
                    "bat_pct": tactical_state.battery_pct,
                    "mode": tactical_state.flight_mode,
                    "pitch": tactical_state.pitch,
                    "roll": tactical_state.roll,
                    "yaw": tactical_state.yaw,
                    "has_gps": tactical_state.has_gps,
                    "gps_lat": tactical_state.gps_lat,
                    "gps_lon": tactical_state.gps_lon,
                    "gps_alt": tactical_state.gps_alt,
                    "gps_spd": tactical_state.gps_spd,
                    "gps_sats": tactical_state.gps_sats
                },
                "pilots": pilot_list[:12],
                "system": {
                    "cpu_temp": tactical_state.sys_cpu_temp,
                    "load": tactical_state.sys_load,
                    "ram_pct": tactical_state.sys_ram_pct,
                    "wifi_clients": tactical_state.sys_wifi_clients
                },
                "sdr": {
                    "available": tactical_state.hackrf_available,
                    "running": tactical_state.hackrf_running,
                    "devices": list(tactical_state.hackrf_devices),
                    "selected_serial": tactical_state.hackrf_selected_serial,
                    "active_serial": tactical_state.hackrf_active_serial,
                    "board": tactical_state.hackrf_board_name,
                    "firmware": tactical_state.hackrf_firmware,
                    "revision": tactical_state.hackrf_revision,
                    "freq_mhz": tactical_state.hackrf_freq_mhz,
                    "lna": tactical_state.hackrf_lna,
                    "vga": tactical_state.hackrf_vga,
                    "fft": tactical_state.latest_fft if tactical_state.hackrf_running else []
                },
                "kraken": {
                    "enabled": tactical_state.kraken_enabled,
                    "mode": tactical_state.kraken_mode,
                    "host": tactical_state.kraken_host,
                    "doa_port": tactical_state.kraken_doa_port,
                    "online": tactical_state.kraken_online,
                    "health": tactical_state.kraken_health,
                    "tuners": tactical_state.kraken_tuners,
                    "freq_mhz": tactical_state.kraken_freq_mhz,
                    "gain_db": tactical_state.kraken_gain_db,
                    "array": tactical_state.kraken_array,
                    "spacing_m": tactical_state.kraken_spacing_m,
                    "bearing_deg": tactical_state.kraken_bearing_deg,
                    "confidence": tactical_state.kraken_confidence,
                    "snr_db": tactical_state.kraken_snr_db,
                    "locked": tactical_state.kraken_locked,
                    "age_sec": round(time.time() - tactical_state.kraken_last_update, 2) if tactical_state.kraken_last_update else 999.0,
                    "spectrum": tactical_state.kraken_spectrum if tactical_state.kraken_enabled else []
                },
                "vtx": {
                    "running": tactical_state.vtx_running,
                    "available": tactical_state.hackrf_available,
                    "channel": tactical_state.vtx_channel,
                    "freq_mhz": tactical_state.vtx_freq_mhz,
                    "standard": tactical_state.vtx_standard,
                    "lna": tactical_state.vtx_lna,
                    "vga": tactical_state.vtx_vga,
                    "invert": tactical_state.vtx_invert,
                    "auto_hsync": tactical_state.vtx_auto_hsync,
                    "line_len": tactical_state.vtx_line_len,
                    "v_hold": tactical_state.vtx_v_hold,
                    "h_hold": tactical_state.vtx_h_hold,
                    "brightness": tactical_state.vtx_brightness,
                    "contrast": tactical_state.vtx_contrast,
                    "sync_locked": tactical_state.vtx_sync_locked,
                    "fps": tactical_state.vtx_fps,
                    "error": tactical_state.vtx_error
                }
            }

        await broadcast_message(frame)


# -----------------------------------------------------------------------------
# FastAPI Routes & WebSockets
# -----------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
        
    with tactical_state.lock:
        init_frame = {
            "type": "init",
            "sitrep": list(tactical_state.sitrep_log),
            "pilots": list(tactical_state.pilots.values())
        }
    await websocket.send_text(json.dumps(init_frame))
    
    try:
        while True:
            data_text = await websocket.receive_text()
            try:
                msg = json.loads(data_text)
            except Exception:
                continue
                
            action = msg.get("action", "")
            
            if action == "set_rate":
                rate_val = msg.get("rate", "AUTO").upper().strip()
                cmd = f"SET_RATE:{rate_val}"
                serial_cmd_queue.put(cmd)
                with tactical_state.lock:
                    tactical_state.active_rate_name = rate_val
                log_sitrep("INFO", "LORA", f"Packet rate switch requested: {rate_val}", voice=True)
                
            elif action == "start_scan":
                rate_val = msg.get("rate", "50HZ").upper().strip()
                band_val = msg.get("band", "AUTO").upper().strip()
                with tactical_state.lock:
                    tactical_state.scan_mode = True
                    tactical_state.scan_rate = rate_val
                    tactical_state.scan_band = band_val
                    tactical_state.active_target_pilot = "SCANNING"
                    tactical_state.active_rate_name = rate_val
                serial_cmd_queue.put("SCAN:START")
                if rate_val:
                    serial_cmd_queue.put(f"SET_RATE:{rate_val}")
                if band_val:
                    serial_cmd_queue.put(f"SET_BAND:{band_val}")
                log_sitrep("INFO", "PILOT", f"Airspace Scan initiated [Rate: {rate_val} | Band: {band_val}]", voice=True)

            elif action == "stop_scan":
                with tactical_state.lock:
                    tactical_state.scan_mode = False
                    if tactical_state.active_target_pilot == "SCANNING":
                        tactical_state.active_target_pilot = "AUTO"
                serial_cmd_queue.put("SCAN:STOP")
                log_sitrep("INFO", "PILOT", "Airspace Scan stopped", voice=False)

            elif action == "set_band":
                band_val = msg.get("band", "AUTO").upper().strip()
                with tactical_state.lock:
                    tactical_state.scan_band = band_val
                serial_cmd_queue.put(f"SET_BAND:{band_val}")
                log_sitrep("INFO", "LORA", f"Listening band switch: {band_val}", voice=False)

            elif action == "clear_pilots":
                with tactical_state.lock:
                    tactical_state.pilots.clear()
                log_sitrep("INFO", "PILOT", "Airspace pilot inventory cleared", voice=False)

            elif action == "lock_pilot":
                target = msg.get("target", "AUTO").strip()
                with tactical_state.lock:
                    if tactical_state.scan_mode:
                        tactical_state.scan_mode = False
                        serial_cmd_queue.put("SCAN:STOP")

                    if target == "AUTO" or target == tactical_state.active_target_pilot:
                        tactical_state.active_target_pilot = "AUTO"
                        serial_cmd_queue.put("LOCK_PILOT:AUTO")
                        log_sitrep("INFO", "PILOT", "Target lock released -> AUTO mode", voice=True)
                    else:
                        pin_rate = None
                        p = tactical_state.pilots.get(target)
                        if not p and "," in target:
                            try:
                                parts = [int(x) for x in target.split(",")]
                                norm_uid = f"{parts[0]:02X}:{parts[1]:02X}:{parts[2]:02X}"
                                p = tactical_state.pilots.get(norm_uid)
                                target = norm_uid
                            except Exception:
                                pass
                        if p and "u3" in p and "u4" in p and "u5" in p:
                            target_uid = p.get("uid", target)
                            tactical_state.active_target_pilot = target_uid
                            cmd = f"LOCK_PILOT:{p['u3']},{p['u4']},{p['u5']}"
                            pr = str(p.get("rate", "")).upper().strip()
                            if pr and pr not in ("UNKNOWN", "AUTO"):
                                pin_rate = pr
                        elif ":" in target:
                            parts = [int(x, 16) for x in target.split(":")]
                            tactical_state.active_target_pilot = target
                            cmd = f"LOCK_PILOT:{','.join(map(str, parts))}"
                        elif "," in target:
                            parts = [int(x) for x in target.split(",")]
                            tactical_state.active_target_pilot = f"{parts[0]:02X}:{parts[1]:02X}:{parts[2]:02X}"
                            cmd = f"LOCK_PILOT:{','.join(map(str, parts))}"
                        else:
                            tactical_state.active_target_pilot = target
                            cmd = f"LOCK_PILOT:{target}"
                        # Pin the demodulator to the pilot's discovered rate FIRST so it locks
                        # cleanly, instead of staying in auto-rate scan (which rotates onto wrong
                        # rates seeing only noise and collapses the RC feed to ~1-2 pkt/s).
                        if pin_rate:
                            tactical_state.active_rate_name = pin_rate
                            serial_cmd_queue.put(f"SET_RATE:{pin_rate}")
                        serial_cmd_queue.put(cmd)
                        log_sitrep("CRIT", "PILOT", f"TARGET LOCKED: UID {tactical_state.active_target_pilot} @ {pin_rate or 'current rate'}", voice=True)
                
            elif action == "serial_tx":
                cmd = msg.get("cmd", "").strip()
                if cmd:
                    serial_cmd_queue.put(cmd)
                    
            elif action == "set_port":
                chosen = msg.get("port", "").strip()
                if chosen:
                    with tactical_state.lock:
                        tactical_state.user_selected_port = chosen
                    log_sitrep("INFO", "LORA", f"Switching serial port to: {chosen}", voice=False)

            elif action == "set_sdr_device":
                dev_serial = msg.get("serial", "").strip().lower()
                if dev_serial:
                    with tactical_state.lock:
                        tactical_state.hackrf_selected_serial = dev_serial
                        cur = next((d for d in tactical_state.hackrf_devices if d["serial"] == dev_serial), None)
                        if cur:
                            tactical_state.hackrf_board_name = cur["board"]
                            tactical_state.hackrf_firmware = cur["firmware"]
                            tactical_state.hackrf_revision = cur["revision"]
                    short_s = dev_serial[-8:] if len(dev_serial) >= 8 else dev_serial
                    log_sitrep("INFO", "SDR", f"HackRF target device set to: ...{short_s}", voice=False)

            elif action == "rescan_sdr":
                devs = get_candidate_hackrf_devices()
                with tactical_state.lock:
                    tactical_state.hackrf_devices = devs
                    tactical_state.hackrf_available = (len(devs) > 0)
                log_sitrep("INFO", "SDR", f"HackRF scan complete. Found {len(devs)} device(s).", voice=False)

            elif action == "start_sdr":
                freq = float(msg.get("freq_mhz", 915.0))
                lna = int(msg.get("lna", 32))
                vga = int(msg.get("vga", 30))
                with tactical_state.lock:
                    tactical_state.hackrf_freq_mhz = freq
                    tactical_state.hackrf_lna = lna
                    tactical_state.hackrf_vga = vga
                    tactical_state.hackrf_running = True
                log_sitrep("INFO", "SDR", f"HackRF spectrum analysis started at {freq} MHz", voice=False)
                
            elif action == "stop_sdr":
                with tactical_state.lock:
                    tactical_state.hackrf_running = False
                hackrf_stop_event.set()
                log_sitrep("INFO", "SDR", "HackRF spectrum analysis stopped.", voice=False)

            elif action == "start_doa":
                mode = str(msg.get("mode", "KRAKEN_POLL")).upper().strip()
                if mode not in ("KRAKEN_POLL", "SIMULATOR"):
                    mode = "KRAKEN_POLL"
                host = str(msg.get("host", "127.0.0.1")).strip() or "127.0.0.1"
                doa_port = int(msg.get("doa_port", 8081))
                freq = float(msg.get("freq_mhz", 915.0))
                gain = float(msg.get("gain_db", 30.0))
                array = "UCA" if str(msg.get("array", "UCA")).upper() == "UCA" else "ULA"
                spacing = float(msg.get("spacing_m", 0.135))
                with tactical_state.lock:
                    tactical_state.kraken_mode = mode
                    tactical_state.kraken_host = host
                    tactical_state.kraken_doa_port = doa_port
                    tactical_state.kraken_freq_mhz = freq
                    tactical_state.kraken_gain_db = gain
                    tactical_state.kraken_array = array
                    tactical_state.kraken_spacing_m = spacing
                    tactical_state.kraken_enabled = True
                    tactical_state.kraken_locked = False
                # If DAQ is local, push the requested tuning to its settings.json
                if mode == "KRAKEN_POLL" and host in ("127.0.0.1", "localhost", "::1"):
                    push_kraken_settings(freq, gain, array, spacing)
                tag = "SIMULATOR" if mode == "SIMULATOR" else f"{host}:{doa_port}"
                log_sitrep("INFO", "SDR", f"KrakenSDR Direction Finding started ({tag}) @ {freq:.3f} MHz", voice=True)

            elif action == "stop_doa":
                with tactical_state.lock:
                    tactical_state.kraken_enabled = False
                    tactical_state.kraken_locked = False
                log_sitrep("INFO", "SDR", "KrakenSDR Direction Finding stopped.", voice=False)

            elif action == "set_doa_config":
                with tactical_state.lock:
                    if "freq_mhz" in msg:
                        tactical_state.kraken_freq_mhz = float(msg["freq_mhz"])
                    if "gain_db" in msg:
                        tactical_state.kraken_gain_db = float(msg["gain_db"])
                    if "array" in msg:
                        tactical_state.kraken_array = "UCA" if str(msg["array"]).upper() == "UCA" else "ULA"
                    if "spacing_m" in msg:
                        tactical_state.kraken_spacing_m = float(msg["spacing_m"])
                    if "host" in msg and str(msg["host"]).strip():
                        tactical_state.kraken_host = str(msg["host"]).strip()
                    if "doa_port" in msg:
                        tactical_state.kraken_doa_port = int(msg["doa_port"])
                    f = tactical_state.kraken_freq_mhz
                    g = tactical_state.kraken_gain_db
                    a = tactical_state.kraken_array
                    s = tactical_state.kraken_spacing_m
                    h = tactical_state.kraken_host
                pushed = False
                if h in ("127.0.0.1", "localhost", "::1"):
                    pushed = push_kraken_settings(f, g, a, s)
                log_sitrep("INFO", "SDR",
                           f"Kraken retune: {f:.3f} MHz | Gain {g:.0f}dB | {a} ({s:.3f}m)"
                           + ("" if pushed else " [state only - DAQ not local]"),
                           voice=False)

            elif action == "start_vtx":
                ch = str(msg.get("channel", "")).upper().strip()
                if ch in VTX_CHANNELS:
                    freq = float(VTX_CHANNELS[ch])
                else:
                    freq = float(msg.get("freq_mhz", 5800.0))
                    ch = msg.get("channel", f"{freq:.0f}")
                std = "NTSC" if str(msg.get("standard", "PAL")).upper() == "NTSC" else "PAL"
                with tactical_state.lock:
                    # VTX video needs exclusive HackRF access -> stop the spectrum stare
                    tactical_state.hackrf_running = False
                    tactical_state.vtx_channel = ch
                    tactical_state.vtx_freq_mhz = freq
                    tactical_state.vtx_standard = std
                    tactical_state.vtx_lna = int(msg.get("lna", 32))
                    tactical_state.vtx_vga = int(msg.get("vga", 30))
                    tactical_state.vtx_invert = bool(msg.get("invert", False))
                    if "auto_hsync" in msg: tactical_state.vtx_auto_hsync = bool(msg["auto_hsync"])
                    if "line_len" in msg: tactical_state.vtx_line_len = max(600.0, min(700.0, float(msg["line_len"])))
                    if "v_hold" in msg: tactical_state.vtx_v_hold = max(0, min(625, int(msg["v_hold"])))
                    if "h_hold" in msg: tactical_state.vtx_h_hold = max(-320, min(320, int(msg["h_hold"])))
                    if "brightness" in msg: tactical_state.vtx_brightness = max(-50.0, min(50.0, float(msg["brightness"])))
                    if "contrast" in msg: tactical_state.vtx_contrast = max(0.5, min(3.0, float(msg["contrast"])))
                    tactical_state.vtx_error = ""
                    tactical_state.vtx_running = True
                hackrf_stop_event.set()  # release any running hackrf_transfer
                vtx_restart_event.set()  # ensure worker (re)starts on new params
                log_sitrep("INFO", "SDR", f"VTX video requested: {ch} ({freq:.0f} MHz, {std})", voice=False)

            elif action == "stop_vtx":
                with tactical_state.lock:
                    tactical_state.vtx_running = False
                vtx_restart_event.set()
                log_sitrep("INFO", "SDR", "VTX video decode stopped.", voice=False)

            elif action == "set_vtx_tuning":
                with tactical_state.lock:
                    old_freq = tactical_state.vtx_freq_mhz
                    # Channel / frequency (needs a HackRF re-tune -> decoder restart)
                    if "channel" in msg and str(msg["channel"]).upper() in VTX_CHANNELS:
                        tactical_state.vtx_channel = str(msg["channel"]).upper()
                        tactical_state.vtx_freq_mhz = float(VTX_CHANNELS[tactical_state.vtx_channel])
                    elif "freq_mhz" in msg:
                        tactical_state.vtx_freq_mhz = float(msg["freq_mhz"])
                        tactical_state.vtx_channel = str(msg.get("channel", f"{tactical_state.vtx_freq_mhz:.0f}"))
                    if "lna" in msg:
                        tactical_state.vtx_lna = int(msg["lna"])
                    if "vga" in msg:
                        tactical_state.vtx_vga = int(msg["vga"])
                    # Live sync / centering / picture params (no restart needed)
                    if "standard" in msg:
                        tactical_state.vtx_standard = "NTSC" if str(msg["standard"]).upper() == "NTSC" else "PAL"
                    if "invert" in msg:
                        tactical_state.vtx_invert = bool(msg["invert"])
                    if "auto_hsync" in msg:
                        tactical_state.vtx_auto_hsync = bool(msg["auto_hsync"])
                    if "line_len" in msg:
                        tactical_state.vtx_line_len = max(600.0, min(700.0, float(msg["line_len"])))
                    if "v_hold" in msg:
                        tactical_state.vtx_v_hold = max(0, min(625, int(msg["v_hold"])))
                    if "h_hold" in msg:
                        tactical_state.vtx_h_hold = max(-320, min(320, int(msg["h_hold"])))
                    if "brightness" in msg:
                        tactical_state.vtx_brightness = max(-50.0, min(50.0, float(msg["brightness"])))
                    if "contrast" in msg:
                        tactical_state.vtx_contrast = max(0.5, min(3.0, float(msg["contrast"])))
                    running = tactical_state.vtx_running
                    ch = tactical_state.vtx_channel
                    fmhz = tactical_state.vtx_freq_mhz
                    needs_restart = ("lna" in msg or "vga" in msg or abs(fmhz - old_freq) > 1e-6)
                if running and needs_restart:
                    vtx_restart_event.set()   # re-open HackRF on the new frequency / gain
                    log_sitrep("INFO", "SDR", f"VTX retune: {ch} ({fmhz:.0f} MHz)", voice=False)
                elif running:
                    apply_vtx_tuning()        # live sync/centering/picture update, no restart

            elif action == "clear_sitrep":
                with tactical_state.lock:
                    tactical_state.sitrep_log.clear()
                    
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] Client error: {e}")
    finally:
        connected_clients.discard(websocket)


static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)
    
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def root():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>CEMA Tactical Suite</h1><p>Static files loading...</p>")

@app.get("/manifest.json")
async def manifest():
    m_file = os.path.join(static_dir, "manifest.json")
    if os.path.exists(m_file):
        return FileResponse(m_file, media_type="application/manifest+json")
    return HTMLResponse("{}", media_type="application/json")

@app.get("/sw.js")
async def service_worker():
    sw_file = os.path.join(static_dir, "sw.js")
    if os.path.exists(sw_file):
        return FileResponse(sw_file, media_type="application/javascript")
    return HTMLResponse("", media_type="application/javascript")


@app.get("/vtx/stream.mjpeg")
async def vtx_mjpeg_stream():
    """MJPEG multipart stream of the decoded analog FPV video for an <img> tag."""
    boundary = "cemavtxframe"

    async def gen():
        global _vtx_placeholder_jpeg
        if _vtx_placeholder_jpeg is None:
            _vtx_placeholder_jpeg = _vtx_make_placeholder("NO SIGNAL")
        last_id = None
        idle_ticks = 0
        while True:
            frame = vtx_latest_jpeg if tactical_state.vtx_running else None
            if frame is None:
                frame = _vtx_placeholder_jpeg
                idle_ticks += 1
                # If VTX is off and the client keeps the socket, keep it alive at low rate
                if not tactical_state.vtx_running and idle_ticks > 6000:
                    break
            else:
                idle_ticks = 0
            if frame:
                yield (b"--" + boundary.encode() + b"\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                       + frame + b"\r\n")
            await asyncio.sleep(0.04)  # ~25 fps

    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={boundary}")


main_loop: Optional[asyncio.AbstractEventLoop] = None

@app.on_event("startup")
async def startup_event():
    global main_loop
    main_loop = asyncio.get_running_loop()
    
    t_serial = threading.Thread(target=serial_reader_thread, daemon=True)
    t_serial.start()
    
    t_sys = threading.Thread(target=system_monitor_thread, daemon=True)
    t_sys.start()
    
    t_sdr = threading.Thread(target=hackrf_dsp_worker, daemon=True)
    t_sdr.start()

    t_kraken = threading.Thread(target=kraken_doa_worker, daemon=True)
    t_kraken.start()

    t_vtx = threading.Thread(target=vtx_video_worker, daemon=True)
    t_vtx.start()

    asyncio.create_task(telemetry_broadcast_loop())
    print("[BACKEND] CEMA Tactical Suite Engine Online.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
