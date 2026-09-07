"""
LoRa Node Bridge & Multi-Station Telemetry Engine
Supports: LilyGO T3-S3 (SX1276/SX1262), Heltec WiFi LoRa 32 V3, and Dual-Node Arrays
AeroTrack & CEMA Tactical Intelligence Suite
"""

import time
import re
import math
import queue
import serial
import serial.tools.list_ports
from PyQt6.QtCore import QObject, QThread, pyqtSignal

def get_available_com_ports():
    try:
        ports = serial.tools.list_ports.comports()
        result = []
        for p in ports:
            result.append(p.device)
        if "COM6" not in result:
            result.insert(0, "COM6")
        return result
    except Exception:
        return ["COM6", "COM7"]

class HeltecLoraThread(QThread):
    """
    Thread-safe asynchronous serial thread for a single LoRa node (LilyGO T3-S3 or Heltec V3).
    All port writes are queued and executed strictly within the worker thread context.
    """
    rc_data_received = pyqtSignal(dict)
    telemetry_link_received = pyqtSignal(dict)
    battery_received = pyqtSignal(dict)
    gps_received = pyqtSignal(dict)
    attitude_received = pyqtSignal(dict)
    flight_mode_received = pyqtSignal(dict)
    sync_discovered = pyqtSignal(dict)
    rate_detected = pyqtSignal(dict)
    pilot_discovered = pyqtSignal(dict)
    status_changed = pyqtSignal(str, bool)

    def __init__(self, port="COM6", baud=921600, node_id="Node_1", node_name="LoRa Sniffer", parent=None):
        super().__init__(parent)
        self.port = port
        self.baud = baud
        self.node_id = node_id
        self.node_name = node_name
        self.running = False
        self.ser = None
        self.cmd_queue = queue.Queue()
        self.persistent_channels = [1500] * 16
        self.persistent_channels[2] = 988
        self.persistent_channels[4] = 1000

    def send_command(self, cmd_str):
        """Thread-safe command enqueue from GUI or background workers."""
        try:
            if cmd_str:
                self.cmd_queue.put(str(cmd_str).strip())
                return True
        except Exception as e:
            print(f"[{self.node_name}] Command queue error: {e}")
        return False

    def set_port(self, port_name):
        self.port = port_name
        if self.ser and getattr(self.ser, 'is_open', False):
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def run(self):
        self.running = True
        while self.running:
            try:
                # 1. Process any pending outgoing serial commands safely on worker thread
                while not self.cmd_queue.empty():
                    try:
                        cmd = self.cmd_queue.get_nowait()
                        if self.ser and getattr(self.ser, 'is_open', False):
                            msg = (cmd + "\n").encode('utf-8')
                            self.ser.write(msg)
                            self.ser.flush()
                    except queue.Empty:
                        break
                    except Exception as e:
                        print(f"[{self.node_name}] Serial write error: {e}")

                # 2. Open serial port if needed
                if self.ser is None or not getattr(self.ser, 'is_open', False):
                    try:
                        # Open without pulsing DTR/RTS so connecting doesn't auto-reset the ESP32-S3
                        self.ser = serial.Serial()
                        self.ser.port = self.port
                        self.ser.baudrate = self.baud
                        self.ser.timeout = 0.1
                        self.ser.dsrdtr = False
                        self.ser.rtscts = False
                        try:
                            self.ser.dtr = False
                            self.ser.rts = False
                        except Exception:
                            pass
                        self.ser.open()
                        self.status_changed.emit(f"{self.node_name}: CONNECTED ({self.port})", True)
                    except Exception:
                        self.status_changed.emit(f"{self.node_name}: WAITING ({self.port})...", False)
                        time.sleep(0.5)
                        continue

                # 3. Read incoming serial telemetry line
                line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue

                # 0. [RATE LOCKED] / [RATE AUTO] Rate:50Hz | SF:8 | BW:500kHz | Interval:20000us
                rate_match = re.search(r'\[RATE (?:LOCKED|AUTO)\]\s*Rate:([\w\s]+?)\s*\|\s*SF:(\d+)\s*\|\s*BW:([\d\.]+)kHz\s*\|\s*Interval:(\d+)us', line)
                if rate_match:
                    rate_name = rate_match.group(1).strip()
                    sf = int(rate_match.group(2))
                    bw = float(rate_match.group(3))
                    interval = int(rate_match.group(4))
                    rate_dict = {
                        "node_id": self.node_id,
                        "node_name": self.node_name,
                        "rate_name": rate_name,
                        "sf": sf,
                        "bw_khz": bw,
                        "interval_us": interval,
                        "raw_line": line,
                        "timestamp": time.time()
                    }
                    self.rate_detected.emit(rate_dict)
                    continue

                # 1. [RC 50Hz] / [RC 100Hz] RSSI: -11 dBm | SNR:+10.8 dB | CH1..CH16 | ARM:OFF
                if line.startswith("[RC"):
                    rc_meta = re.search(r'\[RC(?:\s+([\d\w\s]+))?\]\s*RSSI:\s*([-\d\.\+]+).*?SNR:\s*([-\d\.\+]+)', line)
                    if rc_meta:
                        now = time.time()
                        packet_rate = rc_meta.group(1).strip() if rc_meta.group(1) else "50Hz"
                        rssi = float(rc_meta.group(2))
                        snr = float(rc_meta.group(3))

                        ch_matches = re.findall(r'CH(\d+):(\d+)', line)
                        for ch_str, val_str in ch_matches:
                            idx = int(ch_str) - 1
                            if 0 <= idx < 16:
                                self.persistent_channels[idx] = int(val_str)

                        arm_match = re.search(r'ARM:(ON|OFF)', line)
                        is_armed = (arm_match.group(1) == "ON") if arm_match else (self.persistent_channels[4] > 1500)
                        ch_list = list(self.persistent_channels)

                        rc_dict = {
                            "node_id": self.node_id,
                            "node_name": self.node_name,
                            "rate": packet_rate,
                            "packet_rate": packet_rate,
                            "rssi": rssi,
                            "snr": snr,
                            "ch1": ch_list[0] if len(ch_list) > 0 else 1500,
                            "ch2": ch_list[1] if len(ch_list) > 1 else 1500,
                            "ch3": ch_list[2] if len(ch_list) > 2 else 988,
                            "ch4": ch_list[3] if len(ch_list) > 3 else 1500,
                            "channels": ch_list,
                            "armed": is_armed,
                            "timestamp": now
                        }
                        self.rc_data_received.emit(rc_dict)
                        continue

                # 2. [PILOT DISCOVERED] UID3:130 UID4:33 UID5:85 | CRC:0x2156 | RSSI:-45 | Rate:50Hz
                # 2. [PILOT DISCOVERED] UID4:33 UID5:85 | CRC:0x2156 | RSSI:-45 | Rate:50Hz
                pilot_m = re.search(r'\[PILOT DISCOVERED\]\s*(?:UID3:(\d+)\s+)?UID4:(\d+)\s+UID5:(\d+)\s*\|\s*CRC:0x([0-9A-Fa-f]+)\s*\|\s*RSSI:([-\d\.\+]+)\s*\|\s*Rate:([\w\s]+)', line)
                if pilot_m:
                    u3 = int(pilot_m.group(1)) if pilot_m.group(1) else 130
                    u4 = int(pilot_m.group(2))
                    u5 = int(pilot_m.group(3))
                    crc_int = int(pilot_m.group(4), 16)
                    crc_str = f"{crc_int:04X}"
                    rssi = float(pilot_m.group(5))
                    rate = pilot_m.group(6).strip()
                    uid_str = f"{u4:02X}:{u5:02X}" if not pilot_m.group(1) else f"{u3:02X}:{u4:02X}:{u5:02X}"

                    p_dict = {
                        "node_id": self.node_id,
                        "node_name": self.node_name,
                        "uid_str": uid_str,
                        "u3": u3,
                        "u4": u4,
                        "u5": u5,
                        "uid3": u3,
                        "uid4": u4,
                        "uid5": u5,
                        "crc_init": crc_str,
                        "crc_int": crc_int,
                        "rssi": rssi,
                        "rate": rate,
                        "packet_rate": rate,
                        "timestamp": time.time()
                    }
                    self.pilot_discovered.emit(p_dict)
                    continue

                # 3. [SYNC VERIFIED] UID4:33 UID5:85 | CRC: 0x2156 | HopIdx:12 Nonce:84 | Rate:50Hz
                sync_m = re.search(r'\[SYNC VERIFIED\]\s*(?:UID3:(\d+)\s+)?UID4:(\d+)\s+UID5:(\d+)\s*\|\s*CRC:\s*0x([0-9A-Fa-f]+)\s*\|\s*HopIdx:(\d+)\s+Nonce:(\d+)\s*\|\s*Rate:([\w\s]+)', line)
                if sync_m:
                    u3 = int(sync_m.group(1)) if sync_m.group(1) else 130
                    u4 = int(sync_m.group(2))
                    u5 = int(sync_m.group(3))
                    crc_int = int(sync_m.group(4), 16)
                    crc_str = f"{crc_int:04X}"
                    hop_idx = int(sync_m.group(5))
                    nonce = int(sync_m.group(6))
                    rate = sync_m.group(7).strip()

                    sync_dict = {
                        "node_id": self.node_id,
                        "node_name": self.node_name,
                        "u3": u3,
                        "u4": u4,
                        "u5": u5,
                        "uid3": u3,
                        "uid4": u4,
                        "uid5": u5,
                        "crc_init": crc_str,
                        "crc_int": crc_int,
                        "hop_idx": hop_idx,
                        "nonce": nonce,
                        "rate": rate,
                        "timestamp": time.time()
                    }
                    self.sync_discovered.emit(sync_dict)
                    continue

                # 4. [TLM GPS] Lat:51.50742 Lon:-0.12781 Alt:124m Spd:48km/h Sats:14
                gps_m = re.search(r'\[TLM GPS\]\s*Lat:([-\d\.]+)\s+Lon:([-\d\.]+)\s+Alt:([-\d\.]+)m\s+Spd:([-\d\.]+)km/h\s+Sats:(\d+)', line)
                if gps_m:
                    gps_dict = {
                        "node_id": self.node_id,
                        "node_name": self.node_name,
                        "lat": float(gps_m.group(1)),
                        "lon": float(gps_m.group(2)),
                        "alt": float(gps_m.group(3)),
                        "spd": float(gps_m.group(4)),
                        "sats": int(gps_m.group(5)),
                        "timestamp": time.time()
                    }
                    self.gps_received.emit(gps_dict)
                    continue

                # 5. [TLM BATTERY] Volt:15.8V Curr:12.4A mAh:680
                bat_m = re.search(r'\[TLM BATTERY\]\s*Volt:([-\d\.]+)V\s+Curr:([-\d\.]+)A\s+mAh:(\d+)', line)
                if bat_m:
                    v = float(bat_m.group(1))
                    curr = float(bat_m.group(2))
                    mah = int(bat_m.group(3))
                    cells = max(1, round(v / 3.8))
                    cell_v = v / cells
                    pct = max(0.0, min(100.0, (cell_v - 3.5) / 0.7 * 100.0))
                    bat_dict = {
                        "node_id": self.node_id,
                        "node_name": self.node_name,
                        "voltage": v,
                        "current": curr,
                        "mah": mah,
                        "battery_pct": round(pct, 1),
                        "timestamp": time.time()
                    }
                    self.battery_received.emit(bat_dict)
                    continue

                # 6. [TLM ATTITUDE] Pitch:+12.4deg Roll:-4.1deg Yaw:182.0deg
                att_m = re.search(r'\[TLM ATTITUDE\]\s*Pitch:([-\d\.\+]+)deg\s+Roll:([-\d\.\+]+)deg\s+Yaw:([-\d\.\+]+)deg', line)
                if att_m:
                    att_dict = {
                        "node_id": self.node_id,
                        "node_name": self.node_name,
                        "pitch": float(att_m.group(1)),
                        "roll": float(att_m.group(2)),
                        "yaw": float(att_m.group(3)),
                        "timestamp": time.time()
                    }
                    self.attitude_received.emit(att_dict)
                    continue

                # 7. [TLM MODE] Mode:ACRO
                mode_m = re.search(r'\[TLM MODE\]\s*Mode:(\w+)', line)
                if mode_m:
                    mode_dict = {
                        "node_id": self.node_id,
                        "node_name": self.node_name,
                        "mode": mode_m.group(1),
                        "timestamp": time.time()
                    }
                    self.flight_mode_received.emit(mode_dict)
                    continue

                # 8. [TLM LINK] Link:100% RSSI1:-45dBm RSSI2:-48dBm SNR:+11dB Power:25mW
                link_m = re.search(r'\[TLM LINK\]\s*Link:(\d+)%\s+RSSI1:([-\d\.]+)dBm\s+RSSI2:([-\d\.]+)dBm\s+SNR:([-\d\.\+]+)dB\s+Power:(\d+)mW', line)
                if link_m:
                    lq = int(link_m.group(1))
                    r1 = float(link_m.group(2))
                    r2 = float(link_m.group(3))
                    snr = float(link_m.group(4))
                    pwr = int(link_m.group(5))
                    link_dict = {
                        "node_id": self.node_id,
                        "node_name": self.node_name,
                        "drone_lq": lq,
                        "link_quality": lq,
                        "drone_rssi": r1,
                        "rssi1": r1,
                        "rssi2": r2,
                        "drone_snr": snr,
                        "snr": snr,
                        "power_mw": pwr,
                        "timestamp": time.time()
                    }
                    self.telemetry_link_received.emit(link_dict)
                    continue

            except Exception as e:
                time.sleep(0.2)

    def stop(self):
        self.running = False
        if self.ser and getattr(self.ser, 'is_open', False):
            try:
                self.ser.close()
            except Exception:
                pass
        self.wait(500)


