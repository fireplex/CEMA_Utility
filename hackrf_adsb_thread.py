"""
Multi-Source ADS-B Receiver Thread: HackRF 1090MHz SDR (C DLL Slicer) + Live OpenSky + Beast/AVR TCP
AeroTrack CEMA Tactical Airspace Intelligence Suite
"""

import os
import sys
import time
import math
import json
import ctypes
import socket
import urllib.request
import subprocess
import threading
from PyQt6.QtCore import QThread, pyqtSignal
from adsb_decoder import ADSBDecoder

CALLBACK_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int)

class HackRFADSBThread(QThread):
    aircraft_updated_signal = pyqtSignal(dict)
    stats_updated_signal = pyqtSignal(dict)

    def __init__(self, ref_lat=51.5074, ref_lon=-0.1278, mode="auto", sample_rate=2000000, lna_gain=40, vga_gain=32, tcp_host="127.0.0.1", tcp_port=30002):
        super().__init__()
        self.ref_lat = ref_lat
        self.ref_lon = ref_lon
        self.mode = mode # "live_opensky", "hackrf_sdr", "tcp_beast", "simulation", "auto"
        self.sample_rate = sample_rate
        self.lna_gain = lna_gain
        self.vga_gain = vga_gain
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.running = True
        self.decoder = ADSBDecoder(ref_lat=ref_lat, ref_lon=ref_lon)
        self.process = None

        # Load native C Mode-S pulse slicer DLL
        self.slicer_dll = None
        try:
            dll_path = os.path.join(os.path.dirname(__file__), "adsb_slicer.dll")
            if os.path.exists(dll_path):
                self.slicer_dll = ctypes.CDLL(dll_path)
        except Exception:
            self.slicer_dll = None

        self._c_callback = CALLBACK_TYPE(self._on_c_frame_sliced)

    def _on_c_frame_sliced(self, hex_frame_bytes, rssi_val):
        try:
            hex_str = hex_frame_bytes.decode('ascii', errors='ignore')
            ac = self.decoder.decode_hex_frame(hex_str, rssi=-40.0)
            if ac:
                self.aircraft_updated_signal.emit(ac.to_dict())
        except Exception:
            pass

    def run(self):
        if self.mode == "live_opensky":
            self._run_opensky_loop()
        elif self.mode == "tcp_beast":
            self._run_tcp_loop()
        elif self.mode == "hackrf_sdr":
            self._run_hardware_loop()
        elif self.mode == "simulation":
            self._run_simulation_loop()
        else: # "auto"
            try:
                self._run_opensky_loop()
            except Exception:
                self._run_simulation_loop()

    def _run_opensky_loop(self):
        """Streams real-time live commercial and military aircraft via OpenSky API."""
        while self.running:
            try:
                lat = self.ref_lat
                lon = self.ref_lon
                delta = 1.2
                url = f"https://opensky-network.org/api/states/all?lamin={lat-delta}&lomin={lon-delta}&lamax={lat+delta}&lomax={lon+delta}"
                
                req = urllib.request.Request(url, headers={"User-Agent": "AeroTrack-CEMA/1.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())
                    states = data.get("states", []) or []

                    now = time.time()
                    for s in states:
                        if not s[0] or s[6] is None or s[5] is None:
                            continue
                        icao = s[0].upper()
                        cs = s[1].strip() if s[1] else "UNKNOWN"
                        country = s[2] if len(s) > 2 and s[2] else "Civil"
                        lat_val = round(s[6], 5)
                        lon_val = round(s[5], 5)
                        alt_ft = int((s[7] or 0) * 3.28084)
                        spd_kts = int((s[9] or 0) * 1.94384)
                        trk_deg = round(s[10] or 0.0, 1)
                        vr_fpm = int((s[11] or 0) * 196.85)
                        squawk = s[14] if len(s) > 14 and s[14] else "----"

                        ac = self.decoder.get_or_create_aircraft(icao)
                        ac.callsign = cs
                        ac.country = country
                        ac.lat = lat_val
                        ac.lon = lon_val
                        ac.altitude_ft = alt_ft
                        ac.speed_kts = spd_kts
                        ac.track_deg = trk_deg
                        ac.vert_rate_fpm = vr_fpm
                        ac.squawk = squawk
                        ac.last_seen = now
                        ac.msg_count += 1

                        if squawk in ["7700", "7600", "7500"]:
                            ac.is_emergency = True
                            ac.emergency_type = f"EMERGENCY (SQUAWK {squawk})"

                        ac.track_history.append((lat_val, lon_val, alt_ft, now))
                        if len(ac.track_history) > 60:
                            ac.track_history.pop(0)

                        self.aircraft_updated_signal.emit(ac.to_dict())

                    self.stats_updated_signal.emit({
                        "total_ac": len(self.decoder.aircraft_db),
                        "msg_rate": len(states),
                        "hardware": "LIVE OPENSKY FEED (100% REAL-TIME AIRCRAFT)"
                    })

            except Exception:
                time.sleep(2.0)

            for _ in range(50):
                if not self.running:
                    break
                time.sleep(0.1)

    def _run_tcp_loop(self):
        """Connects to local dump1090 / readsb raw AVR stream on TCP port 30002."""
        while self.running:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5.0)
                s.connect((self.tcp_host, self.tcp_port))
                self.stats_updated_signal.emit({
                    "total_ac": len(self.decoder.aircraft_db),
                    "msg_rate": 0,
                    "hardware": f"TCP MODE-S FEED ({self.tcp_host}:{self.tcp_port})"
                })
                
                buf = ""
                while self.running:
                    data = s.recv(4096).decode('ascii', errors='ignore')
                    if not data:
                        break
                    buf += data
                    lines = buf.split("\n")
                    buf = lines.pop()
                    for line in lines:
                        line = line.strip()
                        if line.startswith("*") and line.endswith(";"):
                            hex_frame = line[1:-1]
                            ac = self.decoder.decode_hex_frame(hex_frame)
                            if ac:
                                self.aircraft_updated_signal.emit(ac.to_dict())
            except Exception:
                time.sleep(2.0)

    def _run_hardware_loop(self):
        """Runs hackrf_transfer stream at 1090 MHz and feeds adsb_slicer.dll."""
        # Ensure any stale hackrf_transfer process is killed
        try:
            subprocess.run(["taskkill", "/F", "/IM", "hackrf_transfer.exe"], capture_ok=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception:
            pass

        cmd = [
            "hackrf_transfer",
            "-r", "-",
            "-f", "1090000000",
            "-s", str(self.sample_rate),
            "-l", str(self.lna_gain),
            "-g", str(self.vga_gain),
            "-a", "1"
        ]
        try:
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=65536)
            self.stats_updated_signal.emit({
                "total_ac": 0,
                "msg_rate": 0,
                "hardware": "HACKRF ONE 1090 MHz SDR [LISTENING ON RF]"
            })
            
            buf = bytearray()
            chunk_size = 32768
            total_msgs = 0
            t_rate = time.time()

            while self.running and self.process.poll() is None:
                chunk = self.process.stdout.read(16384)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) >= chunk_size:
                    raw_iq = bytes(buf[:chunk_size])
                    del buf[:chunk_size]

                    num_samples = len(raw_iq) // 2
                    if self.slicer_dll:
                        n_found = self.slicer_dll.process_iq_samples(raw_iq, num_samples, self._c_callback)
                        total_msgs += n_found

                if time.time() - t_rate >= 1.0:
                    self.stats_updated_signal.emit({
                        "total_ac": len(self.decoder.aircraft_db),
                        "msg_rate": total_msgs,
                        "hardware": f"HACKRF ONE 1090 MHz SDR [{total_msgs} msgs/s]"
                    })
                    total_msgs = 0
                    t_rate = time.time()

        except Exception as e:
            self.stats_updated_signal.emit({
                "total_ac": 0,
                "msg_rate": 0,
                "hardware": f"HACKRF SDR ERROR: {e}"
            })
            time.sleep(2.0)

    def _run_simulation_loop(self):
        """Generates realistic synthetic airspace traffic."""
        fleet = [
            {"icao": "40621D", "cs": "EZY8544", "lat": self.ref_lat + 0.18, "lon": self.ref_lon - 0.22, "alt": 28000, "spd": 420, "trk": 135.0, "vr": -800, "sq": "1422", "em": False},
            {"icao": "4840D6", "cs": "KLM1023", "lat": self.ref_lat - 0.25, "lon": self.ref_lon + 0.35, "alt": 34000, "spd": 465, "trk": 270.0, "vr": 0, "sq": "3215", "em": False},
            {"icao": "43C420", "cs": "RFR410",  "lat": self.ref_lat + 0.32, "lon": self.ref_lon + 0.15, "alt": 18500, "spd": 380, "trk": 210.0, "vr": -1200, "sq": "7000", "em": False},
            {"icao": "3C6541", "cs": "DLH942",  "lat": self.ref_lat - 0.40, "lon": self.ref_lon - 0.30, "alt": 39000, "spd": 490, "trk": 080.0, "vr": 0, "sq": "2201", "em": False},
            {"icao": "A8921B", "cs": "AAL109",  "lat": self.ref_lat + 0.45, "lon": self.ref_lon - 0.40, "alt": 12000, "spd": 290, "trk": 105.0, "vr": -1500, "sq": "0421", "em": False},
            {"icao": "407101", "cs": "BAW142",  "lat": self.ref_lat - 0.12, "lon": self.ref_lon - 0.08, "alt": 4200,  "spd": 210, "trk": 275.0, "vr": -600, "sq": "7700", "em": True}
        ]

        for f in fleet:
            ac = self.decoder.get_or_create_aircraft(f["icao"])
            ac.callsign = f["cs"]
            ac.lat = f["lat"]
            ac.lon = f["lon"]
            ac.altitude_ft = f["alt"]
            ac.speed_kts = f["spd"]
            ac.track_deg = f["trk"]
            ac.vert_rate_fpm = f["vr"]
            ac.squawk = f["sq"]
            ac.is_emergency = f["em"]

        fps_timer = time.time()
        while self.running:
            now = time.time()
            dt = 1.0

            for f in fleet:
                ac = self.decoder.aircraft_db.get(f["icao"])
                if not ac: continue

                dist_nm = (ac.speed_kts / 3600.0) * dt
                dist_deg = dist_nm / 60.0
                rad = math.radians(ac.track_deg)
                ac.lat += dist_deg * math.cos(rad)
                ac.lon += dist_deg * math.sin(rad) / math.cos(math.radians(ac.lat))
                ac.altitude_ft += int((ac.vert_rate_fpm / 60.0) * dt)
                if ac.altitude_ft < 1500:
                    ac.altitude_ft = 1500
                    ac.vert_rate_fpm = 0

                ac.last_seen = now
                ac.msg_count += 4
                ac.rssi = -50.0

                ac.track_history.append((ac.lat, ac.lon, ac.altitude_ft, now))
                if len(ac.track_history) > 60:
                    ac.track_history.pop(0)

                self.aircraft_updated_signal.emit(ac.to_dict())

            self.stats_updated_signal.emit({
                "total_ac": len(self.decoder.aircraft_db),
                "msg_rate": len(fleet) * 4,
                "hardware": "AEROTRACK SIMULATOR (1090 MHz)"
            })
            time.sleep(1.0)

    def stop(self):
        self.running = False
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
        self.wait(1000)
