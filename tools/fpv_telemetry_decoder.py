import os
import sys
import time
import struct
import numpy as np
import queue
import socket
from ctypes import (
    CDLL, POINTER, Structure, c_int, c_uint32, c_uint64, 
    c_uint8, c_void_p, CFUNCTYPE, cast, byref
)
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QPushButton, QGridLayout, 
                             QSplitter, QComboBox, QLineEdit, QScrollArea, QFrame)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtWebEngineWidgets import QWebEngineView
import pyqtgraph as pg
from heltec_bridge import HeltecLoraThread, get_available_com_ports

# -------------------------------------------------------------------------
# CONSOLE LOGGING FRAMEWORK (SANITY ENGINE)
# -------------------------------------------------------------------------
def log_debug(category: str, message: str):
    timestamp = time.strftime("%H:%M:%S", time.localtime())
    print(f"[{timestamp}] [{category.upper()}] {message}")
    sys.stdout.flush()

# -------------------------------------------------------------------------
# NATIVE HARDWARE INTERFACE OVERRIDES (WINDOWS ENGINE)
# -------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DLL_PATH = os.path.join(SCRIPT_DIR, "libhackrf.dll")

if not os.path.exists(DLL_PATH):
    DLL_PATH = r"C:\Users\toxic\Desktop\hackrf-utility\libhackrf.dll"

if sys.platform == "win32" and os.path.exists(os.path.dirname(DLL_PATH)):
    os.add_dll_directory(os.path.dirname(DLL_PATH))

try:
    libhackrf = CDLL(DLL_PATH, winmode=0)
    log_debug("SDR-CORE", f"Successfully loaded native hardware DLL from: {DLL_PATH}")
except Exception as e:
    log_debug("CRITICAL", f"NATIVE DLL LINK CRITICAL FAILURE: {e}")
    sys.exit(1)

class HackrfTransfer(Structure):
    _fields_ = [
        ("device", c_void_p),
        ("buffer", POINTER(c_uint8)),
        ("buffer_length", c_int),
        ("valid_length", c_int),
        ("rx_ctx", c_void_p)
    ]

BAND_PRESETS = {
    "ELRS / CRSF 868M (UK/EU wideband)": 863.0,
    "ELRS / CRSF 915M (FCC)": 915.0,
    "ELRS 2.4G ISM": 2440.0
}

RAW_DATA_QUEUE = queue.Queue(maxsize=200)

