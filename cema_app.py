import sys
import time
import json
import os
import subprocess
import threading
import datetime
import queue
from collections import deque
import numpy as np
import scipy.signal
import sounddevice as sd
import cv2
import pyqtgraph as pg
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                             QComboBox, QSpinBox, QDoubleSpinBox, QGridLayout, QGroupBox, QSlider, 
                             QTextEdit, QListWidget, QListWidgetItem, QTabWidget, QTreeWidget, 
                             QTreeWidgetItem, QSplitter, QProgressBar, QFrame, QSizePolicy, QMenu, QInputDialog, QCheckBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QTimer, QPointF, QRectF
from PyQt6.QtGui import QPainter, QPen, QBrush, QColor, QFont, QPolygonF, QImage, QPixmap
from PyQt6.QtWebEngineWidgets import QWebEngineView
from heltec_bridge import HeltecLoraThread, get_available_com_ports

# --- DSP Functions ---
def detect_peaks(fft_data, additive_threshold=20.0, min_distance=10):
    noise_floor = np.median(fft_data)
    threshold = noise_floor + additive_threshold
    peaks = []
    for i in range(1, len(fft_data) - 1):
        if fft_data[i] > threshold and fft_data[i] > fft_data[i-1] and fft_data[i] > fft_data[i+1]:
            if not peaks or (i - peaks[-1]) >= min_distance:
                peaks.append(i)
            elif fft_data[i] > fft_data[peaks[-1]]:
                peaks[-1] = i
    return peaks

def classify_modulation(iq_complex, avg_cva, avg_mag):
    if avg_mag < 6:
        return "Noise/Quiet", 0.95, avg_cva
    cva = avg_cva
    if cva < 0.45:
        return "FM/FSK/CW", 0.85, cva
    elif cva < 0.65:
        return "QAM/Digital", 0.65, cva
    elif cva < 0.85:
        return "AM/Analog", 0.60, cva
    else:
        return "Wideband/Impulsive", 0.40, cva

# --- HackRF Stare Thread (with Intelligence logic) ---
class HackRFThread(QThread):
    error_signal = pyqtSignal(str)

    def __init__(self, freq_hz, lna, vga):
        super().__init__()
        self.freq_hz = freq_hz
        self.lna = lna
        self.vga = vga
        self.process = None
        self.running = False
        self.latest_data = None
        self.last_process_time = 0
        self.last_high_snr_time = 0
        self.start_time = time.time()
        self.active_blocks = 0
        self.last_pulse_duration = 0.0
        self.duty_window = deque(maxlen=1000)
        self.current_duty_cycle = 0.0
        self.audio_buffer = deque(maxlen=1024)
        self.ctcss_detected = False
        self.vfo_offset_hz = 0.0
        self.phase_acc = 0.0
        
        self.decode_video = False
        self.video_q = queue.Queue(maxsize=3)
        self.video_buffer = []
        self.video_buffer_len = 0
        
        self.play_audio = False
        self.audio_q = queue.Queue(maxsize=2000)
        
        def audio_callback(outdata, frames, time_info, status):
            chunk = np.zeros(frames, dtype=np.float32)
            idx = 0
            while idx < frames:
                if not hasattr(self, 'audio_rem'):
                    self.audio_rem = np.array([], dtype=np.float32)
                    
                if len(self.audio_rem) == 0:
                    try:
                        self.audio_rem = self.audio_q.get_nowait()
                    except queue.Empty:
                        break
                        
                take = min(frames - idx, len(self.audio_rem))
                chunk[idx:idx+take] = self.audio_rem[:take]
                self.audio_rem = self.audio_rem[take:]
                idx += take
                
            outdata[:, 0] = chunk

        self.audio_callback = audio_callback
        self.running = True
        self.last_process_time = time.time()
        self.start_time = time.time()
        self.last_high_snr_time = time.time()

    def run(self):
        self.audio_stream = sd.OutputStream(device=3, samplerate=50000, channels=1, dtype='float32', callback=self.audio_callback)
        cmd = [
            "hackrf_transfer",
            "-r", "-",
            "-f", str(self.freq_hz),
            "-s", "20000000",
            "-l", str(self.lna),
            "-g", str(self.vga),
            "-a", "1"
        ]
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, creationflags=creationflags)
        except FileNotFoundError:
            self.error_signal.emit("hackrf_transfer not found in PATH")
            return

        BLOCK_SIZE = 32768
        ALPHA_FFT = 0.4
        ALPHA_MOD = 0.02
        fft_avg = None
        fft_max = None
        cva_avg = 0.5
        
        signal_active = False
        fingerprint = None
        bw_estimate = 0
        BLOCK_SIZE = 32000
        acc_buf = bytearray()
        
        while self.running:
            chunk = self.process.stdout.read(BLOCK_SIZE - len(acc_buf))
            if not chunk:
                time.sleep(0.01)
                continue
                
            acc_buf.extend(chunk)
            if len(acc_buf) < BLOCK_SIZE:
                continue
                
            raw_block = bytes(acc_buf[:BLOCK_SIZE])
            del acc_buf[:BLOCK_SIZE]
            
            # MICRO-TIMING ENGINE (Runs on every block at ~600Hz, decoupled from UI)
            if True: # Kept for indentation
                block_data = np.frombuffer(raw_block, dtype=np.int8).astype(np.float32)
                i_b = block_data[0::2]
                q_b = block_data[1::2]
                i_b = i_b - np.mean(i_b)
                q_b = q_b - np.mean(q_b)
                block_energy = np.mean(i_b**2 + q_b**2)
                
                # CTCSS Sub-audible Extraction (Ultra-fast FM Demodulation decimation)
                iq_b = i_b + 1j * q_b
                cross_corr_20mhz = iq_b[1:] * np.conj(iq_b[:-1])
                block_audio = np.angle(np.mean(cross_corr_20mhz))
                self.audio_buffer.append(block_audio)
                

                
                if self.play_audio:
                    # 0. Apply VFO Digital Down-Conversion (Shift target frequency to DC)
                    if self.vfo_offset_hz != 0.0:
                        phase_increment = -2 * np.pi * self.vfo_offset_hz / 20000000.0
                        phase_array = self.phase_acc + np.arange(len(i_b)) * phase_increment
                        self.phase_acc = (phase_array[-1] + phase_increment) % (2 * np.pi)
                        shift_vector = np.exp(1j * phase_array)
                        iq_shifted = (i_b + 1j * q_b) * shift_vector
                    else:
                        self.phase_acc = 0.0
                        iq_shifted = i_b + 1j * q_b

                    # 1. RF Low-Pass Filter (Decimate I/Q by 20 -> 1 MHz SR)
                    # This drops 95% of the 20MHz static, but keeps enough Nyquist headroom to prevent phase-aliasing
                    iq_dec = np.mean(iq_shifted.reshape(-1, 20), axis=1)
                    
                    if not hasattr(self, 'last_iq'):
                        self.last_iq = iq_dec[0]
                    
                    # Prevent phase jumps between blocks
                    iq_concat = np.concatenate(([self.last_iq], iq_dec))
                    self.last_iq = iq_dec[-1]
                    
                    # 2. FM Demodulation (1 MHz intermediate sample rate)
                    fm_audio = np.angle(iq_concat[1:] * np.conj(iq_concat[:-1]))
                    
                    # 3. Audio Decimation (Decimate by 20 -> 50 kHz audio)
                    # 75kHz deviation at 1MHz = 0.47 radians max. Multiply by 2.0 to normalize.
                    decimated = np.clip(np.mean(fm_audio.reshape(-1, 20), axis=1) * 2.0, -1.0, 1.0)
                    
                    if not self.audio_q.full():
                        self.audio_q.put(decimated)
                        
                    if not hasattr(self, 'debug_blocks'): self.debug_blocks = 0
                    self.debug_blocks += 1
                    
                    if self.debug_blocks % 200 == 0:
                        with open("audio_debug.txt", "a") as f:
                            f.write(f"qsize={self.audio_q.qsize()}, active={self.audio_stream.active}\n")
                        
                    # Jitter Buffer: Wait until we have 50 blocks (~40ms of audio) before starting the stream
                    if not self.audio_stream.active and self.audio_q.qsize() > 50:
                        try:
                            self.audio_stream.start()
                            with open("audio_debug.txt", "a") as f:
                                f.write("STREAM STARTED SUCCESSFULLY\n")
                        except Exception as e:
                            with open("audio_debug.txt", "a") as f:
                                f.write(f"STREAM START FAILED: {e}\n")
                
                if not hasattr(self, 'bg_energy'):
                    self.bg_energy = block_energy
                else:
                    # Very slow moving average for dynamic noise floor
                    self.bg_energy = (0.99 * self.bg_energy) + (0.01 * block_energy)
                    
                if self.decode_video:
                    fm_demod = np.angle(iq_b[1:] * np.conj(iq_b[:-1]))
                    self.video_buffer.append(fm_demod)
                    self.video_buffer_len += len(fm_demod)

                if block_energy > (self.bg_energy + 30.0):
                    self.active_blocks += 1
                    self.duty_window.append(1)
                else:
                    self.duty_window.append(0)
                    if self.active_blocks > 0:
                        # 16384 samples at 20MS/s = 0.8192 ms per block
                        duration_ms = self.active_blocks * 0.8192
                        if duration_ms > 1.5:  # Filter out random micro-static
                            self.last_pulse_duration = duration_ms
                        self.active_blocks = 0
                        
                        # --- DYNAMIC PYTHON PLL H-SYNC ALIGNMENT ---
                        if self.decode_video and len(self.video_buffer) > 0:
                            raw_1d_video = np.concatenate(self.video_buffer)
                            
                            # CRITICAL: Remove DC Bias to center the FM deviation
                            raw_1d_video = raw_1d_video - np.mean(raw_1d_video)
                            
                            # 1. Fast Low Pass Filter (Smooths FM static)
                            kernel = np.ones(20) / 20.0
                            smoothed_video = np.convolve(raw_1d_video, kernel, mode='same')
                            
                            # 2. Dynamic Hardware Drift Correction via SciPy
                            sync_indices, _ = scipy.signal.find_peaks(
                                -smoothed_video, 
                                distance=1200, 
                                prominence=0.8
                            )
                            
                            # Target line length for PAL at 20 MSPS
                            LINE_LEN = 1280  
                            
                            # 3. Dynamically slice the raw video line-by-line
                            valid_lines = []
                            for idx in sync_indices:
                                if idx + LINE_LEN < len(raw_1d_video):
                                    valid_lines.append(raw_1d_video[idx : idx + LINE_LEN])
                                    
                            # 4. Construct the frame
                            if len(valid_lines) > 50:
                                aligned_frame = np.array(valid_lines)
                                clamped_frame = np.clip(aligned_frame, -2.5, 2.5)
                                normalized = cv2.normalize(clamped_frame, None, 0, 255, cv2.NORM_MINMAX)
                                frame_uint8 = np.uint8(normalized)
                                
                                if not self.video_q.full():
                                    self.video_q.put(frame_uint8)
                                    
                            # Flush buffers for the next frame chunk
                            self.video_buffer = []
                            self.video_buffer_len = 0

            current_time = time.time()
            if current_time - self.last_process_time < 0.04:
                continue

            chunk = raw_block[:2048]
            if len(chunk) != 2048:
                continue

            data = np.frombuffer(chunk, dtype=np.int8).astype(np.float32)
            i_coords = data[0::2]
            q_coords = data[1::2]
            if len(i_coords) != len(q_coords) or len(i_coords) == 0:
                continue

            i_coords = i_coords - np.mean(i_coords)
            q_coords = q_coords - np.mean(q_coords)
            iq_complex = i_coords + 1j * q_coords

            window = np.hanning(len(iq_complex))
            windowed_iq = iq_complex * window

            fft_result = np.fft.fftshift(np.fft.fft(windowed_iq))
            magnitude = np.abs(fft_result) / len(windowed_iq)

            if fft_avg is None:
                fft_avg = magnitude
                fft_max = magnitude
            else:
                fft_avg = (ALPHA_FFT * magnitude) + ((1 - ALPHA_FFT) * fft_avg)
                fft_max = np.maximum(fft_max * 0.995, magnitude)

            const_i = i_coords[:100]
            const_q = q_coords[:100]

            N = 8
            if len(i_coords) >= N:
                filt_i = np.convolve(i_coords, np.ones(N)/N, mode='valid')
                filt_q = np.convolve(q_coords, np.ones(N)/N, mode='valid')
                filt_iq = filt_i + 1j * filt_q
                current_mag = np.abs(filt_iq)
                current_cva = np.std(current_mag) / np.mean(current_mag) if np.mean(current_mag) > 0 else 1.0
                cva_avg = (ALPHA_MOD * current_cva) + ((1 - ALPHA_MOD) * cva_avg)
                avg_mag = np.mean(current_mag)
                mod_type, confidence, _ = classify_modulation(iq_complex, cva_avg, avg_mag)
            else:
                mod_type = "UNKNOWN"
                avg_mag = 0
                
            # 1. PTT Transient Fingerprinting
            noise_floor = np.mean(magnitude)
            peak_val = np.max(magnitude)
            current_time = time.time()
            
            if (peak_val - noise_floor) > 12.0:
                self.last_high_snr_time = current_time
                if not signal_active:
                    signal_active = True
                    # If the signal was already active when we tuned to this frequency, it is a continuous signal (like FM radio)
                    # We ignore it. We only fingerprint NEW bursts (tactical PTT) that start after we've been staring.
                    if (current_time - self.start_time) > 1.5:
                        # Rising-Edge Trigger: Find the exact microsecond the signal spikes
                        over_thresh = np.where(magnitude > (noise_floor + 10.0))[0]
                        trigger_idx = over_thresh[0] if len(over_thresh) > 0 else 0
                        
                        end_idx = min(len(iq_complex), trigger_idx + 128)
                        transient_samples = iq_complex[trigger_idx:end_idx]
                        if len(transient_samples) < 128:
                            transient_samples = np.pad(transient_samples, (0, 128 - len(transient_samples)))
                            
                        transient_fft = np.abs(np.fft.fft(transient_samples))
                        top_bins = np.argsort(transient_fft)[-3:]
                        fingerprint = f"{top_bins[2]:02X}{top_bins[1]:02X}{top_bins[0]:02X}"
                    else:
                        fingerprint = None
            else:
                # Temporal Debounce: Wait 1.5 seconds of dead silence before dropping signal_active
                # This prevents FM radio fading or dead-air from triggering dozens of false "new" fingerprints
                if signal_active and (current_time - self.last_high_snr_time) > 1.5:
                    signal_active = False
                    fingerprint = None
                
            # 2. Bandwidth Estimation
            if signal_active:
                noise_floor = np.median(fft_avg)
                active_bins = np.sum(fft_avg > noise_floor * 2.0)
                bw_estimate = active_bins * (20000000 / 1024)
            else:
                bw_estimate = 0
                
            # 3. Watchlist Target Matching (Moved to Main UI Thread)
            peaks = detect_peaks(fft_avg, additive_threshold=20.0, min_distance=15)
            
            # Analog Video Subcarrier Detection (5.8 GHz FPVs)
            if len(peaks) >= 2 and bw_estimate > 5000000:
                for p1 in peaks:
                    for p2 in peaks:
                        if p1 != p2:
                            dist = abs(p1 - p2)
                            if (304 <= dist <= 310) or (330 <= dist <= 336):
                                mod_type = " FPV ANALOG VIDEO"
                                break

            if len(self.duty_window) > 100:
                self.current_duty_cycle = (sum(self.duty_window) / len(self.duty_window)) * 100.0

            if len(self.audio_buffer) == 1024 and self.current_duty_cycle > 10.0:
                audio_fft = np.abs(np.fft.rfft(self.audio_buffer))
                # 1220 Hz sample rate. Bins 56:214 correspond exactly to 67 Hz - 254 Hz (Standard CTCSS)
                ctcss_band = audio_fft[56:214]
                bg_band = np.concatenate((audio_fft[10:50], audio_fft[220:400]))
                if np.max(ctcss_band) > (np.mean(bg_band) * 4.0):
                    self.ctcss_detected = True
                else:
                    self.ctcss_detected = False
            else:
                self.ctcss_detected = False

            self.latest_data = {
                "mode": "STARE",
                "fft": fft_avg,
                "fft_max": fft_max,
                "const_i": const_i,
                "const_q": const_q,
                "mod_type": mod_type,
                "peaks": peaks,
                "fingerprint": fingerprint,
                "bw": bw_estimate,
                "peak_power": float(peak_val),
                "pulse_ms": self.last_pulse_duration,
                "duty_cycle": self.current_duty_cycle,
                "ctcss": self.ctcss_detected
            }
            self.last_process_time = time.time()

        if self.process:
            self.process.terminate()
            
        if hasattr(self, 'audio_stream'):
            try:
                self.audio_stream.stop()
                self.audio_stream.close()
            except:
                pass

    def stop(self):
        self.running = False


