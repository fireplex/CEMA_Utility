import time
import re
import serial
import serial.tools.list_ports
from PyQt6.QtCore import QThread, pyqtSignal

def get_available_com_ports():
    ports = serial.tools.list_ports.comports()
    result = []
    for p in ports:
        result.append(p.device)
    if "COM6" not in result:
        result.insert(0, "COM6")
    return result

class HeltecLoraThread(QThread):
    rc_data_received = pyqtSignal(dict)
    telemetry_link_received = pyqtSignal(dict)
    battery_received = pyqtSignal(dict)
    gps_received = pyqtSignal(dict)
    sync_discovered = pyqtSignal(dict)
    status_changed = pyqtSignal(str, bool)

    def __init__(self, port="COM6", baud=115200, parent=None):
        super().__init__(parent)
        self.port = port
        self.baud = baud
        self.running = False
        self.ser = None

    def set_port(self, port_name):
        self.port = port_name
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def run(self):
        self.running = True
        while self.running:
            try:
                if self.ser is None or not self.ser.is_open:
                    try:
                        self.ser = serial.Serial(self.port, self.baud, timeout=0.2)
                        self.status_changed.emit(f"HELTEC V3: CONNECTED ({self.port})", True)
                    except Exception as e:
                        self.status_changed.emit(f"HELTEC V3: WAITING ({self.port})...", False)
                        time.sleep(1.0)
                        continue

                line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue

                # 1. [RC 50Hz] RSSI: -11 dBm | SNR:+10.8 dB | CH1:1502 | CH2:1499 | CH3: 989 | CH4:1500 | ARM:OFF
                rc_match = re.search(r'\[RC.*?\]\s*RSSI:\s*([-\d\.\+]+).*?SNR:\s*([-\d\.\+]+).*?CH1:\s*(\d+).*?CH2:\s*(\d+).*?CH3:\s*(\d+).*?CH4:\s*(\d+).*?ARM:\s*(\w+)', line)
                if rc_match:
                    rc_dict = {
                        "rssi": float(rc_match.group(1)),
                        "snr": float(rc_match.group(2)),
                        "ch1": int(rc_match.group(3)),
                        "ch2": int(rc_match.group(4)),
                        "ch3": int(rc_match.group(5)),
                        "ch4": int(rc_match.group(6)),
                        "armed": (rc_match.group(7).upper() in ["ON", "ARM", "1", "TRUE"]),
                        "raw_line": line,
                        "timestamp": time.time()
                    }
                    self.rc_data_received.emit(rc_dict)
                    continue

                # 2. [TLM LINK] DroneRSSI:-60 | DroneLQ:100 | DroneSNR:+26
                tlm_link = re.search(r'\[TLM LINK\]\s*DroneRSSI:\s*([-\d\.\+]+)\s*\|\s*DroneLQ:\s*(\d+)\s*\|\s*DroneSNR:\s*([-\d\.\+]+)', line)
                if tlm_link:
                    link_dict = {
                        "drone_rssi": int(float(tlm_link.group(1))),
                        "drone_lq": int(tlm_link.group(2)),
                        "drone_snr": int(float(tlm_link.group(3))),
                        "raw_line": line,
                        "timestamp": time.time()
                    }
                    self.telemetry_link_received.emit(link_dict)
                    continue

                # 3. [TLM BAT] V:16.4 | I:12.5 | Batt:85
                tlm_bat = re.search(r'\[TLM BAT\]\s*V:\s*([\d\.]+)\s*\|\s*I:\s*([\d\.]+)\s*\|\s*Batt:\s*(\d+)', line)
                if tlm_bat:
                    bat_dict = {
                        "voltage": float(tlm_bat.group(1)),
                        "current": float(tlm_bat.group(2)),
                        "battery_pct": int(tlm_bat.group(3)),
                        "raw_line": line,
                        "timestamp": time.time()
                    }
                    self.battery_received.emit(bat_dict)
                    continue

                # 4. [TLM GPS] Lat:51.5074 | Lon:-0.1278 | Alt:45 | Spd:48 | Sats:16
                tlm_gps = re.search(r'\[TLM GPS\]\s*Lat:\s*([-\d\.]+)\s*\|\s*Lon:\s*([-\d\.]+)\s*\|\s*Alt:\s*([\d\.]+)\s*\|\s*Spd:\s*([\d\.]+)\s*\|\s*Sats:\s*(\d+)', line)
                if tlm_gps:
                    gps_dict = {
                        "lat": float(tlm_gps.group(1)),
                        "lon": float(tlm_gps.group(2)),
                        "alt": float(tlm_gps.group(3)),
                        "spd": float(tlm_gps.group(4)),
                        "sats": int(tlm_gps.group(5)),
                        "raw_line": line,
                        "timestamp": time.time()
                    }
                    self.gps_received.emit(gps_dict)
                    continue

                # 5. [AUTODISCOVERY SYNC] / [SYNC VERIFIED] UID4:33 UID5:85 | CRC: 0x2156 | HopIdx:200 Nonce:35
                sync_match = re.search(r'\[(?:AUTODISCOVERY SYNC|SYNC VERIFIED)\].*?UID4:\s*(\d+)\s*UID5:\s*(\d+).*?CRC:\s*0x([0-9A-Fa-f]+).*?HopIdx:\s*(\d+)\s*Nonce:\s*(\d+)', line)
                if sync_match:
                    sync_dict = {
                        "uid4": int(sync_match.group(1)),
                        "uid5": int(sync_match.group(2)),
                        "crc_init": sync_match.group(3),
                        "hop_idx": int(sync_match.group(4)),
                        "nonce": int(sync_match.group(5)),
                        "raw_line": line,
                        "timestamp": time.time()
                    }
                    self.sync_discovered.emit(sync_dict)
                    continue

            except Exception as e:
                if self.ser:
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                    self.ser = None
                self.status_changed.emit(f"HELTEC ERROR ({self.port})", False)
                time.sleep(1.0)

    def stop(self):
        self.running = False
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self.wait()