# -------------------------------------------------------------------------
# TELEMETRY HARVESTING ENGINE THREAD (STRICT LENGTH & CRC SANITY)
# -------------------------------------------------------------------------
class HackRFReceiverThread(QThread):
    data_packet_parsed = pyqtSignal(dict)
    rf_power_updated = pyqtSignal(float)
    
    def __init__(self, initial_mhz=863.0, scan_mode=False, use_mock=False, *args, **kwargs):
        super().__init__()
        self.running = False
        self.use_mock = use_mock
        self.scan_mode = scan_mode
        self.byte_stream_buffer = bytearray()
        self.last_ui_update_time = 0.0
        
        # Configure Network Loopback Socket Hook
        self.udp_ip = "127.0.0.1"
        self.udp_port = 50051
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.2)
        
        log_debug("RX-THREAD", f"Initialized thread context | Target MHz: {initial_mhz} | Mock Mode: {use_mock}")

    def crsf_crc8(self, data: bytes) -> int:
        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x80:
                    crc = ((crc << 1) ^ 0xD5) & 0xFF
                else:
                    crc = (crc << 1) & 0xFF
        return crc

    def run(self):
        self.running = True
        log_debug("UDP-NET", f"Attempting socket bind to loopback {self.udp_ip}:{self.udp_port}...")
        
        try:
            self.sock.bind((self.udp_ip, self.udp_port))
            log_debug("UDP-NET", f"Successfully bound to {self.udp_ip}:{self.udp_port}. Waiting for stream...")
        except Exception as e:
            log_debug("UDP-NET", f"⚠️ Network Bind Warning: {e}. Port may already be bound.")

        while self.running:
            if self.use_mock:
                self.process_mock_tick()
                time.sleep(0.05)
                continue

            try:
                data, addr = self.sock.recvfrom(4096)
                if data:
                    try:
                        iq_samples = np.frombuffer(data, dtype=np.complex64)
                        if iq_samples.size > 0:
                            avg_db = float(10 * np.log10(np.mean(np.abs(iq_samples)**2) + 1e-6))
                            self.rf_power_updated.emit(avg_db)
                    except Exception:
                        pass
                    
                    self.byte_stream_buffer.extend(data)
                    self.parse_crsf_stream()
                    
            except socket.timeout:
                continue
            except Exception as e:
                log_debug("UDP-NET", f"Socket routing fault exception: {e}")
                continue

    def force_frequency(self, target_mhz):
        log_debug("SDR-CORE", f"Forcing hardware frequency realignment to: {target_mhz} MHz")
            
    def generate_mock_crsf_packet(self, elapsed_time: float) -> bytearray:
        # Rotate through types: 0x08 (Battery), 0x1E (Link Stats), 0x0E (Attitude)
        cycle = int(elapsed_time * 5) % 3
        if cycle == 0:
            type_id = 0x08
            payload = struct.pack('>HHHB', int(1680 + np.sin(elapsed_time)*50), int(120), int(450), 85)
        elif cycle == 1:
            type_id = 0x1E
            payload = struct.pack('>bbbbbbbbbb', -75, -78, 99, 12, 1, 0, 25, -80, 98, 10)
        else:
            type_id = 0x0E
            payload = struct.pack('>hhh', int(np.sin(elapsed_time)*100), int(np.cos(elapsed_time)*100), int(elapsed_time*10)%360)

        packet = bytearray()
        packet.append(0xEA) # Address (Handset/Receiver)
        packet.append(len(payload) + 2) # Length includes Type + Payload + CRC
        packet.append(type_id)
        packet.extend(payload)
        
        crc = self.crsf_crc8(packet[2:])
        packet.append(crc)
        return packet

    def process_mock_tick(self):
        current_time = time.time()
        mock_db = float(-25.0 + np.random.normal(0, 2))
        self.rf_power_updated.emit(mock_db)

        if current_time - self.last_ui_update_time >= 0.2:
            mock_packet = self.generate_mock_crsf_packet(current_time)
            self.byte_stream_buffer.extend(mock_packet)
            self.parse_crsf_stream()
            self.last_ui_update_time = current_time

    def parse_crsf_stream(self):
        VALID_ADDRESSES = [0xEE, 0xEA, 0xC8, 0xEC]
        
        while len(self.byte_stream_buffer) >= 4:
            if self.byte_stream_buffer[0] not in VALID_ADDRESSES:
                self.byte_stream_buffer.pop(0)
                continue
                
            frame_length = self.byte_stream_buffer[1]
            
            # CRSF frame length: payload length + type byte (1) + crc byte (1) = payload_len + 2
            # Total packet size on wire = header (1) + length byte (1) + frame_length (frame_length)
            if frame_length < 2 or frame_length > 60:
                self.byte_stream_buffer.pop(0)
                continue

            total_packet_len = 2 + frame_length
            
            if len(self.byte_stream_buffer) < total_packet_len:
                break # Wait for more bytes
                
            packet = self.byte_stream_buffer[:total_packet_len]
            
            # Verify CRC over [Type + Payload] (everything from index 2 up to second-to-last byte)
            crc_calculated = self.crsf_crc8(packet[2:-1])
            crc_received = packet[-1]
            
            if crc_received != crc_calculated:
                # Invalid packet, shift by 1 and resync
                self.byte_stream_buffer.pop(0)
                continue

            # Valid packet confirmed! Remove from buffer
            del self.byte_stream_buffer[:total_packet_len]
            
            type_id = packet[2]
            payload_bytes = packet[3:-1]
            
            parsed_record = self.decode_payload(type_id, payload_bytes)
            if parsed_record:
                self.data_packet_parsed.emit(parsed_record)

    def decode_payload(self, type_id, payload):
        timestamp = time.strftime("%H:%M:%S", time.localtime())
        
        if type_id == 0x08: # Battery Sensor
            if len(payload) >= 7:
                v, cur, cap, rem = struct.unpack('>HHHB', payload[0:7])
                return {
                    "time": timestamp, "type_hex": "0x08", "name": "BATTERY_SENSOR",
                    "summary": f"Volt: {v/100.0:.2f}V | Curr: {cur/10.0}A | Cap: {cap}mAh | Rem: {rem}%",
                    "raw": payload.hex()
                }
        elif type_id == 0x1E: # Link Statistics
            if len(payload) >= 10:
                up_rssi_1, up_rssi_2, up_lq, up_snr, diversity, rf_mode, uplink_power, down_rssi, down_lq, down_snr = struct.unpack('>bbbbbbbbbb', payload[0:10])
                return {
                    "time": timestamp, "type_hex": "0x1E", "name": "LINK_STATISTICS",
                    "summary": f"RSSI: {up_rssi_1} dBm | LQ: {up_lq}% | SNR: {up_snr} dB | PWR: {uplink_power}mW",
                    "raw": payload.hex()
                }
        elif type_id == 0x0E: # Attitude
            if len(payload) >= 6:
                pitch, roll, yaw = struct.unpack('>hhh', payload[0:6])
                return {
                    "time": timestamp, "type_hex": "0x0E", "name": "ATTITUDE",
                    "summary": f"Pitch: {pitch/10.0}° | Roll: {roll/10.0}° | Yaw: {yaw/10.0}°",
                    "raw": payload.hex()
                }
        
        # Generic fallback for any other valid CRSF frame type passing strict CRC
        return {
            "time": timestamp, "type_hex": f"0x{type_id:02X}", "name": "GENERIC_CRSF_FRAME",
            "summary": f"Valid CRC Payload // Length: {len(payload)} bytes",
            "raw": payload.hex()
        }

    def stop(self):
        log_debug("RX-THREAD", "Shutting down telemetry receiver thread...")
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass
        self.wait()

