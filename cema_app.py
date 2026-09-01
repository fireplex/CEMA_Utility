import sys
import time
import json
import os
import subprocess
import threading
import datetime
import queue
import ctypes
import socket
import urllib.request
import random
import math
from collections import deque
import numpy as np
import scipy.signal
import scipy.ndimage
import sounddevice as sd
import cv2
try:
    import PyQt6.QtOpenGLWidgets
    import PyQt6.QtOpenGL
    import PyQt6.uic
except ImportError:
    pass

import pyqtgraph as pg
from PyQt6.QtWidgets import (QApplication, QMainWindow, QDialog, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                             QComboBox, QSpinBox, QDoubleSpinBox, QGridLayout, QGroupBox, QSlider, 
                             QTextEdit, QListWidget, QListWidgetItem, QTabWidget, QTreeWidget, 
                             QTreeWidgetItem, QSplitter, QProgressBar, QFrame, QSizePolicy, QMenu, 
                             QInputDialog, QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QTimer, QPointF, QRectF
from PyQt6.QtGui import QPainter, QPen, QBrush, QColor, QFont, QPolygonF, QImage, QPixmap, QShortcut, QKeySequence
from PyQt6.QtWebEngineWidgets import QWebEngineView
from heltec_bridge import HeltecLoraThread, get_available_com_ports
from neural_amc import get_neural_amc
from tactical_copilot import get_tactical_copilot

# --- Tactical Waterfall Colormaps ---
TACTICAL_COLORMAPS = {
    "Inferno (Default)": pg.ColorMap(
        np.array([0.0, 0.25, 0.5, 0.75, 1.0]),
        np.array([
            [0, 0, 0, 255], [30, 58, 138, 255], [6, 182, 212, 255], [250, 204, 21, 255], [220, 38, 38, 255]
        ], dtype=np.ubyte)
    ),
    "Thermal Green (NVG)": pg.ColorMap(
        np.array([0.0, 0.2, 0.5, 0.8, 1.0]),
        np.array([
            [0, 10, 0, 255], [0, 45, 10, 255], [16, 185, 129, 255], [52, 211, 153, 255], [236, 253, 245, 255]
        ], dtype=np.ubyte)
    ),
    "CRT Phosphor / Amber": pg.ColorMap(
        np.array([0.0, 0.2, 0.5, 0.8, 1.0]),
        np.array([
            [10, 5, 0, 255], [60, 25, 0, 255], [217, 119, 6, 255], [251, 191, 36, 255], [254, 243, 199, 255]
        ], dtype=np.ubyte)
    ),
    "Viridis": pg.ColorMap(
        np.array([0.0, 0.25, 0.5, 0.75, 1.0]),
        np.array([
            [68, 1, 84, 255], [59, 82, 139, 255], [33, 145, 140, 255], [94, 201, 98, 255], [253, 231, 37, 255]
        ], dtype=np.ubyte)
    ),
    "Plasma": pg.ColorMap(
        np.array([0.0, 0.25, 0.5, 0.75, 1.0]),
        np.array([
            [13, 8, 135, 255], [126, 3, 168, 255], [204, 71, 120, 255], [248, 149, 64, 255], [240, 249, 33, 255]
        ], dtype=np.ubyte)
    ),
    "Cold Ice Blue": pg.ColorMap(
        np.array([0.0, 0.25, 0.5, 0.75, 1.0]),
        np.array([
            [2, 6, 23, 255], [15, 23, 42, 255], [14, 165, 233, 255], [186, 230, 253, 255], [248, 250, 252, 255]
        ], dtype=np.ubyte)
    ),
    "Monochrome White-Hot": pg.ColorMap(
        np.array([0.0, 0.3, 0.7, 1.0]),
        np.array([
            [0, 0, 0, 255], [75, 85, 99, 255], [209, 213, 219, 255], [255, 255, 255, 255]
        ], dtype=np.ubyte)
    ),
}

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

def classify_modulation(iq_complex, avg_cva, avg_mag, is_carrier_active=True):
    if not is_carrier_active or avg_mag < 8:
        return "Noise/Inactive", 0.95, avg_cva
    cva = avg_cva
    if cva < 0.40:
        return "FM/FSK/CW", 0.85, cva
    elif cva < 0.65:
        return "QAM/Digital", 0.65, cva
    elif cva < 0.85:
        return "AM/Analog", 0.60, cva
    else:
        return "Wideband/Impulsive", 0.40, cva

def speak_tactical_alert(text):
    def _worker():
        try:
            import win32com.client
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            speaker.Speak(text)
        except Exception:
            try:
                clean_text = text.replace("'", "").replace('"', '')
                creationflags = 0x08000000 if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
                subprocess.run(
                    ["powershell", "-NoProfile", "-c", f"(New-Object -ComObject SAPI.SpVoice).Speak('{clean_text}')"],
                    capture_output=True,
                    timeout=3,
                    creationflags=creationflags
                )
            except Exception:
                pass
    threading.Thread(target=_worker, daemon=True).start()

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
        self.neural_amc = get_neural_amc()
        self.last_valid_mod_type = "Noise / Standby"
        self.last_valid_bw = 0
        self.last_valid_const_i = []
        self.last_valid_const_q = []
        self.hanning_window = np.hanning(1024).astype(np.float32)
        
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
        try:
            self.audio_stream = sd.OutputStream(device=3, samplerate=50000, channels=1, dtype='float32', callback=self.audio_callback)
        except Exception:
            try:
                self.audio_stream = sd.OutputStream(samplerate=50000, channels=1, dtype='float32', callback=self.audio_callback)
            except Exception:
                self.audio_stream = None
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

            window = self.hanning_window if len(iq_complex) == 1024 else np.hanning(len(iq_complex))
            windowed_iq = iq_complex * window

            fft_result = np.fft.fftshift(np.fft.fft(windowed_iq))
            magnitude = np.abs(fft_result) / len(windowed_iq)

            # 1. DC Spike Notch Filter (Suppress central LO leakage spike)
            center_bin = len(magnitude) // 2
            bg_left = np.median(magnitude[max(0, center_bin-10):max(0, center_bin-3)])
            bg_right = np.median(magnitude[min(len(magnitude), center_bin+3):min(len(magnitude), center_bin+10)])
            dc_val = (bg_left + bg_right) / 2.0
            magnitude[center_bin-2:center_bin+3] = dc_val

            # 2. Vectorized CA-CFAR Noise Floor Equalization (Flattens 20MHz filter skirts)
            G_cell = 4
            T_cell = 16
            tot_w = 2 * (G_cell + T_cell) + 1
            grd_w = 2 * G_cell + 1
            sum_tot = scipy.ndimage.uniform_filter1d(magnitude, size=tot_w, mode='reflect') * tot_w
            sum_grd = scipy.ndimage.uniform_filter1d(magnitude, size=grd_w, mode='reflect') * grd_w
            cfar_floor = (sum_tot - sum_grd) / (tot_w - grd_w)
            med_floor = np.median(cfar_floor)
            if med_floor > 1e-6:
                magnitude = np.maximum(0, magnitude - (cfar_floor - med_floor))

            if fft_avg is None:
                fft_avg = magnitude
                fft_max = magnitude
            else:
                fft_avg = (ALPHA_FFT * magnitude) + ((1 - ALPHA_FFT) * fft_avg)
                fft_max = np.maximum(fft_max * 0.995, magnitude)

            const_i = i_coords[:100]
            const_q = q_coords[:100]

            noise_floor = np.mean(magnitude)
            peak_val = np.max(magnitude)
            carrier_snr = peak_val - noise_floor
            is_carrier_active = (carrier_snr > 10.0)
            current_time = time.time()

            N = 8
            if len(i_coords) >= N and is_carrier_active:
                neural_classified = False
                if getattr(self, 'neural_amc', None) and self.neural_amc.is_ready:
                    ai_class, ai_conf, ai_note, _ = self.neural_amc.classify(iq_complex, carrier_snr)
                    if ai_class not in ("UNKNOWN", "Noise / Floor"):
                        mod_type = f"AI: {ai_class} ({ai_conf:.0f}%)"
                        confidence = ai_conf / 100.0
                        neural_classified = True

                if not neural_classified:
                    filt_i = np.convolve(i_coords, np.ones(N)/N, mode='valid')
                    filt_q = np.convolve(q_coords, np.ones(N)/N, mode='valid')
                    filt_iq = filt_i + 1j * filt_q
                    current_mag = np.abs(filt_iq)
                    current_cva = np.std(current_mag) / np.mean(current_mag) if np.mean(current_mag) > 0 else 1.0
                    cva_avg = (ALPHA_MOD * current_cva) + ((1 - ALPHA_MOD) * cva_avg)
                    avg_mag = np.mean(current_mag)
                    mod_type, confidence, _ = classify_modulation(iq_complex, cva_avg, avg_mag, True)

                # Persist active classification
                self.last_valid_mod_type = mod_type
                self.last_valid_const_i = const_i
                self.last_valid_const_q = const_q
                self.last_high_snr_time = current_time
            else:
                # In between rapid FHSS hops or pulsed telemetry packets, hold the classification for 1.8s
                if (current_time - self.last_high_snr_time) < 1.8:
                    mod_type = self.last_valid_mod_type
                    const_i = self.last_valid_const_i
                    const_q = self.last_valid_const_q
                else:
                    mod_type = "Noise/Inactive"
                    const_i = []
                    const_q = []
                avg_mag = 0
                
            # 1. PTT Transient Fingerprinting
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
                # Temporal Debounce: Wait 1.8 seconds of dead silence before dropping signal_active
                if signal_active and (current_time - self.last_high_snr_time) > 1.8:
                    signal_active = False
                    fingerprint = None
                
            # 2. Bandwidth Estimation with Persistence
            if (current_time - self.last_high_snr_time) < 1.8:
                noise_floor_m = np.median(fft_avg)
                active_bins = np.sum(fft_avg > noise_floor_m * 1.8)
                bw_estimate = active_bins * (20000000 / 1024)
                if bw_estimate > 0:
                    self.last_valid_bw = bw_estimate
                else:
                    bw_estimate = self.last_valid_bw
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
        if self.process:
            try:
                self.process.terminate()
                self.process.kill()
            except: pass


# --- HackRF Sweep Thread ---
class HackRFSweepThread(QThread):
    error_signal = pyqtSignal(str)

    def __init__(self, start_hz, end_hz, lna, vga, bin_width=1000000):
        super().__init__()
        self.start_hz = start_hz
        self.end_hz = end_hz
        self.lna = lna
        self.vga = vga
        self.bin_width = int(bin_width)
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

    def stop(self):
        self.running = False
        if self.process:
            try:
                self.process.terminate()
                self.process.kill()
            except: pass

# --- FPV Video Decoding & Stream Bridge Engine ---
FPV_VIDEO_CHANNELS = {
    "RaceBand R5 (5805 MHz)": 5805,
    "RaceBand R5 (5806 MHz)": 5806,
    "RaceBand R1 (5658 MHz)": 5658,
    "RaceBand R2 (5695 MHz)": 5695,
    "RaceBand R3 (5732 MHz)": 5732,
    "RaceBand R4 (5769 MHz)": 5769,
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
                Qt.TransformationMode.SmoothTransformation
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
            painter.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "NO ACTIVE VIDEO STREAM\n[ Select Channel && Click 'START VIDEO STREAM' ]")

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

    def mouseDoubleClickEvent(self, event):
        if hasattr(self, 'on_double_click_cb') and callable(self.on_double_click_cb):
            self.on_double_click_cb()
        super().mouseDoubleClickEvent(event)

class FloatingVideoWindow(QDialog):
    """
    Dedicated Resizable / Full-Screen Floating Video Window.
    Allows viewing the drone FPV feed at high resolution, full-screen, or on a second monitor.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📹 Live Drone Video Stream - CEMA Tactical View")
        self.resize(960, 720)
        self.setMinimumSize(480, 360)
        self.setStyleSheet("background-color: #050811; color: white;")
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        
        self.video_display = VideoDisplayWidget()
        self.video_display.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.video_display)

class DoACompassWidget(QWidget):
    """
    Military-grade 360-degree Tactical Compass Rose & MUSIC Polar Spectrum HUD.
    Visualizes:
    1. Continuous 360° azimuth bearing needle with smoothed inertial damping.
    2. MUSIC (Multiple Signal Classification) spatial pseudo-spectrum polar power graph.
    3. Angular uncertainty / confidence arc.
    4. Concentric polar power grid (dB levels) and cardinal degree markings.
    5. Real-time digital telemetry overlay.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(280, 280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.bearing_deg = 0.0
        self.target_bearing_deg = 0.0
        self.confidence = 0.0
        self.snr_db = 0.0
        self.freq_mhz = 915.000
        self.array_name = "5-UCA (Circular)"
        self.spectrum = [0.0] * 360
        self.is_locked = False
        
        # 60 FPS smooth needle animation timer
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self._animate_step)
        self.anim_timer.start(16)

    def set_bearing_data(self, data):
        self.target_bearing_deg = float(data.get("doa_deg", 0.0))
        self.confidence = float(data.get("confidence", 0.0))
        self.snr_db = float(data.get("snr_db", 0.0))
        self.freq_mhz = float(data.get("freq_mhz", self.freq_mhz))
        spec = data.get("spectrum", None)
        if spec and len(spec) == 360:
            self.spectrum = spec
        self.is_locked = (self.confidence > 50.0)

    def _animate_step(self):
        diff = (self.target_bearing_deg - self.bearing_deg + 180.0) % 360.0 - 180.0
        if abs(diff) > 0.05:
            self.bearing_deg = (self.bearing_deg + 0.18 * diff) % 360.0
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        w = self.width()
        h = self.height()
        cx = w / 2.0
        cy = h / 2.0
        radius = min(cx, cy) - 16.0
        if radius < 40:
            return

        # 1. Outer Bezel & Dark CRT Background
        painter.setPen(QPen(QColor("#1e293b"), 2))
        painter.setBrush(QBrush(QColor("#060a14")))
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # 2. Polar Radar Grid (Concentric Rings: 25%, 50%, 75%, 100%)
        grid_pen = QPen(QColor("#0ea5e9"))
        grid_pen.setStyle(Qt.PenStyle.DashLine)
        grid_pen.setWidthF(1.0)
        painter.setPen(grid_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        
        for frac in [0.25, 0.50, 0.75]:
            painter.drawEllipse(QPointF(cx, cy), radius * frac, radius * frac)

        # 3. Radial Spokes every 30 degrees
        spoke_pen = QPen(QColor(14, 165, 233, 40), 1.0)
        painter.setPen(spoke_pen)
        for deg in range(0, 360, 30):
            rad = math.radians(deg - 90)
            x1 = cx + (radius * 0.25) * math.cos(rad)
            y1 = cy + (radius * 0.25) * math.sin(rad)
            x2 = cx + radius * math.cos(rad)
            y2 = cy + radius * math.sin(rad)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        # 4. Polar MUSIC Pseudo-Spectrum Power Polygon
        if self.is_locked and any(p > 0.01 for p in self.spectrum):
            spec_poly = QPolygonF()
            inner_r = radius * 0.25
            dyn_r = radius * 0.70
            for deg in range(360):
                power = max(0.0, min(1.0, self.spectrum[deg]))
                r_val = inner_r + dyn_r * power
                rad = math.radians(deg - 90)
                px = cx + r_val * math.cos(rad)
                py = cy + r_val * math.sin(rad)
                spec_poly.append(QPointF(px, py))
            
            painter.setPen(QPen(QColor(56, 189, 248, 200), 1.5))
            painter.setBrush(QBrush(QColor(56, 189, 248, 45)))
            painter.drawPolygon(spec_poly)

        # 5. Degree Dial & Tick Marks
        painter.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
        for deg in range(0, 360, 5):
            rad = math.radians(deg - 90)
            is_cardinal = (deg % 90 == 0)
            is_major = (deg % 30 == 0)
            is_medium = (deg % 10 == 0)
            
            tick_len = 10 if is_cardinal else (7 if is_major else (4 if is_medium else 2))
            tick_color = QColor("#38bdf8") if is_cardinal else (QColor("#94a3b8") if is_major else QColor(148, 163, 184, 80))
            
            x_outer = cx + (radius - 2) * math.cos(rad)
            y_outer = cy + (radius - 2) * math.sin(rad)
            x_inner = cx + (radius - 2 - tick_len) * math.cos(rad)
            y_inner = cy + (radius - 2 - tick_len) * math.sin(rad)
            
            painter.setPen(QPen(tick_color, 1.5 if is_cardinal else 1.0))
            painter.drawLine(QPointF(x_inner, y_inner), QPointF(x_outer, y_outer))
            
            if is_cardinal or is_major:
                lbl_r = radius - 18
                lx = cx + lbl_r * math.cos(rad)
                ly = cy + lbl_r * math.sin(rad)
                
                label = "N" if deg == 0 else ("E" if deg == 90 else ("S" if deg == 180 else ("W" if deg == 270 else f"{deg}°")))
                lbl_color = QColor("#f59e0b") if is_cardinal else QColor("#94a3b8")
                painter.setPen(QPen(lbl_color))
                painter.drawText(QRectF(lx - 16, ly - 10, 32, 20), Qt.AlignmentFlag.AlignCenter, label)

        # 6. Confidence Arc / Uncertainty Halo
        if self.is_locked:
            conf_sigma = max(3.0, 35.0 * (1.0 - self.confidence / 100.0))
            arc_color = QColor(16, 185, 129, 60) if self.confidence >= 80 else (QColor(245, 158, 11, 60) if self.confidence >= 50 else QColor(239, 68, 68, 60))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(arc_color))
            
            start_ang = (-(self.bearing_deg + conf_sigma) + 90) * 16
            span_ang = (2.0 * conf_sigma) * 16
            painter.drawPie(QRectF(cx - radius * 0.92, cy - radius * 0.92, radius * 1.84, radius * 1.84), int(start_ang), int(span_ang))

        # 7. Target Line-of-Bearing (LoB) Ray & Arrow Needle
        if self.is_locked or self.confidence > 0:
            needle_rad = math.radians(self.bearing_deg - 90)
            nx = cx + (radius - 12) * math.cos(needle_rad)
            ny = cy + (radius - 12) * math.sin(needle_rad)
            
            # Glow Line
            glow_color = QColor("#10b981") if self.confidence >= 80 else (QColor("#f59e0b") if self.confidence >= 50 else QColor("#ef4444"))
            painter.setPen(QPen(glow_color, 3.0))
            painter.drawLine(QPointF(cx, cy), QPointF(nx, ny))
            
            # Arrow Tip Head
            left_rad = needle_rad + math.radians(150)
            right_rad = needle_rad - math.radians(150)
            ax1 = nx + 14 * math.cos(left_rad)
            ay1 = ny + 14 * math.sin(left_rad)
            ax2 = nx + 14 * math.cos(right_rad)
            ay2 = ny + 14 * math.sin(right_rad)
            
            arrow_poly = QPolygonF([QPointF(nx, ny), QPointF(ax1, ay1), QPointF(ax2, ay2)])
            painter.setBrush(QBrush(glow_color))
            painter.drawPolygon(arrow_poly)

        # 8. Center Hub
        painter.setPen(QPen(QColor("#38bdf8"), 2))
        painter.setBrush(QBrush(QColor("#0f172a")))
        painter.drawEllipse(QPointF(cx, cy), 12, 12)
        painter.setBrush(QBrush(QColor("#38bdf8")))
        painter.drawEllipse(QPointF(cx, cy), 4, 4)

        # 9. Tactical Telemetry Overlay Banner
        hud_w = min(260, w - 24)
        hud_h = 50
        hx = cx - hud_w / 2.0
        hy = h - hud_h - 10
        
        painter.setPen(QPen(QColor("#1e293b"), 1))
        painter.setBrush(QBrush(QColor(6, 10, 20, 220)))
        painter.drawRoundedRect(QRectF(hx, hy, hud_w, hud_h), 4, 4)
        
        cardinals = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        card_idx = int((self.bearing_deg + 11.25) / 22.5) % 16
        card_str = cardinals[card_idx]
        
        painter.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        status_col = "#10b981" if self.confidence >= 80 else ("#f59e0b" if self.confidence >= 50 else "#94a3b8")
        painter.setPen(QPen(QColor(status_col)))
        painter.drawText(QRectF(hx, hy + 4, hud_w, 20), Qt.AlignmentFlag.AlignCenter, f"BEARING: {self.bearing_deg:05.1f}° {card_str}")
        
        painter.setFont(QFont("Consolas", 8))
        painter.setPen(QPen(QColor("#38bdf8")))
        painter.drawText(QRectF(hx, hy + 26, hud_w, 18), Qt.AlignmentFlag.AlignCenter, f"CONF: {self.confidence:.0f}% | SNR: {self.snr_db:.0f}dB | {self.freq_mhz:.2f}MHz")

class KrakenDoAThread(QThread):
    bearing_signal = pyqtSignal(dict)
    status_signal = pyqtSignal(str, str) # msg, color

    def __init__(self, mode="KRAKEN_TCP", host="127.0.0.1", port=8081, freq_mhz=915.0, parent=None):
        super().__init__(parent)
        self.mode = mode # "KRAKEN_TCP", "HTTP", "UDP", "SIMULATOR"
        self.host = host
        self.port = port
        self.freq_mhz = freq_mhz
        self.running = False
        self.sim_angle = 135.0
        self.sim_speed = 0.4
        
    def run(self):
        self.running = True
        self.status_signal.emit(f"Kraken DoA active ({self.mode})", "#10b981")
        
        while self.running:
            try:
                if self.mode == "SIMULATOR":
                    self.sim_angle = (self.sim_angle + self.sim_speed + random.uniform(-0.35, 0.35)) % 360.0
                    conf = min(98.5, max(65.0, 92.0 + random.uniform(-4.0, 4.0)))
                    snr = min(35.0, max(12.0, 26.0 + random.uniform(-1.5, 1.5)))
                    
                    spectrum = [0.0] * 360
                    main_deg = int(self.sim_angle)
                    multipath_deg = (main_deg + 85) % 360
                    for deg in range(360):
                        diff_main = min(abs(deg - main_deg), 360 - abs(deg - main_deg))
                        diff_multi = min(abs(deg - multipath_deg), 360 - abs(deg - multipath_deg))
                        p_main = max(0.0, 1.0 - (diff_main / 22.0) ** 2)
                        p_multi = 0.35 * max(0.0, 1.0 - (diff_multi / 32.0) ** 2)
                        p_noise = random.uniform(0.02, 0.07)
                        spectrum[deg] = max(0.0, min(1.0, p_main + p_multi + p_noise))
                    
                    data = {
                        "doa_deg": self.sim_angle,
                        "confidence": conf,
                        "snr_db": snr,
                        "freq_mhz": self.freq_mhz,
                        "spectrum": spectrum,
                        "timestamp": time.time()
                    }
                    self.bearing_signal.emit(data)
                    time.sleep(0.08)

                elif self.mode in ["KRAKEN_POLL", "KRAKEN_WS", "KRAKEN_TCP"]:
                    # Direct live reader for Kraken DOA_value.html stream (Port 8081 & WSL UNC)
                    wsl_unc_path = r"\\wsl.localhost\Ubuntu\home\feeka\krakensdr_doa\krakensdr_doa\_share\DOA_value.html"
                    http_url = f"http://{self.host}:{self.port}/DOA_value.html"
                    
                    last_ts = 0
                    consecutive_errs = 0
                    self.status_signal.emit("Kraken Live Stream Connected", "#10b981")
                    
                    while self.running:
                        raw_text = None
                        try:
                            # Fast Path: Check direct WSL file if local
                            if self.host in ["127.0.0.1", "localhost"] and os.path.exists(wsl_unc_path):
                                try:
                                    with open(wsl_unc_path, 'r', encoding='utf-8', errors='ignore') as f:
                                        raw_text = f.read().strip()
                                except Exception:
                                    pass
                            
                            # Network / Fallback Path: HTTP GET from Kraken Data Server
                            if not raw_text:
                                req = urllib.request.Request(http_url, headers={'User-Agent': 'CEMA-Tracker/1.0'})
                                with urllib.request.urlopen(req, timeout=1.0) as resp:
                                    if resp.status == 200:
                                        raw_text = resp.read().decode('utf-8', errors='ignore').strip()

                            if raw_text and "," in raw_text:
                                parts = [p.strip() for p in raw_text.split(",") if p.strip()]
                                if len(parts) >= 6:
                                    try:
                                        ts = float(parts[0])
                                        if ts != last_ts:
                                            last_ts = ts
                                            bearing = float(parts[1])
                                            conf_raw = float(parts[2])
                                            pwr = float(parts[3])
                                            freq_val = float(parts[4])
                                            f_mhz = freq_val / 1e6 if freq_val > 1e5 else freq_val

                                            # Parse 360-degree MUSIC Pseudo-Spectrum array
                                            spec = [0.0] * 360
                                            if len(parts) >= 17 + 100:
                                                try:
                                                    spec_raw = [float(x) for x in parts[17:17+360]]
                                                    max_s = max(spec_raw) if max(spec_raw) > 0 else 1.0
                                                    min_s = min(spec_raw)
                                                    range_s = max_s - min_s if max_s > min_s else 1.0
                                                    spec = [(x - min_s) / range_s for x in spec_raw]
                                                except Exception:
                                                    pass

                                            # Calculate 0-100% confidence percentage
                                            conf = min(100.0, max(0.0, conf_raw * 18.0)) if conf_raw < 10.0 else min(100.0, conf_raw)
                                            
                                            self.bearing_signal.emit({
                                                "doa_deg": bearing,
                                                "confidence": conf,
                                                "snr_db": pwr,
                                                "freq_mhz": f_mhz,
                                                "spectrum": spec,
                                                "timestamp": ts / 1000.0 if ts > 1e11 else time.time()
                                            })
                                            consecutive_errs = 0
                                    except ValueError:
                                        pass
                        except Exception as e:
                            consecutive_errs += 1
                            if consecutive_errs > 5 and self.running:
                                self.status_signal.emit(f"Kraken waiting for DOA data... ({e})", "#f59e0b")
                        
                        time.sleep(0.05) # 20 Hz update rate

                elif self.mode == "HTTP":
                    url = f"http://{self.host}:{self.port}/settings"
                    req = urllib.request.Request(url, headers={'User-Agent': 'CEMA-Tracker/1.0'})
                    try:
                        with urllib.request.urlopen(req, timeout=1.5) as resp:
                            if resp.status == 200:
                                raw = json.loads(resp.read().decode('utf-8'))
                                bearing = float(raw.get('doa_deg', raw.get('bearing', 0.0)))
                                conf = float(raw.get('confidence', raw.get('conf', 85.0)))
                                self.bearing_signal.emit({
                                    "doa_deg": bearing,
                                    "confidence": conf,
                                    "snr_db": float(raw.get('snr', 20.0)),
                                    "freq_mhz": float(raw.get('center_freq', self.freq_mhz * 1e6)) / 1e6,
                                    "spectrum": [0.0] * 360,
                                    "timestamp": time.time()
                                })
                    except Exception:
                        pass
                    time.sleep(0.15)
                    
                elif self.mode == "UDP":
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.settimeout(1.0)
                    self.current_socket = sock
                    try:
                        sock.bind(("", int(self.port)))
                        while self.running:
                            try:
                                pkt, _ = sock.recvfrom(8192)
                                raw = json.loads(pkt.decode('utf-8', errors='ignore'))
                                bearing = float(raw.get('doa_deg', raw.get('bearing', 0.0)))
                                conf = float(raw.get('confidence', raw.get('conf', 85.0)))
                                self.bearing_signal.emit({
                                    "doa_deg": bearing,
                                    "confidence": conf,
                                    "snr_db": float(raw.get('snr', 20.0)),
                                    "freq_mhz": float(raw.get('frequency', self.freq_mhz)),
                                    "spectrum": raw.get('spectrum', [0.0] * 360),
                                    "timestamp": time.time()
                                })
                            except socket.timeout:
                                continue
                    except Exception as e:
                        if self.running:
                            self.status_signal.emit(f"UDP Error: {e}", "#ef4444")
                            time.sleep(1.0)
                    finally:
                        try: sock.close()
                        except: pass
                        self.current_socket = None
            except Exception as e:
                if self.running:
                    self.status_signal.emit(f"Kraken DoA Error: {e}", "#ef4444")
                    time.sleep(1.0)

    def stop(self):
        self.running = False
        if hasattr(self, 'current_socket') and self.current_socket:
            try: self.current_socket.close()
            except: pass
        self.wait(800)

class NativeHackRFVideoThread(QThread):
    frame_ready = pyqtSignal(QImage, bool, float)
    status_signal = pyqtSignal(str)

    def __init__(self, freq_mhz=5805, standard="PAL", invert_polarity=False, lna=32, vga=32):
        super().__init__()
        self.freq_mhz = freq_mhz
        self.standard = standard
        self.invert_polarity = invert_polarity
        self.lna = lna
        self.vga = vga
        self.running = True
        self.color_palette = "GRAYSCALE"
        self.brightness = 0.0
        self.contrast = 1.0
        self.auto_hsync = True
        self.manual_line_len = 640.0
        self.c_lib = None
        self._load_c_lib()

    def _load_c_lib(self):
        possible_paths = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "fpv_decoder.dll"),
            "fpv_decoder.dll"
        ]
        for p in possible_paths:
            if os.path.exists(p):
                try:
                    lib = ctypes.CDLL(p)
                    lib.fpv_decoder_start.argtypes = [ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
                    lib.fpv_decoder_start.restype = ctypes.c_int
                    lib.fpv_decoder_set_tuning.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_int, ctypes.c_float, ctypes.c_int, ctypes.c_int]
                    lib.fpv_decoder_set_tuning.restype = None
                    lib.fpv_decoder_get_frame.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_float)]
                    lib.fpv_decoder_get_frame.restype = ctypes.c_int
                    lib.fpv_decoder_stop.argtypes = []
                    lib.fpv_decoder_stop.restype = None
                    self.c_lib = lib
                    break
                except Exception:
                    pass

    def set_freq(self, freq_mhz):
        self.freq_mhz = freq_mhz

    def set_tuning(self, standard, invert_polarity, palette, brightness, contrast, auto_hsync=True, manual_line_len=640.0, auto_vsync=True, v_hold_offset=0, h_hold_offset=0):
        self.standard = standard
        self.invert_polarity = invert_polarity
        self.color_palette = palette
        self.brightness = brightness
        self.contrast = contrast
        self.auto_hsync = auto_hsync
        self.manual_line_len = manual_line_len
        self.v_hold_offset = v_hold_offset
        self.h_hold_offset = h_hold_offset
        if self.c_lib:
            std_code = 0 if "PAL" in standard else 1
            self.c_lib.fpv_decoder_set_tuning(
                std_code,
                1 if invert_polarity else 0,
                float(brightness),
                float(contrast),
                1 if auto_hsync else 0,
                float(manual_line_len),
                int(v_hold_offset),
                int(h_hold_offset)
            )

    def run(self):
        if not self.c_lib:
            self.status_signal.emit("C FPV Decoder DLL (fpv_decoder.dll) not found.")
            return

        freq_hz = int(self.freq_mhz * 1e6)
        std_code = 0 if "PAL" in self.standard else 1
        
        self.status_signal.emit(f"Starting C FPV Decoder on {self.freq_mhz} MHz (10 MSPS, LNA={self.lna}, VGA={self.vga})...")
        res = self.c_lib.fpv_decoder_start(freq_hz, 10000000, self.lna, self.vga, 1)
        if res != 0:
            self.status_signal.emit(f"Failed to open HackRF in C decoder (code {res}). Ensure SDRangel is closed.")
            return

        self.c_lib.fpv_decoder_set_tuning(
            std_code,
            1 if self.invert_polarity else 0,
            float(self.brightness),
            float(self.contrast),
            1 if self.auto_hsync else 0,
            float(self.manual_line_len),
            int(getattr(self, 'v_hold_offset', 0)),
            int(getattr(self, 'h_hold_offset', 0))
        )

        width = 640
        height = 480
        frame_buf = (ctypes.c_uint8 * (width * height))()
        sync_locked = ctypes.c_int(0)
        fps_val = ctypes.c_float(0.0)

        # Hardware Qt Color Tables (Zero-copy indexed palette rendering)
        from PyQt6.QtGui import qRgb
        color_table_gray = [qRgb(i, i, i) for i in range(256)]
        color_table_green = [qRgb(int(i * 0.15), i, int(i * 0.15)) for i in range(256)]
        color_table_amber = [qRgb(i, int(i * 0.65), int(i * 0.15)) for i in range(256)]

        while self.running:
            got_frame = self.c_lib.fpv_decoder_get_frame(
                frame_buf,
                width,
                height,
                ctypes.byref(sync_locked),
                ctypes.byref(fps_val)
            )
            if not got_frame:
                time.sleep(0.005)
                continue

            # Zero-copy direct QImage from C memory buffer
            qimg = QImage(frame_buf, width, height, width, QImage.Format.Format_Indexed8)
            if self.color_palette == "TACTICAL_GREEN":
                qimg.setColorTable(color_table_green)
            elif self.color_palette == "AMBER_FLIR":
                qimg.setColorTable(color_table_amber)
            else:
                qimg.setColorTable(color_table_gray)

            self.frame_ready.emit(qimg.copy(), bool(sync_locked.value), float(fps_val.value))
            time.sleep(0.015)

        try:
            self.c_lib.fpv_decoder_stop()
        except Exception:
            pass

    def stop(self):
        self.running = False
        self.wait(500)
        if self.c_lib:
            try:
                self.c_lib.fpv_decoder_stop()
            except Exception:
                pass

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


