"""
Tactical Deep Learning Automatic Modulation Classifier (Neural AMC)
CEMA Utility - Air-Gapped On-Device RF Intelligence Engine

Performs real-time modulation classification on raw complex I/Q data
using a lightweight 1D Residual Convolutional Neural Network (ResNet-1D).
Runs 100% locally and air-gapped on CPU (< 1ms latency per buffer).
"""

import os
import time
import threading
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

AMC_CLASSES = [
    "LoRa / CSS (Chirp Spread Spectrum)",
    "GFSK / 2-FSK (Crossfire / Telemetry)",
    "4-FSK / C4FM (Digital Mobile Radio)",
    "BPSK / QPSK (Digital Data Link)",
    "16-QAM / 64-QAM (Digital Video)",
    "OFDM / Wideband Digital (OcuSync / HD Link)",
    "Analog FM / WFM (FPV Video / Voice)",
    "Analog AM / DSB (Aviation / Voice)",
    "CW / Carrier Spike (Beacon / Jammer)"
]

TACTICAL_HINTS = {
    0: "ExpressLRS / Meshtastic / LoRa Drone Control Link",
    1: "TBS Crossfire / Drone Telemetry / RC Link",
    2: "Digital Mobile Radio / P25 / C4FM Protocol",
    3: "Military / Commercial Digital Data Link",
    4: "Digital FPV Video Carrier (Walksnail / Avatar)",
    5: "DJI OcuSync / Wi-Fi HD Video Downlink",
    6: "Analog FPV Video (NTSC/PAL) / FM Audio",
    7: "Aviation VHF AM / DSB Carrier",
    8: "Continuous Wave / CW Beacon / Unmodulated Jammer"
}