# --- HackRF Sweep Thread ---
class HackRFSweepThread(QThread):
    error_signal = pyqtSignal(str)

    def __init__(self, start_hz, end_hz, lna, vga):
        super().__init__()
        self.start_hz = start_hz
        self.end_hz = end_hz
        self.lna = lna
        self.vga = vga
        self.bin_width = 1000000 # 1 MHz
        self.num_bins = int((end_hz - start_hz) / self.bin_width)
        if self.num_bins <= 0:
            self.num_bins = 1
            
        self.sweep_data = np.zeros(self.num_bins)
        self.sweep_max = np.zeros(self.num_bins)
        self.start_time = time.time()
        self.last_high_snr_time = time.time()
        
        self.running = True
        self.process = None
        self.latest_data = None

    def run(self):
        cmd = [
            "hackrf_sweep",
            "-f", f"{int(self.start_hz/1e6)}:{int(self.end_hz/1e6)}",
            "-w", str(self.bin_width),
            "-l", str(self.lna),
            "-g", str(self.vga)
        ]
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, creationflags=creationflags)
        except FileNotFoundError:
            self.error_signal.emit("hackrf_sweep not found in PATH")
            return

        while self.running:
            line = self.process.stdout.readline()
            if not line:
                break
                
            if line.startswith("202"):
                parts = line.split(", ")
                if len(parts) >= 7:
                    try:
                        hz_low = int(parts[2])
                        dbs = [float(x) for x in parts[6:]]
                        
                        start_idx = int((hz_low - self.start_hz) / self.bin_width)
                        if 0 <= start_idx < self.num_bins:
                            for i, db in enumerate(dbs):
                                if start_idx + i < self.num_bins:
                                    val = max(0, db + 100) 
                                    self.sweep_data[start_idx + i] = val
                                    self.sweep_max[start_idx + i] = max(self.sweep_max[start_idx + i] * 0.95, val)
                                    
                        self.latest_data = {
                            "mode": "SWEEP",
                            "fft": self.sweep_data.copy(),
                            "fft_max": self.sweep_max.copy(),
                            "const_i": [],
                            "const_q": [],
                            "mod_type": "SWEEPING...",
                            "peaks": [],
                            "fingerprint": None,
                            "bw": 0
                        }
                    except ValueError:
                        pass

        if self.process:
            self.process.terminate()

# --- FPV Video Decoding & Stream Bridge Engine ---
FPV_VIDEO_CHANNELS = {
    "RaceBand R1 (5658 MHz)": 5658,
    "RaceBand R2 (5695 MHz)": 5695,
    "RaceBand R3 (5732 MHz)": 5732,
    "RaceBand R4 (5769 MHz)": 5769,
    "RaceBand R5 (5806 MHz)": 5806,
    "RaceBand R6 (5843 MHz)": 5843,
    "RaceBand R7 (5880 MHz)": 5880,
    "RaceBand R8 (5917 MHz)": 5917,
    "FatShark F1 (5740 MHz)": 5740,
    "FatShark F2 (5760 MHz)": 5760,
    "FatShark F3 (5780 MHz)": 5780,
    "FatShark F4 (5800 MHz)": 5800,
    "FatShark F5 (5820 MHz)": 5820,
    "FatShark F6 (5840 MHz)": 5840,
    "FatShark F7 (5860 MHz)": 5860,
    "FatShark F8 (5880 MHz)": 5880,
    "Band A - A1 (5865 MHz)": 5865,
    "Band A - A2 (5845 MHz)": 5845,
    "Band A - A3 (5825 MHz)": 5825,
    "Band A - A4 (5805 MHz)": 5805,
    "Band A - A5 (5785 MHz)": 5785,
    "Band A - A6 (5765 MHz)": 5765,
    "Band A - A7 (5745 MHz)": 5745,
    "Band A - A8 (5725 MHz)": 5725,
    "Band B - B1 (5733 MHz)": 5733,
    "Band B - B2 (5752 MHz)": 5752,
    "Band B - B3 (5771 MHz)": 5771,
    "Band B - B4 (5790 MHz)": 5790,
    "Band B - B5 (5809 MHz)": 5809,
    "Band B - B6 (5828 MHz)": 5828,
    "Band B - B7 (5847 MHz)": 5847,
    "Band B - B8 (5866 MHz)": 5866,
    "Band E - E1 (5705 MHz)": 5705,
    "Band E - E2 (5685 MHz)": 5685,
    "Band E - E3 (5665 MHz)": 5665,
    "Band E - E4 (5645 MHz)": 5645,
    "Band E - E5 (5885 MHz)": 5885,
    "Band E - E6 (5905 MHz)": 5905,
    "Band E - E7 (5925 MHz)": 5925,
    "Band E - E8 (5945 MHz)": 5945,
    "1.2GHz - CH1 (1080 MHz)": 1080,
    "1.2GHz - CH2 (1120 MHz)": 1120,
    "1.2GHz - CH3 (1160 MHz)": 1160,
    "1.2GHz - CH4 (1200 MHz)": 1200,
    "1.2GHz - CH5 (1240 MHz)": 1240,
    "1.2GHz - CH6 (1280 MHz)": 1280,
    "1.2GHz - CH7 (1320 MHz)": 1320,
    "1.2GHz - CH8 (1360 MHz)": 1360,
}

