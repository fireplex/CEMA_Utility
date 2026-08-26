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
    attitude_received = pyqtSignal(dict)
    flight_mode_received = pyqtSignal(dict)
    sync_discovered = pyqtSignal(dict)
    rate_detected = pyqtSignal(dict)
    pilot_discovered = pyqtSignal(dict)
    status_changed = pyqtSignal(str, bool)

    def __init__(self, port="COM6", baud=115200, parent=None):
        super().__init__(parent)
        self.port = port
        self.baud = baud
        self.running = False
        self.ser = None

    def send_command(self, cmd_str):
        if self.ser and self.ser.is_open:
            try:
                msg = (cmd_str.strip() + "\n").encode('utf-8')
                self.ser.write(msg)
                self.ser.flush()
                return True
            except Exception as e:
                print(f"[HELTEC BRIDGE] Serial write error: {e}")
        return False

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

                # 0. [RATE LOCKED] Rate:50Hz | SF:8 | BW:500kHz | Interval:20000us
                rate_match = re.search(r'\[RATE (?:LOCKED|AUTO)\]\s*Rate:(\w+)\s*\|\s*SF:(\d+)\s*\|\s*BW:([\d\.]+)kHz\s*\|\s*Interval:(\d+)us', line)
                if rate_match:
                    rate_dict = {
                        "rate_name": rate_match.group(1),
                        "sf": int(rate_match.group(2)),
                        "bw_khz": float(rate_match.group(3)),
                        "interval_us": int(rate_match.group(4)),
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

                        # Extract all CH1..CH16 channels into persistent state
                        if not hasattr(self, 'persistent_channels'):
                            self.persistent_channels = [1500] * 16
                            self.persistent_channels[2] = 988
                            self.persistent_channels[4] = 1000

                        ch_found = {}
                        for m in re.finditer(r'CH(\d+):\s*(\d+)', line):
                            ch_num = int(m.group(1))
                            ch_val = int(m.group(2))
                            if 1 <= ch_num <= 16:
                                self.persistent_channels[ch_num - 1] = ch_val
                                ch_found[ch_num] = ch_val

                        arm_match = re.search(r'ARM:\s*(\w+)', line)
                        armed_state = (arm_match and arm_match.group(1).upper() in ["ON", "ARM", "1", "TRUE"]) or (self.persistent_channels[4] > 1500)

                        if 5 not in ch_found:
                            self.persistent_channels[4] = 2000 if armed_state else 1000

                        channels = list(self.persistent_channels)

                        # Rate-limit GUI event emission to ~25Hz (every 40ms) unless ARM status changed
                        last_rc = getattr(self, '_last_rc_emit', 0)
                        last_arm = getattr(self, '_last_arm_state', None)
                        if (now - last_rc >= 0.038) or (armed_state != last_arm):
                            self._last_rc_emit = now
                            self._last_arm_state = armed_state
                            rc_dict = {
                                "packet_rate": packet_rate,
                                "rssi": rssi,
                                "snr": snr,
                                "channels": channels,
                                "ch1": channels[0],
                                "ch2": channels[1],
                                "ch3": channels[2],
                                "ch4": channels[3],
                                "ch5": channels[4],
                                "ch6": channels[5],
                                "ch7": channels[6],
                                "ch8": channels[7],
                                "ch9": channels[8],
                                "ch10": channels[9],
                                "ch11": channels[10],
                                "ch12": channels[11],
                                "ch13": channels[12],
                                "ch14": channels[13],
                                "ch15": channels[14],
                                "ch16": channels[15],
                                "armed": armed_state,
                                "raw_line": line,
                                "timestamp": now
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

                # 3. [TLM BAT] V:16.4 | I:12.5 | Cap:850 | Batt:85
                tlm_bat = re.search(r'\[TLM BAT\]\s*V:\s*([\d\.]+)\s*\|\s*I:\s*([\d\.]+)(?:\s*\|\s*Cap:\s*(\d+))?\s*\|\s*Batt:\s*(\d+)', line)
                if tlm_bat:
                    bat_dict = {
                        "voltage": float(tlm_bat.group(1)),
                        "current": float(tlm_bat.group(2)),
                        "capacity_mah": int(tlm_bat.group(3)) if tlm_bat.group(3) else 0,
                        "battery_pct": int(tlm_bat.group(4)),
                        "raw_line": line,
                        "timestamp": time.time()
                    }
                    self.battery_received.emit(bat_dict)
                    continue

                # 3b. [TLM ATT] Pitch:12 | Roll:-3 | Yaw:85
                tlm_att = re.search(r'\[TLM ATT\]\s*Pitch:\s*([-\d\.]+)\s*\|\s*Roll:\s*([-\d\.]+)\s*\|\s*Yaw:\s*([-\d\.]+)', line)
                if tlm_att:
                    att_dict = {
                        "pitch": float(tlm_att.group(1)),
                        "roll": float(tlm_att.group(2)),
                        "yaw": float(tlm_att.group(3)),
                        "raw_line": line,
                        "timestamp": time.time()
                    }
                    self.attitude_received.emit(att_dict)
                    continue

                # 3c. [TLM MODE] Mode:ANGLE
                tlm_mode = re.search(r'\[TLM MODE\]\s*Mode:\s*(\w+)', line)
                if tlm_mode:
                    mode_dict = {
                        "mode": tlm_mode.group(1).upper(),
                        "raw_line": line,
                        "timestamp": time.time()
                    }
                    self.flight_mode_received.emit(mode_dict)
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

                # 6. [PILOT DISCOVERED] UID3:130 UID4:33 UID5:85 | CRC:0x2156 | RSSI:-45 | Rate:50Hz
                pilot_match = re.search(r'\[PILOT DISCOVERED\].*?UID3:\s*(\d+)\s*UID4:\s*(\d+)\s*UID5:\s*(\d+).*?CRC:\s*0x([0-9A-Fa-f]+).*?RSSI:\s*([-\d\.]+).*?Rate:\s*([^\r\n\|]+)', line)
                if pilot_match:
                    u3 = int(pilot_match.group(1))
                    u4 = int(pilot_match.group(2))
                    u5 = int(pilot_match.group(3))
                    p_dict = {
                        "u3": u3,
                        "u4": u4,
                        "u5": u5,
                        "uid_str": f"{u3}:{u4}:{u5}",
                        "crc_init": pilot_match.group(4),
                        "rssi": float(pilot_match.group(5)),
                        "rate": pilot_match.group(6).strip(),
                        "raw_line": line,
                        "timestamp": time.time()
                    }
                    self.pilot_discovered.emit(p_dict)
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