class ResidualBlock1D(nn.Module):
    def __init__(self, in_ch, out_ch, stride=1):
        super().__init__()
        self.conv1 = nn.Conv1d(in_ch, out_ch, kernel_size=5, stride=stride, padding=2, bias=False)
        self.bn1 = nn.BatchNorm1d(out_ch)
        self.conv2 = nn.Conv1d(out_ch, out_ch, kernel_size=5, stride=1, padding=2, bias=False)
        self.bn2 = nn.BatchNorm1d(out_ch)

        self.shortcut = nn.Sequential()
        if stride != 1 or in_ch != out_ch:
            self.shortcut = nn.Sequential(
                nn.Conv1d(in_ch, out_ch, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm1d(out_ch)
            )

    def forward(self, x):
        res = self.shortcut(x)
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        return F.relu(out + res)

class TacticalAMCNet(nn.Module):
    def __init__(self, num_classes=9):
        super().__init__()
        # Input shape: (Batch, 4, 1024) -> [I_norm, Q_norm, Mag_norm, Phase_Delta]
        self.stem = nn.Sequential(
            nn.Conv1d(4, 32, kernel_size=7, stride=2, padding=3, bias=False),
            nn.BatchNorm1d(32),
            nn.ReLU(),
            nn.MaxPool1d(2)
        )
        self.layer1 = ResidualBlock1D(32, 64, stride=2)
        self.layer2 = ResidualBlock1D(64, 128, stride=2)
        self.layer3 = ResidualBlock1D(128, 128, stride=2)
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.fc = nn.Sequential(
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(64, num_classes)
        )

    def forward(self, x):
        x = self.stem(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.pool(x).squeeze(-1)
        return self.fc(x)

def extract_4ch_features(iq_complex, target_len=1024):
    if len(iq_complex) < target_len:
        pad = target_len - len(iq_complex)
        sig = np.pad(iq_complex, (0, pad), mode='constant')
    else:
        sig = iq_complex[:target_len]

    i_ch = np.real(sig).astype(np.float32)
    q_ch = np.imag(sig).astype(np.float32)

    i_std = np.std(i_ch) + 1e-6
    q_std = np.std(q_ch) + 1e-6
    i_norm = (i_ch - np.mean(i_ch)) / i_std
    q_norm = (q_ch - np.mean(q_ch)) / q_std

    mag = np.abs(sig).astype(np.float32)
    mag_norm = (mag - np.mean(mag)) / (np.std(mag) + 1e-6)

    delayed = np.roll(sig, 1)
    delayed[0] = sig[0]
    d_phase = np.angle(sig * np.conj(delayed)).astype(np.float32)
    d_phase_norm = d_phase / np.pi

    features = np.stack([i_norm, q_norm, mag_norm, d_phase_norm], axis=0)
    return features

def _synthesize_training_batch(batch_size=256, seq_len=1024):
    y = np.random.randint(0, len(AMC_CLASSES), size=batch_size)
    t = np.arange(seq_len, dtype=np.float32)
    sigs = np.zeros((batch_size, seq_len), dtype=np.complex64)

    for i in range(batch_size):
        c = y[i]
        snr_db = np.random.uniform(4.0, 30.0)
        snr_linear = 10.0 ** (snr_db / 10.0)
        fo = np.random.uniform(-0.06, 0.06)
        carrier = np.exp(1j * (2 * np.pi * fo * t + np.random.uniform(0, 2*np.pi)))

        if c == 0:  # LoRa / CSS
            bw = np.random.uniform(0.1, 0.4)
            chirp_rate = bw / seq_len
            phase = 2 * np.pi * (0.5 * chirp_rate * (t ** 2))
            sig = np.exp(1j * phase) * carrier
        elif c == 1:  # GFSK / 2-FSK
            sym_len = np.random.choice([16, 32, 64])
            num_syms = int(np.ceil(seq_len / sym_len))
            bits = np.random.choice([-1.0, 1.0], size=num_syms)
            sig_bits = np.repeat(bits, sym_len)[:seq_len]
            dev = np.random.uniform(0.04, 0.09)
            phase = 2 * np.pi * np.cumsum(sig_bits * dev)
            sig = np.exp(1j * phase) * carrier
        elif c == 2:  # 4-FSK
            sym_len = np.random.choice([16, 32, 64])
            num_syms = int(np.ceil(seq_len / sym_len))
            symbols = np.random.choice([-3.0, -1.0, 1.0, 3.0], size=num_syms)
            sig_syms = np.repeat(symbols, sym_len)[:seq_len]
            dev = np.random.uniform(0.015, 0.035)
            phase = 2 * np.pi * np.cumsum(sig_syms * dev)
            sig = np.exp(1j * phase) * carrier
        elif c == 3:  # BPSK / QPSK
            sym_len = np.random.choice([16, 32, 64])
            num_syms = int(np.ceil(seq_len / sym_len))
            constel = np.array([1+1j, -1+1j, -1-1j, 1-1j]) / np.sqrt(2)
            syms = np.random.choice(constel, size=num_syms)
            sig = np.repeat(syms, sym_len)[:seq_len] * carrier
        elif c == 4:  # 16-QAM
            sym_len = np.random.choice([16, 32])
            num_syms = int(np.ceil(seq_len / sym_len))
            grid = np.array([-3, -1, 1, 3])
            constel = (np.repeat(grid, 4) + 1j * np.tile(grid, 4)) / np.sqrt(10)
            syms = np.random.choice(constel, size=num_syms)
            sig = np.repeat(syms, sym_len)[:seq_len] * carrier
        elif c == 5:  # OFDM
            num_sub = 64
            symbols = (np.random.randn(num_sub) + 1j * np.random.randn(num_sub)) / np.sqrt(2)
            ofdm_time = np.fft.ifft(symbols)
            repeats = int(np.ceil(seq_len / num_sub))
            sig = np.tile(ofdm_time, repeats)[:seq_len] * carrier
        elif c == 6:  # Analog FM
            mod_sig = np.sin(2 * np.pi * 0.003 * t) + 0.5 * np.cos(2 * np.pi * 0.008 * t)
            phase = 2 * np.pi * np.cumsum(mod_sig * 0.03)
            sig = np.exp(1j * phase) * carrier
        elif c == 7:  # Analog AM
            mod_sig = 0.5 + 0.5 * np.sin(2 * np.pi * 0.004 * t)
            sig = mod_sig * carrier
        else:  # CW
            sig = carrier

        noise = (np.random.randn(seq_len) + 1j * np.random.randn(seq_len)) / np.sqrt(2)
        p_sig = np.mean(np.abs(sig)**2) + 1e-12
        target_noise_p = p_sig / snr_linear
        scaled_noise = noise * np.sqrt(target_noise_p)
        sigs[i, :] = sig + scaled_noise

    X = np.zeros((batch_size, 4, seq_len), dtype=np.float32)
    for i in range(batch_size):
        X[i] = extract_4ch_features(sigs[i], target_len=seq_len)

    return torch.from_numpy(X), torch.from_numpy(y).long()

class TacticalNeuralAMC:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TacticalNeuralAMC, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, model_dir=None):
        if getattr(self, '_initialized', False):
            return
        self.model_dir = model_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models')
        os.makedirs(self.model_dir, exist_ok=True)
        self.model_path = os.path.join(self.model_dir, 'tactical_amc_resnet1d.pt')
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = TacticalAMCNet(num_classes=len(AMC_CLASSES)).to(self.device)
        self.is_ready = False
        self.training_in_progress = False

        self._load_or_train_model()
        self._initialized = True

    def _load_or_train_model(self):
        if os.path.exists(self.model_path):
            try:
                state_dict = torch.load(self.model_path, map_location=self.device, weights_only=True)
                self.model.load_state_dict(state_dict)
                self.model.eval()
                self.is_ready = True
                return
            except Exception:
                pass

        threading.Thread(target=self._train_and_save, daemon=True).start()

    def _train_and_save(self, epochs=35):
        self.training_in_progress = True
        try:
            optimizer = torch.optim.AdamW(self.model.parameters(), lr=0.003, weight_decay=1e-4)
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
            criterion = nn.CrossEntropyLoss()

            for _ in range(epochs):
                self.model.train()
                X_b, y_b = _synthesize_training_batch(batch_size=256, seq_len=1024)
                X_b, y_b = X_b.to(self.device), y_b.to(self.device)
                optimizer.zero_grad()
                out = self.model(X_b)
                loss = criterion(out, y_b)
                loss.backward()
                optimizer.step()
                scheduler.step()

            self.model.eval()
            torch.save(self.model.state_dict(), self.model_path)
            self.is_ready = True
        except Exception:
            pass
        finally:
            self.training_in_progress = False

    def classify(self, iq_complex, snr_db=20.0):
        if not self.is_ready or len(iq_complex) < 128:
            return 'UNKNOWN', 0.0, '', {}

        if snr_db < 10.0:
            return 'Noise / Floor', 95.0, 'Background thermal noise', {'Noise': 95.0}

        try:
            feat = extract_4ch_features(iq_complex, target_len=1024)
            x_tensor = torch.from_numpy(feat).unsqueeze(0).to(self.device)

            with torch.no_grad():
                logits = self.model(x_tensor)
                probs = F.softmax(logits, dim=1).cpu().numpy()[0]

            top_idx = int(np.argmax(probs))
            conf = float(probs[top_idx] * 100.0)
            class_name = AMC_CLASSES[top_idx]
            tactical_note = TACTICAL_HINTS.get(top_idx, '')

            top_probs = {AMC_CLASSES[i]: round(float(probs[i] * 100.0), 1) for i in np.argsort(probs)[::-1][:3]}
            return class_name, round(conf, 1), tactical_note, top_probs
        except Exception:
            return 'UNKNOWN', 0.0, '', {}

_amc_instance = None

def get_neural_amc():
    global _amc_instance
    if _amc_instance is None:
        _amc_instance = TacticalNeuralAMC()
    return _amc_instance