class VideoDisplayWidget(QWidget):
    """
    High-Performance Tactical CRT / FPV Video Display Widget.
    Renders analog PAL/NTSC frames or RTSP/UDP streams with tactical OSD overlay.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.current_pixmap = None
        self.osd_channel = "5806 MHz (R5)"
        self.osd_mode = "NATIVE HACKRF (PAL 64.0µs)"
        self.osd_status = "SEEKING CARRIER"
        self.osd_fps = 0.0
        self.show_osd = True
        self.show_reticle = False
        self.setStyleSheet("background-color: #050811; border: 1px solid #1e293b; border-radius: 6px;")

    def update_frame(self, qimage, sync_locked=True, fps=0.0):
        if qimage and not qimage.isNull():
            self.current_pixmap = QPixmap.fromImage(qimage)
            self.osd_status = "LOCKED (PAL 64.0µs)" if sync_locked else "CARRIER DETECTED"
            self.osd_fps = fps
            self.update()

    def set_stream_info(self, channel_str, mode_str):
        self.osd_channel = channel_str
        self.osd_mode = mode_str
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        w = self.width()
        h = self.height()
        
        # Fill background
        painter.fillRect(0, 0, w, h, QColor("#050811"))
        
        if self.current_pixmap and not self.current_pixmap.isNull():
            # Draw video frame preserving 4:3 aspect ratio
            scaled_pixmap = self.current_pixmap.scaled(
                w, h, 
                Qt.AspectRatioMode.KeepAspectRatio, 
                Qt.TransformationMode.FastTransformation
            )
            pw = scaled_pixmap.width()
            ph = scaled_pixmap.height()
            px = (w - pw) // 2
            py = (h - ph) // 2
            painter.drawPixmap(px, py, scaled_pixmap)
        else:
            # Standby / Static Grid Screen
            painter.setPen(QPen(QColor("#1e293b"), 1, Qt.PenStyle.DotLine))
            grid_step = 30
            for gx in range(0, w, grid_step):
                painter.drawLine(gx, 0, gx, h)
            for gy in range(0, h, grid_step):
                painter.drawLine(0, gy, w, gy)
                
            font = QFont("Consolas", 10)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QPen(QColor("#64748b")))
            painter.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "NO ACTIVE VIDEO STREAM\n[ Select Channel & Click 'START VIDEO STREAM' ]")

        # Tactical OSD Overlay
        if self.show_osd:
            painter.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
            
            # Top Banner Background
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(0, 0, 0, 160)))
            painter.drawRoundedRect(QRectF(8, 8, max(200, w - 16), 26), 4, 4)
            
            # Top-Left: Channel & Source
            painter.setPen(QPen(QColor("#38bdf8")))
            painter.drawText(QPointF(16, 25), f"📹 {self.osd_mode} | {self.osd_channel}")
            
            # Top-Right: Status & FPS
            status_color = QColor("#10b981") if "LOCKED" in self.osd_status else QColor("#f59e0b")
            painter.setPen(QPen(status_color))
            fps_str = f"[{self.osd_status}] {self.osd_fps:.1f} FPS"
            painter.drawText(QRectF(w - 250, 8, 234, 26), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, fps_str)
            
            # Reticle / Crosshair (if enabled)
            if self.show_reticle:
                cx, cy = w / 2.0, h / 2.0
                painter.setPen(QPen(QColor(239, 68, 68, 180), 1.5))
                painter.drawLine(QPointF(cx - 20, cy), QPointF(cx + 20, cy))
                painter.drawLine(QPointF(cx, cy - 20), QPointF(cx, cy + 20))
                painter.drawEllipse(QPointF(cx, cy), 35, 35)

class NativeHackRFVideoThread(QThread):
    frame_ready = pyqtSignal(QImage, bool, float)
    status_signal = pyqtSignal(str)

    def __init__(self, freq_mhz=5806, standard="PAL", invert_polarity=False, lna=32, vga=40):
        super().__init__()
        self.freq_mhz = freq_mhz
        self.standard = standard
        self.invert_polarity = invert_polarity
        self.lna = lna
        self.vga = vga
        self.running = True
        self.process = None
        self.color_palette = "GRAYSCALE"
        self.brightness = 0
        self.contrast = 1.0

    def set_freq(self, freq_mhz):
        self.freq_mhz = freq_mhz

    def set_tuning(self, standard, invert_polarity, palette, brightness, contrast):
        self.standard = standard
        self.invert_polarity = invert_polarity
        self.color_palette = palette
        self.brightness = brightness
        self.contrast = contrast

    def run(self):
        cmd = [
            "hackrf_transfer",
            "-f", str(int(self.freq_mhz * 1e6)),
            "-s", "20000000",
            "-a", "1",
            "-l", str(self.lna),
            "-g", str(self.vga),
            "-r", "-"
        ]
        
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            self.process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.DEVNULL, 
                bufsize=20000000, 
                creationflags=creationflags
            )
        except Exception as e:
            self.status_signal.emit(f"Failed to start HackRF: {e}")
            return

        LINE_LEN = 1280 if self.standard == "PAL" else 1271
        SAMPLES_PER_CHUNK = LINE_LEN * 600
        BYTES_PER_CHUNK = SAMPLES_PER_CHUNK * 2
        
        frame_counter = 0
        last_fps_calc = time.time()
        current_fps = 0.0
        
        lut_green = np.zeros((256, 3), dtype=np.uint8)
        lut_green[:, 1] = np.arange(256, dtype=np.uint8)
        lut_green[:, 0] = (np.arange(256) * 0.15).astype(np.uint8)
        lut_green[:, 2] = (np.arange(256) * 0.15).astype(np.uint8)

        lut_amber = np.zeros((256, 3), dtype=np.uint8)
        lut_amber[:, 0] = (np.arange(256) * 0.15).astype(np.uint8)
        lut_amber[:, 1] = (np.arange(256) * 0.65).astype(np.uint8)
        lut_amber[:, 2] = np.arange(256, dtype=np.uint8)

        while self.running:
            raw_data = self.process.stdout.read(BYTES_PER_CHUNK)
            if not raw_data or len(raw_data) < BYTES_PER_CHUNK:
                time.sleep(0.005)
                continue

            LINE_LEN = 1280 if self.standard == "PAL" else 1271
            
            d = np.frombuffer(raw_data, dtype=np.int8).astype(np.float32)
            ib = d[0::2]
            qb = d[1::2]
            
            ib -= np.mean(ib)
            qb -= np.mean(qb)
            iq = ib + 1j * qb
            
            cc = iq[1:] * np.conj(iq[:-1])
            fm_demod = np.angle(cc)
            
            if self.invert_polarity:
                fm_demod = -fm_demod
                
            num_lines = len(fm_demod) // LINE_LEN
            if num_lines < 50:
                continue
                
            chunks = fm_demod[:num_lines * LINE_LEN].reshape((num_lines, LINE_LEN))
            
            sync_indices = np.argmin(chunks, axis=1)
            sync_jitter = float(np.std(sync_indices))
            sync_locked = sync_jitter < (LINE_LEN * 0.15)
            
            abs_syncs = np.arange(num_lines) * LINE_LEN + sync_indices
            valid_syncs = abs_syncs[(abs_syncs >= 0) & (abs_syncs + LINE_LEN < len(fm_demod))]
            
            if len(valid_syncs) > 100:
                line_matrix = fm_demod[valid_syncs[:, None] + np.arange(LINE_LEN)]
            else:
                line_matrix = chunks
                
            clamped = np.clip(line_matrix * self.contrast + (self.brightness / 100.0), -2.2, 2.2)
            norm = ((clamped - clamped.min()) / (clamped.max() - clamped.min() + 1e-6) * 255.0).astype(np.uint8)
            
            resized = cv2.resize(norm, (640, 480), interpolation=cv2.INTER_LINEAR)
            
            if self.color_palette == "TACTICAL_GREEN":
                rgb_frame = lut_green[resized]
                qimg = QImage(rgb_frame.data, 640, 480, 640 * 3, QImage.Format.Format_BGR888).copy()
            elif self.color_palette == "AMBER_FLIR":
                rgb_frame = lut_amber[resized]
                qimg = QImage(rgb_frame.data, 640, 480, 640 * 3, QImage.Format.Format_BGR888).copy()
            else:
                qimg = QImage(resized.data, 640, 480, 640, QImage.Format.Format_Grayscale8).copy()
                
            frame_counter += 1
            now = time.time()
            if now - last_fps_calc >= 1.0:
                current_fps = frame_counter / (now - last_fps_calc)
                frame_counter = 0
                last_fps_calc = now
                
            self.frame_ready.emit(qimg, sync_locked, current_fps)

        if self.process:
            self.process.terminate()
            self.process.wait()

    def stop(self):
        self.running = False
        if self.process:
            try:
                self.process.terminate()
            except: pass
        self.wait(1000)

class ExternalStreamVideoThread(QThread):
    frame_ready = pyqtSignal(QImage, bool, float)
    status_signal = pyqtSignal(str)

    def __init__(self, stream_url="udp://127.0.0.1:5005"):
        super().__init__()
        self.stream_url = stream_url
        self.running = True
        self.cap = None

    def run(self):
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "overrun_nonfatal;1;fifo_size;50000000;timeout;2000000"
        
        self.status_signal.emit(f"Connecting to stream: {self.stream_url}")
        self.cap = cv2.VideoCapture(self.stream_url, cv2.CAP_FFMPEG)
        
        if not self.cap.isOpened():
            self.status_signal.emit(f"Failed to open video stream: {self.stream_url}")
            return
            
        self.status_signal.emit("Connected to video stream.")
        
        frame_counter = 0
        last_fps_calc = time.time()
        current_fps = 0.0

        while self.running:
            ret, frame = self.cap.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue
                
            resized = cv2.resize(frame, (640, 480))
            qimg = QImage(resized.data, 640, 480, 640 * 3, QImage.Format.Format_BGR888).copy()
            
            frame_counter += 1
            now = time.time()
            if now - last_fps_calc >= 1.0:
                current_fps = frame_counter / (now - last_fps_calc)
                frame_counter = 0
                last_fps_calc = now
                
            self.frame_ready.emit(qimg, True, current_fps)

        if self.cap:
            self.cap.release()

    def stop(self):
        self.running = False
        if self.cap:
            self.cap.release()
        self.wait(1000)

# --- Tactical Flight & Avionics Widgets (Heltec V3 Live Telemetry) ---
class GimbalHUDWidget(QWidget):
    """
    Tactical 2D Mode 2 Dual-Gimbal HUD.
    Left Box: Throttle (CH3 vertical) & Yaw (CH1 horizontal)
    Right Box: Pitch (CH2 vertical) & Roll (CH4 horizontal)
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 165)
        self.setFixedHeight(180)
        self.ch1_yaw = 1500  # 988 - 2012 µs
        self.ch2_pit = 1500  # 988 - 2012 µs
        self.ch3_thr = 988   # 988 - 2012 µs
        self.ch4_rol = 1500  # 988 - 2012 µs
        self.is_armed = False

    def update_sticks(self, ch1_yaw, ch2_pit, ch3_thr, ch4_rol, is_armed=False):
        self.ch1_yaw = max(988, min(2012, ch1_yaw))
        self.ch2_pit = max(988, min(2012, ch2_pit))
        self.ch3_thr = max(988, min(2012, ch3_thr))
        self.ch4_rol = max(988, min(2012, ch4_rol))
        self.is_armed = is_armed
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        
        # Determine optimal box dimensions
        avail_w = (w - 20) // 2
        avail_h = h - 36
        box_size = max(50, min(avail_w, avail_h))
        y_top = max(4, (avail_h - box_size) // 2 + 4)

        gap = max(8, (w - 2 * box_size) // 3)
        x_left = gap
        x_right = gap + box_size + gap

        thr_pct = max(0.0, min(100.0, (self.ch3_thr - 988.0) / 10.24))
        self._draw_gimbal(
            painter, x_left, y_top, box_size,
            x_val=self.ch1_yaw, y_val=self.ch3_thr,
            title="LEFT: THROTTLE / YAW",
            y_label=f"THR: {self.ch3_thr} µs ({thr_pct:.0f}%)",
            x_label=f"YAW: {self.ch1_yaw} µs",
            is_left=True
        )

        self._draw_gimbal(
            painter, x_right, y_top, box_size,
            x_val=self.ch4_rol, y_val=self.ch2_pit,
            title="RIGHT: PITCH / ROLL",
            y_label=f"PIT: {self.ch2_pit} µs",
            x_label=f"ROL: {self.ch4_rol} µs",
            is_left=False
        )

    def _draw_gimbal(self, painter, x, y, size, x_val, y_val, title, y_label, x_label, is_left=True):
        # Card Background
        painter.setPen(QPen(QColor("#1e293b"), 1.5))
        painter.setBrush(QBrush(QColor("#090d16")))
        painter.drawRoundedRect(QRectF(x, y, size, size), 6, 6)

        cx = x + size / 2.0
        cy = y + size / 2.0

        # Tactical Crosshairs
        painter.setPen(QPen(QColor("#1e293b"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(x + 4, cy), QPointF(x + size - 4, cy))
        painter.drawLine(QPointF(cx, y + 4), QPointF(cx, y + size - 4))

        # 50% / Center Deadband Box
        painter.setPen(QPen(QColor("#334155"), 1, Qt.PenStyle.DotLine))
        center_box = size * 0.22
        painter.drawRect(QRectF(cx - center_box / 2, cy - center_box / 2, center_box, center_box))

        # Normalized stick position [-1.0, 1.0]
        norm_x = (x_val - 1500.0) / 512.0
        norm_y = (y_val - 1500.0) / 512.0

        stick_x = cx + norm_x * (size * 0.40)
        stick_y = cy - norm_y * (size * 0.40)  # Inverted Y for screen coords

        # Dynamic Stick Glow & Cursor
        if self.is_armed:
            glow_color = QColor(239, 68, 68, 60)
            puck_color = QColor("#ef4444")
            ring_color = QColor("#f87171")
        else:
            glow_color = QColor(16, 185, 129, 60)
            puck_color = QColor("#10b981")
            ring_color = QColor("#34d399")

        # Soft glow halo
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(glow_color))
        painter.drawEllipse(QPointF(stick_x, stick_y), 12, 12)

        # Outer ring
        painter.setPen(QPen(ring_color, 1.5))
        painter.setBrush(QBrush(puck_color))
        painter.drawEllipse(QPointF(stick_x, stick_y), 4.5, 4.5)

        # Text labels below box
        font = QFont("Consolas", 8)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#38bdf8") if self.is_armed else QColor("#94a3b8")))
        painter.drawText(QRectF(x, y + size + 2, size, 12), Qt.AlignmentFlag.AlignCenter, y_label)
        painter.setPen(QPen(QColor("#94a3b8")))
        painter.drawText(QRectF(x, y + size + 14, size, 12), Qt.AlignmentFlag.AlignCenter, x_label)


class FlightDynamicsClassifier:
    """
    Real-time flight dynamics & tactical maneuver classifier for ExpressLRS 50Hz streams.
    """
    def __init__(self):
        self.prev_channels = [1500, 1500, 988, 1500]
        self.prev_time = time.time()

    def classify(self, ch1, ch2, ch3, ch4, is_armed):
        now = time.time()
        dt = max(0.01, now - self.prev_time)
        self.prev_time = now

        d_ch = [abs(c - p) / dt for c, p in zip([ch1, ch2, ch3, ch4], self.prev_channels)]
        self.prev_channels = [ch1, ch2, ch3, ch4]

        thr_pct = (ch3 - 988.0) / 10.24
        yaw_def = abs(ch1 - 1500)
        pit_def = abs(ch2 - 1500)
        rol_def = abs(ch4 - 1500)
        max_cyclic = max(pit_def, rol_def, yaw_def)
        total_rate = sum(d_ch)

        if not is_armed:
            return ("🛑 DISARMED / MOTOR SHUTDOWN", "#64748b", f"Motors Idle | Thr: {thr_pct:.0f}%")

        if ch3 < 1040:
            return ("⚠️ ARMED / IDLE READY (LANDED)", "#f59e0b", f"Zero Throttle Spool | Thr: {thr_pct:.0f}%")

        if ch3 > 1850 and max_cyclic < 120:
            return ("⚡ VERTICAL PUNCHOUT / RAPID CLIMB", "#ef4444", f"Max Power Climb | Thr: {thr_pct:.0f}%")

        if max_cyclic > 350 or total_rate > 1500:
            return ("🌪️ HIGH-RATE ACRO / COMBAT EVASION", "#dc2626", f"Aggressive Deflection | Rate: {total_rate:.0f}µs/s")

        if pit_def > 180 and ch3 > 1300:
            direction = "FORWARD TRANSIT" if ch2 > 1500 else "REVERSE PITCH / FLIP"
            return (f"🚀 HIGH-SPEED {direction}", "#3b82f6", f"Pitch Deflection: {pit_def}µs | Thr: {thr_pct:.0f}%")

        if rol_def > 180 and ch3 > 1300:
            return ("🔄 HIGH-G BANKED TURN", "#8b5cf6", f"Roll Deflection: {rol_def}µs | Thr: {thr_pct:.0f}%")

        if 1200 <= ch3 <= 1650 and max_cyclic < 100:
            return ("🎯 STATIONARY HOVER / TARGET LOITER", "#10b981", f"Stable Mid-Throttle | Thr: {thr_pct:.0f}%")

        if ch3 < 1200:
            return ("📉 CONTROLLED DESCENT / SPOOL-DOWN", "#06b6d4", f"Low Throttle Descent | Thr: {thr_pct:.0f}%")

        return ("🚁 ACTIVE FLIGHT / CRUISE", "#10b981", f"Nominal Flight | Thr: {thr_pct:.0f}%")


# --- Main App ---
class CEMAApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CEMA RF Tracking [HackRF + Heltec V3]")
        self.resize(1300, 850)
        
        self.hackrf_thread = None
        self.heltec_thread = None
        
        self.setup_ui()
        
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self.poll_data)
        self.poll_timer.start(33)
        
        self.clock_timer = QTimer()
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)

        self.start_sdr()
        self.start_heltec()

    def setup_ui(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #0b0f19;
                color: #e2e8f0;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 11px;
            }
            QToolTip { background-color: #0f172a; color: #38bdf8; border: 1px solid #0284c7; padding: 5px; font-family: 'Consolas', monospace; font-size: 13px; }
            QMainWindow { background-color: #0b0f19; }
            QLabel { color: #cbd5e1; font-weight: bold; font-family: 'Consolas', 'Courier New', monospace; font-size: 12px; background-color: transparent; }
            QDoubleSpinBox, QSpinBox, QLineEdit { background-color: #0f172a; color: #38bdf8; border: 1px solid #334155; border-radius: 4px; padding: 6px; font-family: 'Consolas', monospace; font-size: 13px; font-weight: bold; }
            QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus { border: 1px solid #38bdf8; }
            QPushButton { background-color: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 8px 16px; font-weight: bold; font-family: 'Consolas', monospace; border-radius: 6px; }
            QPushButton:hover { background-color: #334155; color: white; border: 1px solid #38bdf8; }
            QPushButton:checked { background-color: #0284c7; border: 1px solid #38bdf8; color: white; }
            QGroupBox { color: #38bdf8; border: 1px solid #1e293b; margin-top: 12px; background-color: #0b0f19; font-family: 'Consolas', monospace; border-radius: 8px; font-weight: bold; font-size: 13px; }
            QGroupBox::title { subcontrol-origin: margin; left: 15px; padding: 0 5px 0 5px; color: #38bdf8; }
            QListWidget { background-color: #0f172a; color: #38bdf8; font-family: 'Consolas', monospace; border: 1px solid #1e293b; border-radius: 6px; padding: 5px; font-size: 13px; outline: none; }
            QListWidget::item:selected { background-color: #1e293b; color: #38bdf8; }
            QTextEdit { background-color: #0f172a; color: #10b981; font-family: 'Consolas', monospace; border: 1px solid #1e293b; border-radius: 6px; padding: 5px; font-size: 13px; }
            QComboBox { background-color: #0f172a; color: #f59e0b; font-weight: bold; padding: 6px; border: 1px solid #334155; border-radius: 4px; font-family: 'Consolas', monospace; }
            QTabWidget::pane { border: 1px solid #1e293b; border-radius: 6px; }
            QTabBar::tab { background-color: #0f172a; color: #94a3b8; padding: 10px 20px; border: 1px solid #1e293b; border-top-left-radius: 6px; border-top-right-radius: 6px; font-weight: bold; }
            QTabBar::tab:selected { background-color: #1e293b; color: #38bdf8; border-bottom: 2px solid #38bdf8; }
            QSlider::groove:horizontal { border: 1px solid #334155; height: 6px; background: #0f172a; border-radius: 3px; }
            QSlider::handle:horizontal { background: #38bdf8; border: 1px solid #0284c7; width: 14px; margin: -4px 0; border-radius: 7px; }
        """)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Control Panel
        control_group = QGroupBox("SDR Parameters")
        control_group.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        control_layout = QHBoxLayout()
        
        self.mode_selector = QComboBox()
        self.mode_selector.addItems(["STARE MODE (2MHz)", "SWEEP MODE (Wideband)"])
        self.mode_selector.currentTextChanged.connect(self.mode_changed)
        self.mode_selector.setToolTip("STARE MODE: Analyzes a 2MHz block for deep intelligence (Modulation, Fingerprinting).\nSWEEP MODE: Scans wideband spectrum for Frequency Hoppers and active targets.")
        
        self.freq_input = QDoubleSpinBox()
        self.freq_input.setRange(1, 6000)
        self.freq_input.setValue(97.9)
        self.freq_input.setSuffix(" MHz")
        self.freq_input.setDecimals(3)
        self.freq_input.setSingleStep(0.1)
        self.freq_input.setToolTip("Center Frequency for Stare Mode (MHz).")
        
        self.sweep_start_input = QDoubleSpinBox()
        self.sweep_start_input.setRange(1, 6000)
        self.sweep_start_input.setValue(30)
        self.sweep_start_input.setSuffix(" MHz")
        self.sweep_start_input.setDecimals(1)
        self.sweep_start_input.hide()
        self.sweep_start_input.setToolTip("Start Frequency of the Wideband Sweep (MHz).")

        self.sweep_end_input = QDoubleSpinBox()
        self.sweep_end_input.setRange(1, 6000)
        self.sweep_end_input.setValue(500)
        self.sweep_end_input.setSuffix(" MHz")
        self.sweep_end_input.setDecimals(1)
        self.sweep_end_input.hide()
        self.sweep_end_input.setToolTip("End Frequency of the Wideband Sweep (MHz).")
        
        self.lna_input = QSpinBox()
        self.lna_input.setRange(0, 40)
        self.lna_input.setSingleStep(8)
        self.lna_input.setValue(32)
        self.lna_input.setToolTip("LNA Gain (RF Gain): Increase to boost weak signals at the antenna. Too high will cause distortion.")
        
        self.vga_input = QSpinBox()
        self.vga_input.setRange(0, 62)
        self.vga_input.setSingleStep(2)
        self.vga_input.setValue(40)
        self.vga_input.setToolTip("VGA Gain (IF Gain): Fine-tunes the signal strength before digital conversion.")
        
        self.apply_btn = QPushButton("APPLY / RESTART")
        self.apply_btn.clicked.connect(self.start_sdr)
        self.apply_btn.setToolTip("Restart the SDR with the new parameters.")
        
        self.mask_mode_btn = QPushButton("MASK MODE: OFF")
        self.mask_mode_btn.setCheckable(True)
        self.mask_mode_btn.setStyleSheet("background-color: #475569; color: white;")
        self.mask_mode_btn.toggled.connect(self.toggle_mask_mode)
        self.mask_mode_btn.setToolTip("ON: Left-click and drag on the FFT to draw a grey mask over continuous signals to ignore them.\nOFF: Left-click and drag to pan the spectrum.")
        
        self.decode_video_btn = QPushButton(" DECODE FPV VIDEO")
        self.decode_video_btn.setCheckable(True)
        self.decode_video_btn.setStyleSheet("background-color: #475569; color: white;")
        self.decode_video_btn.toggled.connect(self.toggle_video_mode)
        self.decode_video_btn.setToolTip("Decodes 5.8GHz Analog FPV signals by ripping the raw 20MS/s FM phase array and slicing it via HSync matrix reshaping into a CRT video frame.")
        
        self.toggle_sidebar_btn = QPushButton(" SIDEBAR")
        self.toggle_sidebar_btn.clicked.connect(self.toggle_sidebar)
        self.toggle_sidebar_btn.setToolTip("Hide or show the Intelligence Sidebar.")
        
        self.wf_sens_slider = QSlider(Qt.Orientation.Horizontal)
        self.wf_sens_slider.setRange(1, 255)
        self.wf_sens_slider.setValue(120)
        self.wf_sens_slider.setFixedWidth(120)
        self.wf_sens_slider.setToolTip("Waterfall Sensitivity: Slide left to make faint signals visible, slide right to reduce noise floor clutter.")
        
        self.freeze_btn = QPushButton(" FREEZE")
        self.freeze_btn.setCheckable(True)
        self.freeze_btn.setStyleSheet("background-color: #475569; color: white;")
        self.freeze_btn.toggled.connect(self.toggle_freeze)
        self.freeze_btn.setToolTip("Freeze the display updates so you can analyze the waterfall and spectrum without it moving.")
        
        self.clock_label = QLabel("00:00:00Z")
        self.clock_label.setStyleSheet("color: #10b981; font-size: 16px; font-weight: bold; background: #111; padding: 4px; border: 1px solid #222; border-radius: 4px;")
        self.clock_label.setToolTip("ZULU (UTC) Time")
        
        self.mod_label = QLabel("Modulation: UNKNOWN")
        self.mod_label.setStyleSheet("color: #fbbf24; font-size: 16px; font-weight: bold;")

        control_layout.addWidget(QLabel("Mode:"))
        control_layout.addWidget(self.mode_selector)
        control_layout.addSpacing(10)
        
        self.freq_label = QLabel("Center Freq:")
        control_layout.addWidget(self.freq_label)
        control_layout.addWidget(self.freq_input)
        control_layout.addWidget(self.sweep_start_input)
        control_layout.addWidget(self.sweep_end_input)
        
        control_layout.addSpacing(10)
        control_layout.addWidget(QLabel("LNA Gain:"))
        control_layout.addWidget(self.lna_input)
        control_layout.addSpacing(10)
        control_layout.addWidget(QLabel("VGA Gain:"))
        control_layout.addWidget(self.vga_input)
        control_layout.addSpacing(10)
        control_layout.addWidget(self.apply_btn)
        control_layout.addSpacing(10)
        control_layout.addWidget(self.mask_mode_btn)
        control_layout.addWidget(self.decode_video_btn)
        control_layout.addSpacing(10)
        control_layout.addWidget(self.toggle_sidebar_btn)
        control_layout.addSpacing(10)
        control_layout.addWidget(QLabel("WF Sens:"))
        control_layout.addWidget(self.wf_sens_slider)
        control_layout.addSpacing(10)
        self.freeze_btn = QPushButton(" FREEZE")
        self.freeze_btn.setCheckable(True)
        self.freeze_btn.setStyleSheet("background-color: #475569; color: white;")
        self.freeze_btn.toggled.connect(self.toggle_freeze)
        self.freeze_btn.setToolTip("Freeze the display updates so you can analyze the waterfall and spectrum without it moving.")
        control_layout.addWidget(self.freeze_btn)
        
        control_layout.addSpacing(10)
        self.heltec_port_combo = QComboBox()
        self.heltec_port_combo.addItems(get_available_com_ports())
        self.heltec_port_combo.setToolTip("Select COM Port for Heltec WiFi LoRa 32 V3 sniffer.")
        self.heltec_connect_btn = QPushButton("🚁 HELTEC V3")
        self.heltec_connect_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; border: 1px solid #38bdf8;")
        self.heltec_connect_btn.clicked.connect(self.restart_heltec)
        self.heltec_connect_btn.setToolTip("Connect or Reconnect to Heltec WiFi LoRa 32 V3 sniffer hardware.")
        control_layout.addWidget(self.heltec_port_combo)
        control_layout.addWidget(self.heltec_connect_btn)

        control_layout.addStretch()
        control_layout.addWidget(self.mod_label)
        control_layout.addSpacing(10)
        control_layout.addWidget(self.clock_label)
        
        control_group.setLayout(control_layout)
        main_layout.addWidget(control_group)

        self.body_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.body_splitter.setStyleSheet("QSplitter::handle { background-color: #333; width: 6px; border-radius: 3px; }")
        main_layout.addWidget(self.body_splitter)
        
        graph_widget = QWidget()
        self.graph_layout = QGridLayout(graph_widget)
        self.body_splitter.addWidget(graph_widget)

        # Sidebar with Tabs
        self.sidebar_widget = QWidget()
        sidebar_layout = QVBoxLayout(self.sidebar_widget)
        sidebar_layout.setContentsMargins(0,0,0,0)
        self.sidebar_tabs = QTabWidget()
        
        # Tab 1: Tactical Event Log
        tab_log = QWidget()
        log_layout = QVBoxLayout(tab_log)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setToolTip("Tactical Event Log: Real-time record of anomalies, Watchlist matches, and system alerts.")
        
        log_btn_layout = QHBoxLayout()
        clear_btn = QPushButton("CLEAR LOG")
        clear_btn.clicked.connect(self.log_text.clear)
        export_btn = QPushButton("EXPORT SITREP")
        export_btn.clicked.connect(self.export_log)
        export_btn.setToolTip("Save the current log to a text file for intelligence reporting.")
        
        log_btn_layout.addWidget(clear_btn)
        log_btn_layout.addWidget(export_btn)
        
        log_layout.addWidget(self.log_text)
        log_layout.addLayout(log_btn_layout)
        self.sidebar_tabs.addTab(tab_log, "Events")
        
        # Tab 2: Intelligence & Emitters
        tab_intel = QWidget()
        intel_layout = QVBoxLayout(tab_intel)
        
        clear_intel_btn = QPushButton(" WIPE INTEL DB (RESET SESSION)")
        clear_intel_btn.setStyleSheet("background-color: #ef4444; color: white;")
        clear_intel_btn.clicked.connect(self.clear_intel_db)
        clear_intel_btn.setToolTip("Wipe all known hardware fingerprints and topology links to start a fresh session.")
        intel_layout.addWidget(clear_intel_btn)
        
        intel_layout.addWidget(QLabel("Known Signatures (Watchlist):"))
        self.watchlist_ui = QListWidget()
        self.watchlist_ui.setMaximumHeight(80)
        self.watchlist_ui.setToolTip("Watchlist: The algorithm cross-references live signals against this database to instantly flag enemy drones or data links. Right-click to delete.")
        self.watchlist_ui.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.watchlist_ui.customContextMenuRequested.connect(self.watchlist_context_menu)
        intel_layout.addWidget(self.watchlist_ui)
        
        add_wl_layout = QGridLayout()
        self.wl_name_input = QLineEdit()
        self.wl_name_input.setPlaceholderText("Name (e.g., Orlan-10)")
        
        self.wl_min_bw = QSpinBox()
        self.wl_min_bw.setRange(1, 100000)
        self.wl_min_bw.setValue(10)
        self.wl_min_bw.setSuffix(" kHz")
        self.wl_min_bw.setToolTip("Minimum expected bandwidth.")
        
        self.wl_max_bw = QSpinBox()
        self.wl_max_bw.setRange(1, 100000)
        self.wl_max_bw.setValue(50)
        self.wl_max_bw.setSuffix(" kHz")
        self.wl_max_bw.setToolTip("Maximum expected bandwidth.")
        
        self.wl_mod = QComboBox()
        self.wl_mod.addItems(["FM/FSK/CW", "QAM/Digital", "AM/Analog", "Wideband/Impulsive"])
        self.wl_mod.setToolTip("Expected modulation profile.")
        
        add_wl_btn = QPushButton("+ ADD PROFILE")
        add_wl_btn.setStyleSheet("background-color: #10b981; color: white;")
        add_wl_btn.clicked.connect(self.add_watchlist_item)
        
        add_wl_layout.addWidget(self.wl_name_input, 0, 0, 1, 2)
        add_wl_layout.addWidget(self.wl_min_bw, 1, 0)
        add_wl_layout.addWidget(self.wl_max_bw, 1, 1)
        add_wl_layout.addWidget(self.wl_mod, 2, 0)
        add_wl_layout.addWidget(add_wl_btn, 2, 1)
        intel_layout.addLayout(add_wl_layout)
        
        intel_layout.addWidget(QLabel("Fingerprinted Transmitters:"))
        self.fingerprint_ui = QListWidget()
        self.fingerprint_ui.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.fingerprint_ui.customContextMenuRequested.connect(self.fingerprint_context_menu)
        self.fingerprint_ui.setToolTip("Hardware Fingerprinting: Extracts a physical signature from the radio. The Hex code tracks the radio even if the enemy changes frequency. Right-click to assign names.")
        intel_layout.addWidget(self.fingerprint_ui)
        
        demod_layout = QHBoxLayout()
        self.demod_btn = QPushButton(" DEMODULATE")
        self.demod_btn.setStyleSheet("background-color: #10b981; color: white; font-weight: bold;")
        self.demod_btn.clicked.connect(self.demodulate_selected)
        self.demod_btn.setToolTip("Demodulate the selected unencrypted analog signal.")
        
        self.force_demod_btn = QPushButton(" FORCE DEMOD")
        self.force_demod_btn.setStyleSheet("background-color: #f59e0b; color: white; font-weight: bold;")
        self.force_demod_btn.clicked.connect(self.force_demodulate)
        self.force_demod_btn.setToolTip("Force analog FM demodulation even if encryption is suspected.")
        
        demod_layout.addWidget(self.demod_btn)
        demod_layout.addWidget(self.force_demod_btn)
        intel_layout.addLayout(demod_layout)
        
        intel_layout.addWidget(QLabel("Network Topology (Call & Response):"))
        self.topology_ui = QTreeWidget()
        self.topology_ui.setHeaderLabels(["Emitter Node", "Reply Count"])
        self.topology_ui.setToolTip("Network Topology: Analyzes transmission timings. If a radio consistently replies within 5 seconds of another finishing, the software draws a command link between them.")
        intel_layout.addWidget(self.topology_ui)
        
        intel_layout.addWidget(QLabel("FHSS Networks Tracked:"))
        self.fhss_ui = QListWidget()
        self.fhss_ui.setToolTip("FHSS Tracker: Operates in Sweep Mode. Mathematically calculates the hop-rate of evasive Frequency Hopping spread spectrum military networks.")
        intel_layout.addWidget(self.fhss_ui)
        
        self.sidebar_tabs.addTab(tab_intel, "Intel DB")
        
        # Tab 3: Masks
        tab_mask = QWidget()
        mask_layout = QVBoxLayout(tab_mask)
        self.mask_list = QListWidget()
        self.mask_list.itemDoubleClicked.connect(self.delete_mask)
        self.mask_list.setToolTip("Saved Frequency Masks. Double-click any mask to delete it.")
        save_masks_btn = QPushButton("SAVE MASKS TO DISK")
        save_masks_btn.clicked.connect(self.save_masks)
        mask_layout.addWidget(QLabel("Double-click to delete."))
        mask_layout.addWidget(self.mask_list)
        mask_layout.addWidget(save_masks_btn)
        self.sidebar_tabs.addTab(tab_mask, "Masks")

        sidebar_layout.addWidget(self.sidebar_tabs)
        self.body_splitter.addWidget(self.sidebar_widget)
        self.body_splitter.setSizes([1100, 350])
        
        # Tab 4: Geolocation
        tab_geo = QWidget()
        geo_layout = QVBoxLayout(tab_geo)
        geo_layout.addWidget(self.create_geolocation_ui())
        self.sidebar_tabs.addTab(tab_geo, "Geolocation")

        # Tab 5: Drone Telemetry (Heltec V3)
        tab_drone = QWidget()
        drone_layout = QVBoxLayout(tab_drone)
        drone_layout.addWidget(self.create_drone_telemetry_ui())
        self.sidebar_tabs.addTab(tab_drone, "Drone Telemetry")

        # Tab 6: Drone Video Feed
        tab_video = QWidget()
        video_layout = QVBoxLayout(tab_video)
        video_layout.addWidget(self.create_drone_video_ui())
        self.sidebar_tabs.addTab(tab_video, "Drone Video")
        
        # State
        self.global_masks = []
        self.whitelist_regions = {}
        self.active_events = {}
        self.fingerprint_db = {}
        self.network_links = {}
        self.current_active_fingerprint = None
        self.last_transmission_end = 0
        self.hop_history = []
        self.last_fhss_alert = 0
        self.last_mod_type = "UNKNOWN"
        self.current_drag_region = None
        self.current_mode = "STARE"
        self.watchlist = []
        self.flight_classifier = FlightDynamicsClassifier()
        self.last_pilot_key = None
        self.pilot_rssi_history = []
        self.gps_breadcrumbs_count = 0
        self.last_drone_rssi = -100
        self.last_drone_lq = 0
        self.native_video_thread = None
        self.external_video_thread = None
        self.is_video_streaming = False

        # pyqtgraph setup
        pg.setConfigOptions(antialias=False)
        pg.setConfigOption('background', '#050505')
        pg.setConfigOption('foreground', '#94a3b8')

        self.fft_plot = pg.PlotWidget(title="Real-Time Spectrum (FFT)")
        self.fft_plot.setLabel('bottom', 'Frequency Offset', units='MHz')
        self.fft_plot.setLabel('left', 'Relative Power', units='dB')
        self.fft_plot.setToolTip("Real-Time Spectrum (FFT). In Sweep Mode, click anywhere to instantly snap into Stare Mode (Hunter-Killer transition).")
        self.fft_plot.showGrid(x=True, y=True, alpha=0.3)
        self.fft_plot.setYRange(0, 100)
        self.fft_curve = self.fft_plot.plot(pen=pg.mkPen('#38bdf8', width=2), fillLevel=0, fillBrush=(56, 189, 248, 60))
        self.fft_max_curve = self.fft_plot.plot(pen=pg.mkPen('#fb923c', width=1, style=Qt.PenStyle.DashLine))
        self.peak_scatter = pg.ScatterPlotItem(size=12, pen=pg.mkPen(None), brush=pg.mkBrush(239, 68, 68, 200))
        self.fft_plot.addItem(self.peak_scatter)
        self.bw_left = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#eab308', width=2))
        self.bw_right = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#eab308', width=2))
        self.fft_plot.addItem(self.bw_left)
        self.fft_plot.addItem(self.bw_right)
        self.bw_left.hide()
        self.bw_right.hide()
        
        self.vfo_region = pg.LinearRegionItem([502, 522], brush=pg.mkBrush(34, 197, 94, 70), pen=pg.mkPen('#22c55e', width=2))
        self.vfo_region.setZValue(10)
        self.vfo_region.sigRegionChanged.connect(self.update_vfo_offset)
        self.vfo_region.setToolTip("DEMODULATION VFO: Drag this green mask over a signal to listen to it without changing the center frequency.")
        self.fft_plot.addItem(self.vfo_region)
        
        self.fft_plot.setMouseEnabled(x=True, y=False)
        self.fft_plot._original_mousePressEvent = self.fft_plot.mousePressEvent
        self.fft_plot._original_mouseMoveEvent = self.fft_plot.mouseMoveEvent
        self.fft_plot._original_mouseReleaseEvent = self.fft_plot.mouseReleaseEvent
        self.fft_plot.mousePressEvent = self.fft_mouse_press
        self.fft_plot.mouseMoveEvent = self.fft_mouse_move
        self.fft_plot.mouseReleaseEvent = self.fft_mouse_release
        self.graph_layout.addWidget(self.fft_plot, 0, 0, 1, 2)

        self.waterfall_plot = pg.PlotWidget(title="Waterfall Spectrogram")
        self.waterfall_plot.setLabel('bottom', 'Frequency Offset', units='MHz')
        self.waterfall_image = pg.ImageItem()
        self.waterfall_plot.addItem(self.waterfall_image)
        pos = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
        color = np.array([
            [0, 0, 0, 255], [30, 58, 138, 255], [6, 182, 212, 255], [250, 204, 21, 255], [220, 38, 38, 255]
        ], dtype=np.ubyte)
        cmap = pg.ColorMap(pos, color)
        self.waterfall_image.setLookupTable(cmap.getLookupTable())
        self.waterfall_plot.hideAxis('left')
        self.waterfall_data = np.zeros((100, 1024))
        self.graph_layout.addWidget(self.waterfall_plot, 1, 0, 1, 1)

        self.const_plot = pg.PlotWidget(title="Constellation (Shape = Modulation Type)")
        self.const_plot.setToolTip("I/Q Constellation Diagram.\nShape indicates modulation type:\n- Ring: FM/Analog Voice\n- 2 Clusters: BPSK (Digital)\n- 4 Clusters: QPSK (Digital/Data)\n- Fuzzy Cloud: Noise or Encrypted Signal")
        self.const_plot.setLabel('bottom', 'In-Phase (I)')
        self.const_plot.setLabel('left', 'Quadrature (Q)')
        self.const_plot.showGrid(x=True, y=True, alpha=0.3)
        self.const_plot.setXRange(-128, 128)
        self.const_plot.setYRange(-128, 128)
        self.const_scatter = pg.ScatterPlotItem(size=6, pen=pg.mkPen(None), brush=pg.mkBrush(167, 139, 250, 200))
        self.const_plot.addItem(self.const_scatter)
        
        legend_text = (
            "[MODULATION GUIDE]\n"
            "Ring       = FM (Analog)\n"
            "4 Clusters = QPSK (Digital)\n"
            "2 Clusters = BPSK (Digital)\n"
            "Cloud      = Noise/Encrypted"
        )
        self.const_legend = pg.TextItem(legend_text, color='#38bdf8', anchor=(0, 0))
        self.const_plot.addItem(self.const_legend)
        self.const_legend.setPos(-125, 125)
        
        self.graph_layout.addWidget(self.const_plot, 1, 1, 1, 1)

        self.video_plot = pg.PlotWidget(title="Analog Video Decoder (NTSC/PAL)")
        self.video_plot.setLabel('bottom', 'CRT Horizontal Sweep', units='Samples')
        self.video_plot.setLabel('left', 'CRT Vertical Scanlines', units='Lines')
        self.video_plot.setAspectLocked(True)
        self.video_plot.invertY(True) # CRT draws top-to-bottom
        self.video_image = pg.ImageItem()
        self.video_plot.addItem(self.video_image)
        self.graph_layout.addWidget(self.video_plot, 1, 1, 1, 1)
        self.video_plot.hide()

        self.load_watchlist()
        self.load_fingerprints()
        self.load_topology()
        self.load_masks()
        
    def create_geolocation_ui(self):
        geo_widget = QWidget()
        geo_layout = QVBoxLayout(geo_widget)
        
        # Dashboard Header
        header_layout = QHBoxLayout()
        self.geo_status_label = QLabel("[ TARGET TELEMETRY: AWAITING PROTOCOL LOCK ]")
        self.geo_status_label.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 15px;")
        header_layout.addWidget(self.geo_status_label)

        self.geo_breadcrumbs_lbl = QLabel("[ TRACK POINTS: 0 ]")
        self.geo_breadcrumbs_lbl.setStyleSheet("color: #38bdf8; font-weight: bold; font-size: 13px; font-family: monospace;")
        header_layout.addWidget(self.geo_breadcrumbs_lbl)

        self.geo_clear_btn = QPushButton("🗑️ CLEAR TRACKS")
        self.geo_clear_btn.setStyleSheet("background-color: #1e293b; color: #f87171; font-weight: bold; padding: 4px 8px; border: 1px solid #f87171; border-radius: 4px;")
        self.geo_clear_btn.clicked.connect(self.clear_tactical_tracks)
        header_layout.addWidget(self.geo_clear_btn)

        geo_layout.addLayout(header_layout)
        
        # Interactive Web Map (Leaflet.js)
        self.geo_map_view = QWebEngineView()
        
        # Tactical Dark Map Template with Breadcrumbs Trail and RF Range Ring
        map_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <style>
                body { margin: 0; padding: 0; background-color: #0b0f19; }
                #map { height: 100vh; width: 100vw; }
                .leaflet-container { background-color: #0b0f19 !important; }
            </style>
        </head>
        <body>
            <div id="map"></div>
            <script>
                var map = L.map('map', {zoomControl: false}).setView([0, 0], 2);
                
                L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
                    attribution: '&copy; OpenStreetMap &copy; CartoDB',
                    maxZoom: 19
                }).addTo(map);

                var markers = {};
                var droneTrail = L.polyline([], {
                    color: '#10b981',
                    weight: 3,
                    opacity: 0.85,
                    dashArray: '4, 4'
                }).addTo(map);

                var rfCircle = null;

                function updateTarget(id, lat, lon, title, color) {
                    if (markers[id]) {
                        map.removeLayer(markers[id]);
                    }
                    
                    var iconHtml = `<div style="background-color: ${color}; width: 16px; height: 16px; border-radius: 50%; border: 2px solid white; box-shadow: 0 0 12px ${color};"></div>`;
                    var customIcon = L.divIcon({
                        className: 'custom-div-icon',
                        html: iconHtml,
                        iconSize: [20, 20],
                        iconAnchor: [10, 10]
                    });

                    markers[id] = L.marker([lat, lon], {icon: customIcon}).addTo(map);
                    markers[id].bindPopup("<b>" + title + "</b><br>" + lat.toFixed(5) + ", " + lon.toFixed(5));
                    
                    if (id === 'OP' || id === 'UAV') {
                        map.setView([lat, lon], Math.max(map.getZoom(), 15));
                    }
                }

                function addDroneTrailPoint(lat, lon) {
                    droneTrail.addLatLng([lat, lon]);
                }

                function updateRfRangeRing(lat, lon, radius_meters, color) {
                    var ringColor = color || '#38bdf8';
                    if (!rfCircle) {
                        rfCircle = L.circle([lat, lon], {
                            radius: radius_meters,
                            color: ringColor,
                            fillColor: ringColor,
                            fillOpacity: 0.12,
                            weight: 1.5,
                            dashArray: '4, 4'
                        }).addTo(map);
                    } else {
                        rfCircle.setLatLng([lat, lon]);
                        rfCircle.setRadius(radius_meters);
                        rfCircle.setStyle({color: ringColor, fillColor: ringColor});
                    }
                }

                function clearTacticalTracks() {
                    droneTrail.setLatLngs([]);
                    if (rfCircle) {
                        map.removeLayer(rfCircle);
                        rfCircle = null;
                    }
                }
            </script>
        </body>
        </html>
        """
        
        self.geo_map_view.setHtml(map_html)
        geo_layout.addWidget(self.geo_map_view)
        
        # Coordinates readout & Manual plot
        readout_layout = QHBoxLayout()
        self.geo_lat_input = QLineEdit()
        self.geo_lon_input = QLineEdit()
        self.geo_lat_input.setPlaceholderText("Latitude")
        self.geo_lon_input.setPlaceholderText("Longitude")
        self.geo_plot_btn = QPushButton("PLOT MANUALLY")
        self.geo_plot_btn.setStyleSheet("background-color: #0f172a; color: #38bdf8; font-weight: bold; border: 1px solid #1e293b; padding: 4px 8px; border-radius: 4px;")
        self.geo_plot_btn.clicked.connect(self.manual_plot_target)
        
        readout_layout.addWidget(QLabel("Lat:"))
        readout_layout.addWidget(self.geo_lat_input)
        readout_layout.addWidget(QLabel("Lon:"))
        readout_layout.addWidget(self.geo_lon_input)
        readout_layout.addWidget(self.geo_plot_btn)
        
        geo_layout.addLayout(readout_layout)
        return geo_widget

    def clear_tactical_tracks(self):
        self.gps_breadcrumbs_count = 0
        if hasattr(self, 'geo_breadcrumbs_lbl'):
            self.geo_breadcrumbs_lbl.setText("[ TRACK POINTS: 0 ]")
        if hasattr(self, 'geo_map_view'):
            self.geo_map_view.page().runJavaScript("clearTacticalTracks();")
        self.log_event("🗺️ TACTICAL MAP: Flight path breadcrumbs cleared.")

    def create_drone_telemetry_ui(self):
        drone_widget = QWidget()
        layout = QVBoxLayout(drone_widget)
        layout.setSpacing(8)

        # Header Status
        self.drone_status_label = QLabel("[ HELTEC V3: SEARCHING FOR 915MHz PACKETS ]")
        self.drone_status_label.setStyleSheet("background-color: #090d16; color: #f59e0b; font-weight: bold; font-size: 13px; padding: 6px; border: 1px solid #1e293b; border-radius: 4px;")
        layout.addWidget(self.drone_status_label)

        # Feature 1: Mode 2 Visual 2D Gimbal HUD
        hud_box = QGroupBox("Live Pilot Control Gimbals (Mode 2)")
        hud_box.setStyleSheet("QGroupBox { color: #38bdf8; font-weight: bold; border: 1px solid #1e293b; border-radius: 6px; margin-top: 6px; padding-top: 10px; background-color: #0b0f19; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }")
        hud_layout = QVBoxLayout(hud_box)
        hud_layout.setContentsMargins(6, 6, 6, 6)

        self.gimbal_hud = GimbalHUDWidget()
        hud_layout.addWidget(self.gimbal_hud)

        self.drone_sticks_lbl = QLabel("THR: 988 µs (0%) | YAW: 1500 µs | PIT: 1500 µs | ROL: 1500 µs")
        self.drone_sticks_lbl.setStyleSheet("background-color: #050505; color: #10b981; font-family: monospace; font-size: 12px; padding: 6px; border: 1px solid #1e293b; border-radius: 4px;")
        self.drone_sticks_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hud_layout.addWidget(self.drone_sticks_lbl)
        layout.addWidget(hud_box)

        # Feature 2: Tactical Flight Dynamics & Maneuver Classifier
        classifier_box = QGroupBox("Tactical Flight Dynamics & Maneuver Classifier")
        classifier_box.setStyleSheet("QGroupBox { color: #38bdf8; font-weight: bold; border: 1px solid #1e293b; border-radius: 6px; margin-top: 6px; padding-top: 10px; background-color: #0b0f19; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }")
        classifier_layout = QVBoxLayout(classifier_box)
        classifier_layout.setContentsMargins(6, 6, 6, 6)

        self.maneuver_badge = QLabel("🛑 DISARMED / MOTOR SHUTDOWN")
        self.maneuver_badge.setStyleSheet("background-color: #1e293b; color: #94a3b8; font-weight: bold; font-size: 14px; padding: 8px; border-radius: 4px; border: 1px solid #334155;")
        self.maneuver_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        classifier_layout.addWidget(self.maneuver_badge)

        self.maneuver_detail_lbl = QLabel("Motors Idle | Throttle: 0% | Cyclic Rate: 0 µs/s")
        self.maneuver_detail_lbl.setStyleSheet("color: #64748b; font-family: monospace; font-size: 11px;")
        self.maneuver_detail_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        classifier_layout.addWidget(self.maneuver_detail_lbl)
        layout.addWidget(classifier_box)

        # Feature 4: Dual-Link RF Proximity & Link Margin Gauge
        rf_box = QGroupBox("Dual-Link RF Proximity & Link Margin")
        rf_box.setStyleSheet("QGroupBox { color: #38bdf8; font-weight: bold; border: 1px solid #1e293b; border-radius: 6px; margin-top: 6px; padding-top: 10px; background-color: #0b0f19; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }")
        rf_layout = QGridLayout(rf_box)
        rf_layout.setContentsMargins(8, 8, 8, 8)

        rf_layout.addWidget(QLabel("Pilot Proximity (Station-to-TX):"), 0, 0)
        self.proximity_lbl = QLabel("🟡 MEDIUM TACTICAL RANGE (200m - 800m)")
        self.proximity_lbl.setStyleSheet("color: #eab308; font-weight: bold; font-size: 12px;")
        rf_layout.addWidget(self.proximity_lbl, 0, 1)

        self.sniffer_rssi_bar = QProgressBar()
        self.sniffer_rssi_bar.setRange(-115, -25)
        self.sniffer_rssi_bar.setValue(-115)
        self.sniffer_rssi_bar.setTextVisible(False)
        self.sniffer_rssi_bar.setFixedHeight(8)
        self.sniffer_rssi_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #38bdf8; border-radius: 3px; }")
        rf_layout.addWidget(self.sniffer_rssi_bar, 1, 0, 1, 2)

        rf_layout.addWidget(QLabel("Drone RF Link Quality:"), 2, 0)
        self.link_margin_lbl = QLabel("🟢 NOMINAL LINK (100% RC Integrity)")
        self.link_margin_lbl.setStyleSheet("color: #22c55e; font-weight: bold; font-size: 12px;")
        rf_layout.addWidget(self.link_margin_lbl, 2, 1)

        self.drone_lq_bar = QProgressBar()
        self.drone_lq_bar.setRange(0, 100)
        self.drone_lq_bar.setValue(0)
        self.drone_lq_bar.setTextVisible(False)
        self.drone_lq_bar.setFixedHeight(8)
        self.drone_lq_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #22c55e; border-radius: 3px; }")
        rf_layout.addWidget(self.drone_lq_bar, 3, 0, 1, 2)
        layout.addWidget(rf_box)

        # Stats Grid (Telemetry Readouts)
        grid = QGridLayout()
        self.drone_rssi_lbl = QLabel("Sniffer RSSI: -- dBm")
        self.drone_snr_lbl = QLabel("Sniffer SNR: -- dB")
        self.drone_lq_lbl = QLabel("Drone Link Quality: --%")
        self.drone_remote_rssi_lbl = QLabel("Drone RSSI: -- dBm")
        self.drone_vbat_lbl = QLabel("LiPo Voltage: -- V")
        self.drone_curr_lbl = QLabel("Current Draw: -- A")
        self.drone_pilot_lbl = QLabel("Pilot Hash: --")
        self.drone_arm_lbl = QLabel("Arm State: DISARMED")

        for lbl in [self.drone_rssi_lbl, self.drone_snr_lbl, self.drone_lq_lbl, self.drone_remote_rssi_lbl,
                    self.drone_vbat_lbl, self.drone_curr_lbl, self.drone_pilot_lbl, self.drone_arm_lbl]:
            lbl.setStyleSheet("background-color: #0f172a; color: #38bdf8; border: 1px solid #1e293b; padding: 6px; border-radius: 4px; font-weight: bold; font-size: 12px;")

        grid.addWidget(self.drone_rssi_lbl, 0, 0)
        grid.addWidget(self.drone_snr_lbl, 0, 1)
        grid.addWidget(self.drone_lq_lbl, 1, 0)
        grid.addWidget(self.drone_remote_rssi_lbl, 1, 1)
        grid.addWidget(self.drone_vbat_lbl, 2, 0)
        grid.addWidget(self.drone_curr_lbl, 2, 1)
        grid.addWidget(self.drone_pilot_lbl, 3, 0)
        grid.addWidget(self.drone_arm_lbl, 3, 1)
        layout.addLayout(grid)

        layout.addStretch()
        return drone_widget

    def create_drone_video_ui(self):
        video_widget = QWidget()
        layout = QVBoxLayout(video_widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Tactical Video Display Screen
        self.video_display = VideoDisplayWidget()
        self.video_display.setFixedHeight(210)
        layout.addWidget(self.video_display)

        # Source Selection Group
        source_group = QGroupBox("Video Stream Source")
        source_layout = QVBoxLayout(source_group)
        source_layout.setContentsMargins(6, 6, 6, 6)

        self.video_source_combo = QComboBox()
        self.video_source_combo.addItems([
            "📡 Native HackRF Demod (5.8G / 1.2G)",
            "🌐 SDRangel UDP Stream (udp://127.0.0.1:5005)",
            "🌐 Custom RTSP / UDP / HTTP Stream"
        ])
        self.video_source_combo.currentTextChanged.connect(self.on_video_source_changed)
        source_layout.addWidget(self.video_source_combo)

        # FPV Channel Row
        self.fpv_chan_row = QWidget()
        chan_row_layout = QHBoxLayout(self.fpv_chan_row)
        chan_row_layout.setContentsMargins(0, 0, 0, 0)
        
        self.fpv_channel_combo = QComboBox()
        for label, mhz in FPV_VIDEO_CHANNELS.items():
            self.fpv_channel_combo.addItem(label, mhz)
        self.fpv_channel_combo.setCurrentIndex(4) # Default RaceBand R5 (5806 MHz)
        self.fpv_channel_combo.currentIndexChanged.connect(self.on_fpv_channel_changed)
        
        chan_row_layout.addWidget(QLabel("Preset:"))
        chan_row_layout.addWidget(self.fpv_channel_combo)
        source_layout.addWidget(self.fpv_chan_row)

        # Stream URL Row (for SDRangel / RTSP)
        self.stream_url_row = QWidget()
        stream_url_layout = QHBoxLayout(self.stream_url_row)
        stream_url_layout.setContentsMargins(0, 0, 0, 0)
        
        self.stream_url_input = QLineEdit("udp://127.0.0.1:5005")
        self.stream_url_input.setToolTip("URL or address for SDRangel or RTSP stream.\nExample: udp://127.0.0.1:5005 or rtsp://127.0.0.1:8554/live")
        stream_url_layout.addWidget(QLabel("Stream URI:"))
        stream_url_layout.addWidget(self.stream_url_input)
        source_layout.addWidget(self.stream_url_row)
        self.stream_url_row.hide()

        layout.addWidget(source_group)

        # Demodulator Tuning & Display Controls
        tuning_group = QGroupBox("Demodulator & DSP Tuning")
        tuning_layout = QGridLayout(tuning_group)
        tuning_layout.setContentsMargins(6, 6, 6, 6)

        self.video_standard_combo = QComboBox()
        self.video_standard_combo.addItems(["PAL (64.0 µs / 1280 px)", "NTSC (63.55 µs / 1271 px)"])
        self.video_standard_combo.currentIndexChanged.connect(self.update_video_tuning)
        tuning_layout.addWidget(QLabel("Standard:"), 0, 0)
        tuning_layout.addWidget(self.video_standard_combo, 0, 1)

        self.video_palette_combo = QComboBox()
        self.video_palette_combo.addItems(["Grayscale (Analog CRT)", "Tactical NVG (Green)", "Amber (Thermal FLIR)"])
        self.video_palette_combo.currentIndexChanged.connect(self.update_video_tuning)
        tuning_layout.addWidget(QLabel("Palette:"), 1, 0)
        tuning_layout.addWidget(self.video_palette_combo, 1, 1)

        self.invert_polarity_cb = QCheckBox("Invert Polarity (Sync Up)")
        self.invert_polarity_cb.setToolTip("Toggle if video sync is inverted or video signal appears inverted.")
        self.invert_polarity_cb.toggled.connect(self.update_video_tuning)
        tuning_layout.addWidget(self.invert_polarity_cb, 2, 0, 1, 2)

        self.show_reticle_cb = QCheckBox("Tactical Crosshairs / Reticle")
        self.show_reticle_cb.toggled.connect(self.toggle_video_reticle)
        tuning_layout.addWidget(self.show_reticle_cb, 3, 0, 1, 2)

        # Contrast & Brightness Sliders
        tuning_layout.addWidget(QLabel("Contrast:"), 4, 0)
        self.video_contrast_slider = QSlider(Qt.Orientation.Horizontal)
        self.video_contrast_slider.setRange(50, 300)
        self.video_contrast_slider.setValue(100)
        self.video_contrast_slider.valueChanged.connect(self.update_video_tuning)
        tuning_layout.addWidget(self.video_contrast_slider, 4, 1)

        tuning_layout.addWidget(QLabel("Brightness:"), 5, 0)
        self.video_brightness_slider = QSlider(Qt.Orientation.Horizontal)
        self.video_brightness_slider.setRange(-50, 50)
        self.video_brightness_slider.setValue(0)
        self.video_brightness_slider.valueChanged.connect(self.update_video_tuning)
        tuning_layout.addWidget(self.video_brightness_slider, 5, 1)

        layout.addWidget(tuning_group)

        # Action Buttons
        btn_layout = QHBoxLayout()
        self.toggle_video_btn = QPushButton("▶ START VIDEO STREAM")
        self.toggle_video_btn.setStyleSheet("background-color: #10b981; color: white; font-weight: bold; padding: 8px;")
        self.toggle_video_btn.clicked.connect(self.toggle_video_stream)
        
        self.video_snapshot_btn = QPushButton("📸 SNAPSHOT")
        self.video_snapshot_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-weight: bold; border: 1px solid #38bdf8;")
        self.video_snapshot_btn.clicked.connect(self.capture_video_snapshot)
        
        btn_layout.addWidget(self.toggle_video_btn)
        btn_layout.addWidget(self.video_snapshot_btn)
        layout.addLayout(btn_layout)

        layout.addStretch()
        return video_widget

    def on_video_source_changed(self, text):
        if "Native" in text:
            self.fpv_chan_row.show()
            self.stream_url_row.hide()
            if hasattr(self, 'video_display'):
                chan_text = self.fpv_channel_combo.currentText()
                self.video_display.set_stream_info(chan_text, "NATIVE HACKRF (PAL 64.0µs)")
        else:
            self.fpv_chan_row.hide()
            self.stream_url_row.show()
            if hasattr(self, 'video_display'):
                uri = self.stream_url_input.text()
                self.video_display.set_stream_info(uri, "EXTERNAL STREAM")

    def on_fpv_channel_changed(self, index):
        freq_mhz = self.fpv_channel_combo.currentData()
        if freq_mhz:
            if hasattr(self, 'freq_input'):
                self.freq_input.setValue(float(freq_mhz))
            if hasattr(self, 'video_display'):
                chan_text = self.fpv_channel_combo.currentText()
                self.video_display.set_stream_info(chan_text, "NATIVE HACKRF (PAL 64.0µs)")
            if self.native_video_thread and self.native_video_thread.isRunning():
                self.native_video_thread.set_freq(freq_mhz)

    def update_video_tuning(self):
        standard = "PAL" if "PAL" in self.video_standard_combo.currentText() else "NTSC"
        invert = self.invert_polarity_cb.isChecked()
        palette_text = self.video_palette_combo.currentText()
        palette = "TACTICAL_GREEN" if "Green" in palette_text else ("AMBER_FLIR" if "Amber" in palette_text else "GRAYSCALE")
        contrast = self.video_contrast_slider.value() / 100.0
        brightness = self.video_brightness_slider.value()
        
        if self.native_video_thread and self.native_video_thread.isRunning():
            self.native_video_thread.set_tuning(standard, invert, palette, brightness, contrast)

    def toggle_video_reticle(self, checked):
        if hasattr(self, 'video_display'):
            self.video_display.show_reticle = checked
            self.video_display.update()

    def toggle_video_stream(self):
        if self.is_video_streaming:
            self.stop_video_stream()
        else:
            self.start_video_stream()

    def start_video_stream(self):
        source_mode = self.video_source_combo.currentText()
        if "Native" in source_mode:
            freq_mhz = self.fpv_channel_combo.currentData() or 5806
            standard = "PAL" if "PAL" in self.video_standard_combo.currentText() else "NTSC"
            invert = self.invert_polarity_cb.isChecked()
            palette_text = self.video_palette_combo.currentText()
            palette = "TACTICAL_GREEN" if "Green" in palette_text else ("AMBER_FLIR" if "Amber" in palette_text else "GRAYSCALE")
            contrast = self.video_contrast_slider.value() / 100.0
            brightness = self.video_brightness_slider.value()
            lna = self.lna_input.value()
            vga = self.vga_input.value()

            # Temporarily pause background SDR spectrum thread to yield HackRF USB device
            if self.hackrf_thread and self.hackrf_thread.isRunning():
                self.hackrf_thread.stop()
                self.hackrf_thread.wait(500)

            self.native_video_thread = NativeHackRFVideoThread(
                freq_mhz=freq_mhz,
                standard=standard,
                invert_polarity=invert,
                lna=lna,
                vga=vga
            )
            self.native_video_thread.set_tuning(standard, invert, palette, brightness, contrast)
            self.native_video_thread.frame_ready.connect(self.on_video_frame)
            self.native_video_thread.status_signal.connect(self.log_event)
            self.native_video_thread.start()
            
            chan_name = self.fpv_channel_combo.currentText()
            self.video_display.set_stream_info(chan_name, f"NATIVE HACKRF ({standard})")
            self.log_event(f"Started Native HackRF FPV Video Demodulator on {freq_mhz} MHz ({standard})")
        else:
            stream_url = self.stream_url_input.text().strip()
            self.external_video_thread = ExternalStreamVideoThread(stream_url=stream_url)
            self.external_video_thread.frame_ready.connect(self.on_video_frame)
            self.external_video_thread.status_signal.connect(self.log_event)
            self.external_video_thread.start()
            
            self.video_display.set_stream_info(stream_url, "EXTERNAL STREAM")
            self.log_event(f"Connecting to external video stream: {stream_url}")

        self.is_video_streaming = True
        self.toggle_video_btn.setText("⏹ STOP VIDEO STREAM")
        self.toggle_video_btn.setStyleSheet("background-color: #ef4444; color: white; font-weight: bold; padding: 8px;")

    def stop_video_stream(self):
        if self.native_video_thread:
            self.native_video_thread.stop()
            self.native_video_thread = None
        if self.external_video_thread:
            self.external_video_thread.stop()
            self.external_video_thread = None
            
        self.is_video_streaming = False
        self.toggle_video_btn.setText("▶ START VIDEO STREAM")
        self.toggle_video_btn.setStyleSheet("background-color: #10b981; color: white; font-weight: bold; padding: 8px;")
        
        # Resume background SDR spectrum thread if Stare/Sweep mode active
        if hasattr(self, 'current_mode') and (self.hackrf_thread is None or not self.hackrf_thread.isRunning()):
            self.start_sdr()
            
        self.log_event("Stopped Video Stream.")

    def on_video_frame(self, qimage, sync_locked, fps):
        if hasattr(self, 'video_display'):
            self.video_display.update_frame(qimage, sync_locked, fps)

    def capture_video_snapshot(self):
        if not hasattr(self, 'video_display') or self.video_display.current_pixmap is None:
            self.log_event("Cannot snapshot: No video frame available.")
            return
            
        os.makedirs("captures", exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        chan_mhz = self.fpv_channel_combo.currentData() if hasattr(self, 'fpv_channel_combo') else 5806
        filepath = os.path.join("captures", f"FPV_Capture_{chan_mhz}MHz_{timestamp}.png")
        
        self.video_display.current_pixmap.save(filepath, "PNG")
        self.log_event(f"📸 Saved Video Snapshot: {filepath}")

    def start_heltec(self):
        port = self.heltec_port_combo.currentText() if hasattr(self, 'heltec_port_combo') else "COM6"
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
        if hasattr(self, 'heltec_connect_btn'):
            if is_connected:
                self.heltec_connect_btn.setStyleSheet("background-color: #10b981; color: white; border: 1px solid #22c55e;")
                self.heltec_connect_btn.setText("🚁 HELTEC V3: ONLINE")
            else:
                self.heltec_connect_btn.setStyleSheet("background-color: #1e293b; color: #f87171; border: 1px solid #f87171;")
                self.heltec_connect_btn.setText("🚁 HELTEC V3: RECONNECT")
        if hasattr(self, 'drone_status_label'):
            self.drone_status_label.setText(f"[ {msg.upper()} ]")

    def on_heltec_rc(self, data):
        ch1 = data['ch1']
        ch2 = data['ch2']
        ch3 = data['ch3']
        ch4 = data['ch4']
        rssi = data['rssi']
        snr = data['snr']
        armed = data['armed']

        if hasattr(self, 'heltec_connect_btn'):
            self.heltec_connect_btn.setText(f"🚁 HELTEC: {rssi:.0f}dBm")

        # Feature 1: Update Mode 2 Gimbal HUD
        if self.gimbal_hud:
            self.gimbal_hud.update_sticks(ch1, ch2, ch3, ch4, armed)

        thr_pct = max(0.0, min(100.0, (ch3 - 988.0) / 10.24))
        if hasattr(self, 'drone_sticks_lbl'):
            self.drone_sticks_lbl.setText(f"THR: {ch3} µs ({thr_pct:.0f}%) | YAW: {ch1} µs | PIT: {ch2} µs | ROL: {ch4} µs")

        if hasattr(self, 'drone_rssi_lbl'):
            self.drone_rssi_lbl.setText(f"Sniffer RSSI: {rssi:.0f} dBm")
            self.drone_snr_lbl.setText(f"Sniffer SNR: {snr:+.1f} dB")
            if armed:
                self.drone_arm_lbl.setText("Arm State: ⚠️ ARMED")
                self.drone_arm_lbl.setStyleSheet("background-color: #7f1d1d; color: #fca5a5; font-weight: bold; padding: 6px; border-radius: 4px;")
            else:
                self.drone_arm_lbl.setText("Arm State: DISARMED")
                self.drone_arm_lbl.setStyleSheet("background-color: #0f172a; color: #38bdf8; padding: 6px; border-radius: 4px;")

        # Feature 2: Flight Dynamics & Maneuver Classifier
        if hasattr(self, 'flight_classifier') and hasattr(self, 'maneuver_badge'):
            badge_text, badge_color, detail_text = self.flight_classifier.classify(ch1, ch2, ch3, ch4, armed)
            self.maneuver_badge.setText(badge_text)
            self.maneuver_badge.setStyleSheet(f"background-color: {badge_color}22; color: {badge_color}; font-weight: bold; font-size: 14px; padding: 8px; border-radius: 4px; border: 1px solid {badge_color};")
            self.maneuver_detail_lbl.setText(detail_text)

        # Feature 4: Dual-Link RF Proximity (Station to Pilot)
        if hasattr(self, 'sniffer_rssi_bar') and hasattr(self, 'proximity_lbl'):
            self.sniffer_rssi_bar.setValue(int(rssi))
            if rssi > -55:
                self.proximity_lbl.setText("🔴 IMMEDIATE VICINITY (< 50m)")
                self.proximity_lbl.setStyleSheet("color: #ef4444; font-weight: bold;")
                self.sniffer_rssi_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #ef4444; border-radius: 3px; }")
            elif rssi > -70:
                self.proximity_lbl.setText("🟠 CLOSE PROXIMITY (50m - 200m)")
                self.proximity_lbl.setStyleSheet("color: #f97316; font-weight: bold;")
                self.sniffer_rssi_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #f97316; border-radius: 3px; }")
            elif rssi > -88:
                self.proximity_lbl.setText("🟡 MEDIUM TACTICAL RANGE (200m - 800m)")
                self.proximity_lbl.setStyleSheet("color: #eab308; font-weight: bold;")
                self.sniffer_rssi_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #eab308; border-radius: 3px; }")
            else:
                self.proximity_lbl.setText("🔵 PERIMETER / LONG RANGE (> 800m)")
                self.proximity_lbl.setStyleSheet("color: #38bdf8; font-weight: bold;")
                self.sniffer_rssi_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #38bdf8; border-radius: 3px; }")

        # Feature 3: Intel DB Auto-Fingerprinting for Heltec Pilot
        self.pilot_rssi_history.append((time.time(), rssi))
        if len(self.pilot_rssi_history) > 20:
            self.pilot_rssi_history.pop(0)

        trend = "STATIONARY"
        trend_diff = 0.0
        if len(self.pilot_rssi_history) >= 6:
            dt = self.pilot_rssi_history[-1][0] - self.pilot_rssi_history[0][0]
            if dt > 1.0:
                slope = (self.pilot_rssi_history[-1][1] - self.pilot_rssi_history[0][1]) / dt
                trend_diff = self.pilot_rssi_history[-1][1] - self.pilot_rssi_history[0][1]
                if slope > 1.5:
                    trend = "CLOSING"
                elif slope < -1.5:
                    trend = "FADING"

        pilot_key = self.last_pilot_key or "ELRS_0x2156"
        if pilot_key in self.fingerprint_db:
            entry = self.fingerprint_db[pilot_key]
            entry["trend"] = trend
            entry["trend_diff"] = trend_diff
            entry["last_seen"] = datetime.datetime.now().strftime("%H:%M:%S")
            entry["last_pulse"] = 20.0
            entry["duty_cycle"] = 92.8
            entry["packet_count"] = entry.get("packet_count", 0) + 1
            if not hasattr(self, 'last_fp_save_time') or (time.time() - self.last_fp_save_time > 2.0):
                self.last_fp_save_time = time.time()
                self.save_fingerprints()
                self.refresh_fingerprint_ui()

        if not hasattr(self, 'heltec_last_arm'):
            self.heltec_last_arm = None
        if self.heltec_last_arm != armed:
            self.heltec_last_arm = armed
            self.log_event(f"⚠️ HELTEC PILOT STATE: {'ARMED' if armed else 'DISARMED'} (RSSI: {rssi:.0f} dBm)")

    def on_heltec_tlm_link(self, data):
        lq = data['drone_lq']
        drone_rssi = data['drone_rssi']
        drone_snr = data['drone_snr']
        self.last_drone_rssi = drone_rssi
        self.last_drone_lq = lq

        if hasattr(self, 'drone_lq_lbl'):
            self.drone_lq_lbl.setText(f"Drone Link Quality: {lq}%")
            self.drone_remote_rssi_lbl.setText(f"Drone RSSI: {drone_rssi} dBm")

        if hasattr(self, 'drone_lq_bar') and hasattr(self, 'link_margin_lbl'):
            self.drone_lq_bar.setValue(int(lq))
            if lq >= 85 and drone_rssi > -95:
                self.link_margin_lbl.setText(f"🟢 NOMINAL LINK ({lq}% RC Integrity)")
                self.link_margin_lbl.setStyleSheet("color: #22c55e; font-weight: bold;")
                self.drone_lq_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #22c55e; border-radius: 3px; }")
            elif lq >= 50 or drone_rssi > -105:
                self.link_margin_lbl.setText(f"🟡 MARGIN DEGRADED ({lq}% LQ | {drone_rssi}dBm)")
                self.link_margin_lbl.setStyleSheet("color: #eab308; font-weight: bold;")
                self.drone_lq_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #eab308; border-radius: 3px; }")
            else:
                self.link_margin_lbl.setText(f"🔴 CRITICAL FAILSAFE IMMINENT ({lq}% LQ)")
                self.link_margin_lbl.setStyleSheet("color: #ef4444; font-weight: bold;")
                self.drone_lq_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #ef4444; border-radius: 3px; }")

        self.geo_status_label.setText(f"[ TARGET TELEMETRY: PROTOCOL LOCKED | LQ: {lq}% | RSSI: {drone_rssi}dBm ]")
        self.geo_status_label.setStyleSheet("color: #22c55e; font-weight: bold; font-size: 15px;")
        if not hasattr(self, 'last_tlm_log_time') or (time.time() - self.last_tlm_log_time > 3.0):
            self.last_tlm_log_time = time.time()
            self.log_event(f"🛸 HELTEC DRONE TELEMETRY: LQ={lq}% | DroneRSSI={drone_rssi} dBm | SNR={drone_snr} dB")

    def on_heltec_battery(self, data):
        if hasattr(self, 'drone_vbat_lbl'):
            self.drone_vbat_lbl.setText(f"LiPo Voltage: {data['voltage']:.1f} V")
            self.drone_curr_lbl.setText(f"Current Draw: {data['current']:.1f} A")
        self.log_event(f"🔋 HELTEC DRONE BATTERY: {data['voltage']:.1f}V | {data['current']:.1f}A | {data['battery_pct']}%")

    def on_heltec_gps(self, data):
        lat = data["lat"]
        lon = data["lon"]
        if lat != 0 or lon != 0:
            self.gps_breadcrumbs_count += 1
            if hasattr(self, 'geo_breadcrumbs_lbl'):
                self.geo_breadcrumbs_lbl.setText(f"[ TRACK POINTS: {self.gps_breadcrumbs_count} ]")

            self.update_theoretical_locations(lat - 0.001, lon - 0.001, lat, lon)
            
            # Feature 5: Append point to Leaflet droneTrail polyline
            js_trail = f"addDroneTrailPoint({lat}, {lon});"
            self.geo_map_view.page().runJavaScript(js_trail)

            # Draw estimated RF proximity ring around ground station / pilot
            js_ring = f"updateRfRangeRing({lat - 0.001}, {lon - 0.001}, 250, '#38bdf8');"
            self.geo_map_view.page().runJavaScript(js_ring)

            self.log_event(f"🎯 HELTEC UAV GPS FIX: {lat:.5f}, {lon:.5f} | Alt: {data['alt']}m | Spd: {data['spd']}km/h | Sats: {data['sats']}")

    def on_heltec_sync(self, data):
        crc_hex = data['crc_init']
        pilot_id = f"0x{crc_hex}"
        uid_str = f"{data['uid4']}.{data['uid5']}"
        pilot_key = f"ELRS_{pilot_id}"
        self.last_pilot_key = pilot_key

        if hasattr(self, 'drone_pilot_lbl'):
            self.drone_pilot_lbl.setText(f"Pilot Hash: {pilot_id} (UID {uid_str})")

        # Feature 3: Auto-register into Intel DB
        if pilot_key not in self.fingerprint_db:
            self.fingerprint_db[pilot_key] = {
                "name": f"ELRS Pilot ({pilot_id})",
                "classification": "[ELRS 900MHz]",
                "found_at": 915.00,
                "protocol": "ExpressLRS 900MHz (50Hz SF8)",
                "crc_init": pilot_id,
                "uid": uid_str,
                "first_seen": datetime.datetime.now().strftime("%H:%M:%S"),
                "last_seen": datetime.datetime.now().strftime("%H:%M:%S"),
                "trend": "STATIONARY",
                "trend_diff": 0.0,
                "last_pulse": 20.0,
                "duty_cycle": 92.8,
                "packet_count": 1
            }
            self.save_fingerprints()
            self.refresh_fingerprint_ui()
            self.log_event(f"🎯 INTEL DB: Auto-registered ELRS Pilot {pilot_id} (UID {uid_str})")

        self.log_event(f"⚡ HELTEC DISCOVERED PILOT: Hash {pilot_id} | HopIdx: {data['hop_idx']} | Nonce: {data['nonce']}")

    def manual_plot_target(self):
        """Allows the user to manually test the map via the UI text fields."""
        try:
            lat = float(self.geo_lat_input.text())
            lon = float(self.geo_lon_input.text())
            self.update_theoretical_locations(lat, lon, lat + 0.002, lon + 0.002)
            self.geo_map_view.page().runJavaScript(f"addDroneTrailPoint({lat + 0.002}, {lon + 0.002});")
            self.geo_map_view.page().runJavaScript(f"updateRfRangeRing({lat}, {lon}, 300, '#38bdf8');")
        except ValueError:
            self.log_event("ERROR: Invalid manual coordinates.")

    def update_theoretical_locations(self, op_lat: float, op_lon: float, drone_lat: float, drone_lon: float):
        """
        Public method to drop markers onto the Leaflet map dynamically.
        """
        self.geo_status_label.setText("[ TARGET TELEMETRY: PROTOCOL LOCKED ]")
        self.geo_status_label.setStyleSheet("color: #22c55e; font-weight: bold; font-size: 15px;")
        
        # Execute JS to update operator (red)
        js_op = f"updateTarget('OP', {op_lat}, {op_lon}, 'ENEMY PILOT (CONTROLLER)', '#ef4444');"
        self.geo_map_view.page().runJavaScript(js_op)
        
        # Execute JS to update drone (green)
        js_drone = f"updateTarget('UAV', {drone_lat}, {drone_lon}, 'UAV ASSET', '#22c55e');"
        self.geo_map_view.page().runJavaScript(js_drone)

    def load_topology(self):
        if os.path.exists('topology.json'):
            try:
                with open('topology.json', 'r', encoding='utf-8') as f:
                    self.network_links = json.load(f)
                self.refresh_topology_ui()
            except Exception as e:
                self.log_event(f"Error loading topology: {e}")

    def save_topology(self):
        try:
            with open('topology.json', 'w', encoding='utf-8') as f:
                json.dump(self.network_links, f, indent=4)
        except: pass

    def refresh_topology_ui(self):
        self.topology_ui.clear()
        for source, targets in self.network_links.items():
            source_name = self.fingerprint_db.get(source, {}).get("name", f"0x{source}")
            total_replies = sum(targets.values())
            
            node_title = f"{source_name} (Initiated {total_replies} replies)"
            if total_replies >= 3:
                node_title = " " + node_title
                
            source_item = QTreeWidgetItem([node_title, ""])
            
            for target, count in targets.items():
                target_name = self.fingerprint_db.get(target, {}).get("name", f"0x{target}")
                child_item = QTreeWidgetItem([f" Replied by {target_name}", str(count)])
                source_item.addChild(child_item)
                
            self.topology_ui.addTopLevelItem(source_item)
        self.topology_ui.expandAll()

    def get_band_classification(self, freq_hz):
        mhz = freq_hz / 1e6
        if 3.0 <= mhz <= 30.0: return "HF (Over-the-Horizon / NVIS)"
        if 30.0 <= mhz <= 88.0: return "VHF Low (Tactical Comms / SINCGARS)"
        if 88.0 <= mhz <= 108.0: return "FM Broadcast"
        if 108.0 <= mhz <= 137.0: return "Airband (AM Aviation)"
        if 137.0 <= mhz <= 144.0: return "VHF Govt / Space"
        if 144.0 <= mhz <= 148.0: return "2m Amateur Radio"
        if 148.0 <= mhz <= 156.0: return "VHF Land Mobile (Emergency/PMR)"
        if 156.0 <= mhz <= 162.0: return "VHF Marine Band"
        if 162.0 <= mhz <= 174.0: return "VHF Land Mobile / Govt"
        if 225.0 <= mhz <= 400.0: return "UHF Military (Air/Ground/SATCOM)"
        if 400.0 <= mhz <= 420.0: return "UHF Govt / Trunked"
        if 420.0 <= mhz <= 430.0: return "UHF Amateur"
        if 430.0 <= mhz <= 434.0: return "ISM / LPD433 (Drones/Keys)"
        if 434.0 <= mhz <= 450.0: return "UHF Amateur / PMR446"
        if 450.0 <= mhz <= 470.0: return "UHF Business / PMR / FRS"
        if 868.0 <= mhz <= 870.0: return "ISM 868MHz (LoRa/Telemetry)"
        if 902.0 <= mhz <= 928.0: return "ISM 915MHz (LoRa/Drones)"
        if 930.0 <= mhz <= 960.0: return "GSM-900 / Cellular"
        if 1030.0 <= mhz <= 1090.0: return "Aviation (ADS-B / SSR)"
        if 1176.0 <= mhz <= 1227.0: return "GPS L2 / L5"
        if 1575.42 - 10 <= mhz <= 1575.42 + 10: return "GPS L1"
        if 2400.0 <= mhz <= 2500.0: return "ISM 2.4GHz (Wi-Fi/BT/Drones)"
        if 5725.0 <= mhz <= 5875.0: return "ISM 5.8GHz (Wi-Fi/FPV Drones)"
        return "UNKNOWN BAND"

    def load_fingerprints(self):
        if os.path.exists('fingerprints.json'):
            try:
                with open('fingerprints.json', 'r', encoding='utf-8') as f:
                    self.fingerprint_db = json.load(f)
                self.refresh_fingerprint_ui()
            except Exception as e:
                self.log_event(f"Error loading fingerprints: {e}")

    def save_fingerprints(self):
        try:
            with open('fingerprints.json', 'w', encoding='utf-8') as f:
                json.dump(self.fingerprint_db, f, indent=4)
        except Exception as e:
            self.log_event(f"Error saving fingerprints: {e}")

    def refresh_fingerprint_ui(self):
        self.fingerprint_ui.clear()
        for fp, data in self.fingerprint_db.items():
            name = data.get("name", f"Radio 0x{fp}")
            found_at = data.get("found_at", 0)
            trend = data.get("trend", "STATIONARY")
            diff = data.get("trend_diff", 0.0)
            pulse_ms = data.get("last_pulse", 0.0)
            duty_cycle = data.get("duty_cycle", 0.0)
            classification = data.get("classification", " UNKNOWN")
            pkts = data.get("packet_count", 0)
            
            if trend == "CLOSING":
                trend_str = f" CLOSING (+{diff:.1f}dB)"
            elif trend == "FADING":
                trend_str = f" FADING ({diff:.1f}dB)"
            else:
                trend_str = " STATIONARY"
                
            if str(fp).startswith("ELRS_"):
                uid = data.get("uid", "--")
                item = QListWidgetItem(f"🚁 {classification} | {name} [UID: {uid}] [{trend_str}] ({pkts} pkts)")
                if trend == "CLOSING":
                    item.setForeground(QColor("#ef4444"))
                elif trend == "FADING":
                    item.setForeground(QColor("#94a3b8"))
                else:
                    item.setForeground(QColor("#38bdf8"))
            else:
                pulse_str = f"[{pulse_ms:.1f}ms | {duty_cycle:.1f}% Duty]" if pulse_ms > 0 else ""
                item = QListWidgetItem(f"{classification} | {name} {pulse_str} [{trend_str}] ({found_at:.2f} MHz)")
                if trend == "CLOSING":
                    item.setForeground(QColor("red"))
                elif trend == "FADING":
                    item.setForeground(QColor("gray"))
                
            item.setData(Qt.ItemDataRole.UserRole, fp)
            self.fingerprint_ui.addItem(item)

    def fingerprint_context_menu(self, position):
        item = self.fingerprint_ui.itemAt(position)
        if item is not None:
            menu = QMenu()
            rename_action = menu.addAction("Rename Emitter")
            action = menu.exec(self.fingerprint_ui.viewport().mapToGlobal(position))
            
            if action == rename_action:
                raw_hex = item.data(Qt.ItemDataRole.UserRole)
                current_name = self.fingerprint_db[raw_hex].get("name", f"Radio 0x{raw_hex}")
                new_name, ok = QInputDialog.getText(self, "Rename Emitter", f"Enter name for emitter 0x{raw_hex}:", QLineEdit.EchoMode.Normal, current_name)
                if ok and new_name:
                    self.fingerprint_db[raw_hex]["name"] = new_name
                    self.save_fingerprints()
                    self.refresh_fingerprint_ui()
                    self.log_event(f"Renamed emitter 0x{raw_hex} to {new_name}")

    def load_watchlist(self):
        if not os.path.exists('watchlist.json'):
            default_wl = [
                {"name": "DJI OcuSync (Video Link)", "min_bw": 8000000, "max_bw": 22000000, "mod": "Wideband/Impulsive"},
                {"name": "ExpressLRS/Crossfire FPV", "min_bw": 200000, "max_bw": 1200000, "mod": "Wideband/Impulsive"},
                {"name": "Orlan-10 Telemetry (UAV)", "min_bw": 15000, "max_bw": 50000, "mod": "FM/FSK/CW"},
                {"name": "DMR / P25 Digital Radio", "min_bw": 10000, "max_bw": 15000, "mod": "FM/FSK/CW"}
            ]
            try:
                with open('watchlist.json', 'w', encoding='utf-8') as f:
                    json.dump(default_wl, f, indent=4)
            except: pass
            self.watchlist = default_wl
        else:
            try:
                with open('watchlist.json', 'r', encoding='utf-8') as f:
                    self.watchlist = json.load(f)
            except Exception as e:
                self.log_event(f"Watchlist Error: {e}")
                
        self.refresh_watchlist_ui()

    def refresh_watchlist_ui(self):
        self.watchlist_ui.clear()
        for sig in self.watchlist:
            self.watchlist_ui.addItem(f"{sig['name']} ({sig['mod']}, {sig['min_bw']/1000:.0f}-{sig['max_bw']/1000:.0f} kHz)")

    def add_watchlist_item(self):
        name = self.wl_name_input.text()
        if not name: return
        
        min_bw = self.wl_min_bw.value() * 1000
        max_bw = self.wl_max_bw.value() * 1000
        mod = self.wl_mod.currentText()
        
        self.watchlist.append({
            "name": name,
            "min_bw": min_bw,
            "max_bw": max_bw,
            "mod": mod
        })
        
        try:
            with open('watchlist.json', 'w', encoding='utf-8') as f:
                json.dump(self.watchlist, f, indent=4)
            self.log_event(f"Added {name} to Watchlist")
            self.wl_name_input.clear()
        except: pass
        self.refresh_watchlist_ui()

    def watchlist_context_menu(self, position):
        item = self.watchlist_ui.itemAt(position)
        if item is not None:
            menu = QMenu()
            delete_action = menu.addAction("Delete Profile")
            action = menu.exec(self.watchlist_ui.viewport().mapToGlobal(position))
            
            if action == delete_action:
                idx = self.watchlist_ui.row(item)
                deleted = self.watchlist.pop(idx)
                try:
                    with open('watchlist.json', 'w', encoding='utf-8') as f:
                        json.dump(self.watchlist, f, indent=4)
                    self.log_event(f"Deleted {deleted['name']} from Watchlist")
                except: pass
                self.refresh_watchlist_ui()

    def clear_intel_db(self):
        self.fingerprint_db = {}
        self.network_links = {}
        self.save_fingerprints()
        self.save_topology()
        self.refresh_fingerprint_ui()
        self.refresh_topology_ui()
        self.log_event("INTEL DB WIPED: Fingerprints and Topology reset for fresh session.")

    def mode_changed(self, text):
        if "SWEEP" in text:
            self.freq_input.hide()
            self.freq_label.setText("Sweep Range:")
            self.sweep_start_input.show()
            self.sweep_end_input.show()
            self.const_plot.hide()
            self.vfo_region.hide()
            self.graph_layout.addWidget(self.waterfall_plot, 1, 0, 1, 2)
        else:
            self.freq_input.show()
            self.freq_label.setText("Center Freq:")
            self.sweep_start_input.hide()
            self.sweep_end_input.hide()
            self.graph_layout.addWidget(self.const_plot, 1, 1)
            self.graph_layout.addWidget(self.waterfall_plot, 1, 0)
            self.vfo_region.show()
            self.const_plot.show()

    def toggle_mask_mode(self, checked):
        if checked:
            self.mask_mode_btn.setText("MASK MODE: ON")
            self.mask_mode_btn.setStyleSheet("background-color: #ef4444; color: white;")
            self.fft_plot.setMouseEnabled(x=False, y=False)
        else:
            self.mask_mode_btn.setText("MASK MODE: OFF")
            self.mask_mode_btn.setStyleSheet("background-color: #475569; color: white;")
            self.fft_plot.setMouseEnabled(x=True, y=False)

    def toggle_video_mode(self, checked):
        self.video_mode = checked
        if hasattr(self, 'hackrf_thread') and self.hackrf_thread:
            self.hackrf_thread.decode_video = checked
        if checked:
            self.decode_video_btn.setStyleSheet("background-color: #ef4444; color: white;")
            self.const_plot.hide()
            self.video_plot.show()
        else:
            self.decode_video_btn.setStyleSheet("background-color: #475569; color: white;")
            self.video_plot.hide()
            self.const_plot.show()

    def toggle_sidebar(self):
        self.sidebar_widget.setVisible(not self.sidebar_widget.isVisible())

    def bin_to_freq(self, bin_idx):
        if self.current_mode == "SWEEP":
            return self.sweep_start_input.value() * 1e6 + bin_idx * 1000000
        else:
            center_hz = self.freq_input.value() * 1e6
            return center_hz + (bin_idx - 512) * (2000000 / 1024)

    def freq_to_bin(self, freq_hz):
        if self.current_mode == "SWEEP":
            return (freq_hz - self.sweep_start_input.value() * 1e6) / 1000000
        else:
            center_hz = self.freq_input.value() * 1e6
            return 512 + (freq_hz - center_hz) * 1024 / 2000000

    def save_masks(self):
        try:
            with open('masks.json', 'w', encoding='utf-8') as f:
                json.dump(self.global_masks, f)
            self.log_event("Saved masks to disk")
        except Exception as e:
            self.log_event(f"Error saving masks: {e}")

    def load_masks(self):
        if not os.path.exists('masks.json'): return
        try:
            with open('masks.json', 'r', encoding='utf-8') as f:
                self.global_masks = json.load(f)
            self.refresh_mask_list()
        except Exception as e:
            pass

    def refresh_mask_list(self):
        self.mask_list.clear()
        for m in self.global_masks:
            item = QListWidgetItem(m["name"])
            self.mask_list.addItem(item)
            
    def delete_mask(self, item):
        name = item.text()
        self.global_masks = [m for m in self.global_masks if m["name"] != name]
        self.refresh_mask_list()
        self.draw_visible_regions()
        self.log_event(f"Deleted mask {name}")

    def draw_visible_regions(self):
        for r in self.whitelist_regions.values():
            self.fft_plot.removeItem(r)
        self.whitelist_regions.clear()
        
        if self.current_mode == "SWEEP":
            min_view_hz = self.sweep_start_input.value() * 1e6
            max_view_hz = self.sweep_end_input.value() * 1e6
        else:
            center_hz = self.freq_input.value() * 1e6
            min_view_hz = center_hz - 1000000
            max_view_hz = center_hz + 1000000
        
        for idx, m in enumerate(self.global_masks):
            if m["max_hz"] >= min_view_hz and m["min_hz"] <= max_view_hz:
                bin_min = self.freq_to_bin(m["min_hz"])
                bin_max = self.freq_to_bin(m["max_hz"])
                region = pg.LinearRegionItem(values=[bin_min, bin_max], brush=(100, 100, 100, 80))
                region.global_mask_idx = idx
                region.sigRegionChangeFinished.connect(self.mask_region_adjusted)
                self.fft_plot.addItem(region)
                self.whitelist_regions[idx] = region

    def mask_region_adjusted(self, region):
        idx = getattr(region, 'global_mask_idx', None)
        if idx is not None and idx < len(self.global_masks):
            bounds = region.getRegion()
            f_min = self.bin_to_freq(bounds[0])
            f_max = self.bin_to_freq(bounds[1])
            self.global_masks[idx]["min_hz"] = f_min
            self.global_masks[idx]["max_hz"] = f_max
            new_name = f"Mask {f_min/1e6:.3f} - {f_max/1e6:.3f} MHz"
            self.global_masks[idx]["name"] = new_name
            self.refresh_mask_list()
            self.log_event(f"Adjusted mask bounds")

    def start_sdr(self):
        if self.hackrf_thread is not None:
            self.hackrf_thread.stop()
            self.hackrf_thread.wait()

        lna = self.lna_input.value()
        vga = self.vga_input.value()

        if "SWEEP" in self.mode_selector.currentText():
            self.current_mode = "SWEEP"
            start_hz = self.sweep_start_input.value() * 1e6
            end_hz = self.sweep_end_input.value() * 1e6
            num_bins = int((end_hz - start_hz) / 1000000)
            if num_bins <= 0: num_bins = 1
            
            self.waterfall_data = np.zeros((100, num_bins))
            self.fft_plot.setXRange(0, num_bins)
            self.hop_history.clear()
            
            self.hackrf_thread = HackRFSweepThread(start_hz, end_hz, lna, vga)
            self.log_event(f"SWEEP MODE ENGAGED - {start_hz/1e6} to {end_hz/1e6} MHz")
        else:
            self.current_mode = "STARE"
            freq_hz = int(self.freq_input.value() * 1e6)
            
            self.waterfall_data = np.zeros((100, 1024))
            self.fft_plot.setXRange(0, 1024)
            
            self.hackrf_thread = HackRFThread(freq_hz, lna, vga)
            if hasattr(self, 'audio_playing') and self.audio_playing:
                self.hackrf_thread.play_audio = True
            self.log_event(f"STARE MODE ENGAGED - {freq_hz/1e6} MHz")

        self.waterfall_image.setImage(self.waterfall_data, autoLevels=False, levels=(0, self.wf_sens_slider.value()))
        self.mod_label.setText("Modulation: INITIALIZING...")

        self.hackrf_thread.error_signal.connect(self.handle_error)
        self.hackrf_thread.start()
        self.draw_visible_regions()

    def handle_error(self, err):
        self.log_event(f"ERROR: {err}")

    def log_event(self, msg):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {msg}")

    def fft_mouse_press(self, event):
        scene_pos = self.fft_plot.mapToScene(event.pos())
        items = self.fft_plot.scene().items(scene_pos)
        clicked_on_handle = any(isinstance(item, pg.InfiniteLine) for item in items)
        clicked_on_region = any(isinstance(item, pg.LinearRegionItem) for item in items)
        
        if self.current_mode == "SWEEP" and event.button() == Qt.MouseButton.LeftButton and not (clicked_on_handle or clicked_on_region):
            view_pos = self.fft_plot.plotItem.vb.mapSceneToView(scene_pos)
            freq_mhz = self.bin_to_freq(view_pos.x()) / 1e6
            self.log_event(f"HUNTER-KILLER: Snapping to {freq_mhz:.2f} MHz")
            self.mode_selector.setCurrentText("STARE MODE (2MHz)")
            self.freq_input.setValue(freq_mhz)
            self.start_sdr()
            return

        if self.mask_mode_btn.isChecked() and event.button() == Qt.MouseButton.LeftButton and not (clicked_on_handle or clicked_on_region):
            view_pos = self.fft_plot.plotItem.vb.mapSceneToView(scene_pos)
            self.drag_start_x = view_pos.x()
            self.current_drag_region = pg.LinearRegionItem(values=[self.drag_start_x, self.drag_start_x], brush=(100, 100, 100, 80))
            self.fft_plot.addItem(self.current_drag_region)
            event.accept()
        else:
            self.fft_plot._original_mousePressEvent(event)

    def fft_mouse_move(self, event):
        if hasattr(self, 'current_drag_region') and self.current_drag_region is not None:
            scene_pos = self.fft_plot.mapToScene(event.pos())
            view_pos = self.fft_plot.plotItem.vb.mapSceneToView(scene_pos)
            current_x = view_pos.x()
            self.current_drag_region.setRegion([min(self.drag_start_x, current_x), max(self.drag_start_x, current_x)])
            event.accept()
        else:
            self.fft_plot._original_mouseMoveEvent(event)

    def fft_mouse_release(self, event):
        if event.button() == Qt.MouseButton.LeftButton and hasattr(self, 'current_drag_region') and self.current_drag_region is not None:
            scene_pos = self.fft_plot.mapToScene(event.pos())
            view_pos = self.fft_plot.plotItem.vb.mapSceneToView(scene_pos)
            end_x = view_pos.x()
            r_min, r_max = min(self.drag_start_x, end_x), max(self.drag_start_x, end_x)
            
            self.fft_plot.removeItem(self.current_drag_region)
            self.current_drag_region = None
            
            if r_max - r_min < 5:
                clicked_mask = None
                for idx, region in self.whitelist_regions.items():
                    bounds = region.getRegion()
                    if bounds[0] <= self.drag_start_x <= bounds[1]:
                        clicked_mask = self.global_masks[idx]
                        break
                if clicked_mask:
                    self.global_masks.remove(clicked_mask)
                    self.refresh_mask_list()
                    self.draw_visible_regions()
                    self.log_event(f"Removed mask {clicked_mask['name']}")
            else:
                f_min = self.bin_to_freq(r_min)
                f_max = self.bin_to_freq(r_max)
                name = f"Mask {f_min/1e6:.3f} - {f_max/1e6:.3f} MHz"
                self.global_masks.append({
                    "name": name,
                    "min_hz": f_min,
                    "max_hz": f_max
                })
                self.refresh_mask_list()
                self.draw_visible_regions()
                self.log_event(f"Added {name}")
                
            event.accept()
        else:
            self.fft_plot._original_mouseReleaseEvent(event)

    def export_log(self):
        if not os.path.exists("logs"):
            os.makedirs("logs")
        filename = f"logs/sitrep_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}Z.txt"
        with open(filename, 'w') as f:
            f.write(self.log_text.toPlainText())
        self.log_event(f"SITREP exported to {filename}")

    def toggle_freeze(self, checked):
        if checked:
            self.freeze_btn.setStyleSheet("background-color: #38bdf8; color: black;")
            self.poll_timer.stop()
        else:
            self.freeze_btn.setStyleSheet("background-color: #475569; color: white;")
            self.poll_timer.start(33)

    def update_vfo_offset(self):
        if not hasattr(self, 'hackrf_thread') or self.hackrf_thread is None:
            return
            
        region = self.vfo_region.getRegion()
        center_bin = (region[0] + region[1]) / 2.0
        
        # 1024 bins = 20 MHz bandwidth. Center is bin 512.
        hz_per_bin = 20000000.0 / 1024.0
        offset_hz = (center_bin - 512.0) * hz_per_bin
        self.hackrf_thread.vfo_offset_hz = offset_hz

    def update_clock(self):
        zulu_time = datetime.datetime.utcnow().strftime("%H:%M:%SZ")
        self.clock_label.setText(zulu_time)

    def poll_data(self):
        if self.hackrf_thread and self.hackrf_thread.latest_data:
            data = self.hackrf_thread.latest_data
            self.hackrf_thread.latest_data = None
            self.update_ui(data)

    @pyqtSlot(dict)
    def update_ui(self, data):
        try:
            fft_data = data['fft']
            fft_max = data['fft_max']
            const_i = data['const_i']
            const_q = data['const_q']
            mod_type = data['mod_type']
            peaks = data['peaks']
            mode = data['mode']
            fingerprint = data['fingerprint']
            bw = data['bw']
            peak_power = data.get('peak_power', 0.0)
            pulse_ms = data.get('pulse_ms', 0.0)
            duty_cycle = data.get('duty_cycle', 0.0)
            ctcss = data.get('ctcss', False)

            self.fft_curve.setData(fft_data)
            self.fft_max_curve.setData(fft_max)

            filtered_peaks = []
            active_ranges = [r.getRegion() for r in self.whitelist_regions.values()]
            
            if mode == "SWEEP":
                # FHSS Tracking Logic for Sweep Mode
                noise_floor = np.median(fft_data)
                threshold = noise_floor + 25 # High threshold to filter noise variance
                current_time = time.time()
                
                for i in range(1, len(fft_data)-1):
                    if fft_data[i] > threshold and fft_data[i] > fft_data[i-1] and fft_data[i] > fft_data[i+1]:
                        freq = self.bin_to_freq(i)
                        
                        # Apply Masks
                        masked = False
                        for r_min, r_max in active_ranges:
                            if r_min <= i <= r_max:
                                masked = True
                                break
                        if not masked:
                            self.hop_history.append((current_time, freq))
                            
                self.hop_history = [h for h in self.hop_history if current_time - h[0] < 2.0]
                
                # Filter out continuous carriers (if a freq is hit constantly, it's not a hop)
                freq_counts = {}
                for h in self.hop_history:
                    freq_counts[h[1]] = freq_counts.get(h[1], 0) + 1
                    
                fhss_candidates = [h for h in self.hop_history if freq_counts[h[1]] <= 3]
                unique_freqs = set([h[1] for h in fhss_candidates])
                
                if len(unique_freqs) >= 8 and len(fhss_candidates) >= 12:
                    if current_time - self.last_fhss_alert > 3.0:
                        hops_sec = len(fhss_candidates) / 2.0
                        f_min, f_max = min(unique_freqs), max(unique_freqs)
                        
                        protocol = " Unknown FHSS Network"
                        if 860e6 <= f_min <= 930e6 and hops_sec >= 40:
                            protocol = " TBS Crossfire / ELRS 900M"
                        elif 2400e6 <= f_min <= 2485e6 and hops_sec >= 50:
                            protocol = " DJI OcuSync / ELRS 2.4G"
                        elif 5700e6 <= f_min <= 5900e6 and hops_sec >= 20:
                            protocol = " DJI Digital FPV / Walksnail"
                            
                        net_name = f"{protocol} (~{hops_sec:.0f} hops/s) | {f_min/1e6:.1f}-{f_max/1e6:.1f} MHz"
                        self.log_event(f"DRONE TELEMETRY: {net_name}")
                        
                        items = [self.fhss_ui.item(i).text() for i in range(self.fhss_ui.count())]
                        if net_name not in items:
                            self.fhss_ui.addItem(net_name)
                            
                        self.last_fhss_alert = current_time

            elif mode == "STARE":
                # Watchlist Matching
                if bw > 0:
                    for sig in self.watchlist:
                        if sig["min_bw"] <= bw <= sig["max_bw"] and sig["mod"] == mod_type:
                            mod_type = f" {sig['name']} MATCH"
                            break
                # Peak Anomaly tracking
                for p in peaks:
                    masked = False
                    for r_min, r_max in active_ranges:
                        if r_min <= p <= r_max:
                            masked = True
                            break
                    if not masked:
                        filtered_peaks.append(p)

                current_time = time.time()
                for p in filtered_peaks:
                    is_new = True
                    for active_p, last_time in self.active_events.items():
                        # Spatial Debounce: +/- 15 bins (approx 30kHz)
                        if abs(p - active_p) <= 15 and (current_time - last_time < 5.0):
                            is_new = False
                            # Refresh the timer for this active cluster
                            self.active_events[active_p] = current_time
                            break
                            
                    if is_new:
                        self.log_event(f"ANOMALY: Peak detected at {self.bin_to_freq(p)/1e6:.2f} MHz")
                        self.active_events[p] = current_time
                        
                self.active_events = {k: v for k, v in self.active_events.items() if current_time - v < 10.0}

                # Watchlist & Fingerprint
                if "" in mod_type and mod_type != self.last_mod_type:
                    self.log_event(mod_type)
                    self.sidebar_tabs.setCurrentIndex(1) # Auto-switch to Intel tab
                    
                if fingerprint:
                    f_hz = self.freq_input.value()
                    
                    # Fuzzy match to handle LO drift across sessions
                    matched_fp = None
                    try:
                        top_bins = sorted([int(fingerprint[i:i+2], 16) for i in (0, 2, 4)])
                        for known_hex in self.fingerprint_db.keys():
                            known_bins = sorted([int(known_hex[i:i+2], 16) for i in (0, 2, 4)])
                            if sum(abs(a - b) for a, b in zip(top_bins, known_bins)) <= 12:
                                matched_fp = known_hex
                                break
                    except: pass
                    
                    if matched_fp:
                        fingerprint = matched_fp

                    # --- CONTINUOUS TRACKING (Runs every frame) ---
                    if fingerprint not in self.fingerprint_db:
                        self.fingerprint_db[fingerprint] = {
                            "found_at": f_hz, 
                            "name": f"Radio 0x{fingerprint}", 
                            "classification": self.get_band_classification(f_hz * 1e6),
                            "last_seen": current_time,
                            "power_history": [peak_power],
                            "trend": "STATIONARY",
                            "trend_diff": 0.0,
                            "last_pulse": pulse_ms
                        }
                        self.save_fingerprints()
                        self.refresh_fingerprint_ui()
                        self.log_event(f"NEW EMITTER FINGERPRINTED: 0x{fingerprint}")
                    else:
                        db_entry = self.fingerprint_db[fingerprint]
                        db_entry["last_seen"] = current_time
                        if pulse_ms > 0:
                            db_entry["last_pulse"] = pulse_ms
                        if duty_cycle > 0:
                            db_entry["duty_cycle"] = duty_cycle
                            band_tag = self.get_band_classification(f_hz * 1e6)
                            if duty_cycle > 80.0:
                                if ctcss:
                                    db_entry["classification"] = f"{band_tag} [UNENCRYPTED ANALOG VOICE]"
                                else:
                                    db_entry["classification"] = f"{band_tag} [ENCRYPTED DIGITAL / DATA]"
                            elif duty_cycle > 20.0:
                                db_entry["classification"] = f"{band_tag} [GCS / HIGH-RATE DATA]"
                            elif duty_cycle > 2.0:
                                db_entry["classification"] = f"{band_tag} [UAV TELEMETRY]"
                            else:
                                db_entry["classification"] = f"{band_tag} [MANUAL BURST]"
                        
                        # Approach Profiling Logic (Rolling RSSI Window)
                        last_power_update = db_entry.get("last_power_update", 0)
                        if current_time - last_power_update >= 0.5: # Faster 0.5s updates for car keys
                            history = db_entry.get("power_history", [])
                            history.append(peak_power)
                            if len(history) > 10:
                                history.pop(0)
                            db_entry["power_history"] = history
                            db_entry["last_power_update"] = current_time
                            
                            if len(history) >= 4:
                                half = len(history) // 2
                                first_half_avg = sum(history[:half]) / half
                                second_half_avg = sum(history[half:]) / (len(history) - half)
                                diff = second_half_avg - first_half_avg
                                
                                # Car Key / Saturation Inversion Fix:
                                # If signal is extremely strong (e.g. > 30dB) and drops suddenly, 
                                # it's usually clipping the ADC. We treat massive drops on strong signals as CLOSING.
                                is_saturated = (first_half_avg > 30.0)
                                
                                if diff > 1.5 or (is_saturated and diff < -5.0):
                                    db_entry["trend"] = "CLOSING"
                                    if current_time - db_entry.get("last_alert", 0) > 5.0:
                                        self.log_event(f" TARGET CLOSING: {db_entry.get('name')} (Delta: {diff:.1f}dB)")
                                        db_entry["last_alert"] = current_time
                                elif diff < -1.5:
                                    db_entry["trend"] = "FADING"
                                else:
                                    db_entry["trend"] = "STATIONARY"
                                
                                db_entry["trend_diff"] = diff
                                
                            self.save_fingerprints()
                            self.refresh_fingerprint_ui()

                    # --- EVENT TRIGGERING (Runs once per burst) ---
                    if fingerprint != self.last_fingerprint:
                        db_entry = self.fingerprint_db[fingerprint]
                        last_event_seen = db_entry.get("last_event_seen", 0)
                        if current_time - last_event_seen > 15.0:
                            name = db_entry.get("name", f"Radio 0x{fingerprint}")
                            self.log_event(f"KNOWN EMITTER ACTIVE: {name} (0x{fingerprint})")
                        db_entry["last_event_seen"] = current_time
                        
                    # Network Topology Logic
                    if fingerprint != self.current_active_fingerprint:
                        if self.current_active_fingerprint and (current_time - self.last_transmission_end < 5.0):
                            source = self.current_active_fingerprint
                            target = fingerprint
                            
                            if source not in self.network_links:
                                self.network_links[source] = {}
                            self.network_links[source][target] = self.network_links[source].get(target, 0) + 1
                            
                            self.log_event(f" LINK DETECTED: 0x{source} replied to by 0x{target}")
                            self.save_topology()
                            self.refresh_topology_ui()
                            
                        self.current_active_fingerprint = fingerprint
                        
                    self.last_transmission_end = current_time
                    self.last_fingerprint = fingerprint
                else:
                    if self.current_active_fingerprint:
                        self.current_active_fingerprint = None
                        self.last_transmission_end = current_time
                    self.last_fingerprint = None

                if mod_type != self.last_mod_type and mod_type != "UNKNOWN":
                    self.last_mod_type = mod_type

                peak_x = filtered_peaks
                peak_y = [fft_data[p] for p in filtered_peaks]
                self.peak_scatter.setData(peak_x, peak_y)
                self.const_scatter.setData(const_i, const_q)
                
                if len(filtered_peaks) > 0:
                    best_p = max(filtered_peaks, key=lambda p: fft_data[p])
                    
                    if 'bw' in locals() and bw > 0:
                        bw_bins = bw / (20e6 / 1024)
                        self.bw_left.setPos(best_p - bw_bins/2.0)
                        self.bw_right.setPos(best_p + bw_bins/2.0)
                        self.bw_left.show()
                        self.bw_right.show()
                    else:
                        self.bw_left.hide()
                        self.bw_right.hide()
                else:
                    self.bw_left.hide()
                    self.bw_right.hide()

            self.waterfall_data[:-1, :] = self.waterfall_data[1:, :]
            self.waterfall_data[-1, :] = fft_data
            self.waterfall_image.setImage(self.waterfall_data, autoLevels=False, levels=(0, self.wf_sens_slider.value()))
            
            if hasattr(self, 'video_mode') and self.video_mode and hasattr(self, 'hackrf_thread') and self.hackrf_thread:
                try:
                    cv2.namedWindow("HackRF FPV Intercept", cv2.WINDOW_NORMAL)
                    while not self.hackrf_thread.video_q.empty():
                        frame_img = self.hackrf_thread.video_q.get_nowait()
                        
                        # Force the dynamic chunk into a standard FPV aspect ratio
                        display_frame = cv2.resize(frame_img, (800, 600))
                        
                        cv2.imshow("HackRF FPV Intercept", display_frame)
                        cv2.waitKey(1) 
                except queue.Empty:
                    pass

            if mode == "STARE":
                self.mod_label.setText(f"Modulation: {mod_type} | Est. BW: {bw/1000:.0f} kHz")
            else:
                self.mod_label.setText("Modulation: SWEEPING (Hop Tracker Active)")

        except Exception as e:
            pass

    def demodulate_selected(self):
        item = self.fingerprint_ui.currentItem()
        if not item:
            self.log_event("Error: No signal selected to demodulate.")
            return
            
        raw_hex = item.data(Qt.ItemDataRole.UserRole)
        db_entry = self.fingerprint_db.get(raw_hex)
        if not db_entry:
            return
            
        classification = db_entry.get("classification", "")
        if "UNENCRYPTED ANALOG VOICE" not in classification:
            self.log_event(f"Error: Signal 0x{raw_hex} is not confirmed analog voice. Use Force Demod to bypass.")
            return
            
        freq_mhz = db_entry.get("found_at", self.freq_input.value())
        self.launch_demodulator(freq_mhz)

    def force_demodulate(self):
        item = self.fingerprint_ui.currentItem()
        if not item:
            freq_mhz = self.freq_input.value()
        else:
            raw_hex = item.data(Qt.ItemDataRole.UserRole)
            db_entry = self.fingerprint_db.get(raw_hex, {})
            freq_mhz = db_entry.get("found_at", self.freq_input.value())
            
        self.launch_demodulator(freq_mhz)

    def launch_demodulator(self, freq_mhz):
        if hasattr(self, 'audio_playing') and self.audio_playing:
            if self.hackrf_thread:
                self.hackrf_thread.play_audio = False
            self.audio_playing = False
            self.demod_btn.setText(" DEMODULATE")
            self.force_demod_btn.setText(" FORCE DEMOD")
            self.log_event(" Stopped Native Audio Demodulator.")
            return

        self.log_event(f" Launching Native FM Demodulator on {freq_mhz:.3f} MHz...")
        self.freq_input.setValue(freq_mhz)
        self.audio_playing = True
        self.start_sdr()
        
        self.demod_btn.setText(" STOP AUDIO")
        self.force_demod_btn.setText(" STOP AUDIO")

    def closeEvent(self, event):
        if hasattr(self, 'native_video_thread') and self.native_video_thread:
            self.native_video_thread.stop()
        if hasattr(self, 'external_video_thread') and self.external_video_thread:
            self.external_video_thread.stop()
        if self.heltec_thread:
            self.heltec_thread.stop()
        if self.hackrf_thread:
            self.hackrf_thread.running = False
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = CEMAApp()
    window.show()
    sys.exit(app.exec())