DEFAULT_SETTINGS = {
    "kraken_host": "127.0.0.1",
    "kraken_api_port": 8080,
    "kraken_doa_port": 8081,
    "kraken_wsl_path": r"\\wsl.localhost\Ubuntu\home\feeka\krakensdr_doa\krakensdr_doa\_share",
    "kraken_default_arr": "Uniform Circular Array (UCA)",
    "kraken_default_radius": 0.135,
    "kraken_default_gain": 30.0,
    "heltec_port": "COM6",
    "heltec_baud": 115200,
    "heltec_rate_idx": 0,
    "heltec_auto_reconnect": True,
    "map_provider": "CartoDB Dark Matter (Tactical)",
    "map_home_lat": 51.5074,
    "map_home_lon": -0.1278,
    "map_breadcrumbs_max": 100,
    "map_bearing_trail_max": 50,
    "ui_theme": "Cyberpunk Tactical Dark",
    "ui_fps_target": 30,
    "audio_alerts": False,
    "log_dir": "./cema_logs"
}

def load_app_settings():
    config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cema_settings.json")
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
                res = DEFAULT_SETTINGS.copy()
                res.update(saved)
                return res
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_app_settings(settings_dict):
    config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cema_settings.json")
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(settings_dict, f, indent=4)
        return True
    except Exception as e:
        print(f"[SETTINGS ERROR] Could not save settings: {e}")
        return False


