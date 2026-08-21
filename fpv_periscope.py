import sys
import os
import time
import subprocess
import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QComboBox, QPushButton)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

FPV_CHANNELS = {
    "R1": 5658, "R2": 5695, "R3": 5732, "R4": 5769, "R5": 5806, "R6": 5843, "R7": 5880, "R8": 5917,
    "F1": 5740, "F2": 5760, "F3": 5780, "F4": 5800, "F5": 5820, "F6": 5840, "F7": 5860, "F8": 5880,
    "A1": 5865, "A2": 5845, "A3": 5825, "A4": 5805, "A5": 5785, "A6": 5765, "A7": 5745, "A8": 5725,
    "B1": 5733, "B2": 5752, "B3": 5771, "B4": 5790, "B5": 5809, "B6": 5828, "B7": 5847, "B8": 5866,
    "E1": 5705, "E2": 5685, "E3": 5665, "E4": 5645, "E5": 5885, "E6": 5905, "E7": 5925, "E8": 5945,
    "LR1": 1080, "LR2": 1120, "LR3": 1160, "LR4": 1200, "LR5": 1240, "LR6": 1280, "LR7": 1320, "LR8": 1360
}

class HackRFSweepThread(QThread):
    signal_found = pyqtSignal(int)
    
    def __init__(self):
        super().__init__()
        self.running = True
        self.process = None

    def run(self):
        cmd = [
            "hackrf_sweep",
            "-f", "1080:1360",
            "-f", "5600:6000",
            "-w", "1000000",
            "-a", "1", "-l", "32", "-g", "40"
        ]
        
        self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        powers = {}
        
        while self.running:
            line = self.process.stdout.readline()
            if not line:
                break
                
            parts = line.strip().split(', ')
            if len(parts) < 7:
                continue
                
            try:
                hz_low = int(parts[2])
                hz_bin_width = float(parts[4])
                db_values = [float(x) for x in parts[6:]]
                
                freq_mhz = hz_low / 1e6
                for i, db in enumerate(db_values):
                    bin_freq = freq_mhz + i * (hz_bin_width / 1e6)
                    powers[bin_freq] = db
                    
                if len(powers) > 400: 
                    median_db = np.median(list(powers.values()))
                    for chan_freq in FPV_CHANNELS.values():
                        chan_bins = [db for f, db in powers.items() if abs(f - chan_freq) <= 2.0]
                        if chan_bins:
                            max_chan_db = max(chan_bins)
                            if max_chan_db > median_db + 30:
                                self.signal_found.emit(chan_freq)
                                time.sleep(1.0)
                                break
                    powers.clear()
            except ValueError:
                continue

    def stop(self):
        self.running = False
        if self.process:
            self.process.kill()
        self.wait()

class HackRFVideoThread(QThread):
    frame_ready = pyqtSignal(np.ndarray, float)
    
    def __init__(self, freq_mhz):
        super().__init__()
        self.freq_mhz = freq_mhz
        self.running = True
        self.process = None

    def run(self):
        cmd = [
            "hackrf_transfer",
            "-f", str(int(self.freq_mhz * 1e6)),
            "-s", "20000000",
            "-a", "1", "-l", "32", "-g", "40",
            "-r", "-"
        ]
        
        self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=20000000)
        BLOCK_SIZE = 2000000
        
        while self.running:
            raw_data = self.process.stdout.read(BLOCK_SIZE * 2)
            if not raw_data or len(raw_data) < BLOCK_SIZE * 2:
                continue
                
            data = np.frombuffer(raw_data, dtype=np.int8).astype(np.float32)
            iq = data[0::2] + 1j * data[1::2]
            
            fm_demod = np.diff(np.unwrap(np.angle(iq)))
            
            LINE_LEN = 1270
            fm_demod -= np.mean(fm_demod)
            num_lines = len(fm_demod) // LINE_LEN
            chunks = fm_demod[:num_lines * LINE_LEN].reshape((num_lines, LINE_LEN))
            
            sync_indices = np.argmin(chunks, axis=1)
            sync_std = float(np.std(sync_indices))
            abs_sync_indices = np.arange(num_lines) * LINE_LEN + sync_indices
            
            valid_syncs = abs_sync_indices[abs_sync_indices + LINE_LEN < len(fm_demod)]
            
            if len(valid_syncs) > 50:
                line_indices = valid_syncs[:, None] + np.arange(LINE_LEN)
                image_lines = fm_demod[line_indices]
                self.frame_ready.emit(image_lines, sync_std)
            else:
                self.frame_ready.emit(chunks, sync_std)

    def stop(self):
        self.running = False
        if self.process:
            self.process.kill()
        self.wait()

class PeriscopeApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PERISCOPE // COUNTER-UAS VIDEO INTERCEPTOR")
        self.resize(1024, 768)
        self.vid_thread = None
        self.sweep_thread = None
        self.is_scanning = False
        self.dwell_frames = 0
        
        self.setup_ui()
        r5_index = self.chan_selector.findData(5806)
        if r5_index >= 0:
            self.chan_selector.setCurrentIndex(r5_index)
        self.switch_channel(5806)

    def setup_ui(self):
        self.setStyleSheet("""
            QWidget { background-color: #000000; color: #ef4444; font-family: 'Consolas', monospace; font-weight: bold;}
            QLabel { font-size: 16px; }
            QComboBox { background-color: #111111; color: #ef4444; border: 1px solid #ef4444; padding: 6px; font-size: 14px; }
            QPushButton { background-color: #111111; color: #ef4444; border: 2px solid #ef4444; padding: 8px 16px; font-size: 14px; border-radius: 4px; }
            QPushButton:hover { background-color: #ef4444; color: black; }
            QPushButton:checked { background-color: #ef4444; color: black; }
        """)
        
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        top_bar = QWidget()
        top_layout = QHBoxLayout(top_bar)
        
        self.status_label = QLabel("[ SEEKING SIGNAL ]")
        self.status_label.setStyleSheet("color: #facc15; font-size: 20px; font-weight: bold;")
        
        self.chan_selector = QComboBox()
        for chan, freq in FPV_CHANNELS.items():
            self.chan_selector.addItem(f"{chan} ({freq} MHz)", freq)
            
        self.chan_selector.currentTextChanged.connect(self.on_chan_change)
        
        self.scan_btn = QPushButton("HUNTER-KILLER SCAN")
        self.scan_btn.setCheckable(True)
        self.scan_btn.clicked.connect(self.toggle_scan)
        
        top_layout.addWidget(QLabel("TARGET BAND:"))
        top_layout.addWidget(self.chan_selector)
        top_layout.addWidget(self.scan_btn)
        top_layout.addStretch()
        top_layout.addWidget(self.status_label)
        
        main_layout.addWidget(top_bar)
        
        pg.setConfigOptions(antialias=False)
        pg.setConfigOption('background', '#000000')
        self.video_plot = pg.PlotWidget()
        self.video_plot.setAspectLocked(True)
        self.video_plot.invertY(True)
        self.video_plot.hideAxis('bottom')
        self.video_plot.hideAxis('left')
        
        self.video_image = pg.ImageItem()
        self.video_plot.addItem(self.video_image)
        
        pos = np.array([0.0, 1.0])
        color = np.array([[0,0,0,255], [255,255,255,255]], dtype=np.ubyte)
        cmap = pg.ColorMap(pos, color)
        self.video_image.setLookupTable(cmap.getLookupTable())
        
        main_layout.addWidget(self.video_plot)

    def on_chan_change(self):
        if not self.is_scanning:
            freq = self.chan_selector.currentData()
            if freq:
                self.switch_channel(freq)

    def toggle_scan(self):
        self.is_scanning = self.scan_btn.isChecked()
        if self.is_scanning:
            self.scan_btn.setText("STOP SCANNING")
            self.start_sweep()
        else:
            self.scan_btn.setText("HUNTER-KILLER SCAN")
            if self.sweep_thread:
                self.sweep_thread.stop()
                self.sweep_thread = None
            self.on_chan_change()

    def start_sweep(self):
        self.status_label.setText("[ HUNTER: WIDEBAND SWEEP ]")
        self.status_label.setStyleSheet("color: #ef4444; font-size: 20px; font-weight: bold;")
        if self.vid_thread:
            self.vid_thread.stop()
            self.vid_thread = None
            
        self.sweep_thread = HackRFSweepThread()
        self.sweep_thread.signal_found.connect(self.on_sweep_hit)
        self.sweep_thread.start()

    def on_sweep_hit(self, freq_mhz):
        if self.sweep_thread:
            self.sweep_thread.stop()
            self.sweep_thread = None
            
        self.status_label.setText(f"[ KILLER: VERIFYING {freq_mhz} MHz ]")
        self.status_label.setStyleSheet("color: #facc15; font-size: 20px; font-weight: bold;")
        
        self.chan_selector.blockSignals(True)
        index = self.chan_selector.findData(freq_mhz)
        if index >= 0:
            self.chan_selector.setCurrentIndex(index)
        self.chan_selector.blockSignals(False)
        
        self.dwell_frames = 0
        self.vid_thread = HackRFVideoThread(freq_mhz)
        self.vid_thread.frame_ready.connect(self.update_video)
        self.vid_thread.start()

    def update_video(self, frame_data, sync_std):
        self.video_image.setImage(frame_data.T, autoLevels=True)
        
        if self.is_scanning:
            if sync_std < 100: 
                self.scan_btn.setChecked(False)
                self.is_scanning = False
                self.scan_btn.setText("HUNTER-KILLER SCAN")
                self.status_label.setText(f"[ TARGET LOCKED | {self.chan_selector.currentText()} ]")
                self.status_label.setStyleSheet("color: #22c55e; font-size: 20px; font-weight: bold;")
            else:
                self.dwell_frames += 1
                if self.dwell_frames > 10:
                    self.start_sweep()
        else:
            if sync_std < 100:
                self.status_label.setText(f"[ TARGET LOCKED | {self.chan_selector.currentText()} ]")
                self.status_label.setStyleSheet("color: #22c55e; font-size: 20px; font-weight: bold;")
            else:
                self.status_label.setText("[ STATIC / NO SYNC ]")
                self.status_label.setStyleSheet("color: #ef4444; font-size: 20px; font-weight: bold;")

    def switch_channel(self, freq_mhz):
        if self.sweep_thread:
            self.sweep_thread.stop()
            self.sweep_thread = None
            
        if self.vid_thread:
            self.vid_thread.stop()
            
        self.status_label.setText(f"[ TUNING {freq_mhz} MHz ]")
        self.status_label.setStyleSheet("color: #facc15; font-size: 20px; font-weight: bold;")
        
        self.vid_thread = HackRFVideoThread(freq_mhz)
        self.vid_thread.frame_ready.connect(self.update_video)
        self.vid_thread.start()

    def closeEvent(self, event):
        if self.vid_thread:
            self.vid_thread.stop()
        if self.sweep_thread:
            self.sweep_thread.stop()
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    win = PeriscopeApp()
    win.show()
    sys.exit(app.exec())