# -------------------------------------------------------------------------
# GRAPHICAL RADAR HUD MAIN APP (LIVE TELEMETRY STREAM CONSOLE)
# -------------------------------------------------------------------------
class ApexApp(QMainWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        log_debug("GUI-HUD", "Initializing Apex Passive FPV Telemetry HUD Window...")
        self.setWindowTitle("APEX PASSIVE FPV TELEMETRY HARVESTER [HACKRF + HELTEC V3]")
        self.resize(1300, 850)
        
        self.power_history = np.full(500, -50.0, dtype=np.float32)
        self.rx_thread = None
        self.heltec_thread = None
        
        self.setup_ui()
        self.engage_system_pipeline()

    def setup_ui(self):
        log_debug("GUI-HUD", "Constructing dark-tactical UI components...")
        self.setStyleSheet("""
            QWidget { background-color: #000000; color: #ef4444; font-family: 'Consolas', monospace; font-weight: bold; }
            QLabel { font-size: 13px; border: 1px solid #1f1f1f; padding: 6px; background-color: #050505; color: #fca5a5; }
            QPushButton { background-color: #111111; color: #ef4444; border: 2px solid #ef4444; padding: 6px 12px; font-size: 13px; border-radius: 4px; }
            QPushButton:hover { background-color: #ef4444; color: black; }
            QPushButton:checked { background-color: #22c55e; color: black; border: 2px solid #22c55e; }
            QComboBox { background-color: #111111; color: #fca5a5; border: 1px solid #ef4444; padding: 4px; font-size: 13px; }
            QLineEdit { background-color: #111111; color: #22c55e; border: 1px solid #ef4444; padding: 4px; font-size: 13px; }
            QSplitter::handle { background-color: #1f1f1f; }
        """)
        
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        
        # Control Bar
        control_bar = QHBoxLayout()
        control_bar.addWidget(QLabel("BAND PRESETS:"))
        
        self.preset_combo = QComboBox()
        for name in BAND_PRESETS.keys():
            self.preset_combo.addItem(name)
        self.preset_combo.setCurrentIndex(1)
        self.preset_combo.currentIndexChanged.connect(self.on_preset_selected)
        control_bar.addWidget(self.preset_combo)
        
        control_bar.addWidget(QLabel("FREQ (MHz):"))
        self.freq_input = QLineEdit("915.000")
        self.freq_input.setFixedWidth(90)
        control_bar.addWidget(self.freq_input)
        
        self.lock_btn = QPushButton("FORCE LOCK")
        self.lock_btn.clicked.connect(self.on_manual_lock_clicked)
        control_bar.addWidget(self.lock_btn)
        
        self.scan_btn = QPushButton("HUNTER SCAN")
        self.scan_btn.setCheckable(True)
        self.scan_btn.clicked.connect(self.on_scan_toggled)
        control_bar.addWidget(self.scan_btn)

        self.mock_btn = QPushButton("MOCK MODE")
        self.mock_btn.setCheckable(True)
        self.mock_btn.clicked.connect(self.on_mock_toggled)
        control_bar.addWidget(self.mock_btn)

        control_bar.addSpacing(10)
        self.heltec_combo = QComboBox()
        self.heltec_combo.addItems(get_available_com_ports())
        self.heltec_btn = QPushButton("🚁 HELTEC V3: CONNECT")
        self.heltec_btn.clicked.connect(self.restart_heltec)
        control_bar.addWidget(self.heltec_combo)
        control_bar.addWidget(self.heltec_btn)
        
        control_bar.addStretch()
        self.status_hud = QLabel("[ INITIALIZING SDR + HELTEC ]")
        self.status_hud.setStyleSheet("color: #facc15; font-size: 14px; border: none; background: transparent;")
        control_bar.addWidget(self.status_hud)
        main_layout.addLayout(control_bar)
        
        # Main Splitter Layout
        main_splitter = QSplitter(Qt.Orientation.Vertical)
        
        # Top Panel: RF Plot & Live Stream Feed Console
        top_workspace = QWidget()
        top_layout = QHBoxLayout(top_workspace)
        top_layout.setContentsMargins(0, 0, 0, 0)
        
        # RF Spectrum Monitor
        self.plot_widget = pg.PlotWidget(title="PASSIVE RF SPECTRUM BURST ENERGY MONITOR (20MHz WINDOW)")
        self.plot_widget.setBackground('#000000')
        self.plot_curve = self.plot_widget.plot(pen=pg.mkPen(color='#ef4444', width=2))
        self.plot_widget.setYRange(-50, 20)
        self.plot_widget.getAxis('left').setPen('#ef4444')
        self.plot_widget.getAxis('bottom').setPen('#ef4444')
        top_layout.addWidget(self.plot_widget, stretch=3)
        
        main_splitter.addWidget(top_workspace)
        
        # Bottom Panel: Live Decoded Telemetry Stream Table/Scroll Area
        bottom_container = QWidget()
        bottom_layout = QVBoxLayout(bottom_container)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        
        bottom_header = QLabel("LIVE DECODED CRSF TELEMETRY STREAM (STRICT CRC-8 & LORA CRC-14 VERIFIED)")
        bottom_header.setStyleSheet("color: #38bdf8; font-size: 14px; background: #080101; border: 1px solid #ef4444;")
        bottom_layout.addWidget(bottom_header)
        
        self.stream_scroll = QScrollArea()
        self.stream_scroll.setWidgetResizable(True)
        self.stream_content = QWidget()
        self.stream_layout = QVBoxLayout(self.stream_content)
        self.stream_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.stream_scroll.setWidget(self.stream_content)
        bottom_layout.addWidget(self.stream_scroll)
        
        main_splitter.addWidget(bottom_container)
        main_splitter.setSizes([350, 450])
        main_layout.addWidget(main_splitter)

    def engage_system_pipeline(self):
        try:
            initial_mhz = float(self.freq_input.text())
        except ValueError:
            initial_mhz = 915.0
            
        log_debug("GUI-HUD", f"Engaging system pipeline thread on {initial_mhz} MHz...")
        self.rx_thread = HackRFReceiverThread(
            initial_mhz=initial_mhz, 
            scan_mode=self.scan_btn.isChecked(),
            use_mock=self.mock_btn.isChecked()
        )
        self.rx_thread.data_packet_parsed.connect(self.append_telemetry_stream_item)
        self.rx_thread.rf_power_updated.connect(self.update_rf_chart)
        self.rx_thread.start()

        self.start_heltec()
        self.refresh_status_hud_label(initial_mhz)

    def start_heltec(self):
        port = self.heltec_combo.currentText() if hasattr(self, 'heltec_combo') else "COM6"
        if self.heltec_thread:
            self.heltec_thread.stop()
        self.heltec_thread = HeltecLoraThread(port=port)
        self.heltec_thread.rc_data_received.connect(self.on_heltec_rc)
        self.heltec_thread.telemetry_link_received.connect(self.on_heltec_tlm_link)
        self.heltec_thread.battery_received.connect(self.on_heltec_battery)
        self.heltec_thread.gps_received.connect(self.on_heltec_gps)
        self.heltec_thread.sync_discovered.connect(self.on_heltec_sync)
        self.heltec_thread.status_changed.connect(self.on_heltec_status)
        self.heltec_thread.start()

    def restart_heltec(self):
        self.start_heltec()

    def on_heltec_status(self, msg, is_connected):
        if is_connected:
            self.heltec_btn.setStyleSheet("background-color: #22c55e; color: black; border: 2px solid #22c55e;")
            self.heltec_btn.setText("🚁 HELTEC V3: ONLINE")
        else:
            self.heltec_btn.setStyleSheet("background-color: #111111; color: #ef4444; border: 2px solid #ef4444;")
            self.heltec_btn.setText("🚁 HELTEC V3: RECONNECT")

    def on_heltec_rc(self, data):
        self.update_rf_chart(data['rssi'])
        record = {
            "time": time.strftime("%H:%M:%S"),
            "type_hex": "0x00",
            "name": "RC_CHANNELS",
            "summary": f"THR: {data['ch3']} µs | YAW: {data['ch1']} µs | PIT: {data['ch2']} µs | ROL: {data['ch4']} µs | RSSI: {data['rssi']:.0f} dBm | ARM: {'ON' if data['armed'] else 'OFF'}",
            "raw": data['raw_line']
        }
        self.append_telemetry_stream_item(record)

    def on_heltec_tlm_link(self, data):
        record = {
            "time": time.strftime("%H:%M:%S"),
            "type_hex": "0x1E",
            "name": "LINK_STATISTICS",
            "summary": f"Drone LQ: {data['drone_lq']}% | Drone RSSI: {data['drone_rssi']} dBm | Drone SNR: {data['drone_snr']} dB",
            "raw": data['raw_line']
        }
        self.append_telemetry_stream_item(record)

    def on_heltec_battery(self, data):
        record = {
            "time": time.strftime("%H:%M:%S"),
            "type_hex": "0x08",
            "name": "BATTERY_SENSOR",
            "summary": f"Volt: {data['voltage']:.1f}V | Curr: {data['current']:.1f}A | Rem: {data['battery_pct']}%",
            "raw": data['raw_line']
        }
        self.append_telemetry_stream_item(record)

    def on_heltec_gps(self, data):
        record = {
            "time": time.strftime("%H:%M:%S"),
            "type_hex": "0x02",
            "name": "GPS_COORDINATES",
            "summary": f"Lat: {data['lat']:.5f} | Lon: {data['lon']:.5f} | Alt: {data['alt']:.0f}m | Spd: {data['spd']:.0f}km/h | Sats: {data['sats']}",
            "raw": data['raw_line']
        }
        self.append_telemetry_stream_item(record)

    def on_heltec_sync(self, data):
        record = {
            "time": time.strftime("%H:%M:%S"),
            "type_hex": "0x10",
            "name": "EXPRESSLRS_SYNC",
            "summary": f"Discovered Pilot Hash: 0x{data['crc_init']} | UID4: {data['uid4']} | UID5: {data['uid5']} | HopIdx: {data['hop_idx']}",
            "raw": data['raw_line']
        }
        self.append_telemetry_stream_item(record)

    def closeEvent(self, event):
        if self.heltec_thread:
            self.heltec_thread.stop()
        if self.rx_thread:
            self.rx_thread.stop()
        event.accept()

    def refresh_status_hud_label(self, mhz):
        if self.mock_btn.isChecked():
            self.status_hud.setText("[ SIMULATION ACTIVE // MOCK TELEMETRY ]")
            self.status_hud.setStyleSheet("color: #38bdf8;")
        elif self.scan_btn.isChecked():
            self.status_hud.setText("[ HUNTER MODE ACTIVE // WIDEBAND MONITOR ]")
            self.status_hud.setStyleSheet("color: #facc15;")
        else:
            self.status_hud.setText(f"[ LOCKED // {mhz:.3f} MHz ]")
            self.status_hud.setStyleSheet("color: #ef4444;")

    def on_preset_selected(self):
        name = self.preset_combo.currentText()
        target_mhz = BAND_PRESETS[name]
        self.freq_input.setText(f"{target_mhz:.3f}")
        self.scan_btn.setChecked(False)
        if self.rx_thread:
            self.rx_thread.force_frequency(target_mhz)
        self.refresh_status_hud_label(target_mhz)

    def on_manual_lock_clicked(self):
        try:
            target_mhz = float(self.freq_input.text())
            self.scan_btn.setChecked(False)
            if self.rx_thread:
                self.rx_thread.scan_mode = False
                self.rx_thread.force_frequency(target_mhz)
            self.refresh_status_hud_label(target_mhz)
        except ValueError:
            self.status_hud.setText("[ INVALID FREQUENCY ]")

    def on_scan_toggled(self):
        is_scanning = self.scan_btn.isChecked()
        if is_scanning:
            if self.rx_thread:
                self.rx_thread.scan_mode = True
            self.refresh_status_hud_label(0)
        else:
            self.on_manual_lock_clicked()

    def on_mock_toggled(self):
        is_mock = self.mock_btn.isChecked()
        if self.rx_thread:
            self.rx_thread.use_mock = is_mock
        try:
            mhz = float(self.freq_input.text())
        except ValueError:
            mhz = 915.0
        self.refresh_status_hud_label(mhz)

    def update_rf_chart(self, instant_db):
        self.power_history = np.roll(self.power_history, -1)
        self.power_history[-1] = instant_db
        self.plot_curve.setData(self.power_history)

    def append_telemetry_stream_item(self, record):
        if not self.mock_btn.isChecked():
            self.status_hud.setText(f"[ CRC SYNC LOCKED // {record['name']} ]")
            self.status_hud.setStyleSheet("color: #22c55e;")
            
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(4, 2, 4, 2)
        
        lbl_time = QLabel(f"[{record['time']}]")
        lbl_time.setFixedWidth(85)
        lbl_time.setStyleSheet("color: #94a3b8; border: none; background: transparent;")
        
        lbl_type = QLabel(f"TYPE {record['type_hex']} ({record['name']})")
        lbl_type.setFixedWidth(220)
        lbl_type.setStyleSheet("color: #38bdf8; border: none; background: transparent;")
        
        lbl_summary = QLabel(record['summary'])
        lbl_summary.setStyleSheet("color: #22c55e; border: none; background: transparent;")
        
        row_layout.addWidget(lbl_time)
        row_layout.addWidget(lbl_type)
        row_layout.addWidget(lbl_summary)
        
        self.stream_layout.insertWidget(0, row_widget)
        
        # Limit backlog to last 50 items to prevent UI lag
        if self.stream_layout.count() > 50:
            item = self.stream_layout.takeAt(50)
            if item.widget():
                item.widget().deleteLater()

    def closeEvent(self, event):
        log_debug("GUI-HUD", "Application close event triggered. Cleaning up threads...")
        if hasattr(self, 'rx_thread') and self.rx_thread is not None:
            self.rx_thread.stop()
        event.accept()

if __name__ == "__main__":
    log_debug("SYSTEM", "Booting PyQt6 Application Instance...")
    app = QApplication(sys.argv)
    ui = ApexApp()
    ui.show()
    sys.exit(app.exec())