class PopOutWindow(QMainWindow):
    """
    Tactical Detachable / Pop-Out Container Window.
    Allows undocking any UI sub-module (Tactical Map, FPV Video, DoA Compass, Telemetry Cockpit)
    into a dedicated floating multi-monitor window and re-docking on close.
    """
    closed = pyqtSignal()

    def __init__(self, title, widget, original_parent_layout, original_tab_idx, main_app, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"CEMA Tactical - {title}")
        self.resize(900, 680)
        self.widget = widget
        self.original_layout = original_parent_layout
        self.original_tab_idx = original_tab_idx
        self.main_app = main_app
        
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(6, 6, 6, 6)
        
        # Header bar with Re-dock button
        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(4, 2, 4, 2)
        
        title_lbl = QLabel(f"[ DETACHED VIEW: {title.upper()} ]")
        title_lbl.setStyleSheet("color: #38bdf8; font-weight: bold; font-family: monospace; font-size: 12px;")
        
        dock_btn = QPushButton("DOCK BACK")
        dock_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; border: 1px solid #0284c7; font-weight: bold; padding: 4px 12px; border-radius: 4px;")
        dock_btn.clicked.connect(self.dock_back)
        
        bar_layout.addWidget(title_lbl)
        bar_layout.addStretch()
        bar_layout.addWidget(dock_btn)
        
        layout.addWidget(bar)
        layout.addWidget(widget)
        self.setCentralWidget(container)
        
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #0b0f19; color: #e2e8f0; font-family: 'Consolas', monospace; }
            QPushButton { background-color: #1e293b; color: #38bdf8; border: 1px solid #334155; padding: 4px; }
        """)

    def dock_back(self):
        self.close()

    def closeEvent(self, event):
        if self.widget and self.original_layout:
            self.widget.setParent(None)
            self.original_layout.addWidget(self.widget)
            if self.main_app and hasattr(self.main_app, 'sidebar_tabs'):
                self.main_app.sidebar_tabs.setCurrentIndex(self.original_tab_idx)
        self.closed.emit()
        event.accept()


def calculate_cep_triangulation(bearing_records):
    """
    Computes optimal emitter fix and 95% Circular Error Probable (CEP)
    from a collection of bearing observations: [(lat, lon, bearing_deg, weight), ...]
    Using weighted least-squares line-of-bearing intersection.
    """
    if not bearing_records or len(bearing_records) < 2:
        return None
    
    lats = [r[0] for r in bearing_records]
    lons = [r[1] for r in bearing_records]
    lat0 = sum(lats) / len(lats)
    lon0 = sum(lons) / len(lons)
    
    R = 6378137.0
    lat0_rad = math.radians(lat0)
    
    A = []
    b = []
    for r_lat, r_lon, brng_deg, conf in bearing_records:
        x_obs = math.radians(r_lon - lon0) * R * math.cos(lat0_rad)
        y_obs = math.radians(r_lat - lat0) * R
        theta_rad = math.radians(brng_deg)
        c = math.cos(theta_rad)
        s = math.sin(theta_rad)
        w = max(0.1, conf / 100.0)
        A.append([c * w, -s * w])
        b.append((c * x_obs - s * y_obs) * w)
        
    A = np.array(A)
    b = np.array(b)
    
    try:
        ATA = A.T @ A
        if np.linalg.cond(ATA) > 1e6:
            return None
            
        cov = np.linalg.inv(ATA)
        pos = cov @ A.T @ b
        x_est, y_est = pos[0], pos[1]
        
        t_lon = lon0 + math.degrees(x_est / (R * math.cos(lat0_rad)))
        t_lat = lat0 + math.degrees(y_est / R)
        
        eigvals = np.linalg.eigvals(cov)
        sigma1 = math.sqrt(max(1.0, float(np.real(eigvals[0]))))
        sigma2 = math.sqrt(max(1.0, float(np.real(eigvals[1]))))
        cep_meters = 0.59 * (sigma1 + sigma2) * 1.5
        
        return {
            "lat": float(t_lat),
            "lon": float(t_lon),
            "cep_meters": float(max(5.0, min(10000.0, cep_meters))),
            "num_fixes": len(bearing_records)
        }
    except Exception:
        return None


class TacticalSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CEMA Hardware && System Configuration")
        self.resize(650, 480)
        self.parent_app = parent
        self.settings = load_app_settings()
        self.setup_ui()

    def setup_ui(self):
        self.setStyleSheet("""
            QDialog { background-color: #0b0f19; color: #e2e8f0; font-family: 'Consolas', monospace; }
            QTabWidget::pane { border: 1px solid #1e293b; border-radius: 6px; background-color: #0b0f19; }
            QTabBar::tab { background-color: #0f172a; color: #94a3b8; padding: 8px 16px; border: 1px solid #1e293b; border-top-left-radius: 6px; border-top-right-radius: 6px; font-weight: bold; }
            QTabBar::tab:selected { background-color: #1e293b; color: #38bdf8; border-bottom: 2px solid #38bdf8; }
            QGroupBox { color: #38bdf8; border: 1px solid #1e293b; margin-top: 10px; padding-top: 10px; background-color: #0b0f19; font-weight: bold; border-radius: 6px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QLabel { color: #cbd5e1; font-weight: bold; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background-color: #0f172a; color: #38bdf8; border: 1px solid #334155; border-radius: 4px; padding: 5px; font-weight: bold; }
            QPushButton { background-color: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 6px 14px; font-weight: bold; border-radius: 4px; }
            QPushButton:hover { background-color: #334155; color: white; border: 1px solid #38bdf8; }
            QCheckBox { color: #cbd5e1; font-weight: bold; }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Tab Widget
        tabs = QTabWidget()

        # --- TAB 1: KRAKEN SDR ---
        kraken_tab = QWidget()
        k_layout = QVBoxLayout(kraken_tab)
        k_grp = QGroupBox("KrakenSDR Server && Hardware Settings")
        k_grid = QGridLayout(k_grp)

        self.k_host = QLineEdit(str(self.settings.get("kraken_host", "127.0.0.1")))
        self.k_api_port = QSpinBox()
        self.k_api_port.setRange(1, 65535)
        self.k_api_port.setValue(int(self.settings.get("kraken_api_port", 8080)))

        self.k_doa_port = QSpinBox()
        self.k_doa_port.setRange(1, 65535)
        self.k_doa_port.setValue(int(self.settings.get("kraken_doa_port", 8081)))

        self.k_wsl_path = QLineEdit(str(self.settings.get("kraken_wsl_path", r"\\wsl.localhost\Ubuntu\home\feeka\krakensdr_doa\krakensdr_doa\_share")))

        self.k_arr_combo = QComboBox()
        self.k_arr_combo.addItems(["Uniform Circular Array (UCA)", "Uniform Linear Array (ULA)"])
        self.k_arr_combo.setCurrentText(str(self.settings.get("kraken_default_arr", "Uniform Circular Array (UCA)")))

        self.k_radius = QDoubleSpinBox()
        self.k_radius.setRange(0.01, 5.0)
        self.k_radius.setDecimals(3)
        self.k_radius.setSingleStep(0.005)
        self.k_radius.setSuffix(" m")
        self.k_radius.setValue(float(self.settings.get("kraken_default_radius", 0.135)))

        self.k_gain = QDoubleSpinBox()
        self.k_gain.setRange(0.0, 49.6)
        self.k_gain.setSingleStep(1.0)
        self.k_gain.setSuffix(" dB")
        self.k_gain.setValue(float(self.settings.get("kraken_default_gain", 30.0)))

        k_grid.addWidget(QLabel("Server Host / IP:"), 0, 0)
        k_grid.addWidget(self.k_host, 0, 1)
        k_grid.addWidget(QLabel("REST API Port:"), 1, 0)
        k_grid.addWidget(self.k_api_port, 1, 1)
        k_grid.addWidget(QLabel("DoA Stream Port:"), 2, 0)
        k_grid.addWidget(self.k_doa_port, 2, 1)
        k_grid.addWidget(QLabel("WSL Shared Dir:"), 3, 0)
        k_grid.addWidget(self.k_wsl_path, 3, 1)
        k_grid.addWidget(QLabel("Default Array:"), 4, 0)
        k_grid.addWidget(self.k_arr_combo, 4, 1)
        k_grid.addWidget(QLabel("Default Radius:"), 5, 0)
        k_grid.addWidget(self.k_radius, 5, 1)
        k_grid.addWidget(QLabel("Default Tuner Gain:"), 6, 0)
        k_grid.addWidget(self.k_gain, 6, 1)

        k_layout.addWidget(k_grp)

        k_svc_box = QGroupBox("Daemon && Hardware Service Controls")
        k_svc_layout = QHBoxLayout(k_svc_box)
        
        k_start_btn = QPushButton("Start")
        k_start_btn.clicked.connect(lambda: self.parent_app.start_kraken_service() if self.parent_app else None)
        
        k_stop_btn = QPushButton("Stop")
        k_stop_btn.clicked.connect(lambda: self.parent_app.stop_kraken_service() if self.parent_app else None)
        
        k_restart_btn = QPushButton("Restart")
        k_restart_btn.clicked.connect(lambda: self.parent_app.restart_kraken_service() if self.parent_app else None)
        
        k_repair_btn = QPushButton("Auto-Repair && Fix")
        k_repair_btn.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; border: 1px solid #38bdf8;")
        k_repair_btn.clicked.connect(lambda: self.parent_app.repair_kraken_sdr() if self.parent_app else None)
        
        k_attach_btn = QPushButton("Attach USB")
        k_attach_btn.clicked.connect(lambda: self.parent_app.attach_kraken_usb() if self.parent_app else None)

        k_svc_layout.addWidget(k_start_btn)
        k_svc_layout.addWidget(k_stop_btn)
        k_svc_layout.addWidget(k_restart_btn)
        k_svc_layout.addWidget(k_repair_btn)
        k_svc_layout.addWidget(k_attach_btn)
        
        k_layout.addWidget(k_svc_box)
        k_layout.addStretch()
        tabs.addTab(kraken_tab, "Kraken SDR")

        # --- TAB 2: HELTEC LORA ---
        heltec_tab = QWidget()
        h_layout = QVBoxLayout(heltec_tab)
        h_grp = QGroupBox("Heltec WiFi LoRa 32 V3 Sniffer Interface")
        h_grid = QGridLayout(h_grp)

        self.h_port_combo = QComboBox()
        self.refresh_com_ports()
        
        self.h_refresh_btn = QPushButton("Refresh Ports")
        self.h_refresh_btn.clicked.connect(self.refresh_com_ports)

        port_row_widget = QWidget()
        port_row_layout = QHBoxLayout(port_row_widget)
        port_row_layout.setContentsMargins(0, 0, 0, 0)
        port_row_layout.addWidget(self.h_port_combo, 1)
        port_row_layout.addWidget(self.h_refresh_btn)

        self.h_baud = QComboBox()
        self.h_baud.addItems(["115200", "921600", "57600", "38400", "9600"])
        self.h_baud.setCurrentText(str(self.settings.get("heltec_baud", 115200)))

        self.h_rate = QComboBox()
        self.h_rate.addItems([
            "Auto-Detect (Dynamic Auto-Rate Scanning)",
            "50 Hz (Standard 915MHz - SF8 / 20ms)",
            "25 Hz (Long Range - SF9 / 40ms)",
            "100 Hz (Standard 8ch - SF7 / 10ms)",
            "100 Hz Full (16ch Full Res - SF7 / 10ms)",
            "D50 (Déjà Vu 50Hz - SF7 / 10ms)",
            "150 Hz (SF7 / 6.6ms)",
            "200 Hz (SF6 / 5ms)",
            "250 Hz (SF6 / 4ms)",
            "333 Hz Full (16ch Full Res - SF5 / 3ms)"
        ])
        self.h_rate.setCurrentIndex(int(self.settings.get("heltec_rate_idx", 0)))

        self.h_auto_reconnect = QCheckBox("Auto-reconnect on USB disconnect")
        self.h_auto_reconnect.setChecked(bool(self.settings.get("heltec_auto_reconnect", True)))

        h_grid.addWidget(QLabel("Serial COM Port:"), 0, 0)
        h_grid.addWidget(port_row_widget, 0, 1)
        h_grid.addWidget(QLabel("Serial Baud Rate:"), 1, 0)
        h_grid.addWidget(self.h_baud, 1, 1)
        h_grid.addWidget(QLabel("Startup Packet Rate:"), 2, 0)
        h_grid.addWidget(self.h_rate, 2, 1)
        h_grid.addWidget(self.h_auto_reconnect, 3, 0, 1, 2)

        h_layout.addWidget(h_grp)
        h_layout.addStretch()
        tabs.addTab(heltec_tab, "Heltec Sniffer")

        # --- TAB 3: TACTICAL MAP ---
        map_tab = QWidget()
        m_layout = QVBoxLayout(map_tab)
        m_grp = QGroupBox("Tactical Map, Geolocation && Bearing History")
        m_grid = QGridLayout(m_grp)

        self.m_provider = QComboBox()
        self.m_provider.addItems([
            "CartoDB Dark Matter (Tactical)",
            "OpenStreetMap Standard",
            "CartoDB Positron (Light)",
            "ESRI World Imagery (Satellite)"
        ])
        self.m_provider.setCurrentText(str(self.settings.get("map_provider", "CartoDB Dark Matter (Tactical)")))

        self.m_lat = QDoubleSpinBox()
        self.m_lat.setRange(-90.0, 90.0)
        self.m_lat.setDecimals(5)
        self.m_lat.setValue(float(self.settings.get("map_home_lat", 51.5074)))

        self.m_lon = QDoubleSpinBox()
        self.m_lon.setRange(-180.0, 180.0)
        self.m_lon.setDecimals(5)
        self.m_lon.setValue(float(self.settings.get("map_home_lon", -0.1278)))

        self.m_breadcrumbs = QSpinBox()
        self.m_breadcrumbs.setRange(10, 1000)
        self.m_breadcrumbs.setValue(int(self.settings.get("map_breadcrumbs_max", 100)))

        self.m_bearing_trail = QSpinBox()
        self.m_bearing_trail.setRange(10, 500)
        self.m_bearing_trail.setValue(int(self.settings.get("map_bearing_trail_max", 50)))

        m_grid.addWidget(QLabel("Base Map Layer:"), 0, 0)
        m_grid.addWidget(self.m_provider, 0, 1)
        m_grid.addWidget(QLabel("Home Latitude:"), 1, 0)
        m_grid.addWidget(self.m_lat, 1, 1)
        m_grid.addWidget(QLabel("Home Longitude:"), 2, 0)
        m_grid.addWidget(self.m_lon, 2, 1)
        m_grid.addWidget(QLabel("Max Drone Breadcrumbs:"), 3, 0)
        m_grid.addWidget(self.m_breadcrumbs, 3, 1)
        m_grid.addWidget(QLabel("Max Bearing Lines:"), 4, 0)
        m_grid.addWidget(self.m_bearing_trail, 4, 1)

        m_layout.addWidget(m_grp)
        m_layout.addStretch()
        tabs.addTab(map_tab, "Map && Tracking")

        # --- TAB 4: UI & GENERAL ---
        ui_tab = QWidget()
        u_layout = QVBoxLayout(ui_tab)
        u_grp = QGroupBox("User Interface && Telemetry Preferences")
        u_grid = QGridLayout(u_grp)

        self.u_theme = QComboBox()
        self.u_theme.addItems(["Cyberpunk Tactical Dark", "Stealth Minimalist"])
        self.u_theme.setCurrentText(str(self.settings.get("ui_theme", "Cyberpunk Tactical Dark")))

        self.u_fps = QSpinBox()
        self.u_fps.setRange(10, 60)
        self.u_fps.setValue(int(self.settings.get("ui_fps_target", 30)))
        self.u_fps.setSuffix(" FPS")

        self.u_audio = QCheckBox("Enable Audible Proximity / Maneuver Alert Chimes")
        self.u_audio.setChecked(bool(self.settings.get("audio_alerts", False)))

        self.u_log_dir = QLineEdit(str(self.settings.get("log_dir", "./cema_logs")))

        u_grid.addWidget(QLabel("Theme Palette:"), 0, 0)
        u_grid.addWidget(self.u_theme, 0, 1)
        u_grid.addWidget(QLabel("GUI Target FPS:"), 1, 0)
        u_grid.addWidget(self.u_fps, 1, 1)
        u_grid.addWidget(self.u_audio, 2, 0, 1, 2)
        u_grid.addWidget(QLabel("Log Directory:"), 3, 0)
        u_grid.addWidget(self.u_log_dir, 3, 1)

        u_layout.addWidget(u_grp)
        u_layout.addStretch()
        tabs.addTab(ui_tab, "UI && General")

        layout.addWidget(tabs)

        # Bottom Buttons
        btn_layout = QHBoxLayout()
        reset_btn = QPushButton("Reset Defaults")
        reset_btn.clicked.connect(self.reset_defaults)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        save_btn = QPushButton("Save && Apply")
        save_btn.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; border: 1px solid #38bdf8;")
        save_btn.clicked.connect(self.save_and_apply)

        btn_layout.addWidget(reset_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def refresh_com_ports(self):
        ports = get_available_com_ports()
        self.h_port_combo.clear()
        self.h_port_combo.addItems(ports)
        saved_port = self.settings.get("heltec_port", "COM6")
        idx = self.h_port_combo.findText(saved_port)
        if idx >= 0:
            self.h_port_combo.setCurrentIndex(idx)

    def reset_defaults(self):
        self.settings = DEFAULT_SETTINGS.copy()
        self.k_host.setText(self.settings["kraken_host"])
        self.k_api_port.setValue(self.settings["kraken_api_port"])
        self.k_doa_port.setValue(self.settings["kraken_doa_port"])
        self.k_wsl_path.setText(self.settings["kraken_wsl_path"])
        self.k_arr_combo.setCurrentText(self.settings["kraken_default_arr"])
        self.k_radius.setValue(self.settings["kraken_default_radius"])
        self.k_gain.setValue(self.settings["kraken_default_gain"])
        self.h_baud.setCurrentText(str(self.settings["heltec_baud"]))
        self.h_rate.setCurrentIndex(self.settings["heltec_rate_idx"])
        self.h_auto_reconnect.setChecked(self.settings["heltec_auto_reconnect"])
        self.m_provider.setCurrentText(self.settings["map_provider"])
        self.m_lat.setValue(self.settings["map_home_lat"])
        self.m_lon.setValue(self.settings["map_home_lon"])
        self.m_breadcrumbs.setValue(self.settings["map_breadcrumbs_max"])
        self.m_bearing_trail.setValue(self.settings["map_bearing_trail_max"])
        self.u_theme.setCurrentText(self.settings["ui_theme"])
        self.u_fps.setValue(self.settings["ui_fps_target"])
        self.u_audio.setChecked(self.settings["audio_alerts"])
        self.u_log_dir.setText(self.settings["log_dir"])

    def save_and_apply(self):
        updated = {
            "kraken_host": self.k_host.text().strip(),
            "kraken_api_port": self.k_api_port.value(),
            "kraken_doa_port": self.k_doa_port.value(),
            "kraken_wsl_path": self.k_wsl_path.text().strip(),
            "kraken_default_arr": self.k_arr_combo.currentText(),
            "kraken_default_radius": self.k_radius.value(),
            "kraken_default_gain": self.k_gain.value(),
            "heltec_port": self.h_port_combo.currentText(),
            "heltec_baud": int(self.h_baud.currentText()),
            "heltec_rate_idx": self.h_rate.currentIndex(),
            "heltec_auto_reconnect": self.h_auto_reconnect.isChecked(),
            "map_provider": self.m_provider.currentText(),
            "map_home_lat": self.m_lat.value(),
            "map_home_lon": self.m_lon.value(),
            "map_breadcrumbs_max": self.m_breadcrumbs.value(),
            "map_bearing_trail_max": self.m_bearing_trail.value(),
            "ui_theme": self.u_theme.currentText(),
            "ui_fps_target": self.u_fps.value(),
            "audio_alerts": self.u_audio.isChecked(),
            "log_dir": self.u_log_dir.text().strip()
        }
        save_app_settings(updated)
        if self.parent_app and hasattr(self.parent_app, "apply_runtime_settings"):
            self.parent_app.apply_runtime_settings(updated)
        self.accept()


# --- Main App ---
class CEMAApp(QMainWindow):
    kraken_health_signal = pyqtSignal(str, str, str) # msg, color, border
    copilot_response_signal = pyqtSignal(str, str)   # prompt, response
    copilot_stream_chunk_signal = pyqtSignal(str)    # text_chunk
    embm_viewshed_ready_signal = pyqtSignal(dict)    # viewshed_result

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CEMA RF Tracking [HackRF + Heltec V3]")
        self.resize(1300, 850)
        
        self.settings = load_app_settings()
        self.hackrf_thread = None
        self.heltec_thread = None
        self.floating_video_window = None
        self.last_viewshed_data = None
        
        self.setup_ui()
        self.kraken_health_signal.connect(self._on_kraken_health_updated)
        self.copilot_response_signal.connect(self._on_copilot_response_received)
        self.copilot_stream_chunk_signal.connect(self._on_copilot_stream_chunk)
        self.embm_viewshed_ready_signal.connect(self._on_viewshed_computed)

        self._fingerprints_dirty = False
        self._topology_dirty = False
        self.disk_flush_timer = QTimer()
        self.disk_flush_timer.timeout.connect(self.flush_dirty_state_to_disk)
        self.disk_flush_timer.start(4000)
        
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self.poll_data)
        self.poll_timer.start(33)
        
        self.clock_timer = QTimer()
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)

        self.kraken_health_timer = QTimer()
        self.kraken_health_timer.timeout.connect(self.check_kraken_health)
        self.kraken_health_timer.start(10000)
        QTimer.singleShot(1500, self.check_kraken_health)

        self.start_sdr()
        self.start_heltec()

    def setup_ui(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #070a13;
                color: #e2e8f0;
                font-family: 'Consolas', 'Segoe UI', 'Courier New', monospace;
                font-size: 10pt;
            }
            QToolTip {
                background-color: #0f172a;
                color: #38bdf8;
                border: 1px solid #0284c7;
                padding: 6px;
                font-family: 'Consolas', monospace;
                font-size: 10pt;
                border-radius: 4px;
            }
            QMainWindow { background-color: #070a13; }
            QLabel {
                color: #cbd5e1;
                font-weight: bold;
                font-family: 'Consolas', 'Segoe UI', monospace;
                font-size: 10pt;
                background-color: transparent;
            }
            QDoubleSpinBox, QSpinBox, QLineEdit {
                background-color: #0f172a;
                color: #38bdf8;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 4px 6px;
                font-family: 'Consolas', monospace;
                font-size: 10pt;
                font-weight: bold;
            }
            QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus {
                border: 1px solid #38bdf8;
                background-color: #131d35;
            }
            QPushButton {
                background-color: #1e293b;
                color: #cbd5e1;
                border: 1px solid #334155;
                padding: 5px 10px;
                font-weight: bold;
                font-family: 'Consolas', monospace;
                border-radius: 4px;
                font-size: 10pt;
            }
            QPushButton:hover {
                background-color: #334155;
                color: #f8fafc;
                border: 1px solid #38bdf8;
            }
            QPushButton:checked {
                background-color: #0284c7;
                border: 1px solid #38bdf8;
                color: #ffffff;
            }
            QGroupBox {
                color: #38bdf8;
                border: 1px solid #1e293b;
                margin-top: 10px;
                background-color: #0b0f19;
                font-family: 'Consolas', monospace;
                border-radius: 6px;
                font-weight: bold;
                font-size: 10pt;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
                color: #38bdf8;
            }
            QListWidget, QTreeWidget {
                background-color: #0b0f19;
                color: #38bdf8;
                font-family: 'Consolas', monospace;
                border: 1px solid #1e293b;
                border-radius: 4px;
                padding: 4px;
                font-size: 10pt;
                outline: none;
            }
            QListWidget::item:selected, QTreeWidget::item:selected {
                background-color: #1e293b;
                color: #38bdf8;
                border-radius: 2px;
            }
            QTextEdit {
                background-color: #0b0f19;
                color: #10b981;
                font-family: 'Consolas', monospace;
                border: 1px solid #1e293b;
                border-radius: 4px;
                padding: 5px;
                font-size: 10pt;
            }
            QComboBox {
                background-color: #0f172a;
                color: #f59e0b;
                font-weight: bold;
                padding: 4px 6px;
                border: 1px solid #334155;
                border-radius: 4px;
                font-family: 'Consolas', monospace;
                font-size: 10pt;
            }
            QComboBox:hover {
                border: 1px solid #f59e0b;
            }
            QTabWidget::pane {
                border: 1px solid #1e293b;
                border-radius: 4px;
                background-color: #0b0f19;
            }
            QTabBar::tab {
                background-color: #0f172a;
                color: #94a3b8;
                padding: 6px 12px;
                border: 1px solid #1e293b;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                font-weight: bold;
                font-size: 9.5pt;
            }
            QTabBar::tab:selected {
                background-color: #1e293b;
                color: #38bdf8;
                border-bottom: 2px solid #38bdf8;
            }
            QTabBar::tab:hover:!selected {
                color: #e2e8f0;
                background-color: #162032;
            }
            QSlider::groove:horizontal {
                border: 1px solid #334155;
                height: 6px;
                background: #0f172a;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #38bdf8;
                border: 1px solid #0284c7;
                width: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }
            QSlider::handle:horizontal:hover {
                background: #7dd3fc;
            }
            QScrollBar:vertical {
                background: #070a13;
                width: 8px;
                margin: 0px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #334155;
                min-height: 20px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover {
                background: #38bdf8;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar:horizontal {
                background: #070a13;
                height: 8px;
                margin: 0px;
                border-radius: 4px;
            }
            QScrollBar::handle:horizontal {
                background: #334155;
                min-width: 20px;
                border-radius: 4px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #38bdf8;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            QTableWidget {
                background-color: #0b0f19;
                alternate-background-color: #0f172a;
                gridline-color: #1e293b;
                border: 1px solid #1e293b;
                color: #e2e8f0;
                selection-background-color: #1e293b;
                selection-color: #38bdf8;
                font-family: 'Consolas', monospace;
            }
            QHeaderView::section {
                background-color: #0f172a;
                color: #38bdf8;
                font-weight: bold;
                font-family: 'Consolas', monospace;
                padding: 4px 6px;
                border: 1px solid #1e293b;
            }
        """)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Control Panel (Responsive 2-Row Grid)
        control_group = QGroupBox("SDR && Hardware Parameters")
        control_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        control_layout = QGridLayout(control_group)
        control_layout.setContentsMargins(6, 4, 6, 4)
        control_layout.setSpacing(6)
        
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

        self.sweep_bin_label = QLabel("Bin Res:")
        self.sweep_bin_combo = QComboBox()
        self.sweep_bin_combo.addItems([
            "100 kHz (High-Res)",
            "250 kHz (High-Res)",
            "500 kHz (Balanced)",
            "1 MHz (Default)",
            "2 MHz (Turbo)",
            "5 MHz (Ultra)"
        ])
        self.sweep_bin_combo.setCurrentText("1 MHz (Default)")
        self.sweep_bin_combo.currentIndexChanged.connect(self.on_sweep_bin_changed)
        self.sweep_bin_combo.setToolTip("Wideband Sweep FFT Bin Resolution (-w parameter for hackrf_sweep).")
        self.sweep_bin_label.hide()
        self.sweep_bin_combo.hide()
        self.sweep_bin_width_hz = 1000000

        self.palette_combo = QComboBox()
        self.palette_combo.addItems(list(TACTICAL_COLORMAPS.keys()))
        self.palette_combo.setCurrentText("Inferno (Default)")
        self.palette_combo.currentTextChanged.connect(self.on_palette_changed)
        self.palette_combo.setToolTip("Tactical Waterfall Color Palette.")
        
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
        self.apply_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; border: 1px solid #38bdf8; font-weight: bold; border-radius: 4px; padding: 5px 10px;")
        self.apply_btn.clicked.connect(self.start_sdr)
        self.apply_btn.setToolTip("Restart the SDR with the new parameters.")
        
        self.mask_mode_btn = QPushButton("MASK MODE: OFF")
        self.mask_mode_btn.setCheckable(True)
        self.mask_mode_btn.setStyleSheet("background-color: #475569; color: white; border-radius: 4px; padding: 5px 10px;")
        self.mask_mode_btn.toggled.connect(self.toggle_mask_mode)
        self.mask_mode_btn.setToolTip("ON: Left-click and drag on the FFT to draw a grey mask over continuous signals to ignore them.\nOFF: Left-click and drag to pan the spectrum (Shortcut: Ctrl+M).")
        
        self.decode_video_btn = QPushButton(" DECODE FPV VIDEO")
        self.decode_video_btn.setCheckable(True)
        self.decode_video_btn.setStyleSheet("background-color: #475569; color: white; border-radius: 4px; padding: 5px 10px;")
        self.decode_video_btn.toggled.connect(self.toggle_video_mode)
        self.decode_video_btn.setToolTip("Decodes 5.8GHz Analog FPV signals by ripping the raw 20MS/s FM phase array and slicing it via HSync matrix reshaping into a CRT video frame (Shortcut: Ctrl+V).")
        
        self.toggle_sidebar_btn = QPushButton(" SIDEBAR")
        self.toggle_sidebar_btn.setStyleSheet("background-color: #1e293b; color: #cbd5e1; border: 1px solid #334155; border-radius: 4px; padding: 5px 10px;")
        self.toggle_sidebar_btn.clicked.connect(self.toggle_sidebar)
        self.toggle_sidebar_btn.setToolTip("Hide or show the Intelligence Sidebar (Shortcut: Ctrl+B).")
        
        self.wf_sens_slider = QSlider(Qt.Orientation.Horizontal)
        self.wf_sens_slider.setRange(1, 255)
        self.wf_sens_slider.setValue(120)
        self.wf_sens_slider.setFixedWidth(80)
        self.wf_sens_slider.setToolTip("Waterfall Sensitivity: Slide left to make faint signals visible, slide right to reduce noise floor clutter.")
        
        self.clock_label = QLabel("00:00:00Z")
        self.clock_label.setStyleSheet("color: #10b981; font-size: 11pt; font-weight: bold; background: #111; padding: 3px 6px; border: 1px solid #222; border-radius: 4px;")
        self.clock_label.setToolTip("ZULU (UTC) Time")
        
        self.mod_label = QLabel("Modulation: UNKNOWN")
        self.mod_label.setStyleSheet("color: #fbbf24; font-size: 11pt; font-weight: bold;")

        self.freeze_btn = QPushButton(" FREEZE")
        self.freeze_btn.setCheckable(True)
        self.freeze_btn.setStyleSheet("background-color: #475569; color: white; border-radius: 4px; padding: 5px 10px;")
        self.freeze_btn.toggled.connect(self.toggle_freeze)
        self.freeze_btn.setToolTip("Freeze display updates to analyze waterfall/spectrum without motion (Shortcut: Space).")

        self.heltec_port_combo = QComboBox()
        self.heltec_port_combo.addItems(get_available_com_ports())
        self.heltec_port_combo.setToolTip("Select COM Port for Heltec WiFi LoRa 32 V3 sniffer.")
        self.heltec_connect_btn = QPushButton("HELTEC V3")
        self.heltec_connect_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; border: 1px solid #38bdf8; border-radius: 4px; padding: 5px 10px;")
        self.heltec_connect_btn.clicked.connect(self.restart_heltec)
        self.heltec_connect_btn.setToolTip("Connect or Reconnect to Heltec WiFi LoRa 32 V3 sniffer hardware.")

        self.settings_btn = QPushButton("SETTINGS")
        self.settings_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; border: 1px solid #0284c7; font-weight: bold; border-radius: 4px; padding: 5px 10px;")
        self.settings_btn.clicked.connect(self.open_settings_dialog)
        self.settings_btn.setToolTip("Open Centralized Hardware && System Configuration (Shortcut: F1).")

        # Row 0: SDR Tuning & Reception Controls
        r0 = QHBoxLayout()
        r0.setContentsMargins(0, 0, 0, 0)
        r0.setSpacing(6)
        r0.addWidget(QLabel("Mode:"))
        r0.addWidget(self.mode_selector)
        r0.addSpacing(6)
        self.freq_label = QLabel("Center Freq:")
        r0.addWidget(self.freq_label)
        r0.addWidget(self.freq_input)
        r0.addWidget(self.sweep_start_input)
        r0.addWidget(self.sweep_end_input)
        r0.addWidget(self.sweep_bin_label)
        r0.addWidget(self.sweep_bin_combo)
        r0.addSpacing(6)
        r0.addWidget(QLabel("LNA:"))
        r0.addWidget(self.lna_input)
        r0.addSpacing(6)
        r0.addWidget(QLabel("VGA:"))
        r0.addWidget(self.vga_input)
        r0.addSpacing(6)
        r0.addWidget(self.apply_btn)
        r0.addWidget(self.freeze_btn)
        r0.addSpacing(6)
        r0.addWidget(QLabel("WF Sens:"))
        r0.addWidget(self.wf_sens_slider)
        r0.addStretch()
        r0.addWidget(self.mod_label)
        r0.addSpacing(8)
        r0.addWidget(self.clock_label)
        control_layout.addLayout(r0, 0, 0)

        # Row 1: Tactical Modes, Aux Hardware, and System Dialogs
        r1 = QHBoxLayout()
        r1.setContentsMargins(0, 0, 0, 0)
        r1.setSpacing(6)
        r1.addWidget(self.mask_mode_btn)
        r1.addWidget(self.decode_video_btn)
        r1.addWidget(self.toggle_sidebar_btn)
        r1.addSpacing(6)
        r1.addWidget(QLabel("Palette:"))
        r1.addWidget(self.palette_combo)
        r1.addSpacing(10)
        r1.addWidget(QLabel("Heltec Port:"))
        r1.addWidget(self.heltec_port_combo)
        r1.addWidget(self.heltec_connect_btn)
        r1.addSpacing(6)
        r1.addWidget(self.settings_btn)
        r1.addStretch()
        control_layout.addLayout(r1, 1, 0)

        main_layout.addWidget(control_group)

        # Real-Time Tactical Hardware Telemetry Strip
        status_bar_frame = QFrame()
        status_bar_frame.setStyleSheet("background-color: #060913; border: 1px solid #1e293b; border-radius: 4px; padding: 2px;")
        status_bar_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        status_bar_frame.setFixedHeight(34)
        status_bar_layout = QHBoxLayout(status_bar_frame)
        status_bar_layout.setContentsMargins(6, 2, 6, 2)
        status_bar_layout.setSpacing(8)

        self.badge_sdr = QLabel("[ SDR: HACKRF ONE (20 MS/s) ]")
        self.badge_sdr.setStyleSheet("color: #10b981; background: #060e1a; font-family: 'Consolas', monospace; font-size: 9.5pt; font-weight: bold; padding: 2px 8px; border: 1px solid #10b981; border-radius: 3px;")

        self.badge_heltec = QLabel("[ HELTEC V3: STANDBY ]")
        self.badge_heltec.setStyleSheet("color: #38bdf8; background: #060e1a; font-family: 'Consolas', monospace; font-size: 9.5pt; font-weight: bold; padding: 2px 8px; border: 1px solid #0284c7; border-radius: 3px;")

        self.badge_kraken = QLabel("[ KRAKENSDR: STANDBY ]")
        self.badge_kraken.setStyleSheet("color: #f59e0b; background: #060e1a; font-family: 'Consolas', monospace; font-size: 9.5pt; font-weight: bold; padding: 2px 8px; border: 1px solid #f59e0b; border-radius: 3px;")

        self.badge_copilot = QLabel("[ AI COPILOT: ONLINE ]")
        self.badge_copilot.setStyleSheet("color: #a78bfa; background: #060e1a; font-family: 'Consolas', monospace; font-size: 9.5pt; font-weight: bold; padding: 2px 8px; border: 1px solid #7c3aed; border-radius: 3px;")

        status_bar_layout.addWidget(self.badge_sdr)
        status_bar_layout.addWidget(self.badge_heltec)
        status_bar_layout.addWidget(self.badge_kraken)
        status_bar_layout.addWidget(self.badge_copilot)
        status_bar_layout.addStretch()

        main_layout.addWidget(status_bar_frame)

        # Global Tactical Keyboard Shortcuts
        QShortcut(QKeySequence("F1"), self, self.open_settings_dialog)
        QShortcut(QKeySequence("Space"), self, self.freeze_btn.toggle)
        QShortcut(QKeySequence("Ctrl+M"), self, self.mask_mode_btn.toggle)
        QShortcut(QKeySequence("Ctrl+V"), self, self.decode_video_btn.toggle)
        QShortcut(QKeySequence("Ctrl+B"), self, self.toggle_sidebar)
        QShortcut(QKeySequence("Ctrl+E"), self, self.export_log)
        QShortcut(QKeySequence("Ctrl+H"), self, lambda: self.sidebar_tabs.setCurrentIndex(7))
        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self.sidebar_tabs.setCurrentIndex(0))
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self.sidebar_tabs.setCurrentIndex(1))
        QShortcut(QKeySequence("Ctrl+3"), self, lambda: self.sidebar_tabs.setCurrentIndex(2))
        QShortcut(QKeySequence("Ctrl+4"), self, lambda: self.sidebar_tabs.setCurrentIndex(3))
        QShortcut(QKeySequence("Ctrl+5"), self, lambda: self.sidebar_tabs.setCurrentIndex(4))
        QShortcut(QKeySequence("Ctrl+6"), self, lambda: self.sidebar_tabs.setCurrentIndex(5))
        QShortcut(QKeySequence("Ctrl+7"), self, lambda: self.sidebar_tabs.setCurrentIndex(6))
        QShortcut(QKeySequence("Ctrl+8"), self, lambda: self.sidebar_tabs.setCurrentIndex(7))
        QShortcut(QKeySequence("Ctrl+9"), self, lambda: self.sidebar_tabs.setCurrentIndex(8))
        QShortcut(QKeySequence("Ctrl+I"), self, lambda: self.sidebar_tabs.setCurrentIndex(8))

        self.body_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.body_splitter.setStyleSheet("QSplitter::handle { background-color: #1e293b; width: 6px; border-radius: 3px; }")
        main_layout.addWidget(self.body_splitter, 1)
        
        graph_widget = QWidget()
        self.graph_layout = QGridLayout(graph_widget)
        self.body_splitter.addWidget(graph_widget)

        # Sidebar with Tabs
        self.sidebar_widget = QWidget()
        sidebar_layout = QVBoxLayout(self.sidebar_widget)
        sidebar_layout.setContentsMargins(0,0,0,0)
        self.sidebar_tabs = QTabWidget()
        self.sidebar_tabs.setUsesScrollButtons(True)
        self.sidebar_tabs.setElideMode(Qt.TextElideMode.ElideRight)

        def make_tab_scroll(w):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setStyleSheet("background-color: transparent;")
            scroll.setWidget(w)
            return scroll
        
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
        export_btn.setToolTip("Save the current log to a text file for intelligence reporting (Shortcut: Ctrl+E).")
        
        log_btn_layout.addWidget(clear_btn)
        log_btn_layout.addWidget(export_btn)
        
        log_layout.addWidget(self.log_text)
        log_layout.addLayout(log_btn_layout)
        self.sidebar_tabs.addTab(tab_log, "SITREP Log")
        
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
        
        intel_layout.addWidget(QLabel("Network Topology (Call && Response):"))
        self.topology_ui = QTreeWidget()
        self.topology_ui.setHeaderLabels(["Emitter Node", "Reply Count"])
        self.topology_ui.setToolTip("Network Topology: Analyzes transmission timings. If a radio consistently replies within 5 seconds of another finishing, the software draws a command link between them.")
        intel_layout.addWidget(self.topology_ui)
        
        intel_layout.addWidget(QLabel("FHSS Networks Tracked:"))
        self.fhss_ui = QListWidget()
        self.fhss_ui.setToolTip("FHSS Tracker: Operates in Sweep Mode. Mathematically calculates the hop-rate of evasive Frequency Hopping spread spectrum military networks.")
        intel_layout.addWidget(self.fhss_ui)
        
        self.sidebar_tabs.addTab(make_tab_scroll(tab_intel), "Intel DB")
        
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
        self.sidebar_tabs.addTab(tab_geo, "Geolocation && Map")

        # Tab 5: Drone Telemetry (Heltec V3)
        tab_drone = QWidget()
        drone_layout = QVBoxLayout(tab_drone)
        drone_layout.addWidget(self.create_drone_telemetry_ui())
        self.sidebar_tabs.addTab(make_tab_scroll(tab_drone), "Drone Telemetry")

        # Tab 6: Drone Video Feed
        tab_video = QWidget()
        video_layout = QVBoxLayout(tab_video)
        video_layout.addWidget(self.create_drone_video_ui())
        self.sidebar_tabs.addTab(make_tab_scroll(tab_video), "FPV Video Demod")
        
        # Tab 7: Direction Finding (KrakenSDR)
        tab_kraken = QWidget()
        kraken_layout = QVBoxLayout(tab_kraken)
        kraken_layout.addWidget(self.create_kraken_doa_ui())
        self.sidebar_tabs.addTab(make_tab_scroll(tab_kraken), "Kraken DoA / DF")

        # Tab 8: Autonomous Hunter-Killer Engine
        tab_hk = QWidget()
        hk_layout = QVBoxLayout(tab_hk)
        hk_layout.addWidget(self.create_hunter_killer_ui())
        self.sidebar_tabs.addTab(make_tab_scroll(tab_hk), "Hunter-Killer Engine")

        # Tab 9: Tactical AI Copilot & INTSUM
        tab_copilot = QWidget()
        copilot_layout = QVBoxLayout(tab_copilot)
        copilot_layout.addWidget(self.create_tactical_copilot_ui())
        self.sidebar_tabs.addTab(make_tab_scroll(tab_copilot), "AI Copilot && INTSUM")
        
        # State
        self.kraken_thread = None
        self.last_bearing_deg = 0.0
        self.bearing_history = []
        self.global_masks = []
        self.whitelist_regions = {}
        self.active_events = {}
        self.active_fhss_bands = {}
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

        # Hunter-Killer Engine State
        self.hk_active = False
        self.hk_state = "IDLE"
        self.hk_target_freq = 0.0
        self.hk_stare_start_time = 0.0
        self.hk_last_eval_time = {}
        self.hk_priority_queue = {}
        self.hk_resume_sweep_params = (850.0, 950.0)
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
        
        # Real-time Crosshair and Coordinate HUD
        self.cursor_v_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#38bdf8', width=1, style=Qt.PenStyle.DashLine))
        self.cursor_h_line = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('#38bdf8', width=1, style=Qt.PenStyle.DashLine))
        self.cursor_v_line.setZValue(20)
        self.cursor_h_line.setZValue(20)
        self.cursor_v_line.hide()
        self.cursor_h_line.hide()
        self.fft_plot.addItem(self.cursor_v_line)
        self.fft_plot.addItem(self.cursor_h_line)
        
        self.cursor_hud_text = pg.TextItem(text="", color='#38bdf8', anchor=(1, 0), fill=(15, 23, 42, 220), border='#0284c7')
        self.cursor_hud_text.setZValue(30)
        self.cursor_hud_text.hide()
        self.fft_plot.addItem(self.cursor_hud_text)

        self.vfo_region = pg.LinearRegionItem([502, 522], brush=pg.mkBrush(34, 197, 94, 70), pen=pg.mkPen('#22c55e', width=2))
        self.vfo_region.setZValue(10)
        self.vfo_region.sigRegionChanged.connect(self.update_vfo_offset)
        self.vfo_region.setToolTip("DEMODULATION VFO: Drag this green mask over a signal to listen to it without changing the center frequency.")
        self.fft_plot.addItem(self.vfo_region)
        
        self.fft_plot.setMouseEnabled(x=True, y=False)
        self.fft_plot._original_mousePressEvent = self.fft_plot.mousePressEvent
        self.fft_plot._original_mouseMoveEvent = self.fft_plot.mouseMoveEvent
        self.fft_plot._original_mouseReleaseEvent = self.fft_plot.mouseReleaseEvent
        self.fft_plot._original_mouseDoubleClickEvent = self.fft_plot.mouseDoubleClickEvent
        self.fft_plot._original_leaveEvent = self.fft_plot.leaveEvent
        self.fft_plot.mousePressEvent = self.fft_mouse_press
        self.fft_plot.mouseMoveEvent = self.fft_mouse_move
        self.fft_plot.mouseReleaseEvent = self.fft_mouse_release
        self.fft_plot.mouseDoubleClickEvent = self.fft_mouse_double_click
        self.fft_plot.leaveEvent = self.fft_mouse_leave
        self.graph_layout.addWidget(self.fft_plot, 0, 0, 1, 2)

        self.waterfall_plot = pg.PlotWidget(title="Waterfall Spectrogram")
        self.waterfall_plot.setLabel('bottom', 'Frequency Offset', units='MHz')
        self.waterfall_plot._original_mouseDoubleClickEvent = self.waterfall_plot.mouseDoubleClickEvent
        self.waterfall_plot.mouseDoubleClickEvent = self.waterfall_mouse_double_click
        self.waterfall_image = pg.ImageItem()
        self.waterfall_plot.addItem(self.waterfall_image)
        self.current_cmap = TACTICAL_COLORMAPS["Inferno (Default)"]
        self.waterfall_image.setLookupTable(self.current_cmap.getLookupTable())
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
        self.geo_parent_layout = geo_layout
        
        # Dashboard Header
        header_layout = QHBoxLayout()
        self.geo_status_label = QLabel("[ TARGET TELEMETRY: AWAITING PROTOCOL LOCK ]")
        self.geo_status_label.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 14px;")
        header_layout.addWidget(self.geo_status_label)

        self.geo_breadcrumbs_lbl = QLabel("[ TRACK POINTS: 0 ]")
        self.geo_breadcrumbs_lbl.setStyleSheet("color: #38bdf8; font-weight: bold; font-size: 12px; font-family: monospace;")
        header_layout.addWidget(self.geo_breadcrumbs_lbl)

        self.geo_clear_btn = QPushButton("CLEAR TRACKS")
        self.geo_clear_btn.setStyleSheet("background-color: #1e293b; color: #f87171; font-weight: bold; padding: 4px 8px; border: 1px solid #f87171; border-radius: 4px;")
        self.geo_clear_btn.clicked.connect(self.clear_tactical_tracks)
        header_layout.addWidget(self.geo_clear_btn)

        self.geo_detach_btn = QPushButton("DETACH MAP")
        self.geo_detach_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-weight: bold; padding: 4px 8px; border: 1px solid #0284c7; border-radius: 4px;")
        self.geo_detach_btn.clicked.connect(self.detach_map_window)
        header_layout.addWidget(self.geo_detach_btn)

        geo_layout.addLayout(header_layout)

        # Container wrapper for detaching
        self.geo_map_container = QWidget()
        map_cont_layout = QVBoxLayout(self.geo_map_container)
        map_cont_layout.setContentsMargins(0, 0, 0, 0)
        
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

                var bearingRay = null;
                var triangulationMarkers = [];
                var cepCircles = [];

                function updateBearingLine(originLat, originLon, bearingDeg, lengthMeters, color) {
                    var rayColor = color || '#f59e0b';
                    var R = 6378137;
                    var d = lengthMeters || 6000;
                    var brng = bearingDeg * Math.PI / 180;
                    var lat1 = originLat * Math.PI / 180;
                    var lon1 = originLon * Math.PI / 180;

                    var lat2 = Math.asin(Math.sin(lat1) * Math.cos(d / R) + Math.cos(lat1) * Math.sin(d / R) * Math.cos(brng));
                    var lon2 = lon1 + Math.atan2(Math.sin(brng) * Math.sin(d / R) * Math.cos(lat1), Math.cos(d / R) - Math.sin(lat1) * Math.sin(lat2));

                    var endLat = lat2 * 180 / Math.PI;
                    var endLon = lon2 * 180 / Math.PI;

                    if (!bearingRay) {
                        bearingRay = L.polyline([[originLat, originLon], [endLat, endLon]], {
                            color: rayColor,
                            weight: 3,
                            opacity: 0.9,
                            dashArray: '6, 6'
                        }).addTo(map);
                    } else {
                        bearingRay.setLatLngs([[originLat, originLon], [endLat, endLon]]);
                        bearingRay.setStyle({color: rayColor});
                    }
                }

                function clearBearingLine() {
                    if (bearingRay) {
                        map.removeLayer(bearingRay);
                        bearingRay = null;
                    }
                }

                function addTriangulationFix(lat, lon, label) {
                    var fixHtml = '<div style="background-color: #ef4444; width: 16px; height: 16px; border-radius: 3px; border: 2px solid white; box-shadow: 0 0 14px #ef4444; transform: rotate(45deg);"></div>';
                    var fixIcon = L.divIcon({
                        className: 'tri-icon',
                        html: fixHtml,
                        iconSize: [20, 20],
                        iconAnchor: [10, 10]
                    });
                    var marker = L.marker([lat, lon], {icon: fixIcon}).addTo(map);
                    marker.bindPopup("<b>TRIANGULATED FIX: " + (label || "EMITTER") + "</b><br>" + lat.toFixed(5) + ", " + lon.toFixed(5));
                    triangulationMarkers.push(marker);
                }

                function addCepFix(lat, lon, cepMeters, label) {
                    var fixHtml = '<div style="background-color: #ef4444; width: 14px; height: 14px; border-radius: 2px; border: 2px solid white; box-shadow: 0 0 16px #ef4444; transform: rotate(45deg);"></div>';
                    var fixIcon = L.divIcon({
                        className: 'cep-icon',
                        html: fixHtml,
                        iconSize: [18, 18],
                        iconAnchor: [9, 9]
                    });
                    var marker = L.marker([lat, lon], {icon: fixIcon}).addTo(map);
                    marker.bindPopup("<b>ESTIMATED TARGET FIX (95% CEP)</b><br>" + lat.toFixed(5) + ", " + lon.toFixed(5) + "<br>Accuracy Radius: &plusmn;" + cepMeters.toFixed(1) + "m");
                    triangulationMarkers.push(marker);

                    var circle = L.circle([lat, lon], {
                        radius: cepMeters,
                        color: '#ef4444',
                        fillColor: '#ef4444',
                        fillOpacity: 0.18,
                        weight: 2,
                        dashArray: '5, 5'
                    }).addTo(map);
                    cepCircles.push(circle);
                }

                var viewshedLayer = null;

                function updateViewshedOverlay(imageUrl, southWestLat, southWestLon, northEastLat, northEastLon) {
                    if (viewshedLayer) {
                        map.removeLayer(viewshedLayer);
                        viewshedLayer = null;
                    }
                    if (imageUrl) {
                        var bounds = [[southWestLat, southWestLon], [northEastLat, northEastLon]];
                        viewshedLayer = L.imageOverlay(imageUrl, bounds, {
                            opacity: 0.60,
                            interactive: false
                        }).addTo(map);
                    }
                }

                function clearViewshedOverlay() {
                    if (viewshedLayer) {
                        map.removeLayer(viewshedLayer);
                        viewshedLayer = null;
                    }
                }

                function clearTacticalTracks() {
                    droneTrail.setLatLngs([]);
                    if (rfCircle) {
                        map.removeLayer(rfCircle);
                        rfCircle = null;
                    }
                    if (bearingRay) {
                        map.removeLayer(bearingRay);
                        bearingRay = null;
                    }
                    for (var i = 0; i < triangulationMarkers.length; i++) {
                        map.removeLayer(triangulationMarkers[i]);
                    }
                    triangulationMarkers = [];
                    for (var j = 0; j < cepCircles.length; j++) {
                        map.removeLayer(cepCircles[j]);
                    }
                    cepCircles = [];
                    clearViewshedOverlay();
                }
            </script>
        </body>
        </html>
        """
        
        self.geo_map_view.setHtml(map_html)
        map_cont_layout.addWidget(self.geo_map_view)
        geo_layout.addWidget(self.geo_map_container)
        
        # Coordinates readout & Manual plot
        readout_layout = QHBoxLayout()
        self.geo_lat_input = QLineEdit()
        self.geo_lon_input = QLineEdit()
        self.geo_lat_input.setPlaceholderText("Latitude")
        self.geo_lon_input.setPlaceholderText("Longitude")
        self.geo_lat_input.setText("51.5074")
        self.geo_lon_input.setText("-0.1278")
        
        self.geo_plot_btn = QPushButton("PLOT MANUALLY")
        self.geo_plot_btn.setStyleSheet("background-color: #0f172a; color: #38bdf8; font-weight: bold; border: 1px solid #1e293b; padding: 4px 8px; border-radius: 4px;")
        self.geo_plot_btn.clicked.connect(self.manual_plot_target)

        self.solve_cep_btn = QPushButton("SOLVE CEP FIX")
        self.solve_cep_btn.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; border: 1px solid #38bdf8; padding: 4px 8px; border-radius: 4px;")
        self.solve_cep_btn.clicked.connect(self.solve_multibearing_cep)
        
        readout_layout.addWidget(QLabel("Observer Lat:"))
        readout_layout.addWidget(self.geo_lat_input)
        readout_layout.addWidget(QLabel("Lon:"))
        readout_layout.addWidget(self.geo_lon_input)
        readout_layout.addWidget(self.geo_plot_btn)
        readout_layout.addWidget(self.solve_cep_btn)
        geo_layout.addLayout(readout_layout)

        # EMBM & Tactical Terrain Shadowing Control Panel
        embm_group = QGroupBox("EMBM && Tactical Terrain Shadowing (4/3 Earth && 1st Fresnel Engine)")
        embm_layout = QGridLayout(embm_group)
        embm_layout.setContentsMargins(6, 6, 6, 6)
        embm_layout.setSpacing(6)

        self.embm_mast_spin = QDoubleSpinBox()
        self.embm_mast_spin.setRange(1.0, 100.0)
        self.embm_mast_spin.setValue(10.0)
        self.embm_mast_spin.setSuffix(" m")

        self.embm_uav_alt_spin = QDoubleSpinBox()
        self.embm_uav_alt_spin.setRange(5.0, 500.0)
        self.embm_uav_alt_spin.setValue(25.0)
        self.embm_uav_alt_spin.setSuffix(" m AGL")

        self.embm_range_spin = QDoubleSpinBox()
        self.embm_range_spin.setRange(1.0, 50.0)
        self.embm_range_spin.setValue(15.0)
        self.embm_range_spin.setSuffix(" km")

        self.embm_freq_spin = QDoubleSpinBox()
        self.embm_freq_spin.setRange(30.0, 6000.0)
        self.embm_freq_spin.setValue(915.0)
        self.embm_freq_spin.setSuffix(" MHz")

        self.embm_compute_btn = QPushButton("COMPUTE TERRAIN VIEWSHED")
        self.embm_compute_btn.setStyleSheet("background-color: #10b981; color: white; font-weight: bold; padding: 6px 12px; border-radius: 4px;")
        self.embm_compute_btn.clicked.connect(self.compute_and_render_viewshed)

        self.embm_clear_btn = QPushButton("CLEAR HEATMAP")
        self.embm_clear_btn.setStyleSheet("background-color: #1e293b; color: #f87171; font-weight: bold; padding: 6px; border: 1px solid #f87171; border-radius: 4px;")
        self.embm_clear_btn.clicked.connect(self.clear_viewshed_overlay)

        self.embm_status_lbl = QLabel("[ EMBM: STANDBY | 4/3 EARTH && FRESNEL ENGINE READY ]")
        self.embm_status_lbl.setStyleSheet("background-color: #060a14; color: #38bdf8; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #1e293b; border-radius: 4px;")

        embm_layout.addWidget(QLabel("Mast Height:"), 0, 0)
        embm_layout.addWidget(self.embm_mast_spin, 0, 1)
        embm_layout.addWidget(QLabel("Target Alt:"), 0, 2)
        embm_layout.addWidget(self.embm_uav_alt_spin, 0, 3)
        embm_layout.addWidget(QLabel("Max Horizon:"), 0, 4)
        embm_layout.addWidget(self.embm_range_spin, 0, 5)
        embm_layout.addWidget(QLabel("Frequency:"), 0, 6)
        embm_layout.addWidget(self.embm_freq_spin, 0, 7)

        embm_layout.addWidget(self.embm_compute_btn, 1, 0, 1, 4)
        embm_layout.addWidget(self.embm_clear_btn, 1, 4, 1, 4)
        embm_layout.addWidget(self.embm_status_lbl, 2, 0, 1, 8)

        geo_layout.addWidget(embm_group)
        return geo_widget

    def clear_tactical_tracks(self):
        self.gps_breadcrumbs_count = 0
        if hasattr(self, 'geo_breadcrumbs_lbl'):
            self.geo_breadcrumbs_lbl.setText("[ TRACK POINTS: 0 ]")
        if hasattr(self, 'geo_map_view'):
            self.geo_map_view.page().runJavaScript("clearTacticalTracks();")
        self.log_event("TACTICAL MAP: Flight path breadcrumbs cleared.")

    def create_drone_telemetry_ui(self):
        drone_widget = QWidget()
        layout = QVBoxLayout(drone_widget)
        layout.setSpacing(6)
        self.drone_parent_layout = layout

        # Header Status & Detach Bar
        header_row = QHBoxLayout()
        self.drone_status_label = QLabel("[ HELTEC V3: SEARCHING FOR 915MHz PACKETS ]")
        self.drone_status_label.setStyleSheet("background-color: #090d16; color: #f59e0b; font-weight: bold; font-size: 13px; padding: 6px; border: 1px solid #1e293b; border-radius: 4px;")
        
        self.drone_detach_btn = QPushButton("DETACH COCKPIT")
        self.drone_detach_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-weight: bold; padding: 4px 8px; border: 1px solid #0284c7; border-radius: 4px;")
        self.drone_detach_btn.clicked.connect(self.detach_drone_window)
        
        header_row.addWidget(self.drone_status_label, 1)
        header_row.addWidget(self.drone_detach_btn)
        layout.addLayout(header_row)

        self.drone_cockpit_container = QWidget()
        cockpit_layout = QVBoxLayout(self.drone_cockpit_container)
        cockpit_layout.setContentsMargins(0, 0, 0, 0)
        cockpit_layout.setSpacing(6)

        # Feature 0: ExpressLRS Packet Rate & Dynamic Demodulation Control
        rate_group = QGroupBox("ExpressLRS Sniffer Packet Rate && Dynamic Demodulation")
        rate_group.setStyleSheet("QGroupBox { color: #38bdf8; font-weight: bold; border: 1px solid #1e293b; border-radius: 6px; margin-top: 6px; padding-top: 10px; background-color: #0b0f19; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }")
        rate_layout = QGridLayout(rate_group)
        rate_layout.setContentsMargins(8, 8, 8, 8)

        self.elrs_rate_mode_combo = QComboBox()
        self.elrs_rate_mode_combo.addItems([
            "Auto-Detect (Dynamic Auto-Rate Scanning)",
            "50 Hz (Standard 915MHz - SF8 / 20ms)",
            "25 Hz (Long Range - SF9 / 40ms)",
            "100 Hz (Standard 8ch - SF7 / 10ms)",
            "100 Hz Full (16ch Full Res - SF7 / 10ms)",
            "D50 (Déjà Vu 50Hz - SF7 / 10ms)",
            "150 Hz (SF7 / 6.6ms)",
            "200 Hz (SF6 / 5ms)",
            "250 Hz (SF6 / 4ms)",
            "333 Hz Full (16ch Full Res - SF5 / 3ms)"
        ])
        self.elrs_rate_mode_combo.currentIndexChanged.connect(self.on_elrs_rate_selected)
        rate_layout.addWidget(QLabel("Rate Selector:"), 0, 0)
        rate_layout.addWidget(self.elrs_rate_mode_combo, 0, 1)

        self.elrs_rate_badge = QLabel("[ ACTIVE DEMOD: 50Hz | SF8 | BW: 500kHz | Interval: 20000 µs ]")
        self.elrs_rate_badge.setStyleSheet("background-color: #060a14; color: #38bdf8; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #1e293b; border-radius: 4px;")
        rate_layout.addWidget(self.elrs_rate_badge, 1, 0, 1, 2)
        cockpit_layout.addWidget(rate_group)

        # Feature 0.5: Multi-Pilot Airspace Discovery & Target Selection
        pilot_group = QGroupBox("Pilot Target Selector && Airspace Surveillance")
        pilot_group.setStyleSheet("QGroupBox { color: #38bdf8; font-weight: bold; border: 1px solid #1e293b; border-radius: 6px; margin-top: 6px; padding-top: 10px; background-color: #0b0f19; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }")
        pilot_layout = QGridLayout(pilot_group)
        pilot_layout.setContentsMargins(8, 8, 8, 8)

        self.pilot_selector_combo = QComboBox()
        self.pilot_selector_combo.addItem("Auto-Track Any Pilot (First / Strongest Sync)")
        self.pilot_selector_combo.setToolTip("Select which pilot / transmitter UID to lock and demodulate over the air.")

        self.pilot_lock_btn = QPushButton("LOCK PILOT")
        self.pilot_lock_btn.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; border: 1px solid #38bdf8;")
        self.pilot_lock_btn.clicked.connect(self.on_lock_pilot_clicked)

        self.pilot_auto_btn = QPushButton("AUTO / ANY")
        self.pilot_auto_btn.setStyleSheet("background-color: #1e293b; color: #cbd5e1; border: 1px solid #334155;")
        self.pilot_auto_btn.clicked.connect(self.on_unlock_pilot_clicked)

        self.pilot_target_badge = QLabel("[ ACTIVE TARGET: AUTO / ANY PILOT ]")
        self.pilot_target_badge.setStyleSheet("background-color: #060a14; color: #38bdf8; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #1e293b; border-radius: 4px;")

        pilot_layout.addWidget(QLabel("Target Pilot:"), 0, 0)
        pilot_layout.addWidget(self.pilot_selector_combo, 0, 1)
        pilot_layout.addWidget(self.pilot_lock_btn, 0, 2)
        pilot_layout.addWidget(self.pilot_auto_btn, 0, 3)
        pilot_layout.addWidget(self.pilot_target_badge, 1, 0, 1, 4)

        cockpit_layout.addWidget(pilot_group)

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
        cockpit_layout.addWidget(hud_box)

        # Feature 2: Tactical Flight Dynamics && Maneuver Classifier
        classifier_box = QGroupBox("Tactical Flight Dynamics && Maneuver Classifier")
        classifier_box.setStyleSheet("QGroupBox { color: #38bdf8; font-weight: bold; border: 1px solid #1e293b; border-radius: 6px; margin-top: 6px; padding-top: 10px; background-color: #0b0f19; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }")
        classifier_layout = QVBoxLayout(classifier_box)
        classifier_layout.setContentsMargins(6, 6, 6, 6)

        self.maneuver_badge = QLabel("DISARMED / MOTOR SHUTDOWN")
        self.maneuver_badge.setStyleSheet("background-color: #1e293b; color: #94a3b8; font-weight: bold; font-size: 14px; padding: 8px; border-radius: 4px; border: 1px solid #334155;")
        self.maneuver_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        classifier_layout.addWidget(self.maneuver_badge)

        self.maneuver_detail_lbl = QLabel("Motors Idle | Throttle: 0% | Cyclic Rate: 0 µs/s")
        self.maneuver_detail_lbl.setStyleSheet("color: #64748b; font-family: monospace; font-size: 11px;")
        self.maneuver_detail_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        classifier_layout.addWidget(self.maneuver_detail_lbl)
        cockpit_layout.addWidget(classifier_box)

        # Feature 4: Dual-Link RF Proximity && Link Margin Gauge
        rf_box = QGroupBox("Dual-Link RF Proximity && Link Margin")
        rf_box.setStyleSheet("QGroupBox { color: #38bdf8; font-weight: bold; border: 1px solid #1e293b; border-radius: 6px; margin-top: 6px; padding-top: 10px; background-color: #0b0f19; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }")
        rf_layout = QGridLayout(rf_box)
        rf_layout.setContentsMargins(8, 8, 8, 8)

        rf_layout.addWidget(QLabel("Pilot Proximity (Station-to-TX):"), 0, 0)
        self.proximity_lbl = QLabel("MEDIUM TACTICAL RANGE (200m - 800m)")
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
        self.link_margin_lbl = QLabel("NOMINAL LINK (100% RC Integrity)")
        self.link_margin_lbl.setStyleSheet("color: #22c55e; font-weight: bold; font-size: 12px;")
        rf_layout.addWidget(self.link_margin_lbl, 2, 1)

        self.drone_lq_bar = QProgressBar()
        self.drone_lq_bar.setRange(0, 100)
        self.drone_lq_bar.setValue(0)
        self.drone_lq_bar.setTextVisible(False)
        self.drone_lq_bar.setFixedHeight(8)
        self.drone_lq_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #22c55e; border-radius: 3px; }")
        rf_layout.addWidget(self.drone_lq_bar, 3, 0, 1, 2)
        cockpit_layout.addWidget(rf_box)

        # Feature 3: 16-Channel Live Diagnostic Matrix (ELRS / CRSF)
        ch_box = QGroupBox("16-Channel Live Diagnostic Matrix (ELRS / CRSF)")
        ch_box.setStyleSheet("QGroupBox { color: #38bdf8; font-weight: bold; border: 1px solid #1e293b; border-radius: 6px; margin-top: 6px; padding-top: 10px; background-color: #0b0f19; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }")
        ch_layout = QGridLayout(ch_box)
        ch_layout.setContentsMargins(6, 6, 6, 6)
        ch_layout.setSpacing(4)

        self.channel_bars = []
        self.channel_labels = []
        ch_names = [
            "CH1 (Roll)", "CH2 (Pitch)", "CH3 (Throttle)", "CH4 (Yaw)",
            "CH5 (AUX1/Arm)", "CH6 (AUX2/Mode)", "CH7 (AUX3/VTX)", "CH8 (AUX4/Rescue)",
            "CH9 (AUX5)", "CH10 (AUX6)", "CH11 (AUX7)", "CH12 (AUX8)",
            "CH13 (AUX9)", "CH14 (AUX10)", "CH15 (AUX11)", "CH16 (AUX12)"
        ]

        for idx, name in enumerate(ch_names):
            row = idx // 2
            col_offset = (idx % 2) * 3

            lbl = QLabel(f"{name}:")
            lbl.setStyleSheet("color: #94a3b8; font-size: 8.5pt; font-family: 'Consolas', monospace;")
            
            bar = QProgressBar()
            bar.setRange(988, 2012)
            bar.setValue(1500 if idx != 2 else 988)
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 3px; } QProgressBar::chunk { background-color: #38bdf8; border-radius: 2px; }")
            
            val_lbl = QLabel("1500µs" if idx != 2 else "988µs")
            val_lbl.setStyleSheet("color: #38bdf8; font-weight: bold; font-size: 8.5pt; font-family: 'Consolas', monospace;")
            val_lbl.setFixedWidth(52)

            ch_layout.addWidget(lbl, row, col_offset)
            ch_layout.addWidget(bar, row, col_offset + 1)
            ch_layout.addWidget(val_lbl, row, col_offset + 2)

            self.channel_bars.append(bar)
            self.channel_labels.append(val_lbl)

        cockpit_layout.addWidget(ch_box)

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
        self.drone_att_lbl = QLabel("Attitude: P:--° | R:--° | Y:--°")
        self.drone_fmode_lbl = QLabel("Flight Mode: --")

        for lbl in [self.drone_rssi_lbl, self.drone_snr_lbl, self.drone_lq_lbl, self.drone_remote_rssi_lbl,
                    self.drone_vbat_lbl, self.drone_curr_lbl, self.drone_pilot_lbl, self.drone_arm_lbl,
                    self.drone_att_lbl, self.drone_fmode_lbl]:
            lbl.setStyleSheet("background-color: #0f172a; color: #38bdf8; border: 1px solid #1e293b; padding: 6px; border-radius: 4px; font-weight: bold; font-size: 11px;")

        grid.addWidget(self.drone_rssi_lbl, 0, 0)
        grid.addWidget(self.drone_snr_lbl, 0, 1)
        grid.addWidget(self.drone_lq_lbl, 1, 0)
        grid.addWidget(self.drone_remote_rssi_lbl, 1, 1)
        grid.addWidget(self.drone_vbat_lbl, 2, 0)
        grid.addWidget(self.drone_curr_lbl, 2, 1)
        grid.addWidget(self.drone_pilot_lbl, 3, 0)
        grid.addWidget(self.drone_arm_lbl, 3, 1)
        grid.addWidget(self.drone_att_lbl, 4, 0)
        grid.addWidget(self.drone_fmode_lbl, 4, 1)
        cockpit_layout.addLayout(grid)

        layout.addWidget(self.drone_cockpit_container)
        layout.addStretch()
        return drone_widget

    def create_drone_video_ui(self):
        video_widget = QWidget()
        layout = QVBoxLayout(video_widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        self.video_parent_layout = layout

        # Header Row & Detach Button
        header_row = QHBoxLayout()
        self.video_status_label = QLabel("[ FPV VIDEO DEMODULATOR: READY ]")
        self.video_status_label.setStyleSheet("background-color: #090d16; color: #38bdf8; font-weight: bold; font-size: 13px; padding: 6px; border: 1px solid #1e293b; border-radius: 4px;")
        
        self.video_detach_btn = QPushButton("DETACH VIDEO")
        self.video_detach_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-weight: bold; padding: 4px 8px; border: 1px solid #0284c7; border-radius: 4px;")
        self.video_detach_btn.clicked.connect(self.detach_video_window)
        
        header_row.addWidget(self.video_status_label, 1)
        header_row.addWidget(self.video_detach_btn)
        layout.addLayout(header_row)

        self.auto_fusion_sweep_video_cb = QCheckBox("Auto-Demodulate Video on Sweep VTX Carrier Detect")
        self.auto_fusion_sweep_video_cb.setChecked(True)
        self.auto_fusion_sweep_video_cb.setToolTip("When in Sweep mode, automatically tune and demodulate active 5.8GHz / 1.2GHz video carriers.")
        layout.addWidget(self.auto_fusion_sweep_video_cb)

        # Video container for popping out
        self.video_display_container = QWidget()
        video_cont_layout = QVBoxLayout(self.video_display_container)
        video_cont_layout.setContentsMargins(0, 0, 0, 0)
        video_cont_layout.setSpacing(6)

        # Tactical Video Display Screen (Resizable / Auto-Expanding)
        self.video_display = VideoDisplayWidget()
        self.video_display.setMinimumHeight(260)
        self.video_display.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.video_display.on_double_click_cb = self.toggle_floating_video
        video_cont_layout.addWidget(self.video_display)

        # Video Display Sizing & Floating Pop-Out Controls
        size_btn_layout = QHBoxLayout()
        size_btn_layout.setContentsMargins(0, 0, 0, 0)
        
        self.video_size_combo = QComboBox()
        self.video_size_combo.addItems([
            "Embedded: Compact (240p)",
            "Embedded: Medium (320p)",
            "Embedded: Large (420p)",
            "Embedded: Full (Auto-Expand)"
        ])
        self.video_size_combo.setCurrentIndex(1) # Default Medium (320p)
        self.video_size_combo.currentIndexChanged.connect(self.on_video_size_changed)
        
        self.popout_video_btn = QPushButton("POP-OUT LARGE WINDOW")
        self.popout_video_btn.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; padding: 6px 12px; border-radius: 4px;")
        self.popout_video_btn.setToolTip("Open video stream in a dedicated, freely resizable floating window (Double-click video screen to toggle).")
        self.popout_video_btn.clicked.connect(self.toggle_floating_video)
        
        size_btn_layout.addWidget(self.video_size_combo)
        size_btn_layout.addWidget(self.popout_video_btn)
        video_cont_layout.addLayout(size_btn_layout)

        # Source Selection Group
        source_group = QGroupBox("Video Stream Source")
        source_layout = QVBoxLayout(source_group)
        source_layout.setContentsMargins(6, 6, 6, 6)

        self.video_source_combo = QComboBox()
        self.video_source_combo.addItems([
            "Native HackRF Demod (5.8G / 1.2G)",
            "SDRangel UDP Stream (udp://127.0.0.1:5005)",
            "Custom RTSP / UDP / HTTP Stream"
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
        self.fpv_channel_combo.setCurrentIndex(0) # Default RaceBand R5 (5805 MHz)
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

        video_cont_layout.addWidget(source_group)

        # Demodulator Tuning & Display Controls (SDRangel Style)
        tuning_group = QGroupBox("Demodulator, H-Sync && V-Sync Controls")
        tuning_layout = QGridLayout(tuning_group)
        tuning_layout.setContentsMargins(6, 6, 6, 6)

        self.video_standard_combo = QComboBox()
        self.video_standard_combo.addItems(["PAL (64.0 µs / 640 px)", "NTSC (63.55 µs / 636 px)"])
        self.video_standard_combo.currentIndexChanged.connect(self.update_video_tuning)
        tuning_layout.addWidget(QLabel("Standard:"), 0, 0)
        tuning_layout.addWidget(self.video_standard_combo, 0, 1)

        self.video_palette_combo = QComboBox()
        self.video_palette_combo.addItems(["Grayscale (Analog CRT)", "Tactical NVG (Green)", "Amber (Thermal FLIR)"])
        self.video_palette_combo.currentIndexChanged.connect(self.update_video_tuning)
        tuning_layout.addWidget(QLabel("Palette:"), 1, 0)
        tuning_layout.addWidget(self.video_palette_combo, 1, 1)

        # H-Sync PLL and V-Sync Controls
        self.auto_hsync_cb = QCheckBox("Auto H-Sync Lock (PLL)")
        self.auto_hsync_cb.setChecked(True)
        self.auto_hsync_cb.toggled.connect(self.update_video_tuning)
        tuning_layout.addWidget(self.auto_hsync_cb, 2, 0)

        self.auto_vsync_cb = QCheckBox("Auto V-Sync Lock (V-Hold)")
        self.auto_vsync_cb.setChecked(True)
        self.auto_vsync_cb.toggled.connect(self.update_video_tuning)
        tuning_layout.addWidget(self.auto_vsync_cb, 2, 1)

        self.hsync_lbl = QLabel("H-Sync: 640 px (15.63 kHz)")
        self.hsync_lbl.setStyleSheet("color: #38bdf8; font-weight: bold;")
        tuning_layout.addWidget(self.hsync_lbl, 3, 0)

        self.hsync_slider = QSlider(Qt.Orientation.Horizontal)
        self.hsync_slider.setRange(620, 660)
        self.hsync_slider.setValue(640)
        self.hsync_slider.valueChanged.connect(self.update_video_tuning)
        tuning_layout.addWidget(self.hsync_slider, 3, 1)

        self.vhold_lbl = QLabel("V-Hold: 0 lines")
        self.vhold_lbl.setStyleSheet("color: #38bdf8; font-weight: bold;")
        tuning_layout.addWidget(self.vhold_lbl, 4, 0)

        self.vhold_slider = QSlider(Qt.Orientation.Horizontal)
        self.vhold_slider.setRange(0, 625)
        self.vhold_slider.setValue(0)
        self.vhold_slider.valueChanged.connect(self.update_video_tuning)
        tuning_layout.addWidget(self.vhold_slider, 4, 1)

        self.hhold_lbl = QLabel("H-Hold: 0 px")
        self.hhold_lbl.setStyleSheet("color: #38bdf8; font-weight: bold;")
        tuning_layout.addWidget(self.hhold_lbl, 5, 0)

        self.hhold_slider = QSlider(Qt.Orientation.Horizontal)
        self.hhold_slider.setRange(-320, 320)
        self.hhold_slider.setValue(0)
        self.hhold_slider.valueChanged.connect(self.update_video_tuning)
        tuning_layout.addWidget(self.hhold_slider, 5, 1)

        self.invert_polarity_cb = QCheckBox("Invert Polarity (Sync Up)")
        self.invert_polarity_cb.setToolTip("Toggle if video sync is inverted or video signal appears inverted.")
        self.invert_polarity_cb.toggled.connect(self.update_video_tuning)
        tuning_layout.addWidget(self.invert_polarity_cb, 6, 0)

        self.show_reticle_cb = QCheckBox("Tactical Crosshairs")
        self.show_reticle_cb.toggled.connect(self.toggle_video_reticle)
        tuning_layout.addWidget(self.show_reticle_cb, 6, 1)

        # Contrast & Brightness Sliders
        tuning_layout.addWidget(QLabel("Contrast AGC:"), 7, 0)
        self.video_contrast_slider = QSlider(Qt.Orientation.Horizontal)
        self.video_contrast_slider.setRange(50, 300)
        self.video_contrast_slider.setValue(100)
        self.video_contrast_slider.valueChanged.connect(self.update_video_tuning)
        tuning_layout.addWidget(self.video_contrast_slider, 7, 1)

        tuning_layout.addWidget(QLabel("Brightness:"), 8, 0)
        self.video_brightness_slider = QSlider(Qt.Orientation.Horizontal)
        self.video_brightness_slider.setRange(-50, 50)
        self.video_brightness_slider.setValue(0)
        self.video_brightness_slider.valueChanged.connect(self.update_video_tuning)
        tuning_layout.addWidget(self.video_brightness_slider, 8, 1)

        video_cont_layout.addWidget(tuning_group)

        # Action Buttons
        btn_layout = QHBoxLayout()
        self.toggle_video_btn = QPushButton("START VIDEO STREAM")
        self.toggle_video_btn.setStyleSheet("background-color: #10b981; color: white; font-weight: bold; padding: 8px;")
        self.toggle_video_btn.clicked.connect(self.toggle_video_stream)
        
        self.video_snapshot_btn = QPushButton("SNAPSHOT")
        self.video_snapshot_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-weight: bold; border: 1px solid #38bdf8;")
        self.video_snapshot_btn.clicked.connect(self.capture_video_snapshot)
        
        btn_layout.addWidget(self.toggle_video_btn)
        btn_layout.addWidget(self.video_snapshot_btn)
        video_cont_layout.addLayout(btn_layout)

        layout.addWidget(self.video_display_container)

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
        auto_hsync = self.auto_hsync_cb.isChecked()
        manual_line_len = self.hsync_slider.value()
        auto_vsync = self.auto_vsync_cb.isChecked()
        v_hold_offset = self.vhold_slider.value() if hasattr(self, 'vhold_slider') else 0
        h_hold_offset = self.hhold_slider.value() if hasattr(self, 'hhold_slider') else 0
        
        freq_khz = (10000.0 / manual_line_len)
        self.hsync_lbl.setText(f"H-Sync: {manual_line_len} px ({freq_khz:.2f} kHz)")
        if hasattr(self, 'vhold_lbl'):
            self.vhold_lbl.setText(f"V-Hold: {v_hold_offset} lines")
        if hasattr(self, 'hhold_lbl'):
            self.hhold_lbl.setText(f"H-Hold: {h_hold_offset} px")
        
        if self.native_video_thread and self.native_video_thread.isRunning():
            self.native_video_thread.set_tuning(standard, invert, palette, brightness, contrast, auto_hsync, manual_line_len, auto_vsync, v_hold_offset, h_hold_offset)

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
            freq_mhz = self.fpv_channel_combo.currentData() or 5805
            standard = "PAL" if "PAL" in self.video_standard_combo.currentText() else "NTSC"
            invert = self.invert_polarity_cb.isChecked()
            palette_text = self.video_palette_combo.currentText()
            palette = "TACTICAL_GREEN" if "Green" in palette_text else ("AMBER_FLIR" if "Amber" in palette_text else "GRAYSCALE")
            contrast = self.video_contrast_slider.value() / 100.0
            brightness = self.video_brightness_slider.value()
            lna = max(32, self.lna_input.value())
            vga = max(32, self.vga_input.value())

            # Temporarily pause background SDR spectrum thread to yield HackRF USB device
            if self.hackrf_thread and self.hackrf_thread.isRunning():
                self.hackrf_thread.stop()
                self.hackrf_thread.wait(600)
                time.sleep(0.15)

            auto_hsync = self.auto_hsync_cb.isChecked()
            manual_line_len = self.hsync_slider.value()
            auto_vsync = self.auto_vsync_cb.isChecked()

            self.native_video_thread = NativeHackRFVideoThread(
                freq_mhz=freq_mhz,
                standard=standard,
                invert_polarity=invert,
                lna=lna,
                vga=vga
            )
            v_hold_offset = self.vhold_slider.value() if hasattr(self, 'vhold_slider') else 0
            h_hold_offset = self.hhold_slider.value() if hasattr(self, 'hhold_slider') else 0
            self.native_video_thread.set_tuning(standard, invert, palette, brightness, contrast, auto_hsync, manual_line_len, auto_vsync, v_hold_offset, h_hold_offset)
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

    def on_video_size_changed(self, index):
        if not hasattr(self, 'video_display'):
            return
        if index == 0:
            self.video_display.setMinimumHeight(200)
            self.video_display.setMaximumHeight(240)
        elif index == 1:
            self.video_display.setMinimumHeight(260)
            self.video_display.setMaximumHeight(320)
        elif index == 2:
            self.video_display.setMinimumHeight(360)
            self.video_display.setMaximumHeight(420)
        else:
            self.video_display.setMinimumHeight(240)
            self.video_display.setMaximumHeight(16777215)
        self.video_display.update()

    def toggle_floating_video(self):
        if self.floating_video_window is None:
            self.floating_video_window = FloatingVideoWindow(self)
        if self.floating_video_window.isVisible():
            self.floating_video_window.hide()
        else:
            if hasattr(self, 'video_display'):
                self.floating_video_window.video_display.set_stream_info(
                    self.video_display.osd_channel,
                    self.video_display.osd_mode
                )
                self.floating_video_window.video_display.show_reticle = self.video_display.show_reticle
            self.floating_video_window.show()
            self.floating_video_window.raise_()
            self.floating_video_window.activateWindow()

    def on_video_frame(self, qimage, sync_locked, fps):
        if hasattr(self, 'video_display'):
            self.video_display.update_frame(qimage, sync_locked, fps)
        if self.floating_video_window and self.floating_video_window.isVisible():
            self.floating_video_window.video_display.set_stream_info(
                self.video_display.osd_channel,
                self.video_display.osd_mode
            )
            self.floating_video_window.video_display.show_reticle = self.video_display.show_reticle
            self.floating_video_window.video_display.update_frame(qimage, sync_locked, fps)

    def capture_video_snapshot(self):
        if not hasattr(self, 'video_display') or self.video_display.current_pixmap is None:
            self.log_event("Cannot snapshot: No video frame available.")
            return
            
        self.video_display.current_pixmap.save(filepath, "PNG")
        self.log_event(f"Saved Video Snapshot: {filepath}")

    def create_kraken_doa_ui(self):
        kraken_widget = QWidget()
        layout = QVBoxLayout(kraken_widget)
        layout.setSpacing(6)
        self.kraken_parent_layout = layout

        # Header Status & Detach Button
        header_row = QHBoxLayout()
        self.kraken_status_label = QLabel("[ KRAKENSDR DoA: STANDBY / DISCONNECTED ]")
        self.kraken_status_label.setStyleSheet("background-color: #060a14; color: #f59e0b; font-weight: bold; font-size: 13px; padding: 6px; border: 1px solid #1e293b; border-radius: 4px;")
        
        self.kraken_detach_btn = QPushButton("DETACH COMPASS")
        self.kraken_detach_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-weight: bold; padding: 4px 8px; border: 1px solid #0284c7; border-radius: 4px;")
        self.kraken_detach_btn.clicked.connect(self.detach_kraken_window)
        
        header_row.addWidget(self.kraken_status_label, 1)
        header_row.addWidget(self.kraken_detach_btn)
        layout.addLayout(header_row)

        self.auto_fusion_heltec_kraken_cb = QCheckBox("Auto-Retune DoA Array to Locked Pilot (915 MHz)")
        self.auto_fusion_heltec_kraken_cb.setChecked(True)
        self.auto_fusion_heltec_kraken_cb.setToolTip("Automatically tune the KrakenSDR array to the active 915 MHz frequency whenever an ELRS pilot packet is received.")
        layout.addWidget(self.auto_fusion_heltec_kraken_cb)

        # DoA Compass Container for popping out
        self.doa_compass_container = QWidget()
        compass_cont_layout = QVBoxLayout(self.doa_compass_container)
        compass_cont_layout.setContentsMargins(0, 0, 0, 0)
        compass_cont_layout.setSpacing(6)

        # 360 Tactical Compass HUD (Polar Rose + MUSIC Spectrum)
        self.doa_compass = DoACompassWidget()
        compass_cont_layout.addWidget(self.doa_compass)

        # KrakenSDR Server Daemon && Health Control
        service_group = QGroupBox("KrakenSDR Server Daemon && Health Control")
        service_layout = QGridLayout(service_group)
        service_layout.setContentsMargins(6, 6, 6, 6)

        self.kraken_health_badge = QLabel("[ SERVER: CHECKING CONNECTION... ]")
        self.kraken_health_badge.setStyleSheet("background-color: #060a14; color: #38bdf8; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #1e293b; border-radius: 4px;")

        self.kraken_start_btn = QPushButton("START")
        self.kraken_start_btn.setStyleSheet("background-color: #065f46; color: #34d399; font-weight: bold; border: 1px solid #059669; padding: 5px;")
        self.kraken_start_btn.clicked.connect(self.start_kraken_service)

        self.kraken_stop_btn = QPushButton("STOP")
        self.kraken_stop_btn.setStyleSheet("background-color: #7f1d1d; color: #f87171; font-weight: bold; border: 1px solid #dc2626; padding: 5px;")
        self.kraken_stop_btn.clicked.connect(self.stop_kraken_service)

        self.kraken_restart_btn = QPushButton("RESTART")
        self.kraken_restart_btn.setStyleSheet("background-color: #1e293b; color: #fbbf24; font-weight: bold; border: 1px solid #d97706; padding: 5px;")
        self.kraken_restart_btn.clicked.connect(self.restart_kraken_service)

        self.kraken_check_btn = QPushButton("CHECK HEALTH")
        self.kraken_check_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-weight: bold; border: 1px solid #0284c7; padding: 5px;")
        self.kraken_check_btn.clicked.connect(self.check_kraken_health)

        self.kraken_repair_btn = QPushButton("AUTO-REPAIR && FIX")
        self.kraken_repair_btn.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; border: 1px solid #38bdf8; padding: 5px;")
        self.kraken_repair_btn.clicked.connect(self.repair_kraken_sdr)

        self.kraken_attach_btn = QPushButton("ATTACH USB (WSL)")
        self.kraken_attach_btn.setStyleSheet("background-color: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 5px;")
        self.kraken_attach_btn.clicked.connect(self.attach_kraken_usb)

        service_layout.addWidget(self.kraken_health_badge, 0, 0, 1, 4)
        service_layout.addWidget(self.kraken_start_btn, 1, 0)
        service_layout.addWidget(self.kraken_stop_btn, 1, 1)
        service_layout.addWidget(self.kraken_restart_btn, 1, 2)
        service_layout.addWidget(self.kraken_check_btn, 1, 3)
        service_layout.addWidget(self.kraken_repair_btn, 2, 0, 1, 2)
        service_layout.addWidget(self.kraken_attach_btn, 2, 2, 1, 2)

        compass_cont_layout.addWidget(service_group)

        # Connection & Stream Controls
        conn_group = QGroupBox("KrakenSDR Data Stream Connection")
        conn_layout = QGridLayout(conn_group)
        conn_layout.setContentsMargins(6, 6, 6, 6)

        self.kraken_mode_combo = QComboBox()
        self.kraken_mode_combo.addItems([
            "Local Kraken SDR (Automatic Fast Stream - Recommended)",
            "Target Simulator (Test Mode)",
            "Network IP (HTTP / Port 8081)",
            "UDP Broadcast (Port 5005)"
        ])
        self.kraken_mode_combo.currentIndexChanged.connect(self.on_kraken_mode_changed)
        conn_layout.addWidget(QLabel("Stream Mode:"), 0, 0)
        conn_layout.addWidget(self.kraken_mode_combo, 0, 1)

        # Host / IP Input
        self.kraken_host_row = QWidget()
        host_layout = QHBoxLayout(self.kraken_host_row)
        host_layout.setContentsMargins(0, 0, 0, 0)
        self.kraken_host_input = QLineEdit("127.0.0.1")
        self.kraken_port_input = QSpinBox()
        self.kraken_port_input.setRange(1, 65535)
        self.kraken_port_input.setValue(8081)
        host_layout.addWidget(QLabel("Host:"))
        host_layout.addWidget(self.kraken_host_input)
        host_layout.addWidget(QLabel("Port:"))
        host_layout.addWidget(self.kraken_port_input)
        conn_layout.addWidget(self.kraken_host_row, 1, 0, 1, 2)
        self.kraken_host_row.hide()

        compass_cont_layout.addWidget(conn_group)

        # Antenna Array && Frequency Tuning
        array_group = QGroupBox("Antenna Array && Frequency Tuning")
        array_layout = QGridLayout(array_group)
        array_layout.setContentsMargins(6, 6, 6, 6)

        self.kraken_freq_spin = QDoubleSpinBox()
        self.kraken_freq_spin.setRange(24.0, 1766.0)
        self.kraken_freq_spin.setValue(915.000)
        self.kraken_freq_spin.setDecimals(3)
        self.kraken_freq_spin.setSuffix(" MHz")
        self.kraken_freq_spin.setSingleStep(0.5)
        self.kraken_freq_spin.setToolTip("KrakenSDR Hardware Limits: 24 MHz to 1766 MHz (R820T2 Tuners)")
        self.kraken_freq_spin.valueChanged.connect(self.on_kraken_freq_changed)
        array_layout.addWidget(QLabel("Target VFO:"), 0, 0)
        array_layout.addWidget(self.kraken_freq_spin, 0, 1)

        self.kraken_array_combo = QComboBox()
        self.kraken_array_combo.addItems([
            "5-Antenna Uniform Circular (UCA)",
            "5-Antenna Uniform Linear (ULA)",
            "Custom Array Geometry"
        ])
        self.kraken_array_combo.currentIndexChanged.connect(self.on_kraken_array_type_changed)
        array_layout.addWidget(QLabel("Array Geometry:"), 1, 0)
        array_layout.addWidget(self.kraken_array_combo, 1, 1)

        # Fine-Grained Array Radius / Inter-Element Spacing Input
        self.kraken_radius_spin = QDoubleSpinBox()
        self.kraken_radius_spin.setRange(0.010, 5.000)
        self.kraken_radius_spin.setValue(0.180)
        self.kraken_radius_spin.setDecimals(3)
        self.kraken_radius_spin.setSuffix(" m")
        self.kraken_radius_spin.setSingleStep(0.005)
        self.kraken_radius_spin.setToolTip("Array radius (UCA) or inter-element spacing (ULA) in meters. Configurable down to millimeter precision.")
        self.kraken_radius_spin.valueChanged.connect(self.push_kraken_hardware_settings)
        array_layout.addWidget(QLabel("Array Radius / Spacing:"), 2, 0)
        array_layout.addWidget(self.kraken_radius_spin, 2, 1)

        # Array Physics & Wavelength Info Badge
        self.kraken_phys_lbl = QLabel("λ/2: 16.4 cm | Recommended Radius: 0.180 m")
        self.kraken_phys_lbl.setStyleSheet("color: #38bdf8; font-size: 11px; font-weight: bold;")
        array_layout.addWidget(self.kraken_phys_lbl, 3, 0, 1, 2)

        # Gain Control
        self.kraken_gain_spin = QDoubleSpinBox()
        self.kraken_gain_spin.setRange(0.0, 49.6)
        self.kraken_gain_spin.setValue(30.0)
        self.kraken_gain_spin.setDecimals(1)
        self.kraken_gain_spin.setSuffix(" dB")
        self.kraken_gain_spin.setSingleStep(2.0)
        self.kraken_gain_spin.valueChanged.connect(self.push_kraken_hardware_settings)
        array_layout.addWidget(QLabel("Kraken Gain:"), 4, 0)
        array_layout.addWidget(self.kraken_gain_spin, 4, 1)

        # Quick Preset Buttons (Optimal Non-Ambiguous Radii)
        presets_layout = QHBoxLayout()
        btn_915 = QPushButton("915 MHz (0.135m)")
        btn_915.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-size: 11px; padding: 3px;")
        btn_915.clicked.connect(lambda: (self.kraken_freq_spin.setValue(915.000), self.kraken_array_combo.setCurrentIndex(0), self.kraken_radius_spin.setValue(0.135), self.push_kraken_hardware_settings()))
        btn_868 = QPushButton("868 MHz (0.140m)")
        btn_868.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-size: 11px; padding: 3px;")
        btn_868.clicked.connect(lambda: (self.kraken_freq_spin.setValue(868.000), self.kraken_array_combo.setCurrentIndex(0), self.kraken_radius_spin.setValue(0.140), self.push_kraken_hardware_settings()))
        btn_433 = QPushButton("433 MHz (0.285m)")
        btn_433.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-size: 11px; padding: 3px;")
        btn_433.clicked.connect(lambda: (self.kraken_freq_spin.setValue(433.920), self.kraken_array_combo.setCurrentIndex(0), self.kraken_radius_spin.setValue(0.285), self.push_kraken_hardware_settings()))
        btn_1280 = QPushButton("1280 MHz (0.095m)")
        btn_1280.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-size: 11px; padding: 3px;")
        btn_1280.clicked.connect(lambda: (self.kraken_freq_spin.setValue(1280.000), self.kraken_array_combo.setCurrentIndex(0), self.kraken_radius_spin.setValue(0.095), self.push_kraken_hardware_settings()))
        presets_layout.addWidget(btn_915)
        presets_layout.addWidget(btn_868)
        presets_layout.addWidget(btn_433)
        presets_layout.addWidget(btn_1280)
        array_layout.addLayout(presets_layout, 5, 0, 1, 2)

        # Apply Retune Button
        self.apply_kraken_btn = QPushButton("RETUNE && APPLY TO HARDWARE")
        self.apply_kraken_btn.setStyleSheet("background-color: #1e293b; color: #f59e0b; font-weight: bold; border: 1px solid #f59e0b; padding: 5px;")
        self.apply_kraken_btn.clicked.connect(self.push_kraken_hardware_settings)
        array_layout.addWidget(self.apply_kraken_btn, 6, 0, 1, 2)

        range_hint = QLabel("Kraken Hardware Range: 24 MHz - 1766 MHz (5.8 GHz VTX demodulated via HackRF Tab 5)")
        range_hint.setStyleSheet("color: #64748b; font-size: 10px; font-style: italic;")
        array_layout.addWidget(range_hint, 7, 0, 1, 2)

        compass_cont_layout.addWidget(array_group)

        # Tactical Map & Intel Actions
        actions_group = QGroupBox("Tactical Bearing Actions")
        actions_layout = QGridLayout(actions_group)
        actions_layout.setContentsMargins(6, 6, 6, 6)

        self.auto_cast_map_cb = QCheckBox("Real-Time Map Bearing Ray")
        self.auto_cast_map_cb.setChecked(True)
        actions_layout.addWidget(self.auto_cast_map_cb, 0, 0)

        self.auto_tag_intel_cb = QCheckBox("Auto-Tag Active Intel Emitter")
        self.auto_tag_intel_cb.setChecked(True)
        actions_layout.addWidget(self.auto_tag_intel_cb, 0, 1)

        self.cast_map_btn = QPushButton("PROJECT BEARING TO MAP")
        self.cast_map_btn.setStyleSheet("background-color: #0f172a; color: #38bdf8; font-weight: bold; border: 1px solid #38bdf8; padding: 5px;")
        self.cast_map_btn.clicked.connect(self.cast_bearing_to_map)
        actions_layout.addWidget(self.cast_map_btn, 1, 0)

        self.triangulate_btn = QPushButton("RECORD BEARING && FIX TARGET")
        self.triangulate_btn.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; border: 1px solid #38bdf8; padding: 5px;")
        self.triangulate_btn.clicked.connect(self.fix_triangulated_target)
        actions_layout.addWidget(self.triangulate_btn, 1, 1)

        compass_cont_layout.addWidget(actions_group)
        layout.addWidget(self.doa_compass_container)

        # Main Start / Stop Button
        self.toggle_kraken_btn = QPushButton("▶ START DIRECTION FINDING")
        self.toggle_kraken_btn.setStyleSheet("background-color: #10b981; color: white; font-weight: bold; padding: 8px; font-size: 13px;")
        self.toggle_kraken_btn.clicked.connect(self.toggle_kraken_doa)
        layout.addWidget(self.toggle_kraken_btn)

        layout.addStretch()
        return kraken_widget

    def create_hunter_killer_ui(self):
        hk_widget = QWidget()
        layout = QVBoxLayout(hk_widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # Container for detach/popout capability
        self.hk_container = QWidget()
        self.hk_parent_layout = layout
        hk_cont_layout = QVBoxLayout(self.hk_container)
        hk_cont_layout.setContentsMargins(0, 0, 0, 0)
        hk_cont_layout.setSpacing(6)

        # 1. Master Status Badge & Cycle Breadcrumbs
        status_group = QGroupBox("Autonomous Hunter-Killer Status")
        status_layout = QVBoxLayout(status_group)
        status_layout.setContentsMargins(6, 6, 6, 6)
        status_layout.setSpacing(4)

        self.hk_status_badge = QLabel("[ HUNTER-KILLER: STANDBY / INACTIVE ]")
        self.hk_status_badge.setStyleSheet("background-color: #060a14; color: #94a3b8; font-family: monospace; font-size: 12px; font-weight: bold; padding: 6px; border: 1px solid #1e293b; border-radius: 4px;")
        self.hk_status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_layout.addWidget(self.hk_status_badge)

        self.hk_cycle_breadcrumbs = QLabel("CYCLE: [ 1. HUNT ] ➔ [ 2. DETECT ] ➔ [ 3. STARE && FP ] ➔ [ 4. DoA VECTOR ] ➔ [ 5. THREAT EVAL ] ➔ [ 6. RESUME ]")
        self.hk_cycle_breadcrumbs.setStyleSheet("color: #64748b; font-family: monospace; font-size: 10px; font-weight: bold;")
        self.hk_cycle_breadcrumbs.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_layout.addWidget(self.hk_cycle_breadcrumbs)
        hk_cont_layout.addWidget(status_group)

        # 2. Mission Control & Configuration
        ctrl_group = QGroupBox("Autonomous Mission Rules && Parameters")
        ctrl_layout = QGridLayout(ctrl_group)
        ctrl_layout.setContentsMargins(6, 6, 6, 6)
        ctrl_layout.setSpacing(4)

        ctrl_layout.addWidget(QLabel("Operating Mode:"), 0, 0)
        self.hk_mode_combo = QComboBox()
        self.hk_mode_combo.addItems([
            "Full Autonomous (Hunt ➔ Stare ➔ DoA ➔ Map ➔ Resume)",
            "Semi-Autonomous (Detect && Queue Intercept Candidates)",
            "Target Intercept && Lock (Sustain Track on Target)"
        ])
        ctrl_layout.addWidget(self.hk_mode_combo, 0, 1, 1, 3)

        ctrl_layout.addWidget(QLabel("Tactical Search Band:"), 1, 0)
        self.hk_band_combo = QComboBox()
        self.hk_band_combo.addItems([
            "UAV Control Band (850.0 - 950.0 MHz)",
            "ISM / OcuSync 2.4G (2400.0 - 2485.0 MHz)",
            "FPV Video 5.8G (5640.0 - 5950.0 MHz)",
            "FPV Video 1.2G (1080.0 - 1360.0 MHz)",
            "Tactical VHF / UHF (136.0 - 470.0 MHz)",
            "Full Wideband (100.0 - 6000.0 MHz)",
            "Current Sweep Settings (from Top Toolbar)"
        ])
        ctrl_layout.addWidget(self.hk_band_combo, 1, 1, 1, 3)

        ctrl_layout.addWidget(QLabel("Stare Dwell Time:"), 2, 0)
        self.hk_dwell_spin = QSpinBox()
        self.hk_dwell_spin.setRange(200, 5000)
        self.hk_dwell_spin.setValue(1000)
        self.hk_dwell_spin.setSuffix(" ms")
        self.hk_dwell_spin.setToolTip("Duration the SDR dwells in Stare Mode for Kraken DoA calibration and CVA fingerprinting.")
        ctrl_layout.addWidget(self.hk_dwell_spin, 2, 1)

        ctrl_layout.addWidget(QLabel("SNR Threshold:"), 2, 2)
        self.hk_snr_spin = QSpinBox()
        self.hk_snr_spin.setRange(8, 50)
        self.hk_snr_spin.setValue(22)
        self.hk_snr_spin.setSuffix(" dB")
        self.hk_snr_spin.setToolTip("Required burst amplitude above dynamic noise floor to trigger Killer stare intercept.")
        ctrl_layout.addWidget(self.hk_snr_spin, 2, 3)

        # Automation Checkboxes
        self.hk_auto_kraken_cb = QCheckBox("Auto-Vector KrakenSDR Bearing (DoA)")
        self.hk_auto_kraken_cb.setChecked(True)
        self.hk_auto_kraken_cb.setToolTip("Automatically retune KrakenSDR and record Line of Bearing during stare intercept.")
        ctrl_layout.addWidget(self.hk_auto_kraken_cb, 3, 0, 1, 2)

        self.hk_auto_map_cb = QCheckBox("Auto-Plot Target && CEP Fix to Map")
        self.hk_auto_map_cb.setChecked(True)
        self.hk_auto_map_cb.setToolTip("Automatically plot intercept coordinates and CEP triangulation fix on Tactical Map.")
        ctrl_layout.addWidget(self.hk_auto_map_cb, 3, 2, 1, 2)

        self.hk_audio_alert_cb = QCheckBox("Audible Voice && Tactical Alert Cues")
        self.hk_audio_alert_cb.setChecked(True)
        self.hk_audio_alert_cb.setToolTip("Play voice synthesizer alert announcements when high-priority targets are intercepted.")
        ctrl_layout.addWidget(self.hk_audio_alert_cb, 4, 0, 1, 2)

        self.hk_popout_btn = QPushButton("DETACH H-K")
        self.hk_popout_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-weight: bold; border: 1px solid #38bdf8; padding: 4px;")
        self.hk_popout_btn.clicked.connect(self.detach_hk_window)
        ctrl_layout.addWidget(self.hk_popout_btn, 4, 2, 1, 2)

        hk_cont_layout.addWidget(ctrl_group)

        # 3. Master Engagement Button
        self.toggle_hk_btn = QPushButton("ENGAGE AUTONOMOUS HUNTER-KILLER")
        self.toggle_hk_btn.setStyleSheet("background-color: #10b981; color: white; font-weight: bold; padding: 10px; font-size: 13px; border-radius: 4px;")
        self.toggle_hk_btn.clicked.connect(self.toggle_hunter_killer)
        hk_cont_layout.addWidget(self.toggle_hk_btn)

        # 4. Live Threat Matrix & Target Priority Queue Table
        queue_group = QGroupBox("Target Priority Queue && Active Intercepts")
        queue_layout = QVBoxLayout(queue_group)
        queue_layout.setContentsMargins(4, 4, 4, 4)
        queue_layout.setSpacing(4)

        self.hk_queue_table = QTableWidget()
        self.hk_queue_table.setColumnCount(8)
        self.hk_queue_table.setHorizontalHeaderLabels([
            "PRIORITY", "FREQ (MHz)", "CLASSIFICATION", "THREAT", "FINGERPRINT", "BEARING", "RSSI", "ACTIONS"
        ])
        self.hk_queue_table.horizontalHeader().setStretchLastSection(True)
        self.hk_queue_table.setStyleSheet("QTableWidget { background-color: #030712; color: #f8fafc; gridline-color: #1e293b; font-family: monospace; font-size: 11px; } QHeaderView::section { background-color: #0f172a; color: #94a3b8; font-weight: bold; border: 1px solid #1e293b; padding: 3px; }")
        self.hk_queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.hk_queue_table.setMinimumHeight(160)
        queue_layout.addWidget(self.hk_queue_table)

        # Quick Queue Action Buttons
        queue_btn_layout = QHBoxLayout()
        self.hk_lock_sel_btn = QPushButton("LOCK SELECTED TARGET")
        self.hk_lock_sel_btn.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; padding: 5px;")
        self.hk_lock_sel_btn.clicked.connect(self.lock_selected_hk_target)

        self.hk_wipe_queue_btn = QPushButton("CLEAR QUEUE")
        self.hk_wipe_queue_btn.setStyleSheet("background-color: #ef4444; color: white; font-weight: bold; padding: 5px;")
        self.hk_wipe_queue_btn.clicked.connect(self.clear_hk_queue)

        self.hk_export_btn = QPushButton("EXPORT CSV")
        self.hk_export_btn.setStyleSheet("background-color: #334155; color: white; font-weight: bold; padding: 5px;")
        self.hk_export_btn.clicked.connect(self.export_hk_queue_csv)

        queue_btn_layout.addWidget(self.hk_lock_sel_btn)
        queue_btn_layout.addWidget(self.hk_wipe_queue_btn)
        queue_btn_layout.addWidget(self.hk_export_btn)
        queue_layout.addLayout(queue_btn_layout)

        hk_cont_layout.addWidget(queue_group)

        # 5. Live Intercept Telemetry Card
        telemetry_group = QGroupBox("Last Intercept Metrics && DoA Telemetry")
        telemetry_layout = QGridLayout(telemetry_group)
        telemetry_layout.setContentsMargins(6, 6, 6, 6)
        telemetry_layout.setSpacing(4)

        self.hk_metric_freq = QLabel("Frequency: -- MHz")
        self.hk_metric_freq.setStyleSheet("color: #38bdf8; font-family: monospace; font-size: 11px; font-weight: bold;")
        self.hk_metric_bw = QLabel("Bandwidth: -- kHz")
        self.hk_metric_bw.setStyleSheet("color: #94a3b8; font-family: monospace; font-size: 11px;")
        self.hk_metric_snr = QLabel("Peak SNR: -- dB")
        self.hk_metric_snr.setStyleSheet("color: #94a3b8; font-family: monospace; font-size: 11px;")
        self.hk_metric_pulse = QLabel("Pulse Width: -- ms")
        self.hk_metric_pulse.setStyleSheet("color: #94a3b8; font-family: monospace; font-size: 11px;")

        self.hk_metric_bearing = QLabel("DoA Bearing: --°")
        self.hk_metric_bearing.setStyleSheet("color: #10b981; font-family: monospace; font-size: 12px; font-weight: bold;")
        self.hk_metric_fingerprint = QLabel("Hardware CVA: 0x----")
        self.hk_metric_fingerprint.setStyleSheet("color: #f59e0b; font-family: monospace; font-size: 11px; font-weight: bold;")

        telemetry_layout.addWidget(self.hk_metric_freq, 0, 0)
        telemetry_layout.addWidget(self.hk_metric_bw, 0, 1)
        telemetry_layout.addWidget(self.hk_metric_snr, 1, 0)
        telemetry_layout.addWidget(self.hk_metric_pulse, 1, 1)
        telemetry_layout.addWidget(self.hk_metric_bearing, 2, 0)
        telemetry_layout.addWidget(self.hk_metric_fingerprint, 2, 1)

        hk_cont_layout.addWidget(telemetry_group)

        layout.addWidget(self.hk_container)
        return hk_widget

    def toggle_hunter_killer(self):
        if not getattr(self, 'hk_active', False):
            self.start_hunter_killer()
        else:
            self.stop_hunter_killer()

    def start_hunter_killer(self):
        self.hk_active = True
        self.hk_state = "HUNTING"
        self.toggle_hk_btn.setText("⏹ STOP HUNTER-KILLER")
        self.toggle_hk_btn.setStyleSheet("background-color: #ef4444; color: white; font-weight: bold; padding: 10px; font-size: 13px; border-radius: 4px;")
        
        band_idx = self.hk_band_combo.currentIndex()
        band_map = {
            0: (850.0, 950.0),
            1: (2400.0, 2485.0),
            2: (5640.0, 5950.0),
            3: (1080.0, 1360.0),
            4: (136.0, 470.0),
            5: (100.0, 6000.0)
        }
        if band_idx in band_map:
            start_mhz, end_mhz = band_map[band_idx]
            self.sweep_start_input.setValue(start_mhz)
            self.sweep_end_input.setValue(end_mhz)
            self.hk_resume_sweep_params = (start_mhz, end_mhz)
        else:
            self.hk_resume_sweep_params = (self.sweep_start_input.value(), self.sweep_end_input.value())

        self.hk_status_badge.setText(f"[ HUNTER-KILLER: HUNTING SWEEP {self.hk_resume_sweep_params[0]:.0f} - {self.hk_resume_sweep_params[1]:.0f} MHz ]")
        self.hk_status_badge.setStyleSheet("background-color: #060a14; color: #10b981; font-family: monospace; font-size: 12px; font-weight: bold; padding: 6px; border: 1px solid #10b981; border-radius: 4px;")
        self.hk_cycle_breadcrumbs.setText("CYCLE: ▶ [ 1. HUNT ] ➔ [ 2. DETECT ] ➔ [ 3. STARE && FP ] ➔ [ 4. DoA VECTOR ] ➔ [ 5. THREAT EVAL ] ➔ [ 6. RESUME ]")
        self.hk_cycle_breadcrumbs.setStyleSheet("color: #10b981; font-family: monospace; font-size: 10px; font-weight: bold;")
        
        if "SWEEP" not in self.mode_selector.currentText():
            self.mode_selector.setCurrentText("SWEEP MODE (Wideband)")
        self.start_sdr()
        self.log_event(f"AUTONOMOUS HUNTER-KILLER: Engaged in {self.hk_mode_combo.currentText()} ({self.hk_resume_sweep_params[0]:.1f} - {self.hk_resume_sweep_params[1]:.1f} MHz).")

    def stop_hunter_killer(self):
        self.hk_active = False
        self.hk_state = "IDLE"
        self.toggle_hk_btn.setText("ENGAGE AUTONOMOUS HUNTER-KILLER")
        self.toggle_hk_btn.setStyleSheet("background-color: #10b981; color: white; font-weight: bold; padding: 10px; font-size: 13px; border-radius: 4px;")
        self.hk_status_badge.setText("[ HUNTER-KILLER: STANDBY / INACTIVE ]")
        self.hk_status_badge.setStyleSheet("background-color: #060a14; color: #94a3b8; font-family: monospace; font-size: 12px; font-weight: bold; padding: 6px; border: 1px solid #1e293b; border-radius: 4px;")
        self.hk_cycle_breadcrumbs.setText("CYCLE: [ 1. HUNT ] ➔ [ 2. DETECT ] ➔ [ 3. STARE && FP ] ➔ [ 4. DoA VECTOR ] ➔ [ 5. THREAT EVAL ] ➔ [ 6. RESUME ]")
        self.hk_cycle_breadcrumbs.setStyleSheet("color: #64748b; font-family: monospace; font-size: 10px; font-weight: bold;")
        self.log_event("AUTONOMOUS HUNTER-KILLER: Disengaged.")

    def detach_hk_window(self):
        if not hasattr(self, 'popout_windows'):
            self.popout_windows = {}
        if "hk" in self.popout_windows and self.popout_windows["hk"].isVisible():
            self.popout_windows["hk"].raise_()
            self.popout_windows["hk"].activateWindow()
            return
        win = PopOutWindow("Autonomous Hunter-Killer Engine", self.hk_container, self.hk_parent_layout, 7, self)
        self.popout_windows["hk"] = win
        win.closed.connect(lambda: self.popout_windows.pop("hk", None))
        win.show()

    def calculate_hk_threat_score(self, freq_mhz, rssi, bw_khz, mod_type, is_armed=False):
        score = 20
        if 850 <= freq_mhz <= 950:
            score += 30
        elif 2400 <= freq_mhz <= 2485:
            score += 25
        elif (5640 <= freq_mhz <= 5950) or (1080 <= freq_mhz <= 1360):
            score += 30
        elif 136 <= freq_mhz <= 470:
            score += 15

        if rssi > -60:
            score += 25
        elif rssi > -75:
            score += 15
        elif rssi > -90:
            score += 5

        if "LoRa" in mod_type or "CSS" in mod_type or "ELRS" in mod_type:
            score += 25
        elif "MATCH" in mod_type or "Orlan" in mod_type:
            score += 30
        elif "Video" in mod_type or "PAL" in mod_type or "NTSC" in mod_type or "OFDM" in mod_type or "QAM" in mod_type:
            score += 25
        elif "Crossfire" in mod_type or "FSK" in mod_type:
            score += 20
        elif "Digital" in mod_type or "PSK" in mod_type:
            score += 15
        elif "CW" in mod_type or "Carrier" in mod_type:
            score += 15
        elif "FM" in mod_type:
            score += 10

        if is_armed:
            score += 15

        return min(100, max(10, score))

    def add_or_update_hk_fhss_cluster(self, band_key, f_min_mhz, f_max_mhz, hops_sec, protocol_name, rssi):
        if not hasattr(self, 'hk_priority_queue'):
            self.hk_priority_queue = {}
        if not hasattr(self, 'active_fhss_bands'):
            self.active_fhss_bands = {}

        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        self.active_fhss_bands[band_key] = {
            'f_min': f_min_mhz,
            'f_max': f_max_mhz,
            'last_seen': time.time(),
            'hops_sec': hops_sec
        }
        
        pilot_info = ""
        fingerprint = "0x----"
        if hasattr(self, 'discovered_pilots') and self.discovered_pilots:
            for uid_str, p in self.discovered_pilots.items():
                pilot_info = f" [Pilot {p['u3']}:{p['u4']}:{p['u5']}]"
                fingerprint = f"0x{p['crc_init']}"
                break
        elif hasattr(self, 'last_pilot_key') and self.last_pilot_key:
            pilot_info = f" [{self.last_pilot_key}]"

        center_freq = 915.0 if ("900" in protocol_name or "860" in band_key or "900M" in band_key) else (2440.0 if "2.4" in protocol_name else round((f_min_mhz + f_max_mhz) / 2.0, 1))
        bearing = getattr(self, 'last_bearing_deg', 0.0)

        self.hk_priority_queue[band_key] = {
            "priority": "P1 CRITICAL",
            "p_color": "#ef4444",
            "freq": center_freq,
            "freq_display": f"{center_freq:.1f} (FHSS {f_min_mhz:.0f}-{f_max_mhz:.0f})",
            "mod": f"{protocol_name} (~{hops_sec:.0f} h/s){pilot_info}",
            "score": 85,
            "fingerprint": fingerprint,
            "bearing": bearing,
            "rssi": rssi,
            "last_seen": now_str,
            "timestamp": time.time(),
            "is_fhss": True
        }

        # RETROACTIVE CLEANUP: Purge any early transient discrete entries that fall within the FHSS bandwidth limits
        stale_keys = []
        for k, entry in self.hk_priority_queue.items():
            if not entry.get("is_fhss", False):
                t_freq = entry.get("freq", 0.0)
                if (f_min_mhz - 3.0 <= t_freq <= f_max_mhz + 3.0):
                    stale_keys.append(k)
        if stale_keys:
            for k in stale_keys:
                self.hk_priority_queue.pop(k, None)

        # RETROACTIVE INTEL & ANOMALY CLEANUP: Consolidate any transient fingerprints within FHSS limits
        if hasattr(self, 'fingerprint_db') and self.fingerprint_db:
            for fp, fp_data in self.fingerprint_db.items():
                f_found = fp_data.get("found_at", 0.0)
                if (f_min_mhz - 3.0 <= f_found <= f_max_mhz + 3.0):
                    if "FHSS" not in fp_data.get("classification", ""):
                        fp_data["classification"] = f"FHSS Emitter Node ({protocol_name})"
                        fp_data["name"] = f"FHSS Node 0x{fp}"
            self.refresh_fingerprint_ui()

        if hasattr(self, 'active_events') and self.active_events:
            self.active_events = {f_mhz: t for f_mhz, t in self.active_events.items() if not (f_min_mhz - 3.0 <= (f_mhz if isinstance(f_mhz, (int, float)) and f_mhz > 50 else self.bin_to_freq(f_mhz)/1e6) <= f_max_mhz + 3.0)}

        self.refresh_hk_queue_ui()

    def add_or_update_hk_queue(self, freq_mhz, mod_type, score, fingerprint, bearing, rssi):
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        
        # Deduplication 1: Check active_fhss_bands dictionary
        for b_key, b_info in getattr(self, 'active_fhss_bands', {}).items():
            if (time.time() - b_info.get('last_seen', 0) < 6.0) and (b_info['f_min'] - 3.0 <= freq_mhz <= b_info['f_max'] + 3.0):
                if hasattr(self, 'hk_priority_queue') and b_key in self.hk_priority_queue:
                    self.hk_priority_queue[b_key]["last_seen"] = now_str
                    self.hk_priority_queue[b_key]["rssi"] = rssi
                    self.hk_priority_queue[b_key]["timestamp"] = time.time()
                    self.refresh_hk_queue_ui()
                return

        # Deduplication 2: Check known standard drone FHSS ranges (860-930 MHz, 2400-2485 MHz)
        if 860.0 <= freq_mhz <= 930.0:
            self.add_or_update_hk_fhss_cluster("FHSS_900M", 860.0, 930.0, 50.0, "TBS Crossfire / ELRS 900M", rssi)
            return
        elif 2400.0 <= freq_mhz <= 2485.0:
            self.add_or_update_hk_fhss_cluster("FHSS_2.4G", 2400.0, 2485.0, 50.0, "DJI OcuSync / ELRS 2.4G", rssi)
            return

        key = f"{freq_mhz:.3f}"
        priority = "P3 ADVISORY"
        p_color = "#38bdf8"
        if score >= 70:
            priority = "P1 CRITICAL"
            p_color = "#ef4444"
        elif score >= 45:
            priority = "P2 HIGH"
            p_color = "#f59e0b"

        if not hasattr(self, 'hk_priority_queue'):
            self.hk_priority_queue = {}

        self.hk_priority_queue[key] = {
            "priority": priority,
            "p_color": p_color,
            "freq": freq_mhz,
            "freq_display": f"{freq_mhz:.3f}",
            "mod": mod_type,
            "score": score,
            "fingerprint": fingerprint or "0x----",
            "bearing": bearing,
            "rssi": rssi,
            "last_seen": now_str,
            "timestamp": time.time(),
            "is_fhss": False
        }
        self.refresh_hk_queue_ui()

    def refresh_hk_queue_ui(self):
        if not hasattr(self, 'hk_queue_table') or not hasattr(self, 'hk_priority_queue'):
            return
        
        # Sort targets by threat score descending
        sorted_targets = sorted(self.hk_priority_queue.values(), key=lambda x: x["score"], reverse=True)
        self.hk_queue_table.setRowCount(len(sorted_targets))

        for row, t in enumerate(sorted_targets):
            p_item = QTableWidgetItem(t["priority"])
            p_item.setForeground(QColor(t["p_color"]))
            p_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.hk_queue_table.setItem(row, 0, p_item)

            f_text = t.get("freq_display", f"{t['freq']:.3f}")
            f_item = QTableWidgetItem(f_text)
            f_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.hk_queue_table.setItem(row, 1, f_item)

            m_item = QTableWidgetItem(t["mod"])
            self.hk_queue_table.setItem(row, 2, m_item)

            s_item = QTableWidgetItem(f"{t['score']}/100")
            s_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            s_item.setForeground(QColor(t["p_color"]))
            self.hk_queue_table.setItem(row, 3, s_item)

            fp_item = QTableWidgetItem(t["fingerprint"])
            fp_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            fp_item.setForeground(QColor("#f59e0b"))
            self.hk_queue_table.setItem(row, 4, fp_item)

            b_item = QTableWidgetItem(f"{t['bearing']:.1f}°")
            b_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            b_item.setForeground(QColor("#10b981"))
            self.hk_queue_table.setItem(row, 5, b_item)

            r_item = QTableWidgetItem(f"{t['rssi']:.0f} dBFS")
            r_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.hk_queue_table.setItem(row, 6, r_item)

            action_btn = QPushButton("LOCK")
            action_btn.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; padding: 2px 6px;")
            target_freq = t['freq']
            action_btn.clicked.connect(lambda _, f=target_freq: self.lock_hk_target(f))
            self.hk_queue_table.setCellWidget(row, 7, action_btn)

    def lock_selected_hk_target(self):
        if not hasattr(self, 'hk_queue_table'):
            return
        row = self.hk_queue_table.currentRow()
        if row < 0:
            return
        freq_item = self.hk_queue_table.item(row, 1)
        if freq_item:
            try:
                freq = float(freq_item.text())
                self.lock_hk_target(freq)
            except Exception:
                pass

    def lock_hk_target(self, freq_mhz):
        self.log_event(f"HUNTER-KILLER: Locking SDR into continuous stare on target {freq_mhz:.3f} MHz.")
        self.hk_state = "TRACK_LOCKED"
        self.hk_target_freq = freq_mhz
        if hasattr(self, 'hk_status_badge'):
            self.hk_status_badge.setText(f"[ TRACK LOCKED: CONTINUOUS INTERCEPT @ {freq_mhz:.3f} MHz ]")
            self.hk_status_badge.setStyleSheet("background-color: #060a14; color: #ef4444; font-family: monospace; font-size: 12px; font-weight: bold; padding: 6px; border: 1px solid #ef4444; border-radius: 4px;")
        
        self.mode_selector.setCurrentText("STARE MODE (2MHz)")
        self.freq_input.setValue(freq_mhz)
        self.start_sdr()

    def clear_hk_queue(self):
        if hasattr(self, 'hk_priority_queue'):
            self.hk_priority_queue.clear()
            self.refresh_hk_queue_ui()
            self.log_event("HUNTER-KILLER: Target Priority Queue cleared.")

    def export_hk_queue_csv(self):
        if not hasattr(self, 'hk_priority_queue') or not self.hk_priority_queue:
            self.log_event("HUNTER-KILLER: Queue is empty. Nothing to export.")
            return
        filename = f"hk_intercepts_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write("Priority,Frequency_MHz,Classification,Threat_Score,Fingerprint,Bearing_Deg,RSSI_dBFS,Last_Seen\n")
                for t in self.hk_priority_queue.values():
                    f.write(f"{t['priority']},{t['freq']:.3f},{t['mod']},{t['score']},{t['fingerprint']},{t['bearing']:.1f},{t['rssi']:.1f},{t['last_seen']}\n")
            self.log_event(f"HUNTER-KILLER: Exported intercept queue to {filename}")
        except Exception as e:
            self.log_event(f"HUNTER-KILLER export error: {e}")

    def create_tactical_copilot_ui(self):
        copilot_widget = QWidget()
        layout = QVBoxLayout(copilot_widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # 1. Master Status Badge
        status_group = QGroupBox("Tactical AI Intelligence Copilot")
        status_layout = QVBoxLayout(status_group)
        status_layout.setContentsMargins(6, 6, 6, 6)
        status_layout.setSpacing(4)

        self.copilot_status_badge = QLabel("[ AI COPILOT: ON-DEVICE EW INTELLIGENCE ENGINE (READY) ]")
        self.copilot_status_badge.setStyleSheet("background-color: #060a14; color: #38bdf8; font-family: monospace; font-size: 11px; font-weight: bold; padding: 6px; border: 1px solid #0284c7; border-radius: 4px;")
        self.copilot_status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_layout.addWidget(self.copilot_status_badge)
        layout.addWidget(status_group)

        # 2. Action Control Panel
        actions_group = QGroupBox("Automated Intelligence Actions")
        actions_layout = QGridLayout(actions_group)
        actions_layout.setContentsMargins(6, 6, 6, 6)
        actions_layout.setSpacing(6)

        self.copilot_sitrep_btn = QPushButton("GENERATE NATO SITREP / INTSUM")
        self.copilot_sitrep_btn.setStyleSheet("background-color: #10b981; color: white; font-weight: bold; padding: 8px; font-size: 11pt;")
        self.copilot_sitrep_btn.clicked.connect(self.generate_copilot_sitrep)
        actions_layout.addWidget(self.copilot_sitrep_btn, 0, 0, 1, 2)

        self.copilot_threats_btn = QPushButton("THREAT SUMMARY")
        self.copilot_threats_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-weight: bold; padding: 6px;")
        self.copilot_threats_btn.clicked.connect(lambda: self.copilot_quick_query("Summarize all active threats and priorities"))
        actions_layout.addWidget(self.copilot_threats_btn, 1, 0)

        self.copilot_pilots_btn = QPushButton("PILOT INTEL")
        self.copilot_pilots_btn.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-weight: bold; padding: 6px;")
        self.copilot_pilots_btn.clicked.connect(lambda: self.copilot_quick_query("Summarize decoded pilots and armed telemetry"))
        actions_layout.addWidget(self.copilot_pilots_btn, 1, 1)

        self.copilot_ecm_btn = QPushButton("ECM ADVISORY")
        self.copilot_ecm_btn.setStyleSheet("background-color: #1e293b; color: #f59e0b; font-weight: bold; padding: 6px;")
        self.copilot_ecm_btn.clicked.connect(lambda: self.copilot_quick_query("Recommend electronic countermeasures and jamming vectors"))
        actions_layout.addWidget(self.copilot_ecm_btn, 2, 0)

        self.copilot_export_btn = QPushButton("EXPORT REPORT")
        self.copilot_export_btn.setStyleSheet("background-color: #1e293b; color: #e2e8f0; font-weight: bold; padding: 6px;")
        self.copilot_export_btn.clicked.connect(self.export_copilot_sitrep)
        actions_layout.addWidget(self.copilot_export_btn, 2, 1)

        layout.addWidget(actions_group)

        # 3. Monospace Intelligence Report / Output Viewer
        output_group = QGroupBox("Tactical Report && Intelligence Output")
        output_layout = QVBoxLayout(output_group)
        output_layout.setContentsMargins(6, 6, 6, 6)
        output_layout.setSpacing(4)

        self.copilot_output = QTextEdit()
        self.copilot_output.setReadOnly(True)
        self.copilot_output.setStyleSheet("background-color: #060a14; color: #38bdf8; font-family: 'Consolas', monospace; font-size: 9.5pt; border: 1px solid #1e293b; border-radius: 4px; padding: 6px;")
        self.copilot_output.setMinimumHeight(240)
        output_layout.addWidget(self.copilot_output)
        layout.addWidget(output_group)

        # 4. Interactive Natural Language Operator Query Console
        query_group = QGroupBox("Operator Natural Language Query Console")
        query_layout = QHBoxLayout(query_group)
        query_layout.setContentsMargins(6, 6, 6, 6)
        query_layout.setSpacing(6)

        self.copilot_query_input = QLineEdit()
        self.copilot_query_input.setPlaceholderText("Ask Copilot (e.g., 'What P1 threats are active?', 'Recommend ECM vector')...")
        self.copilot_query_input.returnPressed.connect(self.on_copilot_query_submitted)
        query_layout.addWidget(self.copilot_query_input)

        self.copilot_send_btn = QPushButton("SEND QUERY")
        self.copilot_send_btn.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; padding: 6px 12px;")
        self.copilot_send_btn.clicked.connect(self.on_copilot_query_submitted)
        query_layout.addWidget(self.copilot_send_btn)

        layout.addWidget(query_group)

        self.copilot_output.setText("Tactical AI Intelligence Copilot Initialized.\nClick 'GENERATE NATO SITREP / INTSUM' or submit an operator query.")

        return copilot_widget

    def _build_copilot_context(self):
        lat = 51.5074
        lon = -0.1278
        if hasattr(self, 'geo_lat_input') and self.geo_lat_input.text():
            try: lat = float(self.geo_lat_input.text())
            except ValueError: pass
        if hasattr(self, 'geo_lon_input') and self.geo_lon_input.text():
            try: lon = float(self.geo_lon_input.text())
            except ValueError: pass

        return {
            "station_callsign": "CEMA-STATION-ALPHA",
            "station_loc": (lat, lon),
            "hk_queue": getattr(self, 'hk_priority_queue', {}),
            "pilots": getattr(self, 'discovered_pilots', {}),
            "last_bearing": getattr(self, 'last_bearing_deg', 0.0),
            "bearing_history": getattr(self, 'bearing_history', []),
            "cep_fix": getattr(self, 'last_triangulation_fix', None),
            "viewshed_data": getattr(self, 'last_viewshed_data', None)
        }

    def generate_copilot_sitrep(self):
        from tactical_copilot import get_tactical_copilot
        copilot = get_tactical_copilot()
        ctx = self._build_copilot_context()
        report = copilot.generate_nato_sitrep(ctx)
        self.copilot_output.setText(report)
        self.log_event("TACTICAL AI COPILOT: Generated automated NATO INTSUM / SITREP.")

    def copilot_quick_query(self, prompt):
        from tactical_copilot import get_tactical_copilot
        copilot = get_tactical_copilot()
        ctx = self._build_copilot_context()
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.copilot_output.setText(f"[{timestamp}] OPERATOR QUERY: {prompt}\n" + "="*50 + "\n\n")
        
        def _stream_cb(chunk):
            self.copilot_stream_chunk_signal.emit(chunk)

        def _worker():
            resp = copilot.answer_operator_query(prompt, ctx, stream_callback=_stream_cb)
            self.copilot_response_signal.emit(prompt, resp)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_copilot_stream_chunk(self, chunk):
        self.copilot_output.insertPlainText(chunk)
        cursor = self.copilot_output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.copilot_output.setTextCursor(cursor)

    def _on_copilot_response_received(self, prompt, resp):
        from tactical_copilot import get_tactical_copilot
        copilot = get_tactical_copilot()
        if hasattr(self, 'copilot_status_badge'):
            if copilot.is_slm_ready():
                self.copilot_status_badge.setText(f"[ AI COPILOT: {copilot.slm_status} | VRAM: 2.9GB ]")
                self.copilot_status_badge.setStyleSheet("background-color: #060a14; color: #10b981; font-family: monospace; font-size: 11px; font-weight: bold; padding: 6px; border: 1px solid #10b981; border-radius: 4px;")

    def on_copilot_query_submitted(self):
        query = self.copilot_query_input.text().strip()
        if not query:
            return
        self.copilot_query_input.clear()
        self.copilot_quick_query(query)

    def export_copilot_sitrep(self):
        content = self.copilot_output.toPlainText()
        if not content:
            self.log_event("COPILOT: No report content to export.")
            return
        filename = f"INTSUM_SITREP_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(content)
            self.log_event(f"COPILOT: Exported intelligence report to {filename}")
        except Exception as e:
            self.log_event(f"COPILOT: Export error: {e}")

    def on_kraken_array_type_changed(self, idx):
        freq = self.kraken_freq_spin.value()
        wavelength = 299.792458 / freq if freq > 0 else 0.327
        if idx == 0:
            # UCA: Max unambiguous radius is 0.4253 * wavelength, recommend ~0.41 * wavelength
            rec_radius = round(0.412 * wavelength, 3)
            self.kraken_radius_spin.setValue(rec_radius)
        elif idx == 1:
            # ULA: Max unambiguous element spacing is 0.5 * wavelength, recommend ~0.48 * wavelength
            rec_spacing = round(0.480 * (wavelength / 2.0), 3)
            self.kraken_radius_spin.setValue(rec_spacing)
        self.update_kraken_physics_hint()
        self.push_kraken_hardware_settings()

    def update_kraken_physics_hint(self):
        if not hasattr(self, 'kraken_phys_lbl') or not hasattr(self, 'kraken_freq_spin'):
            return
        freq = self.kraken_freq_spin.value()
        if freq <= 0: return
        wavelength = 299.792458 / freq # in meters
        radius = self.kraken_radius_spin.value() if hasattr(self, 'kraken_radius_spin') else 0.180
        array_idx = self.kraken_array_combo.currentIndex() if hasattr(self, 'kraken_array_combo') else 0
        
        # Kraken's exact physical chord length formula (5-element array):
        # chord = sqrt(2) * radius * sqrt(1 - cos(72°)) = 1.1755705 * radius
        if array_idx == 0: # UCA
            chord_spacing = 1.1755705 * radius
            max_unambig_radius = 0.425325 * wavelength
            max_phase_diff = chord_spacing / wavelength
            phase_diff_deg = max_phase_diff * 360.0
            
            if max_phase_diff > 0.5:
                self.kraken_phys_lbl.setText(f"[ WARNING: AMBIGUOUS (Phase Diff {phase_diff_deg:.1f} deg > 180 deg) ] Set Radius <= {max_unambig_radius:.3f}m")
                self.kraken_phys_lbl.setStyleSheet("color: #ef4444; font-size: 11px; font-weight: bold;")
            elif max_phase_diff < 0.1:
                self.kraken_phys_lbl.setText(f"[ WARNING: ARRAY TOO SMALL (Phase Diff {phase_diff_deg:.1f} deg < 36 deg) ] Recommended: {max_unambig_radius*0.95:.3f}m")
                self.kraken_phys_lbl.setStyleSheet("color: #f59e0b; font-size: 11px; font-weight: bold;")
            else:
                self.kraken_phys_lbl.setText(f"[ OPTIMAL UCA: Phase Diff {phase_diff_deg:.1f} deg (<= 180 deg) ] Max Limit: {max_unambig_radius:.3f}m")
                self.kraken_phys_lbl.setStyleSheet("color: #10b981; font-size: 11px; font-weight: bold;")
        else: # ULA
            max_unambig_spacing = 0.5 * wavelength
            max_phase_diff = radius / wavelength
            phase_diff_deg = max_phase_diff * 360.0
            if max_phase_diff > 0.5:
                self.kraken_phys_lbl.setText(f"[ WARNING: AMBIGUOUS (Spacing {radius:.3f}m > Half-Wave {max_unambig_spacing:.3f}m) ] Phase: {phase_diff_deg:.1f} deg")
                self.kraken_phys_lbl.setStyleSheet("color: #ef4444; font-size: 11px; font-weight: bold;")
            else:
                self.kraken_phys_lbl.setText(f"[ OPTIMAL ULA: Spacing {radius:.3f}m (<= {max_unambig_spacing:.3f}m) ] Phase: {phase_diff_deg:.1f} deg")
                self.kraken_phys_lbl.setStyleSheet("color: #10b981; font-size: 11px; font-weight: bold;")

    def _get_usbipd_path(self):
        candidates = [
            r"C:\Program Files\usbipd-win\usbipd.exe",
            r"C:\Program Files\usbipd\usbipd.exe",
            shutil.which("usbipd") if hasattr(shutil, 'which') else None
        ]
        for p in candidates:
            if p and os.path.exists(p):
                return p
        return "usbipd"

    def ensure_wsl_running(self):
        try:
            res = subprocess.run(["wsl.exe", "-l", "-v"], capture_output=True, text=True, timeout=3)
            if "Ubuntu" in res.stdout and "Running" in res.stdout:
                return True
        except Exception:
            pass
        
        # Start persistent background keepalive
        try:
            if not hasattr(self, '_wsl_keepalive') or self._wsl_keepalive is None or self._wsl_keepalive.poll() is not None:
                self._wsl_keepalive = subprocess.Popen(
                    ["wsl.exe", "-d", "Ubuntu", "-e", "sleep", "infinity"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                )
                time.sleep(1.0)
            return True
        except Exception as e:
            print(f"[WSL KEEPALIVE ERROR] {e}")
            return False

    def start_kraken_service(self):
        self.log_event("KRAKENSDR SERVICE: Initiating startup in WSL2 Ubuntu...")
        if hasattr(self, 'kraken_health_badge'):
            self.kraken_health_badge.setText("[ SERVER: STARTING DAEMON IN WSL... ]")
            self.kraken_health_badge.setStyleSheet("background-color: #060a14; color: #fbbf24; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #d97706; border-radius: 4px;")
        
        def _run_start():
            try:
                self.ensure_wsl_running()
                # Ensure tuners are attached before starting DAQ
                usbipd = self._get_usbipd_path()
                out = subprocess.run([usbipd, "list"], capture_output=True, text=True, timeout=5)
                for line in out.stdout.splitlines():
                    m = re.search(r'^\s*([0-9]+-[0-9]+)\s+0bda:2838', line)
                    if m and "Attached" not in line:
                        subprocess.run([usbipd, "bind", "--force", "--busid", m.group(1)], capture_output=True, timeout=3)
                        subprocess.run([usbipd, "attach", "--wsl", "Ubuntu", "--busid", m.group(1)], capture_output=True, timeout=4)
                
                subprocess.Popen(
                    ["wsl.exe", "-d", "Ubuntu", "-e", "bash", "-c", "cd /home/feeka/krakensdr_doa && ./kraken_doa_start.sh"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                )
            except Exception as e:
                print(f"[KRAKEN START ERROR] {e}")

        threading.Thread(target=_run_start, daemon=True).start()
        QTimer.singleShot(4000, self.check_kraken_health)

    def stop_kraken_service(self):
        self.log_event("KRAKENSDR SERVICE: Stopping DAQ && GUI processes...")
        if hasattr(self, 'kraken_health_badge'):
            self.kraken_health_badge.setText("[ SERVER: STOPPING PROCESSES... ]")
            self.kraken_health_badge.setStyleSheet("background-color: #060a14; color: #f87171; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #dc2626; border-radius: 4px;")
        
        def _run_stop():
            try:
                subprocess.run(
                    ["wsl.exe", "-d", "Ubuntu", "-e", "bash", "-c", "cd /home/feeka/krakensdr_doa && ./kraken_doa_stop.sh; sudo pkill -9 -f 'daq|krakensdr|heimdall' 2>/dev/null"],
                    capture_output=True,
                    timeout=5
                )
            except Exception as e:
                print(f"[KRAKEN STOP ERROR] {e}")

        threading.Thread(target=_run_stop, daemon=True).start()
        QTimer.singleShot(2000, self.check_kraken_health)

    def restart_kraken_service(self):
        self.log_event("KRAKENSDR SERVICE: Restarting DAQ && GUI processes...")
        self.stop_kraken_service()
        QTimer.singleShot(2500, self.start_kraken_service)

    def check_kraken_health(self):
        def _worker():
            host = self.settings.get("kraken_host", "127.0.0.1")
            api_port = int(self.settings.get("kraken_api_port", 8080))
            doa_port = int(self.settings.get("kraken_doa_port", 8081))
            
            api_ok = False
            doa_ok = False
            latency = 0.0
            tuners_found = 0
            proc_running = False

            # Ensure WSL is running
            wsl_up = self.ensure_wsl_running()

            # 1. Test HTTP API (Port 8080)
            try:
                t0 = time.time()
                with urllib.request.urlopen(f"http://{host}:{api_port}/", timeout=1.2) as resp:
                    if resp.status in [200, 302, 301]:
                        api_ok = True
                        latency = (time.time() - t0) * 1000.0
            except Exception:
                pass

            # 2. Test Fast DoA Stream (Port 8081)
            try:
                with urllib.request.urlopen(f"http://{host}:{doa_port}/DOA_value.html", timeout=1.2) as resp:
                    if resp.status == 200:
                        doa_ok = True
            except Exception:
                pass

            # 3. Check WSL Process & USB tuners
            if wsl_up:
                try:
                    out = subprocess.run(
                        ["wsl.exe", "-d", "Ubuntu", "-e", "/bin/bash", "-c", "ps -ef | grep -E '(daq|kraken|heimdall)' | grep -v grep | wc -l; lsusb | grep -c 0bda:2838"],
                        capture_output=True,
                        text=True,
                        timeout=3
                    )
                    if out.returncode == 0:
                        lines = out.stdout.strip().splitlines()
                        if len(lines) >= 1:
                            proc_running = (int(lines[0].strip()) > 0)
                        if len(lines) >= 2:
                            tuners_found = int(lines[1].strip())
                except Exception:
                    pass

            # Determine health state
            if doa_ok or (api_ok and tuners_found == 5):
                msg = f"[ KRAKENSDR ONLINE | Web: {api_port} | DoA Stream: {doa_port} | Ping: {latency:.0f}ms | Tuners: {tuners_found}/5 ]"
                color = "#10b981"
                border = "#10b981"
            elif tuners_found == 5 and proc_running:
                msg = f"[ KRAKENSDR INITIALIZING | Daq Running | Tuners: 5/5 | Web GUI Starting... ]"
                color = "#fbbf24"
                border = "#d97706"
            elif tuners_found > 0 and tuners_found < 5:
                msg = f"[ KRAKENSDR DEGRADED | Tuners: {tuners_found}/5 Attached | Click Auto-Repair ]"
                color = "#f59e0b"
                border = "#d97706"
            elif tuners_found == 0:
                msg = f"[ KRAKENSDR OFFLINE | USB Tuners Detached (0/5) | Click Auto-Repair ]"
                color = "#f87171"
                border = "#dc2626"
            else:
                msg = f"[ KRAKENSDR OFFLINE / STOPPED | Host: {host} | Port {api_port} Unreachable ]"
                color = "#f87171"
                border = "#dc2626"
            
            self.kraken_health_signal.emit(msg, color, border)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_kraken_health_updated(self, msg, color, border):
        if hasattr(self, 'kraken_health_badge'):
            self.kraken_health_badge.setText(msg)
            self.kraken_health_badge.setStyleSheet(f"background-color: #060a14; color: {color}; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid {border}; border-radius: 4px;")

    def attach_kraken_usb(self):
        self.log_event("KRAKENSDR USB: Scanning and attaching RTL-SDR tuners to WSL2...")
        if hasattr(self, 'kraken_health_badge'):
            self.kraken_health_badge.setText("[ USB: ATTACHING TUNERS TO WSL... ]")
            self.kraken_health_badge.setStyleSheet("background-color: #060a14; color: #fbbf24; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #d97706; border-radius: 4px;")

        def _worker():
            count = 0
            try:
                self.ensure_wsl_running()
                usbipd = self._get_usbipd_path()
                out = subprocess.run([usbipd, "list"], capture_output=True, text=True, timeout=6)
                buses = []
                for line in out.stdout.splitlines():
                    m = re.search(r'^\s*([0-9]+-[0-9]+)\s+0bda:2838', line)
                    if m:
                        buses.append(m.group(1))
                
                for bus in buses:
                    subprocess.run([usbipd, "bind", "--force", "--busid", bus], capture_output=True, timeout=3)
                    res = subprocess.run([usbipd, "attach", "--wsl", "Ubuntu", "--busid", bus], capture_output=True, text=True, timeout=5)
                    if res.returncode == 0:
                        count += 1
            except Exception as e:
                print(f"[USB ATTACH ERROR] {e}")

            def _done():
                self.log_event(f"KrakenSDR USB: Successfully attached {count} RTL-SDR tuner(s) to Ubuntu WSL2.")
                self.check_kraken_health()

            QTimer.singleShot(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def repair_kraken_sdr(self):
        self.log_event("KRAKENSDR AUTO-REPAIR: Initiating full diagnosis and self-healing sequence...")
        if hasattr(self, 'kraken_health_badge'):
            self.kraken_health_badge.setText("[ AUTO-REPAIR: DIAGNOSING && HEALING... ]")
            self.kraken_health_badge.setStyleSheet("background-color: #060a14; color: #38bdf8; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #0284c7; border-radius: 4px;")

        def _heal_worker():
            log_steps = []
            
            # Step 1: Ensure Ubuntu WSL is running with persistent keepalive
            try:
                self.ensure_wsl_running()
                log_steps.append("1. Verified Ubuntu WSL2 runtime environment active.")
            except Exception as e:
                log_steps.append(f"1. WSL Boot error: {e}")

            # Step 2: Unload interfering DVB kernel drivers and clear stale shared memory locks
            try:
                subprocess.run(
                    ["wsl.exe", "-d", "Ubuntu", "-e", "bash", "-c", "sudo rm -f /dev/shm/sem.* /dev/shm/*kraken* /dev/shm/*daq* /dev/shm/*heimdall* 2>/dev/null; sudo rmmod dvb_usb_rtl28xxu rtl2832 rtl2830 2>/dev/null"],
                    capture_output=True,
                    timeout=5
                )
                log_steps.append("2. Cleared stale shared memory /dev/shm/* && unloaded conflicting DVB drivers.")
            except Exception as e:
                log_steps.append(f"2. Shared memory cleanup error: {e}")

            # Step 3: Hard kill zombie DAQ & GUI processes
            try:
                subprocess.run(
                    ["wsl.exe", "-d", "Ubuntu", "-e", "bash", "-c", "cd /home/feeka/krakensdr_doa && ./kraken_doa_stop.sh; sudo pkill -9 -f 'daq|krakensdr|heimdall' 2>/dev/null"],
                    capture_output=True,
                    timeout=5
                )
                log_steps.append("3. Terminated old/zombie DAQ and GUI processes.")
            except Exception as e:
                log_steps.append(f"3. Process stop error: {e}")

            # Step 4: Scan and re-attach all 5 RTL-SDR tuners via usbipd
            try:
                usbipd = self._get_usbipd_path()
                out = subprocess.run([usbipd, "list"], capture_output=True, text=True, timeout=5)
                attached_count = 0
                for line in out.stdout.splitlines():
                    m = re.search(r'^\s*([0-9]+-[0-9]+)\s+0bda:2838', line)
                    if m:
                        subprocess.run([usbipd, "bind", "--force", "--busid", m.group(1)], capture_output=True, timeout=3)
                        r = subprocess.run([usbipd, "attach", "--wsl", "Ubuntu", "--busid", m.group(1)], capture_output=True, timeout=5)
                        if r.returncode == 0:
                            attached_count += 1
                log_steps.append(f"4. Attached {attached_count}/5 RTL-SDR tuners to Ubuntu WSL2.")
            except Exception as e:
                log_steps.append(f"4. USB scan error: {e}")

            # Step 5: Validate and repair settings.json
            wsl_settings = r"\\wsl.localhost\Ubuntu\home\feeka\krakensdr_doa\krakensdr_doa\_share\settings.json"
            if os.path.exists(wsl_settings):
                try:
                    with open(wsl_settings, 'r', encoding='utf-8') as f:
                        cfg = json.load(f)
                except Exception:
                    cfg = {}
                
                freq = self.kraken_freq_spin.value() if hasattr(self, 'kraken_freq_spin') else 915.0
                arr_r = self.kraken_radius_spin.value() if hasattr(self, 'kraken_radius_spin') else 0.135
                cfg["center_freq"] = float(freq)
                cfg["vfo_freq_0"] = float(freq * 1e6)
                cfg["uniform_gain"] = 30.0
                cfg["en_doa"] = True
                cfg["ant_arrangement"] = "UCA"
                cfg["ant_spacing_meters"] = float(arr_r)
                cfg["ext_upd_flag"] = True
                try:
                    with open(wsl_settings, 'w', encoding='utf-8') as f:
                        json.dump(cfg, f, indent=2)
                    log_steps.append("5. Repaired settings.json (VFO && Array geometry sanitized).")
                except Exception as e:
                    log_steps.append(f"5. Settings write error: {e}")

            time.sleep(1.0)

            # Step 6: Clean start DAQ and Web GUI
            try:
                subprocess.Popen(
                    ["wsl.exe", "-d", "Ubuntu", "-e", "bash", "-c", "cd /home/feeka/krakensdr_doa && ./kraken_doa_start.sh"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                )
                log_steps.append("6. KrakenSDR DAQ && Web GUI restarted cleanly.")
            except Exception as e:
                log_steps.append(f"6. Start error: {e}")

            def _finish():
                for s in log_steps:
                    self.log_event(f"REPAIR: {s}")
                self.check_kraken_health()
                # Restart DoA stream thread to immediately acquire live stream
                if hasattr(self, 'start_kraken_doa'):
                    self.start_kraken_doa()

            QTimer.singleShot(4000, _finish)

        threading.Thread(target=_heal_worker, daemon=True).start()

    def push_kraken_hardware_settings(self, force=False):
        freq_mhz = round(self.kraken_freq_spin.value(), 3)
        gain_val = round(self.kraken_gain_spin.value() if hasattr(self, 'kraken_gain_spin') else 30.0, 1)
        arr_spacing = round(self.kraken_radius_spin.value() if hasattr(self, 'kraken_radius_spin') else 0.135, 3)
        array_idx = self.kraken_array_combo.currentIndex() if hasattr(self, 'kraken_array_combo') else 0
        
        arr_type = "UCA"
        if array_idx == 1:
            arr_type = "ULA"
        elif array_idx == 2:
            arr_type = "Custom"

        self.update_kraken_physics_hint()

        target_state = (freq_mhz, gain_val, arr_spacing, arr_type)
        if not force and getattr(self, '_last_kraken_pushed_state', None) == target_state:
            return

        wsl_paths = [
            r"\\wsl.localhost\Ubuntu\home\feeka\krakensdr_doa\krakensdr_doa\_share\settings.json",
            r"\\wsl$\Ubuntu\home\feeka\krakensdr_doa\krakensdr_doa\_share\settings.json",
            r"\\wsl.localhost\Ubuntu\home\feeka\krakensdr_doa\krakensdr_doa\_nodejs\settings.json",
            r"\\wsl$\Ubuntu\home\feeka\krakensdr_doa\krakensdr_doa\_nodejs\settings.json"
        ]
        
        cfg = {}
        for p in wsl_paths:
            if os.path.exists(p):
                try:
                    with open(p, 'r', encoding='utf-8') as f:
                        cfg = json.load(f)
                    if cfg:
                        break
                except Exception:
                    pass
        
        if not cfg:
            cfg = {
                "center_freq": float(freq_mhz),
                "uniform_gain": float(gain_val),
                "data_interface": "shmem",
                "default_ip": "0.0.0.0",
                "en_remote_control": False,
                "en_doa": True,
                "ant_arrangement": arr_type,
                "ula_direction": "Both",
                "ant_spacing_meters": float(arr_spacing),
                "doa_method": "MUSIC",
                "active_vfos": 1,
                "output_vfo": 0,
                "vfo_freq_0": float(freq_mhz * 1e6),
                "ext_upd_flag": True
            }

        # Safely update only target parameters
        cfg["center_freq"] = float(freq_mhz)
        cfg["uniform_gain"] = float(gain_val)
        cfg["en_doa"] = True
        cfg["ant_arrangement"] = arr_type
        cfg["ant_spacing_meters"] = float(arr_spacing)
        cfg["vfo_freq_0"] = float(freq_mhz * 1e6)
        cfg["ext_upd_flag"] = True

        # Write merged config back to all accessible WSL paths
        written = False
        for p in wsl_paths:
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, indent=2)
                written = True
            except Exception:
                pass

        # Trigger inotify inside WSL so DAQ hw_controller reloads immediately
        def _touch_wsl():
            try:
                subprocess.run(
                    ["wsl.exe", "-d", "Ubuntu", "-e", "bash", "-c", "touch /home/feeka/krakensdr_doa/krakensdr_doa/_share/settings.json"],
                    capture_output=True,
                    timeout=2
                )
            except Exception:
                pass
        threading.Thread(target=_touch_wsl, daemon=True).start()

        self._last_kraken_pushed_state = target_state
        if written:
            self.log_event(f"Retuned Kraken Hardware: {freq_mhz:.3f} MHz | Gain {gain_val:.1f}dB | {arr_type} (Radius: {arr_spacing:.3f}m)")
        else:
            self.log_event(f"Kraken retune error: Could not write to WSL settings.json paths.")

    def on_kraken_mode_changed(self, index):
        if index == 0:
            self.kraken_host_row.hide()
            self.kraken_host_input.setText("127.0.0.1")
            self.kraken_port_input.setValue(8081)
        elif index == 1:
            self.kraken_host_row.hide()
        elif index == 2:
            self.kraken_host_row.show()
            self.kraken_port_input.setValue(8081)
        elif index == 3:
            self.kraken_host_row.show()
            self.kraken_port_input.setValue(5005)

    def on_kraken_freq_changed(self, freq):
        if hasattr(self, 'doa_compass'):
            self.doa_compass.freq_mhz = freq
        if self.kraken_thread and self.kraken_thread.isRunning():
            self.kraken_thread.freq_mhz = freq
        self.push_kraken_hardware_settings()

    def toggle_kraken_doa(self):
        if self.kraken_thread and self.kraken_thread.isRunning():
            self.stop_kraken_doa()
        else:
            self.start_kraken_doa()

    def start_kraken_doa(self):
        mode_idx = self.kraken_mode_combo.currentIndex()
        if mode_idx == 0:
            mode_str = "KRAKEN_POLL"
            host = "127.0.0.1"
            port = 8081
        elif mode_idx == 1:
            mode_str = "SIMULATOR"
            host = "127.0.0.1"
            port = 8081
        elif mode_idx == 2:
            mode_str = "KRAKEN_POLL"
            host = self.kraken_host_input.text().strip()
            port = self.kraken_port_input.value()
        else:
            mode_str = "UDP"
            host = self.kraken_host_input.text().strip()
            port = self.kraken_port_input.value()

        freq = self.kraken_freq_spin.value()

        if self.kraken_thread:
            self.kraken_thread.stop()

        self.kraken_thread = KrakenDoAThread(mode=mode_str, host=host, port=port, freq_mhz=freq, parent=self)
        self.kraken_thread.bearing_signal.connect(self.on_kraken_bearing)
        self.kraken_thread.status_signal.connect(self.on_kraken_status)
        self.kraken_thread.start()

        self.toggle_kraken_btn.setText("⏹ STOP DIRECTION FINDING")
        self.toggle_kraken_btn.setStyleSheet("background-color: #ef4444; color: white; font-weight: bold; padding: 8px; font-size: 13px;")
        self.log_event(f"Started KrakenSDR Direction Finding ({mode_str} on {host}:{port})")

    def stop_kraken_doa(self):
        if self.kraken_thread:
            self.kraken_thread.stop()
            self.kraken_thread = None

        self.toggle_kraken_btn.setText("▶ START DIRECTION FINDING")
        self.toggle_kraken_btn.setStyleSheet("background-color: #10b981; color: white; font-weight: bold; padding: 8px; font-size: 13px;")
        self.kraken_status_label.setText("[ KRAKENSDR DoA: STANDBY / DISCONNECTED ]")
        self.kraken_status_label.setStyleSheet("background-color: #060a14; color: #f59e0b; font-weight: bold; font-size: 13px; padding: 6px; border: 1px solid #1e293b; border-radius: 4px;")
        
        if hasattr(self, 'geo_map_view'):
            self.geo_map_view.page().runJavaScript("clearBearingLine();")
            
        self.log_event("Stopped KrakenSDR Direction Finding.")

    def on_kraken_status(self, msg, color):
        if hasattr(self, 'kraken_status_label'):
            self.kraken_status_label.setText(f"[ {msg.upper()} ]")
            self.kraken_status_label.setStyleSheet(f"background-color: #060a14; color: {color}; font-weight: bold; font-size: 13px; padding: 6px; border: 1px solid {color}; border-radius: 4px;")

    def on_kraken_bearing(self, data):
        if hasattr(self, 'doa_compass'):
            self.doa_compass.set_bearing_data(data)

        bearing = data.get("doa_deg", 0.0)
        confidence = data.get("confidence", 0.0)
        self.last_bearing_deg = bearing
        self.last_bearing_conf = confidence

        if hasattr(self, 'kraken_status_label') and confidence > 40.0:
            cardinals = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
            card_idx = int((bearing + 11.25) / 22.5) % 16
            card_str = cardinals[card_idx]
            self.kraken_status_label.setText(f"[ DoA LOCKED: {bearing:05.1f}° {card_str} | CONF: {confidence:.0f}% | SNR: {data.get('snr_db',0):.0f}dB ]")
            self.kraken_status_label.setStyleSheet("background-color: #060a14; color: #10b981; font-weight: bold; font-size: 13px; padding: 6px; border: 1px solid #10b981; border-radius: 4px;")

        # Real-time Hunter-Killer Live Bearing Injection
        if hasattr(self, 'hk_priority_queue') and self.hk_priority_queue and confidence > 40.0:
            k_freq = self.kraken_freq_spin.value() if hasattr(self, 'kraken_freq_spin') else 0.0
            updated = False
            for k, entry in self.hk_priority_queue.items():
                e_freq = entry.get("freq", 0.0)
                if abs(e_freq - k_freq) <= 5.0 or (entry.get("is_fhss", False) and abs(e_freq - k_freq) <= 25.0):
                    entry["bearing"] = round(bearing, 1)
                    entry["bearing_conf"] = round(confidence, 1)
                    updated = True
            if updated:
                self.refresh_hk_queue_ui()
            if hasattr(self, 'hk_metric_bearing'):
                self.hk_metric_bearing.setText(f"DoA Bearing: {bearing:05.1f}° (Conf: {confidence:.0f}%)")
                self.hk_metric_bearing.setStyleSheet("color: #10b981; font-family: monospace; font-size: 12px; font-weight: bold;")

        if hasattr(self, 'auto_cast_map_cb') and self.auto_cast_map_cb.isChecked() and hasattr(self, 'geo_map_view') and confidence > 40.0:
            lat = float(self.geo_lat_input.text()) if (hasattr(self, 'geo_lat_input') and self.geo_lat_input.text()) else 51.5074
            lon = float(self.geo_lon_input.text()) if (hasattr(self, 'geo_lon_input') and self.geo_lon_input.text()) else -0.1278
            js = f"if (typeof updateBearingLine === 'function') {{ updateBearingLine({lat}, {lon}, {bearing}, 6000, '#f59e0b'); }}"
            self.geo_map_view.page().runJavaScript(js)

        if hasattr(self, 'auto_tag_intel_cb') and self.auto_tag_intel_cb.isChecked() and self.current_active_fingerprint:
            db_entry = self.fingerprint_db.get(self.current_active_fingerprint)
            if db_entry and confidence > 65.0:
                db_entry["last_bearing_deg"] = round(bearing, 1)
                db_entry["bearing_confidence"] = round(confidence, 1)

    def cast_bearing_to_map(self):
        if not hasattr(self, 'geo_map_view'):
            return
        lat = float(self.geo_lat_input.text()) if (hasattr(self, 'geo_lat_input') and self.geo_lat_input.text()) else 51.5074
        lon = float(self.geo_lon_input.text()) if (hasattr(self, 'geo_lon_input') and self.geo_lon_input.text()) else -0.1278
        bearing = getattr(self, 'last_bearing_deg', 0.0)
        js = f"if (typeof updateBearingLine === 'function') {{ updateBearingLine({lat}, {lon}, {bearing}, 8000, '#10b981'); }}"
        self.geo_map_view.page().runJavaScript(js)
        self.log_event(f"Cast Line-of-Bearing ({bearing:.1f}°) from {lat:.5f}, {lon:.5f} to Tactical Map.")

    def fix_triangulated_target(self):
        if not hasattr(self, 'geo_map_view'):
            return
        lat = float(self.geo_lat_input.text()) if (hasattr(self, 'geo_lat_input') and self.geo_lat_input.text()) else 51.5074
        lon = float(self.geo_lon_input.text()) if (hasattr(self, 'geo_lon_input') and self.geo_lon_input.text()) else -0.1278
        bearing = getattr(self, 'last_bearing_deg', 0.0)
        
        # Save bearing observation into history for multi-point CEP
        if not hasattr(self, 'bearing_history'):
            self.bearing_history = []
        self.bearing_history.append((lat, lon, bearing, 90.0, time.time()))
        
        R = 6378137
        d = 2500
        brng = math.radians(bearing)
        lat1 = math.radians(lat)
        lon1 = math.radians(lon)
        lat2 = math.asin(math.sin(lat1) * math.cos(d / R) + math.cos(lat1) * math.sin(d / R) * math.cos(brng))
        lon2 = lon1 + math.atan2(math.sin(brng) * math.sin(d / R) * math.cos(lat1), math.cos(d / R) - math.sin(lat1) * math.sin(lat2))
        
        t_lat = math.degrees(lat2)
        t_lon = math.degrees(lon2)
        
        freq = self.kraken_freq_spin.value() if hasattr(self, 'kraken_freq_spin') else 915.0
        js = f"if (typeof addTriangulationFix === 'function') {{ addTriangulationFix({t_lat}, {t_lon}, '{freq:.2f}MHz'); }}"
        self.geo_map_view.page().runJavaScript(js)
        self.log_event(f"Recorded Bearing Observation #{len(self.bearing_history)}: {bearing:.1f}° from {lat:.5f}, {lon:.5f}")
        
        # If we have 2 or more bearings, automatically solve multi-bearing CEP fix as well!
        if len(self.bearing_history) >= 2:
            self.solve_multibearing_cep()

    def solve_multibearing_cep(self):
        if not hasattr(self, 'bearing_history') or len(self.bearing_history) < 2:
            lat = float(self.geo_lat_input.text()) if (hasattr(self, 'geo_lat_input') and self.geo_lat_input.text()) else 51.5074
            lon = float(self.geo_lon_input.text()) if (hasattr(self, 'geo_lon_input') and self.geo_lon_input.text()) else -0.1278
            brng = getattr(self, 'last_bearing_deg', 0.0)
            if not hasattr(self, 'bearing_history'):
                self.bearing_history = []
            self.bearing_history.append((lat, lon, brng, 80.0, time.time()))
            self.log_event(f"Captured Line-of-Bearing observation #{len(self.bearing_history)} ({brng:.1f}°). Record another bearing from a different spot to solve CEP.")
            return
        
        records = [(r[0], r[1], r[2], r[3]) for r in self.bearing_history[-8:]]
        fix = calculate_cep_triangulation(records)
        if fix:
            t_lat = fix["lat"]
            t_lon = fix["lon"]
            cep = fix["cep_meters"]
            n = fix["num_fixes"]
            
            if hasattr(self, 'geo_status_label'):
                self.geo_status_label.setText(f"[ TRIANGULATED FIX: {t_lat:.5f}, {t_lon:.5f} | CEP-95: ±{cep:.1f}m | {n} Bearings ]")
                self.geo_status_label.setStyleSheet("background-color: #060a14; color: #10b981; font-weight: bold; font-size: 13px; padding: 4px; border: 1px solid #10b981; border-radius: 4px;")
            
            if hasattr(self, 'geo_map_view'):
                js = f"if (typeof addCepFix === 'function') {{ addCepFix({t_lat}, {t_lon}, {cep}, 'ESTIMATED TARGET FIX'); }}"
                self.geo_map_view.page().runJavaScript(js)
            
            self.log_event(f"TRIANGULATION CEP: Computed Target Fix at {t_lat:.5f}, {t_lon:.5f} (95% CEP radius ±{cep:.1f}m across {n} bearings).")
        else:
            self.log_event("TRIANGULATION CEP: Bearings are parallel or collinear; move to a wider baseline and record again.")

    def compute_and_render_viewshed(self):
        lat = 51.5074
        lon = -0.1278
        if hasattr(self, 'geo_lat_input') and self.geo_lat_input.text():
            try: lat = float(self.geo_lat_input.text())
            except ValueError: pass
        if hasattr(self, 'geo_lon_input') and self.geo_lon_input.text():
            try: lon = float(self.geo_lon_input.text())
            except ValueError: pass

        h_tx = self.embm_mast_spin.value() if hasattr(self, 'embm_mast_spin') else 10.0
        h_rx = self.embm_uav_alt_spin.value() if hasattr(self, 'embm_uav_alt_spin') else 25.0
        max_r = self.embm_range_spin.value() if hasattr(self, 'embm_range_spin') else 15.0
        freq = self.embm_freq_spin.value() if hasattr(self, 'embm_freq_spin') else 915.0

        if hasattr(self, 'embm_status_lbl'):
            self.embm_status_lbl.setText(f"[ EMBM: COMPUTING 4/3 EARTH VIEWSHED ({max_r:.0f}km @ {freq:.0f}MHz)... ]")
            self.embm_status_lbl.setStyleSheet("background-color: #060a14; color: #f59e0b; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #f59e0b; border-radius: 4px;")

        def _worker():
            from terrain_engine import get_terrain_engine
            te = get_terrain_engine()
            res = te.compute_viewshed(lat, lon, h_tx=h_tx, h_rx=h_rx, freq_mhz=freq, max_range_km=max_r)
            self.embm_viewshed_ready_signal.emit(res)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_viewshed_computed(self, data):
        self.last_viewshed_data = data
        bounds = data.get("bounds", [0, 0, 0, 0])
        png_b64 = data.get("png_base64", "")
        los_pct = data.get("los_pct", 0.0)
        diff_pct = data.get("diff_pct", 0.0)
        shadow_pct = data.get("shadow_pct", 0.0)
        elev = data.get("station_elev_m", 0.0)
        blind_sectors = data.get("blind_sectors", [])

        if hasattr(self, 'geo_map_view') and png_b64:
            js = f"if (typeof updateViewshedOverlay === 'function') {{ updateViewshedOverlay('{png_b64}', {bounds[0]}, {bounds[1]}, {bounds[2]}, {bounds[3]}); }}"
            self.geo_map_view.page().runJavaScript(js)

        if hasattr(self, 'embm_status_lbl'):
            status_text = f"[ EMBM: {los_pct:.1f}% LOS | {diff_pct:.1f}% DIFFRACTION | {shadow_pct:.1f}% SHADOW BLIND ({elev:.1f}m MSL) | {len(blind_sectors)} INGRESS CORRIDORS ]"
            self.embm_status_lbl.setText(status_text)
            self.embm_status_lbl.setStyleSheet("background-color: #060a14; color: #10b981; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #10b981; border-radius: 4px;")

        self.log_event(f"EMBM TERRAIN: Computed {data.get('max_range_km')}km viewshed ({los_pct:.1f}% LOS, {shadow_pct:.1f}% Shadow, {len(blind_sectors)} Blind Corridors).")

    def clear_viewshed_overlay(self):
        self.last_viewshed_data = None
        if hasattr(self, 'geo_map_view'):
            self.geo_map_view.page().runJavaScript("if (typeof clearViewshedOverlay === 'function') { clearViewshedOverlay(); }")
        if hasattr(self, 'embm_status_lbl'):
            self.embm_status_lbl.setText("[ EMBM: STANDBY | 4/3 EARTH && FRESNEL ENGINE READY ]")
            self.embm_status_lbl.setStyleSheet("background-color: #060a14; color: #38bdf8; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #1e293b; border-radius: 4px;")
        self.log_event("EMBM TERRAIN: Cleared terrain viewshed heatmap layer.")

    def detach_map_window(self):
        if not hasattr(self, 'popout_windows'):
            self.popout_windows = {}
        if "map" in self.popout_windows and self.popout_windows["map"].isVisible():
            self.popout_windows["map"].raise_()
            self.popout_windows["map"].activateWindow()
            return
        win = PopOutWindow("Tactical Geolocation && Map", self.geo_map_container, self.geo_parent_layout, 3, self)
        self.popout_windows["map"] = win
        win.closed.connect(lambda: self.popout_windows.pop("map", None))
        win.show()

    def detach_video_window(self):
        if not hasattr(self, 'popout_windows'):
            self.popout_windows = {}
        if "video" in self.popout_windows and self.popout_windows["video"].isVisible():
            self.popout_windows["video"].raise_()
            self.popout_windows["video"].activateWindow()
            return
        win = PopOutWindow("FPV Analog Video Feed", self.video_display_container, self.video_parent_layout, 5, self)
        self.popout_windows["video"] = win
        win.closed.connect(lambda: self.popout_windows.pop("video", None))
        win.show()

    def detach_kraken_window(self):
        if not hasattr(self, 'popout_windows'):
            self.popout_windows = {}
        if "kraken" in self.popout_windows and self.popout_windows["kraken"].isVisible():
            self.popout_windows["kraken"].raise_()
            self.popout_windows["kraken"].activateWindow()
            return
        win = PopOutWindow("Kraken 360 DoA Compass HUD", self.doa_compass_container, self.kraken_parent_layout, 6, self)
        self.popout_windows["kraken"] = win
        win.closed.connect(lambda: self.popout_windows.pop("kraken", None))
        win.show()

    def detach_drone_window(self):
        if not hasattr(self, 'popout_windows'):
            self.popout_windows = {}
        if "drone" in self.popout_windows and self.popout_windows["drone"].isVisible():
            self.popout_windows["drone"].raise_()
            self.popout_windows["drone"].activateWindow()
            return
        win = PopOutWindow("Drone Telemetry && Pilot Surveillance", self.drone_cockpit_container, self.drone_parent_layout, 4, self)
        self.popout_windows["drone"] = win
        win.closed.connect(lambda: self.popout_windows.pop("drone", None))
        win.show()

    def start_heltec(self):
        port = self.heltec_port_combo.currentText() if hasattr(self, 'heltec_port_combo') else "COM6"
        if self.heltec_thread:
            self.heltec_thread.stop()
        self.heltec_thread = HeltecLoraThread(port=port)
        self.heltec_thread.rc_data_received.connect(self.on_heltec_rc)
        self.heltec_thread.telemetry_link_received.connect(self.on_heltec_tlm_link)
        self.heltec_thread.battery_received.connect(self.on_heltec_battery)
        self.heltec_thread.attitude_received.connect(self.on_heltec_attitude)
        self.heltec_thread.flight_mode_received.connect(self.on_heltec_flight_mode)
        self.heltec_thread.gps_received.connect(self.on_heltec_gps)
        self.heltec_thread.sync_discovered.connect(self.on_heltec_sync)
        self.heltec_thread.rate_detected.connect(self.on_heltec_rate)
        self.heltec_thread.pilot_discovered.connect(self.on_heltec_pilot_discovered)
        self.heltec_thread.status_changed.connect(self.on_heltec_status)
        self.heltec_thread.start()

    def on_heltec_pilot_discovered(self, data):
        uid_str = data.get("uid_str", "")
        u3 = data.get("u3", 0)
        u4 = data.get("u4", 0)
        u5 = data.get("u5", 0)
        crc = data.get("crc_init", "")
        rssi = data.get("rssi", -100)
        rate = data.get("rate", "50Hz")

        if not hasattr(self, 'discovered_pilots'):
            self.discovered_pilots = {}

        is_new = uid_str not in self.discovered_pilots
        self.discovered_pilots[uid_str] = {
            "u3": u3,
            "u4": u4,
            "u5": u5,
            "crc_init": crc,
            "rssi": rssi,
            "rate": rate,
            "last_seen": time.time()
        }

        if hasattr(self, 'pilot_selector_combo'):
            item_text = f"Pilot UID {u3}:{u4}:{u5} | Rate: {rate} | RSSI: {rssi:.0f} dBm"
            found = False
            for i in range(1, self.pilot_selector_combo.count()):
                if self.pilot_selector_combo.itemData(i) == uid_str:
                    self.pilot_selector_combo.setItemText(i, item_text)
                    found = True
                    break
            if not found:
                self.pilot_selector_combo.addItem(item_text, uid_str)

        # Update active FHSS 900M entry in Hunter-Killer queue immediately with the discovered pilot UID
        if hasattr(self, 'hk_priority_queue') and 'FHSS_900M' in self.hk_priority_queue:
            self.hk_priority_queue['FHSS_900M']['mod'] = f"ELRS 900M ({rate}) [Pilot UID {u3}:{u4}:{u5}]"
            self.hk_priority_queue['FHSS_900M']['fingerprint'] = f"0x{crc}"
            self.hk_priority_queue['FHSS_900M']['rssi'] = rssi
            self.refresh_hk_queue_ui()

        if is_new:
            self.log_event(f"New ELRS Pilot Discovered: UID {u3}:{u4}:{u5} (CRC: 0x{crc}, Rate: {rate}, RSSI: {rssi:.0f} dBm)")
            # Auto-Fusion Hand-Off: Sync Kraken DoA to 915.000 MHz only on initial discovery
            if hasattr(self, 'auto_fusion_heltec_kraken_cb') and self.auto_fusion_heltec_kraken_cb.isChecked():
                if hasattr(self, 'kraken_freq_spin'):
                    self.kraken_freq_spin.blockSignals(True)
                    self.kraken_freq_spin.setValue(915.0)
                    self.kraken_freq_spin.blockSignals(False)
                if hasattr(self, 'kraken_radius_spin') and hasattr(self, 'kraken_array_combo') and self.kraken_array_combo.currentIndex() == 0:
                    self.kraken_radius_spin.blockSignals(True)
                    self.kraken_radius_spin.setValue(0.135)
                    self.kraken_radius_spin.blockSignals(False)
                self.push_kraken_hardware_settings()

    def on_lock_pilot_clicked(self):
        if not hasattr(self, 'pilot_selector_combo'):
            return
        idx = self.pilot_selector_combo.currentIndex()
        if idx <= 0:
            self.on_unlock_pilot_clicked()
            return
        uid_str = self.pilot_selector_combo.itemData(idx)
        if not uid_str or uid_str not in getattr(self, 'discovered_pilots', {}):
            return
        p = self.discovered_pilots[uid_str]
        cmd = f"LOCK_PILOT:{p['u3']},{p['u4']},{p['u5']}"
        if self.heltec_thread:
            self.heltec_thread.send_command(cmd)
        if hasattr(self, 'pilot_target_badge'):
            self.pilot_target_badge.setText(f"[ LOCKED TARGET: PILOT UID {p['u3']}:{p['u4']}:{p['u5']} | CRC 0x{p['crc_init']} ]")
            self.pilot_target_badge.setStyleSheet("background-color: #060a14; color: #f59e0b; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #f59e0b; border-radius: 4px;")
        
        # Update active FHSS 900M entry in Hunter-Killer queue immediately with the locked pilot
        if hasattr(self, 'hk_priority_queue') and 'FHSS_900M' in self.hk_priority_queue:
            self.hk_priority_queue['FHSS_900M']['mod'] = f"ELRS 900M [LOCKED UID {p['u3']}:{p['u4']}:{p['u5']}]"
            self.hk_priority_queue['FHSS_900M']['fingerprint'] = f"0x{p['crc_init']}"
            self.refresh_hk_queue_ui()

        # Auto-Fusion Hand-Off: Sync Kraken DoA to 915.000 MHz
        if hasattr(self, 'auto_fusion_heltec_kraken_cb') and self.auto_fusion_heltec_kraken_cb.isChecked():
            if hasattr(self, 'kraken_freq_spin'):
                self.kraken_freq_spin.blockSignals(True)
                self.kraken_freq_spin.setValue(915.0)
                self.kraken_freq_spin.blockSignals(False)
            if hasattr(self, 'kraken_radius_spin') and hasattr(self, 'kraken_array_combo') and self.kraken_array_combo.currentIndex() == 0:
                self.kraken_radius_spin.blockSignals(True)
                self.kraken_radius_spin.setValue(0.135)
                self.kraken_radius_spin.blockSignals(False)
            self.push_kraken_hardware_settings()
            self.log_event(f"SENSOR FUSION: Synced Kraken DoA Array to 915.000 MHz (Locked Target UID {p['u3']}:{p['u4']}:{p['u5']}).")
                
        self.log_event(f"Target pilot locked: UID {p['u3']}:{p['u4']}:{p['u5']}")

    def on_unlock_pilot_clicked(self):
        if self.heltec_thread:
            self.heltec_thread.send_command("LOCK_PILOT:AUTO")
        if hasattr(self, 'pilot_selector_combo'):
            self.pilot_selector_combo.setCurrentIndex(0)
        if hasattr(self, 'pilot_target_badge'):
            self.pilot_target_badge.setText("[ ACTIVE TARGET: AUTO / ANY PILOT ]")
            self.pilot_target_badge.setStyleSheet("background-color: #060a14; color: #38bdf8; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #1e293b; border-radius: 4px;")
        self.log_event("Target pilot filter set to Auto / Any.")

    def on_elrs_rate_selected(self, index):
        if not self.heltec_thread:
            return
        cmd_map = {
            0: "SET_RATE:AUTO",
            1: "SET_RATE:50HZ",
            2: "SET_RATE:25HZ",
            3: "SET_RATE:100HZ",
            4: "SET_RATE:100HZ FULL",
            5: "SET_RATE:D50",
            6: "SET_RATE:150HZ",
            7: "SET_RATE:200HZ",
            8: "SET_RATE:250HZ",
            9: "SET_RATE:333HZ FULL"
        }
        cmd = cmd_map.get(index, "SET_RATE:AUTO")
        self.heltec_thread.send_command(cmd)
        self.log_event(f"Sent ExpressLRS rate command to Heltec: {cmd}")

    def on_heltec_rate(self, data):
        rate_name = data.get('rate_name', '50Hz')
        sf = data.get('sf', 8)
        bw = data.get('bw_khz', 500.0)
        interval = data.get('interval_us', 20000)
        if hasattr(self, 'elrs_rate_badge'):
            self.elrs_rate_badge.setText(f"[ ACTIVE DEMOD: {rate_name} | SF{sf} | BW: {bw:.0f}kHz | Interval: {interval} µs ]")
            self.elrs_rate_badge.setStyleSheet("background-color: #060a14; color: #10b981; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #10b981; border-radius: 4px;")
        
        # Suppress log event when Auto-Detect scanning is active to prevent event log flooding
        is_auto = hasattr(self, 'elrs_rate_mode_combo') and self.elrs_rate_mode_combo.currentIndex() == 0
        if not is_auto:
            self.log_event(f"Heltec locked ELRS rate: {rate_name} (SF{sf}, BW {bw:.0f}kHz, {interval} µs)")

    def restart_heltec(self):
        self.start_heltec()

    def open_settings_dialog(self):
        dlg = TacticalSettingsDialog(self)
        dlg.exec()

    def apply_runtime_settings(self, settings):
        self.settings = settings
        self.log_event("TACTICAL SETTINGS: Hardware && System configuration updated.")

        # Update Heltec Port if changed
        if hasattr(self, 'heltec_port_combo'):
            idx = self.heltec_port_combo.findText(settings.get('heltec_port', 'COM6'))
            if idx >= 0:
                self.heltec_port_combo.setCurrentIndex(idx)
        if self.heltec_thread and self.heltec_thread.port != settings.get('heltec_port', 'COM6'):
            self.heltec_thread.set_port(settings.get('heltec_port', 'COM6'))
            self.restart_heltec()

        # Update Kraken DoA parameters
        if hasattr(self, 'kraken_thread') and self.kraken_thread:
            self.kraken_thread.wsl_share_path = settings.get('kraken_wsl_path', '')
            host = settings.get('kraken_host', '127.0.0.1')
            port = settings.get('kraken_doa_port', 8081)
            self.kraken_thread.http_url = f"http://{host}:{port}/DOA_value.html"

        if hasattr(self, 'kraken_radius_spin'):
            self.kraken_radius_spin.setValue(settings.get('kraken_default_radius', 0.135))

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
        channels = data.get('channels', [ch1, ch2, ch3, ch4] + [1500] * 12)

        # Synchronize live armed status & telemetry to discovered_pilots for AI Copilot & SITREP
        if not hasattr(self, 'discovered_pilots'):
            self.discovered_pilots = {}
        
        pilot_key = getattr(self, 'last_pilot_key', None) or "ACTIVE_PILOT"
        if pilot_key not in self.discovered_pilots:
            self.discovered_pilots[pilot_key] = {
                "u3": 0, "u4": 0, "u5": 0,
                "crc_init": "2156",
                "rssi": rssi,
                "rate_name": data.get('packet_rate', '50Hz'),
                "armed": armed,
                "channels": channels,
                "last_seen": time.time()
            }
        else:
            self.discovered_pilots[pilot_key]["armed"] = armed
            self.discovered_pilots[pilot_key]["rssi"] = rssi
            self.discovered_pilots[pilot_key]["rate_name"] = data.get('packet_rate', '50Hz')
            self.discovered_pilots[pilot_key]["channels"] = channels
            self.discovered_pilots[pilot_key]["last_seen"] = time.time()

        for p in self.discovered_pilots.values():
            p["armed"] = armed
            p["rate_name"] = data.get('packet_rate', '50Hz')
            p["rssi"] = rssi
            p["last_seen"] = time.time()

        if hasattr(self, 'heltec_connect_btn'):
            self.heltec_connect_btn.setText(f"🚁 HELTEC: {rssi:.0f}dBm")

        pkt_rate = data.get('packet_rate', '50Hz')
        if hasattr(self, 'elrs_rate_badge'):
            if not hasattr(self, 'current_locked_elrs_rate') or self.current_locked_elrs_rate != pkt_rate:
                self.current_locked_elrs_rate = pkt_rate
                self.elrs_rate_badge.setText(f"[ ACTIVE DEMOD: {pkt_rate} (Synchronized) ]")
                self.elrs_rate_badge.setStyleSheet("background-color: #060a14; color: #10b981; font-family: monospace; font-size: 11px; font-weight: bold; padding: 5px; border: 1px solid #10b981; border-radius: 4px;")

        # Feature 1: Update Mode 2 Gimbal HUD
        if self.gimbal_hud:
            self.gimbal_hud.update_sticks(ch1, ch2, ch3, ch4, armed)

        thr_pct = max(0.0, min(100.0, (ch3 - 988.0) / 10.24))
        if hasattr(self, 'drone_sticks_lbl'):
            self.drone_sticks_lbl.setText(f"THR: {ch3} µs ({thr_pct:.0f}%) | YAW: {ch1} µs | PIT: {ch2} µs | ROL: {ch4} µs")

        # Feature 3: Update 16-Channel Diagnostic Matrix
        if hasattr(self, 'channel_bars') and len(self.channel_bars) == 16:
            for idx in range(16):
                val = channels[idx] if idx < len(channels) else 1500
                self.channel_bars[idx].setValue(val)
                self.channel_labels[idx].setText(f"{val}µs")
                if idx == 4: # AUX1 Arm
                    if val > 1500:
                        self.channel_labels[idx].setStyleSheet("color: #ef4444; font-weight: bold; font-size: 8.5pt; font-family: 'Consolas', monospace;")
                        self.channel_bars[idx].setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 3px; } QProgressBar::chunk { background-color: #ef4444; border-radius: 2px; }")
                    else:
                        self.channel_labels[idx].setStyleSheet("color: #10b981; font-weight: bold; font-size: 8.5pt; font-family: 'Consolas', monospace;")
                        self.channel_bars[idx].setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 3px; } QProgressBar::chunk { background-color: #10b981; border-radius: 2px; }")
                elif idx == 5: # AUX2 Flight Mode
                    color = "#10b981" if val < 1300 else ("#f59e0b" if val < 1700 else "#ef4444")
                    self.channel_labels[idx].setStyleSheet(f"color: {color}; font-weight: bold; font-size: 8.5pt; font-family: 'Consolas', monospace;")

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
            self.log_event(f"HELTEC PILOT STATE: {'ARMED' if armed else 'DISARMED'} (RSSI: {rssi:.0f} dBm)")

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
                self.link_margin_lbl.setText(f"NOMINAL LINK ({lq}% RC Integrity)")
                self.link_margin_lbl.setStyleSheet("color: #22c55e; font-weight: bold;")
                self.drone_lq_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #22c55e; border-radius: 3px; }")
            elif lq >= 50 or drone_rssi > -105:
                self.link_margin_lbl.setText(f"MARGIN DEGRADED ({lq}% LQ | {drone_rssi}dBm)")
                self.link_margin_lbl.setStyleSheet("color: #eab308; font-weight: bold;")
                self.drone_lq_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #eab308; border-radius: 3px; }")
            else:
                self.link_margin_lbl.setText(f"CRITICAL FAILSAFE IMMINENT ({lq}% LQ)")
                self.link_margin_lbl.setStyleSheet("color: #ef4444; font-weight: bold;")
                self.drone_lq_bar.setStyleSheet("QProgressBar { background-color: #0f172a; border: 1px solid #1e293b; border-radius: 4px; } QProgressBar::chunk { background-color: #ef4444; border-radius: 3px; }")

        self.geo_status_label.setText(f"[ TARGET TELEMETRY: PROTOCOL LOCKED | LQ: {lq}% | RSSI: {drone_rssi}dBm ]")
        self.geo_status_label.setStyleSheet("color: #22c55e; font-weight: bold; font-size: 15px;")
        if not hasattr(self, 'last_tlm_log_time') or (time.time() - self.last_tlm_log_time > 3.0):
            self.last_tlm_log_time = time.time()
            self.log_event(f"HELTEC DRONE TELEMETRY: LQ={lq}% | DroneRSSI={drone_rssi} dBm | SNR={drone_snr} dB")

    def on_heltec_battery(self, data):
        if hasattr(self, 'drone_vbat_lbl'):
            self.drone_vbat_lbl.setText(f"LiPo Voltage: {data['voltage']:.1f} V")
            self.drone_curr_lbl.setText(f"Current Draw: {data['current']:.1f} A")
        self.log_event(f"HELTEC DRONE BATTERY: {data['voltage']:.1f}V | {data['current']:.1f}A | {data['battery_pct']}%")

    def on_heltec_attitude(self, data):
        pitch = data.get("pitch", 0.0)
        roll = data.get("roll", 0.0)
        yaw = data.get("yaw", 0.0)
        if hasattr(self, 'drone_att_lbl'):
            self.drone_att_lbl.setText(f"Attitude: P:{pitch:+.0f}° | R:{roll:+.0f}° | Y:{yaw:.0f}°")
        if not hasattr(self, 'last_att_log_time') or (time.time() - self.last_att_log_time > 5.0):
            self.last_att_log_time = time.time()
            self.log_event(f"HELTEC UAV ATTITUDE: Pitch={pitch:+.1f}° | Roll={roll:+.1f}° | Yaw={yaw:.1f}°")

    def on_heltec_flight_mode(self, data):
        mode = data.get("mode", "ANGLE")
        if hasattr(self, 'drone_fmode_lbl'):
            self.drone_fmode_lbl.setText(f"Flight Mode: {mode}")
            color = "#10b981" if mode in ["ANGLE", "POSHOLD"] else ("#f59e0b" if mode == "HORIZON" else "#ef4444")
            self.drone_fmode_lbl.setStyleSheet(f"background-color: #0f172a; color: {color}; border: 1px solid #1e293b; padding: 6px; border-radius: 4px; font-weight: bold; font-size: 11px;")
        self.log_event(f"HELTEC UAV FLIGHT MODE: {mode}")

    def on_heltec_gps(self, data):
        lat = data.get("lat", 0.0)
        lon = data.get("lon", 0.0)
        alt = data.get("alt", 0.0)
        spd = data.get("spd", 0.0)
        sats = data.get("sats", 0)

        # Synchronize GPS and kinematic state to discovered_pilots for AI Copilot
        if hasattr(self, 'discovered_pilots'):
            for p in self.discovered_pilots.values():
                if lat != 0 or lon != 0:
                    p["lat"] = lat
                    p["lon"] = lon
                p["alt"] = alt
                p["spd"] = spd
                p["sats"] = sats

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

            self.log_event(f"HELTEC UAV GPS FIX: {lat:.5f}, {lon:.5f} | Alt: {alt}m | Spd: {spd}km/h | Sats: {sats}")

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
            self.log_event(f"INTEL DB: Auto-registered ELRS Pilot {pilot_id} (UID {uid_str})")

        self.log_event(f"HELTEC DISCOVERED PILOT: Hash {pilot_id} | HopIdx: {data['hop_idx']} | Nonce: {data['nonce']}")

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

    def save_topology(self, force=False):
        self._topology_dirty = True
        if force:
            self.flush_dirty_state_to_disk()

    def flush_dirty_state_to_disk(self):
        if getattr(self, '_fingerprints_dirty', False):
            try:
                with open('fingerprints.json', 'w', encoding='utf-8') as f:
                    json.dump(self.fingerprint_db, f, indent=4)
                self._fingerprints_dirty = False
            except Exception as e:
                self.log_event(f"Error saving fingerprints: {e}")

        if getattr(self, '_topology_dirty', False):
            try:
                with open('topology.json', 'w', encoding='utf-8') as f:
                    json.dump(self.network_links, f, indent=4)
                self._topology_dirty = False
            except Exception:
                pass

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

    def save_fingerprints(self, force=False):
        self._fingerprints_dirty = True
        if force:
            self.flush_dirty_state_to_disk()

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
            self.current_mode = "SWEEP"
            self.freq_input.hide()
            self.freq_label.setText("Sweep Range:")
            self.sweep_start_input.show()
            self.sweep_end_input.show()
            if hasattr(self, 'sweep_bin_label'): self.sweep_bin_label.show()
            if hasattr(self, 'sweep_bin_combo'): self.sweep_bin_combo.show()
            self.const_plot.hide()
            self.vfo_region.hide()
            self.graph_layout.addWidget(self.waterfall_plot, 1, 0, 1, 2)
        else:
            self.current_mode = "STARE"
            self.freq_input.show()
            self.freq_label.setText("Center Freq:")
            self.sweep_start_input.hide()
            self.sweep_end_input.hide()
            if hasattr(self, 'sweep_bin_label'): self.sweep_bin_label.hide()
            if hasattr(self, 'sweep_bin_combo'): self.sweep_bin_combo.hide()
            self.graph_layout.addWidget(self.const_plot, 1, 1)
            self.graph_layout.addWidget(self.waterfall_plot, 1, 0)
            self.vfo_region.show()
            self.const_plot.show()

    def on_sweep_bin_changed(self, idx):
        bin_widths = [100000, 250000, 500000, 1000000, 2000000, 5000000]
        if 0 <= idx < len(bin_widths):
            self.sweep_bin_width_hz = bin_widths[idx]
            self.log_event(f"Sweep bin resolution set to {self.sweep_bin_width_hz/1e3:.0f} kHz")
            if self.current_mode == "SWEEP":
                self.start_sdr()

    def on_palette_changed(self, name):
        if name in TACTICAL_COLORMAPS:
            self.current_cmap = TACTICAL_COLORMAPS[name]
            self.waterfall_image.setLookupTable(self.current_cmap.getLookupTable())
            self.log_event(f"Waterfall palette switched to {name}")

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
            bin_w = getattr(self, 'sweep_bin_width_hz', 1000000)
            return self.sweep_start_input.value() * 1e6 + bin_idx * bin_w
        else:
            center_hz = self.freq_input.value() * 1e6
            return center_hz + (bin_idx - 512) * (20000000 / 1024)

    def freq_to_bin(self, freq_hz):
        if self.current_mode == "SWEEP":
            bin_w = getattr(self, 'sweep_bin_width_hz', 1000000)
            return (freq_hz - self.sweep_start_input.value() * 1e6) / bin_w
        else:
            center_hz = self.freq_input.value() * 1e6
            return 512 + (freq_hz - center_hz) * 1024 / 20000000

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
            min_view_hz = center_hz - 10000000
            max_view_hz = center_hz + 10000000
        
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
            bin_width_hz = getattr(self, 'sweep_bin_width_hz', 1000000)
            num_bins = int((end_hz - start_hz) / bin_width_hz)
            if num_bins <= 0: num_bins = 1
            
            self.waterfall_data = np.zeros((100, num_bins))
            self.fft_plot.setXRange(0, num_bins)
            self.hop_history.clear()
            
            self.hackrf_thread = HackRFSweepThread(start_hz, end_hz, lna, vga, bin_width_hz)
            self.log_event(f"SWEEP MODE ENGAGED - {start_hz/1e6:.1f} to {end_hz/1e6:.1f} MHz (Bin Res: {bin_width_hz/1e3:.0f} kHz)")
        else:
            self.current_mode = "STARE"
            freq_hz = int(self.freq_input.value() * 1e6)
            
            self.waterfall_data = np.zeros((100, 1024))
            self.fft_plot.setXRange(0, 1024)
            
            self.hackrf_thread = HackRFThread(freq_hz, lna, vga)
            if hasattr(self, 'audio_playing') and self.audio_playing:
                self.hackrf_thread.play_audio = True
            self.log_event(f"STARE MODE ENGAGED - {freq_hz/1e6:.2f} MHz (20MHz Passband)")

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

    def _handle_jump_to_stare(self, x_val):
        if self.current_mode == "SWEEP":
            freq_mhz = self.bin_to_freq(x_val) / 1e6
            self.log_event(f"[TACTICAL] JUMP-TO-STARE: Tuning 20MHz Stare on {freq_mhz:.2f} MHz")
            self.mode_selector.setCurrentText("STARE MODE (2MHz)")
            self.freq_input.setValue(freq_mhz)
            self.start_sdr()
        elif self.current_mode == "STARE":
            freq_mhz = self.bin_to_freq(x_val) / 1e6
            if abs(freq_mhz - self.freq_input.value()) > 0.5:
                self.log_event(f"[TACTICAL] STARE RETUNE: Centering 20MHz Stare on {freq_mhz:.2f} MHz")
                self.freq_input.setValue(freq_mhz)
                self.start_sdr()

    def fft_mouse_double_click(self, event):
        scene_pos = self.fft_plot.mapToScene(event.pos())
        view_pos = self.fft_plot.plotItem.vb.mapSceneToView(scene_pos)
        self._handle_jump_to_stare(view_pos.x())

    def waterfall_mouse_double_click(self, event):
        scene_pos = self.waterfall_plot.mapToScene(event.pos())
        view_pos = self.waterfall_plot.plotItem.vb.mapSceneToView(scene_pos)
        self._handle_jump_to_stare(view_pos.x())

    def fft_mouse_press(self, event):
        scene_pos = self.fft_plot.mapToScene(event.pos())
        items = self.fft_plot.scene().items(scene_pos)
        clicked_on_handle = any(isinstance(item, pg.InfiniteLine) for item in items)
        clicked_on_region = any(isinstance(item, pg.LinearRegionItem) for item in items)

        if self.mask_mode_btn.isChecked() and event.button() == Qt.MouseButton.LeftButton and not (clicked_on_handle or clicked_on_region):
            view_pos = self.fft_plot.plotItem.vb.mapSceneToView(scene_pos)
            self.drag_start_x = view_pos.x()
            self.current_drag_region = pg.LinearRegionItem(values=[self.drag_start_x, self.drag_start_x], brush=(100, 100, 100, 80))
            self.fft_plot.addItem(self.current_drag_region)
            event.accept()
        else:
            self.fft_plot._original_mousePressEvent(event)

    def fft_mouse_move(self, event):
        scene_pos = self.fft_plot.mapToScene(event.pos())
        if hasattr(self, 'cursor_v_line') and hasattr(self.fft_plot, 'plotItem') and self.fft_plot.plotItem.vb.sceneBoundingRect().contains(scene_pos):
            view_pos = self.fft_plot.plotItem.vb.mapSceneToView(scene_pos)
            x_val = view_pos.x()
            y_val = view_pos.y()
            freq_mhz = self.bin_to_freq(x_val) / 1e6

            self.cursor_v_line.setPos(x_val)
            self.cursor_h_line.setPos(y_val)
            self.cursor_v_line.show()
            self.cursor_h_line.show()

            if hasattr(self, 'cursor_hud_text'):
                vb_range = self.fft_plot.plotItem.vb.viewRange()
                self.cursor_hud_text.setPos(vb_range[0][1], vb_range[1][1])
                self.cursor_hud_text.setText(f" {freq_mhz:.3f} MHz | {y_val:.1f} dBFS ")
                self.cursor_hud_text.show()
        else:
            if hasattr(self, 'cursor_v_line'):
                self.cursor_v_line.hide()
                self.cursor_h_line.hide()
                self.cursor_hud_text.hide()

        if hasattr(self, 'current_drag_region') and self.current_drag_region is not None:
            view_pos = self.fft_plot.plotItem.vb.mapSceneToView(scene_pos)
            current_x = view_pos.x()
            self.current_drag_region.setRegion([min(self.drag_start_x, current_x), max(self.drag_start_x, current_x)])
            event.accept()
        else:
            self.fft_plot._original_mouseMoveEvent(event)

    def fft_mouse_leave(self, event):
        if hasattr(self, 'cursor_v_line'):
            self.cursor_v_line.hide()
            self.cursor_h_line.hide()
            self.cursor_hud_text.hide()
        if event is not None and hasattr(self.fft_plot, '_original_leaveEvent'):
            try:
                self.fft_plot._original_leaveEvent(event)
            except Exception:
                pass

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

        # 1. Real-Time SDR Telemetry Badge
        if hasattr(self, 'badge_sdr'):
            if self.hackrf_thread and getattr(self.hackrf_thread, 'running', False):
                if self.current_mode == "SWEEP":
                    bw_str = f"{getattr(self, 'sweep_bin_width_hz', 1000000)/1e3:.0f}k"
                    self.badge_sdr.setText(f"[ SDR: HACKRF SWEEP ({self.sweep_start_input.value():.0f}-{self.sweep_end_input.value():.0f} MHz | {bw_str}) ]")
                else:
                    self.badge_sdr.setText(f"[ SDR: HACKRF STARE ({self.freq_input.value():.2f} MHz @ 20MSPS) ]")
                self.badge_sdr.setStyleSheet("color: #10b981; background: #060e1a; font-family: 'Consolas', monospace; font-size: 9.5pt; font-weight: bold; padding: 2px 8px; border: 1px solid #10b981; border-radius: 3px;")
            else:
                self.badge_sdr.setText("[ SDR: HACKRF OFFLINE ]")
                self.badge_sdr.setStyleSheet("color: #64748b; background: #060e1a; font-family: 'Consolas', monospace; font-size: 9.5pt; font-weight: bold; padding: 2px 8px; border: 1px solid #334155; border-radius: 3px;")

        # 2. Heltec V3 LoRa Sniffer Telemetry Badge
        if hasattr(self, 'badge_heltec'):
            if self.heltec_thread and getattr(self.heltec_thread, 'running', False):
                pilot_cnt = getattr(self, 'last_pilot_count', 0)
                if pilot_cnt > 0:
                    self.badge_heltec.setText(f"[ HELTEC V3: {pilot_cnt} PILOT(S) ACTIVE ]")
                    self.badge_heltec.setStyleSheet("color: #38bdf8; background: #060e1a; font-family: 'Consolas', monospace; font-size: 9.5pt; font-weight: bold; padding: 2px 8px; border: 1px solid #0284c7; border-radius: 3px;")
                else:
                    self.badge_heltec.setText("[ HELTEC V3: SCANNING ]")
                    self.badge_heltec.setStyleSheet("color: #10b981; background: #060e1a; font-family: 'Consolas', monospace; font-size: 9.5pt; font-weight: bold; padding: 2px 8px; border: 1px solid #10b981; border-radius: 3px;")
            else:
                self.badge_heltec.setText("[ HELTEC V3: STANDBY ]")
                self.badge_heltec.setStyleSheet("color: #64748b; background: #060e1a; font-family: 'Consolas', monospace; font-size: 9.5pt; font-weight: bold; padding: 2px 8px; border: 1px solid #334155; border-radius: 3px;")

        # 3. KrakenSDR Direction Finding Telemetry Badge
        if hasattr(self, 'badge_kraken'):
            if hasattr(self, 'kraken_thread') and self.kraken_thread and getattr(self.kraken_thread, 'running', False):
                b_deg = getattr(self, 'last_bearing_deg', 0.0)
                b_conf = getattr(self, 'last_bearing_conf', 0.0)
                self.badge_kraken.setText(f"[ KRAKENSDR: {b_deg:05.1f}° TRUE (CONF {b_conf:.0f}%) ]")
                self.badge_kraken.setStyleSheet("color: #f59e0b; background: #060e1a; font-family: 'Consolas', monospace; font-size: 9.5pt; font-weight: bold; padding: 2px 8px; border: 1px solid #f59e0b; border-radius: 3px;")
            else:
                self.badge_kraken.setText("[ KRAKENSDR: STANDBY ]")
                self.badge_kraken.setStyleSheet("color: #64748b; background: #060e1a; font-family: 'Consolas', monospace; font-size: 9.5pt; font-weight: bold; padding: 2px 8px; border: 1px solid #334155; border-radius: 3px;")

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
                
                # Auto-Fusion Sweep -> FPV Video Carrier Snapping
                if getattr(self, 'auto_fusion_sweep_video_cb', None) and self.auto_fusion_sweep_video_cb.isChecked():
                    if not self.is_video_streaming and (current_time - getattr(self, 'last_vtx_auto_snap', 0) > 8.0):
                        for h_time, h_freq in self.hop_history:
                            h_mhz = h_freq / 1e6
                            if (5640 <= h_mhz <= 5950) or (1080 <= h_mhz <= 1360):
                                best_chan = None
                                min_diff = 999.0
                                for c_name, c_mhz in FPV_VIDEO_CHANNELS.items():
                                    diff = abs(c_mhz - h_mhz)
                                    if diff < min_diff:
                                        min_diff = diff
                                        best_chan = (c_name, c_mhz)
                                if best_chan and min_diff <= 3.0:
                                    self.last_vtx_auto_snap = current_time
                                    self.log_event(f"SENSOR FUSION: Detected active VTX carrier at {h_mhz:.1f} MHz -> Auto-snapping to {best_chan[0]}.")
                                    idx = self.fpv_channel_combo.findData(best_chan[1])
                                    if idx >= 0:
                                        self.fpv_channel_combo.setCurrentIndex(idx)
                                    self.sidebar_tabs.setCurrentIndex(5)
                                    self.start_video_stream()
                                    break

                # 1. FHSS Tracking & Band Aggregation (Runs First)
                if len(unique_freqs) >= 6 and len(fhss_candidates) >= 10:
                    hops_sec = len(fhss_candidates) / 2.0
                    f_min, f_max = min(unique_freqs), max(unique_freqs)
                    
                    protocol = "Unknown FHSS Network"
                    band_key = "FHSS_UNKNOWN"
                    if 860e6 <= f_min <= 930e6:
                        band_key = "FHSS_900M"
                        protocol = "TBS Crossfire / ELRS 900M"
                    elif 2400e6 <= f_min <= 2485e6:
                        band_key = "FHSS_2.4G"
                        protocol = "DJI OcuSync / ELRS 2.4G"
                    elif 5700e6 <= f_min <= 5900e6:
                        band_key = "FHSS_5.8G"
                        protocol = "DJI Digital FPV / Walksnail"
                        
                    if not hasattr(self, 'active_fhss_bands'):
                        self.active_fhss_bands = {}
                    self.active_fhss_bands[band_key] = {
                        'f_min': f_min / 1e6,
                        'f_max': f_max / 1e6,
                        'last_seen': current_time,
                        'hops_sec': hops_sec
                    }

                    # Consolidated FHSS threat update in Hunter-Killer Priority Queue
                    self.add_or_update_hk_fhss_cluster(band_key, f_min / 1e6, f_max / 1e6, hops_sec, protocol, np.max(fft_data))

                    if current_time - self.last_fhss_alert > 4.0:
                        net_name = f"{protocol} (~{hops_sec:.0f} hops/s) | {f_min/1e6:.1f}-{f_max/1e6:.1f} MHz"
                        self.log_event(f"DRONE TELEMETRY: {net_name}")
                        if hasattr(self, 'fhss_ui'):
                            for idx in range(self.fhss_ui.count() - 1, -1, -1):
                                item_txt = self.fhss_ui.item(idx).text()
                                if ("860" in item_txt or "900M" in item_txt or "Crossfire" in item_txt or "ELRS" in item_txt) and ("900" in protocol or "860" in band_key):
                                    self.fhss_ui.takeItem(idx)
                                elif ("2.4G" in item_txt or "2400" in item_txt or "OcuSync" in item_txt) and ("2.4" in protocol):
                                    self.fhss_ui.takeItem(idx)
                                elif ("5.8G" in item_txt or "5700" in item_txt or "Walksnail" in item_txt) and ("5.8" in protocol):
                                    self.fhss_ui.takeItem(idx)
                            self.fhss_ui.addItem(net_name)
                        self.last_fhss_alert = current_time

                # 2. Autonomous Hunter-Killer Stare Intercept Trigger
                if getattr(self, 'hk_active', False) and self.hk_state == "HUNTING":
                    snr_thresh = self.hk_snr_spin.value() if hasattr(self, 'hk_snr_spin') else 18
                    target_candidate = None
                    max_candidate_snr = 0.0
                    
                    if not hasattr(self, 'hk_candidate_persistence'):
                        self.hk_candidate_persistence = {}
                    if not hasattr(self, 'hk_last_intercept_time'):
                        self.hk_last_intercept_time = 0.0

                    # 2.0-second refractory debounce cooldown between Stare Mode switches
                    if current_time - self.hk_last_intercept_time >= 2.0:
                        for i in range(1, len(fft_data) - 1):
                            snr = fft_data[i] - noise_floor
                            freq_hz = self.bin_to_freq(i)
                            freq_mhz = round(freq_hz / 1e6, 3)

                            # Dynamic SNR threshold: Wideband FPV Video signals spread energy over 8-16 MHz, so allow +10 dB SNR
                            is_vtx_band = (5640.0 <= freq_mhz <= 5950.0) or (1080.0 <= freq_mhz <= 1360.0)
                            effective_snr_thresh = max(10, snr_thresh - 6) if is_vtx_band else snr_thresh

                            # Match sharp peaks or wideband VTX plateau centers
                            is_peak = (fft_data[i] >= fft_data[i-1] and fft_data[i] >= fft_data[i+1] and (fft_data[i] > fft_data[i-1] or fft_data[i] > fft_data[i+1]))
                            if not is_peak and is_vtx_band and snr >= effective_snr_thresh:
                                is_peak = True

                            if snr >= effective_snr_thresh and is_peak:
                                masked = False
                                for r_min, r_max in active_ranges:
                                    if r_min <= i <= r_max:
                                        masked = True
                                        break
                                if masked:
                                    continue

                                # FHSS DEDUPLICATION: Suppress stare interrupts for all active FHSS/LoRa bands (unless it's an FPV video band)
                                is_in_active_fhss = False
                                if not is_vtx_band:
                                    for b_info in getattr(self, 'active_fhss_bands', {}).values():
                                        if (current_time - b_info.get('last_seen', 0) < 6.0) and (b_info['f_min'] - 3.0 <= freq_mhz <= b_info['f_max'] + 3.0):
                                            is_in_active_fhss = True
                                            break
                                if is_in_active_fhss:
                                    continue

                                last_t = self.hk_last_eval_time.get(freq_mhz, 0)
                                if current_time - last_t < 6.0:
                                    continue

                                # M-of-N Candidate Persistence: Signal must appear across at least 2 sweep frames or SNR >= 24 dB or VTX channel match
                                is_known_vtx_chan = False
                                for cfreq in FPV_VIDEO_CHANNELS.values():
                                    if abs(freq_mhz - cfreq) <= 2.5:
                                        is_known_vtx_chan = True
                                        break

                                hits, prev_t = self.hk_candidate_persistence.get(freq_mhz, (0, 0))
                                if current_time - prev_t <= 5.0: # 5.0s window accommodating wideband sweep cycle time
                                    hits += 1
                                else:
                                    hits = 1
                                self.hk_candidate_persistence[freq_mhz] = (hits, current_time)

                                if (hits >= 2 or snr >= 24.0 or is_known_vtx_chan) and (snr > max_candidate_snr):
                                    max_candidate_snr = snr
                                    target_candidate = (freq_mhz, snr, fft_data[i])

                        # Prune stale persistence records
                        self.hk_candidate_persistence = {f: v for f, v in self.hk_candidate_persistence.items() if current_time - v[1] < 6.0}

                        if target_candidate:
                            c_freq, c_snr, c_pwr = target_candidate
                            self.hk_last_eval_time[c_freq] = current_time
                            self.hk_last_intercept_time = current_time
                            self.hk_state = "STARE_INTERCEPT"
                            self.hk_target_freq = c_freq
                            self.hk_stare_start_time = current_time
                            
                            # Check if matching known FPV Video channel
                            matched_vtx_str = ""
                            for cname, cfreq in FPV_VIDEO_CHANNELS.items():
                                if abs(c_freq - cfreq) <= 2.5:
                                    matched_vtx_str = f" [{cname}]"
                                    break

                            if hasattr(self, 'hk_status_badge'):
                                self.hk_status_badge.setText(f"[ KILLER ENGAGED: INTERCEPTING {c_freq:.3f} MHz{matched_vtx_str} (STARE DWELL) ]")
                                self.hk_status_badge.setStyleSheet("background-color: #060a14; color: #f59e0b; font-family: monospace; font-size: 12px; font-weight: bold; padding: 6px; border: 1px solid #f59e0b; border-radius: 4px;")
                            if hasattr(self, 'hk_cycle_breadcrumbs'):
                                self.hk_cycle_breadcrumbs.setText("CYCLE: [ 1. HUNT ] ➔ ▶ [ 2. DETECT ] ➔ [ 3. STARE && FP ] ➔ [ 4. DoA VECTOR ] ➔ [ 5. THREAT EVAL ] ➔ [ 6. RESUME ]")
                                self.hk_cycle_breadcrumbs.setStyleSheet("color: #f59e0b; font-family: monospace; font-size: 10px; font-weight: bold;")
                            
                            self.log_event(f"AUTONOMOUS HUNTER-KILLER: Verified continuous carrier at {c_freq:.3f} MHz{matched_vtx_str} (SNR +{c_snr:.1f} dB). Snapping to Stare Mode.")

                            # Retune Kraken immediately so it synchronizes and computes DoA during the dwell window
                            if hasattr(self, 'hk_auto_kraken_cb') and self.hk_auto_kraken_cb.isChecked():
                                if hasattr(self, 'kraken_freq_spin'):
                                    self.kraken_freq_spin.blockSignals(True)
                                    self.kraken_freq_spin.setValue(c_freq)
                                    self.kraken_freq_spin.blockSignals(False)
                                self.push_kraken_hardware_settings()

                            self.mode_selector.setCurrentText("STARE MODE (2MHz)")
                            self.freq_input.setValue(c_freq)
                            self.start_sdr()

            elif mode == "STARE":
                current_time = time.time()
                # Autonomous Hunter-Killer Stare & Intercept Evaluation
                if getattr(self, 'hk_active', False) and self.hk_state == "STARE_INTERCEPT":
                    dwell_sec = (self.hk_dwell_spin.value() if hasattr(self, 'hk_dwell_spin') else 1000) / 1000.0
                    elapsed = current_time - self.hk_stare_start_time
                    
                    if elapsed >= dwell_sec:
                        self.hk_state = "EVALUATING"
                        
                        # Real Signal Verification Gate: Check if an active carrier actually exists on this frequency
                        is_vtx_freq = (5640.0 <= self.hk_target_freq <= 5950.0) or (1080.0 <= self.hk_target_freq <= 1360.0)
                        
                        matched_vtx_chan = None
                        for cname, cfreq in FPV_VIDEO_CHANNELS.items():
                            if abs(self.hk_target_freq - cfreq) <= 2.5:
                                matched_vtx_chan = cname
                                break

                        if is_vtx_freq or matched_vtx_chan:
                            is_real_signal = (peak_power > -88.0)
                            if matched_vtx_chan:
                                mod_type = f"FPV Video [{matched_vtx_chan}]"
                            else:
                                mod_type = f"FPV Video ({self.hk_target_freq:.1f} MHz)"
                        else:
                            is_real_signal = (peak_power > -82.0 and mod_type != "Noise/Inactive" and "Noise" not in mod_type)
                        
                        if not is_real_signal:
                            self.log_event(f"AUTONOMOUS HUNTER-KILLER: Transient burst cleared @ {self.hk_target_freq:.3f} MHz (Noise floor / no sustained carrier). Resuming sweep.")
                        else:
                            score = self.calculate_hk_threat_score(self.hk_target_freq, peak_power, bw, mod_type)
                            if matched_vtx_chan or is_vtx_freq:
                                score = max(85, score) # Force high priority for detected video links!
                            priority = "P3 ADVISORY"
                            if score >= 70: priority = "P1 CRITICAL"
                            elif score >= 45: priority = "P2 HIGH"

                            # If FPV VTX detected, auto-tune FPV Video channel combo
                            if matched_vtx_chan and hasattr(self, 'fpv_channel_combo'):
                                for idx in range(self.fpv_channel_combo.count()):
                                    if matched_vtx_chan in self.fpv_channel_combo.itemText(idx):
                                        self.fpv_channel_combo.blockSignals(True)
                                        self.fpv_channel_combo.setCurrentIndex(idx)
                                        self.fpv_channel_combo.blockSignals(False)
                                        break
                                if hasattr(self, 'v_freq_spin'):
                                    self.v_freq_spin.blockSignals(True)
                                    self.v_freq_spin.setValue(self.hk_target_freq)
                                    self.v_freq_spin.blockSignals(False)

                            # Update metrics HUD
                            if hasattr(self, 'hk_metric_freq'):
                                self.hk_metric_freq.setText(f"Frequency: {self.hk_target_freq:.3f} MHz")
                                self.hk_metric_bw.setText(f"Bandwidth: {bw:.1f} kHz" if bw > 0 else "Bandwidth: ~8.0 MHz (FPV VTX)")
                                self.hk_metric_snr.setText(f"Peak Power: {peak_power:.1f} dBFS")
                                self.hk_metric_pulse.setText(f"Pulse Width: {pulse_ms:.1f} ms" if pulse_ms > 0 else "Pulse: Continuous")
                                self.hk_metric_fingerprint.setText(f"Hardware CVA: 0x{fingerprint}" if fingerprint else "Hardware CVA: 0x----")

                            bearing = getattr(self, 'last_bearing_deg', 0.0)
                            bearing_conf = getattr(self, 'last_bearing_conf', 0.0)
                            if hasattr(self, 'hk_metric_bearing'):
                                self.hk_metric_bearing.setText(f"DoA Bearing: {bearing:05.1f}° (Conf: {bearing_conf:.0f}%)" if bearing_conf > 30.0 else f"DoA Bearing: {bearing:05.1f}°")

                            # Auto Plot to Map
                            if hasattr(self, 'hk_auto_map_cb') and self.hk_auto_map_cb.isChecked() and hasattr(self, 'geo_map_view') and bearing_conf > 30.0:
                                lat = float(self.geo_lat_input.text()) if (hasattr(self, 'geo_lat_input') and self.geo_lat_input.text()) else 51.5074
                                lon = float(self.geo_lon_input.text()) if (hasattr(self, 'geo_lon_input') and self.geo_lon_input.text()) else -0.1278
                                js = f"if (typeof updateBearingLine === 'function') {{ updateBearingLine({lat}, {lon}, {bearing}, 6000, '{'#ef4444' if score >= 70 else '#f59e0b'}'); }}"
                                self.geo_map_view.page().runJavaScript(js)

                            # Add/Update in Priority Queue Table
                            self.add_or_update_hk_queue(self.hk_target_freq, mod_type, score, fingerprint, bearing, peak_power)

                            # Voice / Speech Alert
                            if hasattr(self, 'hk_audio_alert_cb') and self.hk_audio_alert_cb.isChecked() and score >= 60:
                                speak_tactical_alert(f"Warning. Priority {priority} emitter intercepted on {int(self.hk_target_freq)} megahertz. Threat score {score}.")

                            self.log_event(f"HUNTER-KILLER INTERCEPT: {self.hk_target_freq:.3f} MHz | {priority} (Score {score}/100) | {mod_type} | Bearing {bearing:.1f}°")

                        # Operating Mode Next State Action
                        hk_mode_idx = self.hk_mode_combo.currentIndex() if hasattr(self, 'hk_mode_combo') else 0
                        if hk_mode_idx == 2 and is_real_signal: # Target Intercept & Lock
                            self.hk_state = "TRACK_LOCKED"
                            if hasattr(self, 'hk_status_badge'):
                                self.hk_status_badge.setText(f"[ TRACK LOCKED: CONTINUOUS INTERCEPT @ {self.hk_target_freq:.3f} MHz ]")
                                self.hk_status_badge.setStyleSheet("background-color: #060a14; color: #ef4444; font-family: monospace; font-size: 12px; font-weight: bold; padding: 6px; border: 1px solid #ef4444; border-radius: 4px;")
                            # If FPV VTX locked, start video stream
                            if (is_vtx_freq or matched_vtx_chan) and not getattr(self, 'is_video_streaming', False):
                                self.start_video_stream()
                        else: # Full Autonomous / Semi-Autonomous -> Resume Sweep
                            self.hk_state = "HUNTING"
                            s_min, s_max = getattr(self, 'hk_resume_sweep_params', (850.0, 950.0))
                            if hasattr(self, 'hk_status_badge'):
                                self.hk_status_badge.setText(f"[ HUNTER-KILLER: HUNTING SWEEP {s_min:.0f} - {s_max:.0f} MHz ]")
                                self.hk_status_badge.setStyleSheet("background-color: #060a14; color: #10b981; font-family: monospace; font-size: 12px; font-weight: bold; padding: 6px; border: 1px solid #10b981; border-radius: 4px;")
                            if hasattr(self, 'hk_cycle_breadcrumbs'):
                                self.hk_cycle_breadcrumbs.setText("CYCLE: ▶ [ 1. HUNT ] ➔ [ 2. DETECT ] ➔ [ 3. STARE && FP ] ➔ [ 4. DoA VECTOR ] ➔ [ 5. THREAT EVAL ] ➔ [ 6. RESUME ]")
                                self.hk_cycle_breadcrumbs.setStyleSheet("color: #10b981; font-family: monospace; font-size: 10px; font-weight: bold;")
                            
                            self.sweep_start_input.setValue(s_min)
                            self.sweep_end_input.setValue(s_max)
                            self.mode_selector.setCurrentText("SWEEP MODE (Wideband)")
                            self.start_sdr()

                # Watchlist Matching
                if bw > 0:
                    for sig in self.watchlist:
                        if sig["min_bw"] <= bw <= sig["max_bw"] and sig["mod"] == mod_type:
                            mod_type = f" {sig['name']} MATCH"
                            break
                # Peak Anomaly tracking (Wideband unexpected anomaly detector)
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
                    p_freq_mhz = self.bin_to_freq(p) / 1e6

                    # 1. FHSS / LoRa Suppression: Suppress peak anomalies inside active FHSS networks
                    is_in_fhss = False
                    for b_info in getattr(self, 'active_fhss_bands', {}).values():
                        if (current_time - b_info.get('last_seen', 0) < 6.0) and (b_info['f_min'] - 2.0 <= p_freq_mhz <= b_info['f_max'] + 2.0):
                            is_in_fhss = True
                            break
                    if is_in_fhss:
                        continue

                    # 2. Standard UAV Drone Control Bands (860-930 MHz, 2400-2485 MHz): Auto-suppressed from anomaly spam
                    if (860.0 <= p_freq_mhz <= 930.0) or (2400.0 <= p_freq_mhz <= 2485.0):
                        continue

                    # 3. In Stare Mode: The center carrier is the target being monitored, not an unexpected anomaly
                    if mode == "STARE":
                        center_f = self.freq_input.value()
                        if abs(p_freq_mhz - center_f) <= 0.8:
                            continue

                    is_new = True
                    for active_freq, last_time in list(self.active_events.items()):
                        # Wideband Spatial Debounce: +/- 1.0 MHz with a 15.0-second cooldown
                        if abs(p_freq_mhz - active_freq) <= 1.0 and (current_time - last_time < 15.0):
                            is_new = False
                            self.active_events[active_freq] = current_time
                            break
                            
                    if is_new:
                        self.log_event(f"ANOMALY: Unexpected spectral peak detected at {p_freq_mhz:.3f} MHz")
                        self.active_events[p_freq_mhz] = current_time
                        
                self.active_events = {k: v for k, v in self.active_events.items() if current_time - v < 20.0}

                # Watchlist & Fingerprint
                if "🚨" in mod_type and mod_type != self.last_mod_type:
                    self.log_event(mod_type)
                    
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
                                        self.log_event(f"TARGET CLOSING: {db_entry.get('name')} (Delta: {diff:.1f}dB)")
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
                            
                            self.log_event(f"LINK DETECTED: 0x{source} replied to by 0x{target}")
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
                if bw > 0 or (mod_type != "Noise/Inactive" and "Noise" not in mod_type):
                    self.mod_label.setText(f"Modulation: {mod_type} | Est. BW: {bw/1000:.0f} kHz")
                else:
                    self.mod_label.setText("Modulation: Standby / Idle (Carrier Gate Active)")
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
            self.log_event("Stopped Native Audio Demodulator.")
            return

        self.log_event(f"Launching Native FM Demodulator on {freq_mhz:.3f} MHz...")
        self.freq_input.setValue(freq_mhz)
        self.audio_playing = True
        self.start_sdr()
        
        self.demod_btn.setText(" STOP AUDIO")
        self.force_demod_btn.setText(" STOP AUDIO")

    def closeEvent(self, event):
        self.flush_dirty_state_to_disk()
        if hasattr(self, 'kraken_thread') and self.kraken_thread:
            self.kraken_thread.stop()
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
    app.setFont(QFont("Consolas", 10))
    window = CEMAApp()
    window.show()
    sys.exit(app.exec())