class DualLoRaManager(QObject):
    """
    Multi-station manager coordinating LilyGO T3-S3 (Node 1) and Heltec V3 (Node 2).
    Calculates differential RSSI (Delta-RSSI) and emits multi-sensor geolocation fixes.
    """
    fused_pilot_discovered = pyqtSignal(dict)
    differential_rssi_fix = pyqtSignal(dict)
    node_status_signal = pyqtSignal(str, str, bool)

    def __init__(self, port_node_a="COM6", port_node_b="COM7", parent=None):
        super().__init__(parent)
        self.node_a = HeltecLoraThread(port=port_node_a, node_id="NODE_A", node_name="LILYGO T3-S3 (Primary Tracker)")
        self.node_b = HeltecLoraThread(port=port_node_b, node_id="NODE_B", node_name="HELTEC V3 (Airspace Sentry)")
        
        self.last_beacons = {} # {uid_str: {node_id: (rssi, timestamp)}}
        
        # Connect signals
        self.node_a.pilot_discovered.connect(lambda d: self._on_pilot_beacon("NODE_A", d))
        self.node_b.pilot_discovered.connect(lambda d: self._on_pilot_beacon("NODE_B", d))
        self.node_a.status_changed.connect(lambda s, ok: self.node_status_signal.emit("NODE_A", s, ok))
        self.node_b.status_changed.connect(lambda s, ok: self.node_status_signal.emit("NODE_B", s, ok))

    def start_nodes(self):
        if not self.node_a.isRunning():
            self.node_a.start()
        if not self.node_b.isRunning():
            self.node_b.start()

    def stop_nodes(self):
        self.node_a.stop()
        self.node_b.stop()

    def send_command(self, cmd_str):
        try:
            res_a = self.node_a.send_command(cmd_str)
            res_b = self.node_b.send_command(cmd_str)
            return res_a or res_b
        except Exception as e:
            return False

    def _on_pilot_beacon(self, node_id, data):
        try:
            uid = f"{data['u3']:02X}:{data['u4']:02X}:{data['u5']:02X}"
            now = time.time()
            
            if uid not in self.last_beacons:
                self.last_beacons[uid] = {}
            self.last_beacons[uid][node_id] = (data["rssi"], now)

            self.fused_pilot_discovered.emit(data)

            # Check for Dual-Station intersection
            other_node = "NODE_B" if node_id == "NODE_A" else "NODE_A"
            if other_node in self.last_beacons[uid]:
                other_rssi, other_time = self.last_beacons[uid][other_node]
                if now - other_time < 0.25: # Within 250ms window
                    rssi_a = data["rssi"] if node_id == "NODE_A" else other_rssi
                    rssi_b = other_rssi if node_id == "NODE_A" else data["rssi"]
                    delta_rssi = rssi_a - rssi_b
                    dist_ratio = math.pow(10.0, delta_rssi / (10.0 * 2.4))

                    fix = {
                        "uid": uid,
                        "u3": data["u3"],
                        "u4": data["u4"],
                        "u5": data["u5"],
                        "rssi_node_a": rssi_a,
                        "rssi_node_b": rssi_b,
                        "delta_rssi_db": round(delta_rssi, 1),
                        "dist_ratio": round(dist_ratio, 2),
                        "dominant_station": "NODE_A (LilyGO)" if delta_rssi > 0 else "NODE_B (Heltec)",
                        "timestamp": now
                    }
                    self.differential_rssi_fix.emit(fix)
        except Exception:
            pass
