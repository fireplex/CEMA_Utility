/**
 * CEMA Tactical Suite - Mobile Tactical Web Engine & PWA
 * Real-Time Canvas HUDs, WebSocket LoRa Bridge & DSP Streamer
 */

// Polyfill for Canvas roundRect across older Android WebViews
if (typeof CanvasRenderingContext2D !== 'undefined' && !CanvasRenderingContext2D.prototype.roundRect) {
  CanvasRenderingContext2D.prototype.roundRect = function(x, y, w, h, r) {
    if (!r) r = 0;
    if (typeof r === 'number') r = [r, r, r, r];
    const r0 = r[0] || 0, r1 = r[1] || 0, r2 = r[2] || 0, r3 = r[3] || 0;
    this.moveTo(x + r0, y);
    this.lineTo(x + w - r1, y);
    this.quadraticCurveTo(x + w, y, x + w, y + r1);
    this.lineTo(x + w, y + h - r2);
    this.quadraticCurveTo(x + w, y + h, x + w - r2, y + h);
    this.lineTo(x + r3, y + h);
    this.quadraticCurveTo(x, y + h, x, y + h - r3);
    this.lineTo(x, y + r0);
    this.quadraticCurveTo(x, y, x + r0, y);
    this.closePath();
    return this;
  };
}

// Global UI & State
// ----------------------------------------------------------------------------
let ws = null;
let voiceEnabled = false;
let sdrRunning = false;
let deferredInstallPrompt = null;

// Smoothed State for 60fps Canvas Interpolation
const state = {
  // LoRa / Node
  connected: false,
  port: "None",
  rate_name: "AUTO",
  target_pilot: "AUTO",
  is_armed: false,
  
  // Channels (CH1..CH16)
  channels: [1500, 1500, 988, 1500, 1000, 1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500],
  smooth_ch: [1500, 1500, 988, 1500], // [Yaw, Pitch, Thr, Rol]
  
  // Attitude & Dynamics
  pitch: 0.0,
  roll: 0.0,
  yaw: 0.0,
  smooth_pitch: 0.0,
  smooth_roll: 0.0,
  smooth_yaw: 0.0,
  
  // Telemetry
  rssi: -115.0,
  snr: 0.0,
  drone_lq: 0,
  voltage: 0.0,
  bat_pct: 0.0,
  flight_mode: "STANDBY",
  maneuver: "DISARMED / MOTOR SHUTDOWN",
  maneuver_detail: "Motors Idle | Throttle: 0% | Cyclic Rate: 0 us/s",
  
  // HackRF Spectrum
  sdr_available: false,
  sdr_fft: [],

  // KrakenSDR Direction Finding
  kraken_enabled: false,
  kraken_bearing: 0.0,
  kraken_smooth_bearing: 0.0,
  kraken_confidence: 0.0,
  kraken_snr: 0.0,
  kraken_locked: false,
  kraken_spectrum: [],
  kraken_age: 999.0
};

let krakenRunning = false;
let vtxRunning = false;
let vtxStreamActive = false;

// ----------------------------------------------------------------------------
// PWA & Service Worker Setup
// ----------------------------------------------------------------------------
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(err => console.log('SW reg error:', err));
  });
}

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  deferredInstallPrompt = e;
  const btn = document.getElementById('btnInstall');
  if (btn) btn.style.display = 'inline-flex';
});

document.getElementById('btnInstall')?.addEventListener('click', async () => {
  if (deferredInstallPrompt) {
    deferredInstallPrompt.prompt();
    const { outcome } = await deferredInstallPrompt.userChoice;
    if (outcome === 'accepted') {
      document.getElementById('btnInstall').style.display = 'none';
    }
    deferredInstallPrompt = null;
  }
});

// ----------------------------------------------------------------------------
// Navigation Tabs
// ----------------------------------------------------------------------------
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    
    btn.classList.add('active');
    const targetId = btn.dataset.tab;
    const targetPanel = document.getElementById(targetId);
    if (targetPanel) targetPanel.classList.add('active');
    
    resizeCanvases();
  });
});

// ----------------------------------------------------------------------------
// Voice Synthesis Engine (TTS)
// ----------------------------------------------------------------------------
const btnVoice = document.getElementById('btnVoice');
btnVoice?.addEventListener('click', () => {
  voiceEnabled = !voiceEnabled;
  btnVoice.textContent = voiceEnabled ? "🔊 VOICE: ON" : "🔇 VOICE: OFF";
  btnVoice.style.color = voiceEnabled ? "var(--cyan-bright)" : "var(--text-muted)";
});

function speakTactical(text) {
  if (!voiceEnabled || !('speechSynthesis' in window)) return;
  try {
    window.speechSynthesis.cancel(); // Don't queue up a backlog
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.05;
    utterance.pitch = 0.95;
    window.speechSynthesis.speak(utterance);
  } catch (e) {
    console.warn("TTS error:", e);
  }
}

// ----------------------------------------------------------------------------
// Channel Diagnostic Matrix UI Setup
// ----------------------------------------------------------------------------
const CH_NAMES = [
  "CH1 (Roll)", "CH2 (Pitch)", "CH3 (Throttle)", "CH4 (Yaw)",
  "CH5 (AUX1/Arm)", "CH6 (AUX2/Mode)", "CH7 (AUX3/VTX)", "CH8 (AUX4/Rescue)",
  "CH9 (AUX5)", "CH10 (AUX6)", "CH11 (AUX7)", "CH12 (AUX8)",
  "CH13 (AUX9)", "CH14 (AUX10)", "CH15 (AUX11)", "CH16 (AUX12)"
];

function initChannelGrid() {
  const container = document.getElementById('channelGrid');
  if (!container) return;
  container.innerHTML = '';

  CH_NAMES.forEach((name, idx) => {
    const row = document.createElement('div');
    row.className = 'channel-row';
    row.innerHTML = `
      <div class="channel-name">${name}</div>
      <div class="channel-bar-bg">
        <div id="chFill_${idx}" class="channel-bar-fill" style="width: 50%;"></div>
      </div>
      <div id="chVal_${idx}" class="channel-val">1500us</div>
    `;
    container.appendChild(row);
  });
}
initChannelGrid();

// ----------------------------------------------------------------------------
// WebSocket Connection & Data Ingestion
// ----------------------------------------------------------------------------
function connectWebSocket() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${location.host}/ws`;
  
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    updateBadge('wsBadge', 'online', 'SYS: CONNECTED');
  };

  ws.onclose = () => {
    updateBadge('wsBadge', 'warn', 'SYS: RECONNECTING...');
    setTimeout(connectWebSocket, 2000);
  };

  ws.onerror = () => {
    ws.close();
  };

  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      handleMessage(msg);
    } catch (e) {
      console.error("WS Parse Error:", e);
    }
  };
}

function handleMessage(msg) {
  if (msg.type === 'init') {
    // Initial backlog load
    if (msg.sitrep) {
      msg.sitrep.forEach(addSitrepEntry);
    }
    if (msg.pilots) {
      updatePilotsTable(msg.pilots);
    }
  } else if (msg.type === 'telemetry') {
    applyTelemetry(msg);
  } else if (msg.type === 'sitrep') {
    addSitrepEntry(msg.entry);
  }
}

function applyTelemetry(data) {
  const n = data.node;
  const rc = data.rc;
  const m = data.maneuver;
  const tlm = data.telemetry;
  const lk = data.link;
  const sys = data.system;
  const sdr = data.sdr;

  // Node & Badges
  state.connected = n.connected;
  state.port = n.port;
  state.rate_name = n.rate_name;
  state.target_pilot = n.target_pilot;
  state.is_armed = rc.armed;

  if (n.connected) {
    updateBadge('loraBadge', 'online', `LORA: ${n.port.replace('/dev/', '')}`);
  } else {
    updateBadge('loraBadge', '', 'LORA: STANDBY');
  }

  document.getElementById('rateText').textContent = n.rate_name;

  // Target Intercept & Pilot Lock Status HUD on Tab 1
  const targetLockHUD = document.getElementById('targetLockHUD');
  const targetLockTitle = document.getElementById('targetLockTitle');
  const targetLockBadge = document.getElementById('targetLockBadge');
  const targetLockIcon = document.getElementById('targetLockIcon');
  const hudTargetRate = document.getElementById('hudTargetRate');
  const hudTargetRssi = document.getElementById('hudTargetRssi');
  const hudTargetBeacons = document.getElementById('hudTargetBeacons');
  const btnQuickUnlock = document.getElementById('btnQuickUnlock');
  const pilotBadge = document.getElementById('pilotBadge');
  const pilotText = document.getElementById('pilotText');

  const lockedTarget = n.target_pilot || "AUTO";
  const isLocked = Boolean(lockedTarget && lockedTarget !== "AUTO");
  const pilots = data.pilots || [];

  // Find pilot object matching the locked target or first active pilot
  let activePilot = null;
  if (isLocked) {
    activePilot = pilots.find(p => p.uid === lockedTarget || (p.u3 && lockedTarget.includes(`${p.u3},${p.u4},${p.u5}`)));
  } else if (pilots.length > 0) {
    activePilot = pilots[0];
  }

  if (targetLockHUD) {
    if (n.scan_mode) {
      targetLockHUD.className = 'card target-lock-hud';
      targetLockHUD.style.borderLeftColor = 'var(--amber-bright)';
      if (targetLockIcon) targetLockIcon.textContent = '🔍';
      if (targetLockTitle) targetLockTitle.textContent = `AIRSPACE SCAN ACTIVE: ${n.scan_rate || '50Hz'} @ ${n.scan_band || 'AUTO'}`;
      if (hudTargetRate) hudTargetRate.textContent = n.scan_rate || n.rate_name || '--';
      if (hudTargetRssi) hudTargetRssi.textContent = '-- dBm';
      if (hudTargetBeacons) hudTargetBeacons.textContent = String(pilots.length);
      if (targetLockBadge) {
        targetLockBadge.className = 'badge warn';
        targetLockBadge.textContent = 'SCANNING';
      }
      if (btnQuickUnlock) btnQuickUnlock.style.display = 'none';
    } else if (isLocked) {
      targetLockHUD.className = 'card target-lock-hud locked';
      targetLockHUD.style.borderLeftColor = '';
      if (targetLockIcon) targetLockIcon.textContent = '🎯';
      if (targetLockTitle) targetLockTitle.textContent = `TARGET LOCKED: UID ${lockedTarget}`;
      if (hudTargetRate) hudTargetRate.textContent = (activePilot ? activePilot.rate : n.rate_name) || '--';
      if (hudTargetRssi) {
        const rVal = activePilot ? activePilot.rssi : rc.rssi;
        hudTargetRssi.textContent = `${(rVal !== undefined && rVal !== null) ? rVal.toFixed(0) : '--'} dBm`;
      }
      if (hudTargetBeacons) hudTargetBeacons.textContent = activePilot ? activePilot.count : '--';
      if (targetLockBadge) {
        targetLockBadge.className = 'badge online';
        targetLockBadge.textContent = 'LOCKED';
      }
      if (btnQuickUnlock) btnQuickUnlock.style.display = 'inline-block';
    } else if (activePilot) {
      targetLockHUD.className = 'card target-lock-hud acquiring';
      targetLockHUD.style.borderLeftColor = '';
      if (targetLockIcon) targetLockIcon.textContent = '🎯';
      if (targetLockTitle) targetLockTitle.textContent = `AUTO-ACQUIRED: UID ${activePilot.uid}`;
      if (hudTargetRate) hudTargetRate.textContent = activePilot.rate || n.rate_name || '--';
      if (hudTargetRssi) hudTargetRssi.textContent = `${(activePilot.rssi !== undefined && activePilot.rssi !== null) ? activePilot.rssi.toFixed(0) : rc.rssi.toFixed(0)} dBm`;
      if (hudTargetBeacons) hudTargetBeacons.textContent = activePilot.count;
      if (targetLockBadge) {
        targetLockBadge.className = 'badge online';
        targetLockBadge.textContent = 'TRACKING';
      }
      if (btnQuickUnlock) btnQuickUnlock.style.display = 'none';
    } else {
      targetLockHUD.className = 'card target-lock-hud';
      targetLockHUD.style.borderLeftColor = '';
      if (targetLockIcon) targetLockIcon.textContent = '🎯';
      if (targetLockTitle) targetLockTitle.textContent = 'AUTO-ACQUIRE (MONITORING ALL PILOTS)';
      if (hudTargetRate) hudTargetRate.textContent = n.rate_name || '--';
      if (hudTargetRssi) hudTargetRssi.textContent = `${(rc.rssi !== undefined && rc.rssi !== null) ? rc.rssi.toFixed(0) : '--'} dBm`;
      if (hudTargetBeacons) hudTargetBeacons.textContent = '0';
      if (targetLockBadge) {
        targetLockBadge.className = 'badge';
        targetLockBadge.textContent = 'STANDBY';
      }
      if (btnQuickUnlock) btnQuickUnlock.style.display = 'none';
    }
  }

  // Airspace Survey Scan UI on Tab 2
  state.scan_mode = Boolean(n.scan_mode);
  state.scan_rate = n.scan_rate || "50HZ";
  state.scan_band = n.scan_band || "AUTO";

  const scanStatusBadge = document.getElementById('scanStatusBadge');
  const btnToggleScan = document.getElementById('btnToggleScan');
  if (scanStatusBadge && btnToggleScan) {
    if (state.scan_mode) {
      scanStatusBadge.className = 'badge warn';
      scanStatusBadge.textContent = `SCANNING (${state.scan_rate} @ ${state.scan_band})`;
      btnToggleScan.textContent = "⏹ STOP SCAN";
      btnToggleScan.style.backgroundColor = "var(--red-bright)";
    } else {
      scanStatusBadge.className = 'badge';
      scanStatusBadge.textContent = "SCAN: STANDBY";
      btnToggleScan.textContent = "🔍 START SCAN";
      btnToggleScan.style.backgroundColor = "";
    }
  }

  // Header Pilot Target Badge
  if (pilotText) {
    if (n.scan_mode) {
      pilotText.textContent = `🔍 SCANNING (${state.scan_rate})`;
      if (pilotBadge) pilotBadge.className = 'badge warn';
    } else if (isLocked) {
      pilotText.textContent = `🎯 ${lockedTarget} (LOCKED)`;
      if (pilotBadge) pilotBadge.className = 'badge online';
    } else if (activePilot) {
      pilotText.textContent = `AUTO (${activePilot.uid})`;
      if (pilotBadge) pilotBadge.className = 'badge';
    } else {
      pilotText.textContent = 'AUTO';
      if (pilotBadge) pilotBadge.className = 'badge';
    }
  }

  const armBadge = document.getElementById('armBadge');
  const armText = document.getElementById('armText');
  if (rc.armed) {
    armBadge.className = 'badge armed';
    armText.textContent = 'ARMED (MOTORS ON)';
  } else {
    armBadge.className = 'badge';
    armText.textContent = 'DISARMED (SAFE)';
  }

  // System Stats
  document.getElementById('sysText').textContent = `${sys.cpu_temp}°C | LOAD:${sys.load}`;

  // Port Selector & Active Port Badge
  const activePortBadge = document.getElementById('activePortBadge');
  if (activePortBadge) {
    activePortBadge.textContent = n.connected ? `CONNECTED: ${n.port}` : `SEARCHING... (${n.port})`;
    activePortBadge.className = n.connected ? 'badge online' : 'badge';
  }
  const portSelect = document.getElementById('portSelect');
  if (portSelect && n.available_ports && n.available_ports.length > 0) {
    const existing = Array.from(portSelect.options).map(o => o.value).filter(v => v);
    if (JSON.stringify(existing) !== JSON.stringify(n.available_ports)) {
      portSelect.innerHTML = '<option value="">Auto-Detect USB Serial</option>';
      n.available_ports.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p;
        opt.textContent = p + (p.includes('USB') ? ' (LoRa Node / CP210x)' : '');
        if (p === n.port) opt.selected = true;
        portSelect.appendChild(opt);
      });
    }
  }

  // Maneuver Banner
  const mTitle = document.getElementById('maneuverTitle');
  const mDetail = document.getElementById('maneuverDetail');
  mTitle.textContent = m.name;
  mDetail.textContent = m.detail;
  if (rc.armed) {
    mTitle.classList.add('armed');
  } else {
    mTitle.classList.remove('armed');
  }

  // RC Channels
  state.channels = rc.channels;
  for (let i = 0; i < 16; i++) {
    const val = rc.channels[i] || 1500;
    const fill = document.getElementById(`chFill_${i}`);
    const lbl = document.getElementById(`chVal_${i}`);
    if (fill && lbl) {
      const pct = Math.max(0, Math.min(100, (val - 988) / 10.24));
      fill.style.width = `${pct}%`;
      lbl.textContent = `${val}us`;
      if (i === 4 && rc.armed) {
        fill.classList.add('armed');
      } else {
        fill.classList.remove('armed');
      }
    }
  }

  // Targets for 60fps Gimbal interpolation
  // Mode 2: CH1=Yaw, CH2=Pitch, CH3=Throttle, CH4=Roll
  state.smooth_ch[0] += (rc.channels[0] - state.smooth_ch[0]) * 0.4;
  state.smooth_ch[1] += (rc.channels[1] - state.smooth_ch[1]) * 0.4;
  state.smooth_ch[2] += (rc.channels[2] - state.smooth_ch[2]) * 0.4;
  state.smooth_ch[3] += (rc.channels[3] - state.smooth_ch[3]) * 0.4;

  // Gimbal text readout
  document.getElementById('gimbalReadout').textContent = 
    `THR: ${rc.thr_pct}% | YAW: ${rc.channels[0]}us | PIT: ${rc.channels[1]}us | ROL: ${rc.channels[3]}us`;

  // Attitude
  state.pitch = tlm.pitch || 0.0;
  state.roll = tlm.roll || 0.0;
  state.yaw = tlm.yaw || 0.0;
  state.smooth_pitch += (state.pitch - state.smooth_pitch) * 0.3;
  state.smooth_roll += (state.roll - state.smooth_roll) * 0.3;
  state.smooth_yaw += (state.yaw - state.smooth_yaw) * 0.3;

  document.getElementById('attitudeReadout').textContent = 
    `P: ${state.pitch >= 0 ? '+' : ''}${state.pitch.toFixed(1)}° | R: ${state.roll >= 0 ? '+' : ''}${state.roll.toFixed(1)}° | Y: ${state.yaw.toFixed(0)}°`;

  // Overview metrics
  document.getElementById('mRssi').textContent = rc.rssi.toFixed(0);
  document.getElementById('mSnr').textContent = (rc.snr >= 0 ? '+' : '') + rc.snr.toFixed(1);
  document.getElementById('mVolt').textContent = tlm.voltage.toFixed(1);
  document.getElementById('mBatPct').textContent = tlm.bat_pct.toFixed(0);
  document.getElementById('mLq').textContent = lk.lq;
  document.getElementById('mPwr').textContent = lk.power_mw;
  document.getElementById('mMode').textContent = tlm.mode || "STANDBY";
  document.getElementById('mSats').textContent = `${tlm.gps_sats} Sats`;

  // Active Radio Config text
  document.getElementById('rateDetailText').textContent = 
    `${n.rate_name} | SF${n.sf} | BW:${n.bw_khz}kHz | Interval:${n.interval_us}us`;

  // Airspace Pilots Table
  if (data.pilots) {
    updatePilotsTable(data.pilots);
  }

  // Serial Console Diagnostics
  const rxText = document.getElementById('rxCountText');
  if (rxText) rxText.textContent = `RX: ${n.rx_count} frames`;

  const consoleEl = document.getElementById('serialConsole');
  if (consoleEl && n.last_serial_line && n.last_serial_line !== state.last_serial_line) {
    state.last_serial_line = n.last_serial_line;
    const line = n.last_serial_line;
    // Rate-limit noisy per-packet RC lines (decoded sticks stream up to ~25/s) to
    // ~1/sec so they don't flood the console; always show other events immediately.
    const isRC = line.startsWith('[RC ');
    const now = Date.now();
    if (!isRC || (now - (state._lastRcConsoleMs || 0) >= 1000)) {
      if (isRC) state._lastRcConsoleMs = now;
      // Only autoscroll if the user is already near the bottom (don't yank while
      // they're scrolled up reading history).
      const nearBottom = (consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight) < 40;
      const lineDiv = document.createElement('div');
      lineDiv.style.fontFamily = "var(--font-mono)";
      lineDiv.style.fontSize = "10.5px";
      lineDiv.style.color = "var(--text-muted)";
      lineDiv.style.whiteSpace = "nowrap";
      lineDiv.style.overflow = "hidden";
      lineDiv.style.textOverflow = "ellipsis";
      lineDiv.textContent = `[${new Date().toLocaleTimeString()}] ${line}`;
      consoleEl.appendChild(lineDiv);
      while (consoleEl.children.length > 50) consoleEl.removeChild(consoleEl.firstChild);
      if (nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
    }
  }

  // SDR Status & Device Information
  state.sdr_available = sdr.available;
  state.sdr_running = sdr.running;
  state.sdr_freq_mhz = sdr.freq_mhz;
  if (sdr.fft && sdr.fft.length > 0) {
    state.sdr_fft = sdr.fft;
    state.fftSeq = (state.fftSeq || 0) + 1;
  }
  sdrRunning = sdr.running;

  // Sync Start/Stop button state
  const btnToggleSdr = document.getElementById('btnToggleSdr');
  if (btnToggleSdr) {
    if (sdr.running) {
      btnToggleSdr.textContent = "STOP SPECTRUM";
      btnToggleSdr.style.backgroundColor = "var(--red-bright)";
    } else {
      btnToggleSdr.textContent = "START SPECTRUM";
      btnToggleSdr.style.backgroundColor = "var(--cyan-dim)";
    }
  }

  // Header SDR Badge
  if (sdr.available) {
    const sdrName = sdr.board ? sdr.board.replace('HackRF ', '') : 'ONE';
    updateBadge('sdrBadge', 'online', `SDR: ${sdrName}`);
  } else {
    updateBadge('sdrBadge', '', 'SDR: STANDBY');
  }

  // SDR Hardware Badge
  const sdrHardwareBadge = document.getElementById('sdrHardwareBadge');
  if (sdrHardwareBadge) {
    if (sdr.available) {
      sdrHardwareBadge.className = 'badge online';
      sdrHardwareBadge.textContent = sdr.running ? 'HARDWARE: ONLINE (ACTIVE)' : 'HARDWARE: READY';
    } else {
      sdrHardwareBadge.className = 'badge';
      sdrHardwareBadge.textContent = 'HARDWARE: NO SDR FOUND';
    }
  }

  // SDR Streaming Badge
  const sdrStreamingBadge = document.getElementById('sdrStreamingBadge');
  if (sdrStreamingBadge) {
    if (sdr.running) {
      sdrStreamingBadge.className = 'badge online';
      sdrStreamingBadge.textContent = `STREAMING (${sdr.freq_mhz} MHz)`;
    } else {
      sdrStreamingBadge.className = 'badge';
      sdrStreamingBadge.textContent = 'STANDBY';
    }
  }

  // Hardware Metadata labels
  const sdrBoardText = document.getElementById('sdrBoardText');
  if (sdrBoardText) sdrBoardText.textContent = sdr.board || '--';
  const sdrRevisionText = document.getElementById('sdrRevisionText');
  if (sdrRevisionText) sdrRevisionText.textContent = sdr.revision || '--';
  const sdrFirmwareText = document.getElementById('sdrFirmwareText');
  if (sdrFirmwareText) sdrFirmwareText.textContent = sdr.firmware || '--';
  const sdrSerialText = document.getElementById('sdrSerialText');
  if (sdrSerialText) {
    sdrSerialText.textContent = sdr.selected_serial ? `...${sdr.selected_serial.slice(-8)}` : '--';
  }

  // SDR Device Dropdown
  const sdrSelect = document.getElementById('sdrDeviceSelect');
  if (sdrSelect && sdr.devices) {
    const existing = Array.from(sdrSelect.options).map(o => o.value).filter(v => v);
    const incoming = sdr.devices.map(d => d.serial);
    if (JSON.stringify(existing) !== JSON.stringify(incoming)) {
      sdrSelect.innerHTML = '<option value="">Auto-Detect HackRF SDR</option>';
      sdr.devices.forEach(d => {
        const opt = document.createElement('option');
        opt.value = d.serial;
        opt.textContent = `[#${d.index}] ${d.board} (Rev ${d.revision}) - ...${d.short_serial}`;
        if (d.serial === sdr.selected_serial) opt.selected = true;
        sdrSelect.appendChild(opt);
      });
    }
  }

  // ---- KrakenSDR Direction Finding ----
  const k = data.kraken;
  if (k) {
    state.kraken_enabled = k.enabled;
    state.kraken_bearing = k.bearing_deg;
    state.kraken_confidence = k.confidence;
    state.kraken_snr = k.snr_db;
    state.kraken_locked = k.locked;
    state.kraken_age = k.age_sec;
    if (k.spectrum && k.spectrum.length === 360) state.kraken_spectrum = k.spectrum;
    krakenRunning = k.enabled;

    // Header badge
    if (k.enabled && k.locked && k.age_sec < 2.0) {
      updateBadge('krakenBadge', 'online', `DF: ${k.bearing_deg.toFixed(0)}° ${bearingCardinal(k.bearing_deg)}`);
    } else if (k.enabled) {
      updateBadge('krakenBadge', 'warn', 'DF: ACQUIRING');
    } else {
      updateBadge('krakenBadge', '', 'DF: STANDBY');
    }

    // Hardware / health badge
    const hb = document.getElementById('krakenHealthBadge');
    if (hb) {
      const hmap = { ONLINE: 'online', SIMULATOR: 'online', INITIALIZING: 'warn', DEGRADED: 'warn', OFFLINE: '' };
      hb.className = 'badge ' + (hmap[k.health] || '');
      hb.textContent = `HARDWARE: ${k.health} (${k.tuners}/5)`;
    }
    const tEl = document.getElementById('krakenTuners');
    if (tEl) tEl.textContent = k.tuners;
    const ssEl = document.getElementById('krakenStreamState');
    if (ssEl) ssEl.textContent = k.online ? 'REACHABLE' : 'UNREACHABLE';

    // Streaming badge + toggle button sync
    const strBadge = document.getElementById('krakenStreamingBadge');
    if (strBadge) {
      if (k.enabled) {
        strBadge.className = 'badge online';
        strBadge.textContent = k.mode === 'SIMULATOR' ? `SIMULATING (${k.freq_mhz} MHz)` : `LIVE (${k.freq_mhz} MHz)`;
      } else {
        strBadge.className = 'badge';
        strBadge.textContent = 'STANDBY';
      }
    }
    const tglBtn = document.getElementById('btnToggleKraken');
    if (tglBtn) {
      if (k.enabled) {
        tglBtn.textContent = '⏹ STOP DIRECTION FINDING';
        tglBtn.style.backgroundColor = 'var(--red-bright)';
      } else {
        tglBtn.textContent = '▶ START DIRECTION FINDING';
        tglBtn.style.backgroundColor = 'var(--cyan-dim)';
      }
    }

    // Readout line
    const rd = document.getElementById('krakenReadout');
    if (rd) {
      if (k.enabled && k.locked && k.age_sec < 2.0) {
        rd.textContent = `[ DoA LOCKED: ${k.bearing_deg.toFixed(1)}° ${bearingCardinal(k.bearing_deg)} | CONF: ${k.confidence.toFixed(0)}% | SNR: ${k.snr_db.toFixed(0)}dB ]`;
        rd.style.color = 'var(--green-bright, #10b981)';
      } else if (k.enabled) {
        rd.textContent = '[ KRAKENSDR DoA: ACQUIRING SIGNAL... ]';
        rd.style.color = 'var(--amber-bright, #f59e0b)';
      } else {
        rd.textContent = '[ KRAKENSDR DoA: STANDBY / DISCONNECTED ]';
        rd.style.color = 'var(--amber-bright, #f59e0b)';
      }
    }
  }

  // ---- VTX Analog Video Decode ----
  const v = data.vtx;
  if (v) {
    vtxRunning = v.running;

    // Header badge
    if (v.running && v.sync_locked) {
      updateBadge('vtxBadge', 'online', `VTX: ${v.channel} LOCK`);
    } else if (v.running) {
      updateBadge('vtxBadge', 'warn', `VTX: ${v.channel} SYNC?`);
    } else {
      updateBadge('vtxBadge', '', 'VTX: STANDBY');
    }

    // Streaming badge
    const sb = document.getElementById('vtxStreamBadge');
    if (sb) {
      if (v.running) {
        sb.className = 'badge online';
        sb.textContent = `DECODING ${v.channel} (${v.freq_mhz.toFixed(0)} MHz ${v.standard})`;
      } else {
        sb.className = 'badge';
        sb.textContent = 'STANDBY';
      }
    }

    const syncEl = document.getElementById('vtxSync');
    if (syncEl) { syncEl.textContent = v.running ? (v.sync_locked ? 'LOCKED' : 'SEARCHING') : '--'; syncEl.style.color = v.sync_locked ? 'var(--green-bright,#10b981)' : 'var(--text-dim)'; }
    const fpsEl = document.getElementById('vtxFps');
    if (fpsEl) fpsEl.textContent = (v.fps || 0).toFixed(1);
    const errEl = document.getElementById('vtxError');
    if (errEl) errEl.textContent = v.error || '';

    // Toggle button
    const tb = document.getElementById('btnToggleVtx');
    if (tb) {
      if (v.running) { tb.textContent = '⏹ STOP VIDEO'; tb.style.backgroundColor = 'var(--red-bright)'; }
      else { tb.textContent = '▶ START VIDEO'; tb.style.backgroundColor = 'var(--cyan-dim)'; }
    }

    // Manage the MJPEG <img> stream: attach when running, detach when stopped (once, not per-frame)
    const img = document.getElementById('vtxVideo');
    if (img) {
      if (v.running && !vtxStreamActive) {
        img.src = '/vtx/stream.mjpeg?t=' + Date.now();
        vtxStreamActive = true;
      } else if (!v.running && vtxStreamActive) {
        img.src = '';
        vtxStreamActive = false;
      }
    }
  }
}

function bearingCardinal(bearing) {
  const c = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
  return c[Math.floor(((bearing % 360) + 11.25) / 22.5) % 16];
}

function updateBadge(id, className, text) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = 'badge ' + (className || '');
  const span = el.querySelector('span:last-child') || el;
  span.textContent = text;
}

// ----------------------------------------------------------------------------
// Airspace Pilot Table UI
// ----------------------------------------------------------------------------
function updatePilotsTable(pilots) {
  const tbody = document.getElementById('pilotTableBody');
  if (!tbody) return;

  if (!pilots || pilots.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 14px;">Awaiting LoRa sync beacons...</td></tr>`;
    return;
  }

  let html = '';
  pilots.forEach(p => {
    const isLocked = state.target_pilot === p.uid || (p.u3 && state.target_pilot.includes(`${p.u3},${p.u4},${p.u5}`));
    const ageSec = Math.max(0, Math.round(Date.now() / 1000 - p.last_seen));
    
    html += `
      <tr class="pilot-row">
        <td style="font-weight: 800; color: ${isLocked ? 'var(--cyan-bright)' : 'var(--text-main)'};">
          ${isLocked ? '🎯 ' : ''}${p.uid}
        </td>
        <td><span class="badge" style="padding: 2px 4px; font-size: 10px;">${p.proto || 'v4'}</span></td>
        <td><span class="badge" style="padding: 2px 4px; font-size: 10px;">${p.rate}</span></td>
        <td style="color: var(--cyan-bright); font-weight: 700;">${p.rssi ? p.rssi.toFixed(0) : '--'} dBm</td>
        <td>${p.count}</td>
        <td style="color: var(--text-muted);">${ageSec}s ago</td>
        <td>
          <button class="btn-tactical ${isLocked ? 'secondary' : ''}" style="padding: 3px 8px; font-size: 10.5px; ${isLocked ? 'border-color: var(--green-bright); color: var(--green-bright); font-weight: 800;' : ''}" onclick="lockPilotUID('${p.uid}')">
            ${isLocked ? 'RELEASE' : 'LOCK'}
          </button>
        </td>
      </tr>
    `;
  });
  tbody.innerHTML = html;
}

window.lockPilotUID = function(uidParam) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "lock_pilot", target: uidParam }));
  }
};

// ----------------------------------------------------------------------------
// SITREP Log Stream
// ----------------------------------------------------------------------------
function addSitrepEntry(entry) {
  const container = document.getElementById('sitrepList');
  if (!container) return;

  const div = document.createElement('div');
  div.className = 'log-entry';
  div.innerHTML = `
    <span class="log-ts">[${entry.timestamp}]</span>
    <span class="log-badge ${entry.level}">${entry.level}</span>
    <span class="log-msg"><strong style="color: var(--cyan-bright);">[${entry.category}]</strong> ${entry.message}</span>
  `;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;

  // Voice announcement
  if (entry.voice) {
    speakTactical(entry.message.replace(/UID|CRIT|WARN|ALERT|NOTICE/gi, ''));
  }
}

// ----------------------------------------------------------------------------
// Action Handlers (Port switch, Rate lock, Pilot lock, Serial send, SDR toggle)
// ----------------------------------------------------------------------------
document.getElementById('btnApplyPort')?.addEventListener('click', () => {
  const chosen = document.getElementById('portSelect').value;
  if (chosen && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "set_port", port: chosen }));
  }
});

document.getElementById('btnApplyRate')?.addEventListener('click', () => {
  const val = document.getElementById('rateSelect').value;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "set_rate", rate: val }));
  }
});

document.getElementById('btnAutoPilot')?.addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "lock_pilot", target: "AUTO" }));
  }
});

document.getElementById('btnQuickUnlock')?.addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "lock_pilot", target: "AUTO" }));
  }
});

document.getElementById('btnToggleScan')?.addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    if (state.scan_mode) {
      ws.send(JSON.stringify({ action: "stop_scan" }));
    } else {
      const r = document.getElementById('scanRateSelect')?.value || "50HZ";
      const b = document.getElementById('scanBandSelect')?.value || "AUTO";
      ws.send(JSON.stringify({ action: "start_scan", rate: r, band: b }));
    }
  }
});

document.getElementById('btnClearPilots')?.addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "clear_pilots" }));
  }
});

document.getElementById('scanRateSelect')?.addEventListener('change', (e) => {
  if (state.scan_mode && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "set_rate", rate: e.target.value }));
  }
});

document.getElementById('scanBandSelect')?.addEventListener('change', (e) => {
  if (state.scan_mode && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "set_band", band: e.target.value }));
  }
});

document.getElementById('btnSendSerial')?.addEventListener('click', () => {
  const inp = document.getElementById('inputSerialCmd');
  const cmd = inp.value.trim();
  if (cmd && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "serial_tx", cmd: cmd }));
    inp.value = '';
  }
});

document.getElementById('btnClearSitrep')?.addEventListener('click', () => {
  const container = document.getElementById('sitrepList');
  if (container) container.innerHTML = '';
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "clear_sitrep" }));
  }
});

document.getElementById('btnApplySdrDevice')?.addEventListener('click', () => {
  const chosen = document.getElementById('sdrDeviceSelect').value;
  if (chosen && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "set_sdr_device", serial: chosen }));
  }
});

document.getElementById('btnScanSdr')?.addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "rescan_sdr" }));
  }
});

document.getElementById('btnToggleSdr')?.addEventListener('click', () => {
  const btn = document.getElementById('btnToggleSdr');
  sdrRunning = !sdrRunning;
  if (sdrRunning) {
    const freq = parseFloat(document.getElementById('sdrFreq').value) || 915.0;
    const lna = parseInt(document.getElementById('sdrLna').value) || 32;
    const vga = parseInt(document.getElementById('sdrVga').value) || 30;
    ws.send(JSON.stringify({ action: "start_sdr", freq_mhz: freq, lna: lna, vga: vga }));
    btn.textContent = "STOP SPECTRUM";
    btn.style.backgroundColor = "var(--red-bright)";
  } else {
    ws.send(JSON.stringify({ action: "stop_sdr" }));
    btn.textContent = "START SPECTRUM";
    btn.style.backgroundColor = "var(--cyan-dim)";
  }
});

document.getElementById('sdrSens')?.addEventListener('input', (e) => {
  const lbl = document.getElementById('sdrSensVal');
  if (lbl) lbl.textContent = `${e.target.value}dB`;
});

// ----------------------------------------------------------------------------
// KrakenSDR Direction Finding controls
// ----------------------------------------------------------------------------
function krakenModeUI() {
  const mode = document.getElementById('krakenMode')?.value || 'KRAKEN_POLL';
  const hostRow = document.getElementById('krakenHostRow');
  if (hostRow) hostRow.style.display = (mode === 'SIMULATOR') ? 'none' : 'flex';
}

function krakenPhysicsUI() {
  const arr = document.getElementById('krakenArray')?.value || 'UCA';
  const hint = document.getElementById('krakenPhysicsHint');
  if (hint) {
    hint.textContent = (arr === 'UCA')
      ? 'UCA — unambiguous 360° coverage'
      : 'ULA — 180° coverage, front/back ambiguity';
  }
}

document.getElementById('krakenMode')?.addEventListener('change', krakenModeUI);
document.getElementById('krakenArray')?.addEventListener('change', krakenPhysicsUI);

function krakenConfigPayload() {
  return {
    mode: document.getElementById('krakenMode')?.value || 'KRAKEN_POLL',
    host: document.getElementById('krakenHost')?.value.trim() || '127.0.0.1',
    doa_port: parseInt(document.getElementById('krakenPort')?.value) || 8081,
    freq_mhz: parseFloat(document.getElementById('krakenFreq')?.value) || 915.0,
    gain_db: parseFloat(document.getElementById('krakenGain')?.value) || 30.0,
    array: document.getElementById('krakenArray')?.value || 'UCA',
    spacing_m: parseFloat(document.getElementById('krakenSpacing')?.value) || 0.135
  };
}

document.getElementById('btnToggleKraken')?.addEventListener('click', () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  krakenRunning = !krakenRunning;
  if (krakenRunning) {
    ws.send(JSON.stringify(Object.assign({ action: 'start_doa' }, krakenConfigPayload())));
  } else {
    ws.send(JSON.stringify({ action: 'stop_doa' }));
  }
});

document.getElementById('btnKrakenApply')?.addEventListener('click', () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify(Object.assign({ action: 'set_doa_config' }, krakenConfigPayload())));
});

krakenModeUI();
krakenPhysicsUI();

// ----------------------------------------------------------------------------
// VTX Analog Video Decode controls
// ----------------------------------------------------------------------------
function vtxG(id) { return document.getElementById(id); }
function vtxNum(id, d) { const v = parseFloat(vtxG(id)?.value); return isNaN(v) ? d : v; }

// Full config for start (channel + gain + all sync/picture params)
function vtxStartPayload() {
  return {
    channel: vtxG('vtxChannel')?.value || 'F4',
    standard: vtxG('vtxStandard')?.value || 'PAL',
    lna: vtxNum('vtxLna', 32),
    vga: vtxNum('vtxVga', 30),
    invert: !!vtxG('vtxInvert')?.checked,
    auto_hsync: !!vtxG('vtxAutoHsync')?.checked,
    line_len: vtxNum('vtxLineLen', 640),
    v_hold: vtxNum('vtxVHold', 0),
    h_hold: vtxNum('vtxHHold', 0),
    contrast: vtxNum('vtxContrast', 100) / 100.0,
    brightness: vtxNum('vtxBrightness', 0)
  };
}

// Send one tuning field (minimal payload so only channel/gain trigger a re-tune)
function vtxSend(fields) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(Object.assign({ action: 'set_vtx_tuning' }, fields)));
  }
}

document.getElementById('btnToggleVtx')?.addEventListener('click', () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  vtxRunning = !vtxRunning;
  if (vtxRunning) {
    ws.send(JSON.stringify(Object.assign({ action: 'start_vtx' }, vtxStartPayload())));
  } else {
    ws.send(JSON.stringify({ action: 'stop_vtx' }));
  }
});

// Channel / gain -> re-tune (decoder re-opens the HackRF)
vtxG('vtxChannel')?.addEventListener('change', e => vtxSend({ channel: e.target.value }));
vtxG('vtxLna')?.addEventListener('change', e => vtxSend({ lna: parseInt(e.target.value) || 32 }));
vtxG('vtxVga')?.addEventListener('change', e => vtxSend({ vga: parseInt(e.target.value) || 30 }));

// Standard / invert / auto-hsync -> live
vtxG('vtxStandard')?.addEventListener('change', e => vtxSend({ standard: e.target.value }));
vtxG('vtxInvert')?.addEventListener('change', e => vtxSend({ invert: e.target.checked }));
vtxG('vtxAutoHsync')?.addEventListener('change', e => vtxSend({ auto_hsync: e.target.checked }));

// Sync / centering / picture sliders -> live, with label updates
vtxG('vtxLineLen')?.addEventListener('input', e => {
  const px = parseInt(e.target.value);
  const khz = (10000.0 / px).toFixed(2);
  vtxG('vtxLineLenVal').textContent = `${px} px (${khz} kHz)`;
  vtxSend({ line_len: px });
});
vtxG('vtxVHold')?.addEventListener('input', e => {
  vtxG('vtxVHoldVal').textContent = `${e.target.value} lines`;
  vtxSend({ v_hold: parseInt(e.target.value) });
});
vtxG('vtxHHold')?.addEventListener('input', e => {
  vtxG('vtxHHoldVal').textContent = `${e.target.value} px`;
  vtxSend({ h_hold: parseInt(e.target.value) });
});
vtxG('vtxContrast')?.addEventListener('input', e => {
  const c = parseInt(e.target.value) / 100.0;
  vtxG('vtxContrastVal').textContent = `${c.toFixed(2)}x`;
  vtxSend({ contrast: c });
});
vtxG('vtxBrightness')?.addEventListener('input', e => {
  vtxG('vtxBrightnessVal').textContent = `${e.target.value}`;
  vtxSend({ brightness: parseInt(e.target.value) });
});

// Re-center: reset V/H hold to 0
document.getElementById('btnVtxRecenter')?.addEventListener('click', () => {
  if (vtxG('vtxVHold')) { vtxG('vtxVHold').value = 0; vtxG('vtxVHoldVal').textContent = '0 lines'; }
  if (vtxG('vtxHHold')) { vtxG('vtxHHold').value = 0; vtxG('vtxHHoldVal').textContent = '0 px'; }
  vtxSend({ v_hold: 0, h_hold: 0 });
});

// ----------------------------------------------------------------------------
// 60 FPS Canvas Graphics: Mode 2 Gimbal HUD
// ----------------------------------------------------------------------------
const gimbalCanvas = document.getElementById('gimbalCanvas');
const gCtx = gimbalCanvas?.getContext('2d');

function renderGimbalHUD() {
  if (!gCtx) return;
  const w = gimbalCanvas.width;
  const h = gimbalCanvas.height;

  gCtx.clearRect(0, 0, w, h);

  const boxSize = Math.min(180, (w - 40) / 2);
  const yTop = (h - boxSize) / 2;
  const gap = (w - 2 * boxSize) / 3;
  const xLeft = gap;
  const xRight = gap + boxSize + gap;

  // Mode 2 channels
  const chYaw = state.smooth_ch[0]; // CH1 Yaw (988..2012)
  const chPit = state.smooth_ch[1]; // CH2 Pitch (988..2012)
  const chThr = state.smooth_ch[2]; // CH3 Throttle (988..2012)
  const chRol = state.smooth_ch[3]; // CH4 Roll (988..2012)
  const isArmed = state.is_armed;

  // Left Gimbal: Throttle (Y) / Yaw (X)
  drawSingleGimbal(gCtx, xLeft, yTop, boxSize, chYaw, chThr, "LEFT: THROTTLE / YAW", `THR: ${chThr.toFixed(0)}us`, `YAW: ${chYaw.toFixed(0)}us`, isArmed);

  // Right Gimbal: Pitch (Y) / Roll (X)
  drawSingleGimbal(gCtx, xRight, yTop, boxSize, chRol, chPit, "RIGHT: PITCH / ROLL", `PIT: ${chPit.toFixed(0)}us`, `ROL: ${chRol.toFixed(0)}us`, isArmed);
}

function drawSingleGimbal(ctx, x, y, size, valX, valY, title, labelY, labelX, armed) {
  // Box background
  ctx.fillStyle = "#090d16";
  ctx.strokeStyle = armed ? "#ef4444" : "#1e293b";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.roundRect(x, y, size, size, 6);
  ctx.fill();
  ctx.stroke();

  const cx = x + size / 2;
  const cy = y + size / 2;

  // Crosshairs
  ctx.strokeStyle = "#1e293b";
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(x + 6, cy); ctx.lineTo(x + size - 6, cy);
  ctx.moveTo(cx, y + 6); ctx.lineTo(cx, y + size - 6);
  ctx.stroke();
  ctx.setLineDash([]);

  // Center Deadband Box
  ctx.strokeStyle = "#334155";
  ctx.lineWidth = 1;
  ctx.setLineDash([2, 3]);
  const db = size * 0.12;
  ctx.strokeRect(cx - db / 2, cy - db / 2, db, db);
  ctx.setLineDash([]);

  // Stick Position Mapping (988 to 2012 us -> -1 to +1)
  const normX = Math.max(-1, Math.min(1, (valX - 1500) / 512));
  // Invert Y: 988 at bottom (screen max y), 2012 at top (screen min y)
  const normY = Math.max(-1, Math.min(1, (valY - 1500) / 512));

  const pad = 14;
  const radiusRange = (size / 2) - pad;
  const stickX = cx + normX * radiusRange;
  const stickY = cy - normY * radiusRange;

  // Stick Cursor Glow & Center
  ctx.beginPath();
  ctx.arc(stickX, stickY, 8, 0, Math.PI * 2);
  ctx.fillStyle = armed ? "rgba(239, 68, 68, 0.3)" : "rgba(56, 189, 248, 0.3)";
  ctx.fill();

  ctx.beginPath();
  ctx.arc(stickX, stickY, 4.5, 0, Math.PI * 2);
  ctx.fillStyle = armed ? "#ef4444" : "#38bdf8";
  ctx.fill();

  // Header and values text
  ctx.font = "9px 'SF Mono', Consolas, monospace";
  ctx.fillStyle = "#94a3b8";
  ctx.textAlign = "left";
  ctx.fillText(title, x + 6, y + 14);

  ctx.fillStyle = "#64748b";
  ctx.fillText(labelY, x + 6, y + size - 16);
  ctx.fillText(labelX, x + 6, y + size - 6);
}

// ----------------------------------------------------------------------------
// 60 FPS Canvas Graphics: Artificial Horizon & Attitude HUD
// ----------------------------------------------------------------------------
const attitudeCanvas = document.getElementById('attitudeCanvas');
const aCtx = attitudeCanvas?.getContext('2d');

function renderAttitudeHUD() {
  if (!aCtx) return;
  const w = attitudeCanvas.width;
  const h = attitudeCanvas.height;
  const cx = w / 2;
  const cy = h / 2;

  aCtx.clearRect(0, 0, w, h);

  const pitch = state.smooth_pitch; // deg (-90 to +90)
  const roll = state.smooth_roll;   // deg (-180 to +180)
  const yaw = state.smooth_yaw;

  aCtx.save();
  // Clip to inner card
  aCtx.beginPath();
  aCtx.roundRect(8, 8, w - 16, h - 16, 6);
  aCtx.clip();

  // Sky / Ground background
  aCtx.save();
  aCtx.translate(cx, cy);
  aCtx.rotate((-roll * Math.PI) / 180);

  const pixelsPerDegree = 3.2;
  const pitchOffset = pitch * pixelsPerDegree;

  // Sky
  aCtx.fillStyle = "#0c1729";
  aCtx.fillRect(-w, -h * 2 + pitchOffset, w * 2, h * 2);

  // Ground
  aCtx.fillStyle = "#161c24";
  aCtx.fillRect(-w, pitchOffset, w * 2, h * 2);

  // Horizon Line
  aCtx.strokeStyle = "#38bdf8";
  aCtx.lineWidth = 2;
  aCtx.beginPath();
  aCtx.moveTo(-w, pitchOffset);
  aCtx.lineTo(w, pitchOffset);
  aCtx.stroke();

  // Pitch Ladder Rungs (-40 to +40 in 10 deg intervals)
  aCtx.strokeStyle = "rgba(56, 189, 248, 0.7)";
  aCtx.fillStyle = "rgba(56, 189, 248, 0.9)";
  aCtx.font = "9px 'SF Mono', Consolas, monospace";
  aCtx.textAlign = "left";
  aCtx.lineWidth = 1.5;

  for (let d = -40; d <= 40; d += 10) {
    if (d === 0) continue;
    const yPos = pitchOffset - (d * pixelsPerDegree);
    const rungW = (d % 20 === 0) ? 38 : 22;

    aCtx.beginPath();
    aCtx.moveTo(-rungW, yPos);
    aCtx.lineTo(rungW, yPos);
    aCtx.stroke();

    if (d % 20 === 0) {
      aCtx.fillText(`${Math.abs(d)}°`, rungW + 4, yPos + 3);
    }
  }
  aCtx.restore();

  // Fixed Aircraft Reticle (Amber wings and center dot)
  aCtx.strokeStyle = "#f59e0b";
  aCtx.fillStyle = "#f59e0b";
  aCtx.lineWidth = 2.5;

  // Left wing
  aCtx.beginPath();
  aCtx.moveTo(cx - 40, cy);
  aCtx.lineTo(cx - 15, cy);
  aCtx.lineTo(cx - 15, cy + 6);
  aCtx.stroke();

  // Right wing
  aCtx.beginPath();
  aCtx.moveTo(cx + 15, cy);
  aCtx.lineTo(cx + 40, cy);
  aCtx.lineTo(cx + 15, cy + 6);
  aCtx.stroke();

  // Center pip
  aCtx.beginPath();
  aCtx.arc(cx, cy, 3, 0, Math.PI * 2);
  aCtx.fill();

  // Roll arc at top
  aCtx.strokeStyle = "rgba(255, 255, 255, 0.2)";
  aCtx.lineWidth = 1.5;
  aCtx.beginPath();
  aCtx.arc(cx, cy, 75, Math.PI * 1.2, Math.PI * 1.8);
  aCtx.stroke();

  // Roll pointer triangle
  aCtx.save();
  aCtx.translate(cx, cy);
  aCtx.rotate((-roll * Math.PI) / 180);
  aCtx.fillStyle = "#38bdf8";
  aCtx.beginPath();
  aCtx.moveTo(0, -75);
  aCtx.lineTo(-4, -83);
  aCtx.lineTo(4, -83);
  aCtx.closePath();
  aCtx.fill();
  aCtx.restore();

  // Top Compass Ribbon
  drawCompassRibbon(aCtx, cx, 14, w - 40, yaw);

  aCtx.restore();
}

function drawCompassRibbon(ctx, x, y, width, heading) {
  ctx.fillStyle = "rgba(9, 13, 22, 0.85)";
  ctx.strokeStyle = "#1e293b";
  ctx.lineWidth = 1;
  ctx.fillRect(x - width / 2, y, width, 18);
  ctx.strokeRect(x - width / 2, y, width, 18);

  ctx.fillStyle = "#38bdf8";
  ctx.font = "bold 9px 'SF Mono', Consolas, monospace";
  ctx.textAlign = "center";

  // Center heading index
  const hText = `${Math.round(heading % 360)}°`;
  ctx.fillText(hText, x, y + 13);
}

// ----------------------------------------------------------------------------
// HackRF Spectrum & Waterfall Canvas Engine
// ----------------------------------------------------------------------------
const specCanvas = document.getElementById('spectrumCanvas');
const sCtx = specCanvas?.getContext('2d');

const wfCanvas = document.getElementById('waterfallCanvas');
const wCtx = wfCanvas?.getContext('2d');

const PALETTES = {
  inferno: [
    [4, 4, 12],
    [30, 58, 138],
    [6, 182, 212],
    [250, 204, 21],
    [239, 68, 68]
  ],
  green: [
    [2, 8, 4],
    [6, 78, 59],
    [16, 185, 129],
    [52, 211, 153],
    [240, 253, 244]
  ],
  amber: [
    [8, 4, 2],
    [120, 53, 15],
    [217, 119, 6],
    [251, 191, 36],
    [254, 243, 199]
  ],
  ice: [
    [3, 6, 15],
    [14, 116, 144],
    [6, 182, 212],
    [56, 189, 248],
    [240, 249, 255]
  ]
};

function getColormapRGB(val, paletteName = 'inferno') {
  const stops = PALETTES[paletteName] || PALETTES.inferno;
  const v = Math.max(0, Math.min(1, val));
  const segment = Math.min(3, Math.floor(v * 4));
  const t = (v - segment * 0.25) * 4.0;
  const c0 = stops[segment];
  const c1 = stops[segment + 1];
  return [
    Math.round(c0[0] + (c1[0] - c0[0]) * t),
    Math.round(c0[1] + (c1[1] - c0[1]) * t),
    Math.round(c0[2] + (c1[2] - c0[2]) * t)
  ];
}

let smoothedNoiseFloor = null;
let lastRenderedFftSeq = -1;

function renderSpectrumWaterfall() {
  if (!state.sdr_fft || state.sdr_fft.length === 0) return;

  const fft = state.sdr_fft;
  const numBins = fft.length;

  // Dynamically compute noise floor from 20th percentile to auto-track environment
  const sorted = [...fft].sort((a, b) => a - b);
  const currentFloor = sorted[Math.floor(numBins * 0.20)];
  if (smoothedNoiseFloor === null) {
    smoothedNoiseFloor = currentFloor;
  } else {
    smoothedNoiseFloor = smoothedNoiseFloor * 0.90 + currentFloor * 0.10;
  }

  // Operator contrast/sensitivity slider (dB dynamic range span)
  const sensInput = document.getElementById('sdrSens');
  const dynamicRange = sensInput ? parseFloat(sensInput.value) : 35.0;

  // Baseline sits slightly below noise floor so noise ripples are visible at bottom ~8%
  const baseline = smoothedNoiseFloor - (dynamicRange * 0.08);
  const ceiling = baseline + dynamicRange;

  const paletteSelect = document.getElementById('sdrPalette');
  const paletteName = paletteSelect ? paletteSelect.value : 'inferno';

  const f0 = state.sdr_freq_mhz || parseFloat(document.getElementById('sdrFreq')?.value) || 915.0;

  // 1. Render Spectrum FFT
  if (sCtx && specCanvas.width > 50) {
    const sw = specCanvas.width;
    const sh = specCanvas.height;
    const plotH = sh - 22; // Leave bottom 22px for frequency axis

    sCtx.fillStyle = "#030712";
    sCtx.fillRect(0, 0, sw, sh);

    // Power dBFS grid lines
    sCtx.lineWidth = 1;
    sCtx.strokeStyle = "#111c30";
    sCtx.fillStyle = "#475569";
    sCtx.font = "9px 'SF Mono', Consolas, monospace";

    for (let i = 1; i <= 4; i++) {
      const y = Math.round((plotH / 5) * i);
      sCtx.beginPath();
      sCtx.moveTo(0, y);
      sCtx.lineTo(sw, y);
      sCtx.stroke();

      const gridDb = ceiling - (i / 5) * dynamicRange;
      sCtx.fillText(`${gridDb.toFixed(0)} dBFS`, 6, y - 3);
    }

    // Central LO frequency vertical dashed line
    const centerX = sw / 2;
    sCtx.strokeStyle = "#1e293b";
    sCtx.beginPath();
    sCtx.moveTo(centerX, 0);
    sCtx.lineTo(centerX, plotH);
    sCtx.stroke();

    // Frequency bottom axis bar
    sCtx.fillStyle = "#0b1324";
    sCtx.fillRect(0, plotH, sw, 22);
    sCtx.strokeStyle = "#1e293b";
    sCtx.beginPath();
    sCtx.moveTo(0, plotH);
    sCtx.lineTo(sw, plotH);
    sCtx.stroke();

    // Frequency labels
    sCtx.fillStyle = "#94a3b8";
    sCtx.fillText(`${(f0 - 10.0).toFixed(1)} MHz`, 8, sh - 7);
    sCtx.textAlign = "center";
    sCtx.fillText(`${f0.toFixed(2)} MHz (CENTER)`, centerX, sh - 7);
    sCtx.textAlign = "right";
    sCtx.fillText(`${(f0 + 10.0).toFixed(1)} MHz`, sw - 8, sh - 7);
    sCtx.textAlign = "left";

    // FFT Curve with subtle gradient fill
    let maxDb = -999;
    let maxIdx = 0;

    const grad = sCtx.createLinearGradient(0, 0, 0, plotH);
    grad.addColorStop(0, "rgba(56, 189, 248, 0.40)");
    grad.addColorStop(1, "rgba(56, 189, 248, 0.02)");

    sCtx.beginPath();
    sCtx.moveTo(0, plotH);

    for (let i = 0; i < numBins; i++) {
      const x = (i / (numBins - 1)) * sw;
      const db = fft[i];
      if (db > maxDb) {
        maxDb = db;
        maxIdx = i;
      }
      const yNorm = Math.max(0, Math.min(1, (db - baseline) / dynamicRange));
      const y = plotH - yNorm * plotH;
      sCtx.lineTo(x, y);
    }
    sCtx.lineTo(sw, plotH);
    sCtx.closePath();
    sCtx.fillStyle = grad;
    sCtx.fill();

    // Re-trace stroke on top for crisp trace
    sCtx.strokeStyle = "#38bdf8";
    sCtx.lineWidth = 1.4;
    sCtx.beginPath();
    for (let i = 0; i < numBins; i++) {
      const x = (i / (numBins - 1)) * sw;
      const db = fft[i];
      const yNorm = Math.max(0, Math.min(1, (db - baseline) / dynamicRange));
      const y = plotH - yNorm * plotH;
      if (i === 0) sCtx.moveTo(x, y);
      else sCtx.lineTo(x, y);
    }
    sCtx.stroke();

    // Peak Marker
    const peakX = (maxIdx / (numBins - 1)) * sw;
    const peakYNorm = Math.max(0, Math.min(1, (maxDb - baseline) / dynamicRange));
    const peakY = plotH - peakYNorm * plotH;
    const peakFreq = f0 - 10.0 + (maxIdx / (numBins - 1)) * 20.0;

    sCtx.fillStyle = "#ef4444";
    sCtx.beginPath();
    sCtx.arc(peakX, peakY, 3.5, 0, Math.PI * 2);
    sCtx.fill();

    sCtx.fillStyle = "#fecaca";
    sCtx.font = "bold 9.5px 'SF Mono', Consolas, monospace";
    const tagText = `${peakFreq.toFixed(2)} MHz | ${maxDb.toFixed(1)} dBFS`;
    const tagWidth = sCtx.measureText(tagText).width;
    const tagX = Math.max(8, Math.min(sw - tagWidth - 8, peakX + 8));
    const tagY = Math.max(14, peakY - 6);
    sCtx.fillText(tagText, tagX, tagY);
  }

  // 2. Render Waterfall Spectrogram (Scrolls row-by-row on new FFT sequence)
  if (wCtx && wfCanvas.width > 50 && state.fftSeq !== lastRenderedFftSeq) {
    lastRenderedFftSeq = state.fftSeq;
    const ww = wfCanvas.width;
    const wh = wfCanvas.height;

    // Shift previous image down by 1 row
    wCtx.drawImage(wfCanvas, 0, 0, ww, wh - 1, 0, 1, ww, wh - 1);

    // Draw new row at top
    const imgData = wCtx.createImageData(ww, 1);
    const data = imgData.data;

    for (let x = 0; x < ww; x++) {
      const binIdx = Math.floor((x / ww) * numBins);
      const db = fft[binIdx] !== undefined ? fft[binIdx] : baseline;
      const norm = Math.max(0, Math.min(1, (db - baseline) / dynamicRange));
      const [r, g, b] = getColormapRGB(norm, paletteName);

      const idx = x * 4;
      data[idx] = r;
      data[idx + 1] = g;
      data[idx + 2] = b;
      data[idx + 3] = 255;
    }
    wCtx.putImageData(imgData, 0, 0);
  }
}

// ----------------------------------------------------------------------------
// Canvas Resize Helper
// ----------------------------------------------------------------------------
function resizeCanvases() {
  const gContainer = gimbalCanvas?.parentElement;
  if (gContainer && gimbalCanvas) {
    const rect = gContainer.getBoundingClientRect();
    if (rect.width > 0) {
      gimbalCanvas.width = rect.width;
      gimbalCanvas.height = 220;
    }
  }

  const aContainer = attitudeCanvas?.parentElement;
  if (aContainer && attitudeCanvas) {
    const rect = aContainer.getBoundingClientRect();
    if (rect.width > 0) {
      attitudeCanvas.width = rect.width;
      attitudeCanvas.height = 220;
    }
  }

  if (specCanvas && specCanvas.parentElement && specCanvas.parentElement.clientWidth > 50) {
    const targetW = specCanvas.parentElement.clientWidth - 20;
    if (specCanvas.width !== targetW) specCanvas.width = targetW;
  }
  if (wfCanvas && wfCanvas.parentElement && wfCanvas.parentElement.clientWidth > 50) {
    const targetW = wfCanvas.parentElement.clientWidth - 20;
    if (wfCanvas.width !== targetW) {
      wfCanvas.width = targetW;
      if (wCtx) {
        wCtx.fillStyle = "#030712";
        wCtx.fillRect(0, 0, wfCanvas.width, wfCanvas.height);
      }
    }
  }
}

window.addEventListener('resize', resizeCanvases);

// ----------------------------------------------------------------------------
// Main 60 FPS Animation Loop
// ----------------------------------------------------------------------------
function animationFrameLoop() {
  renderGimbalHUD();
  renderAttitudeHUD();
  renderSpectrumWaterfall();
  renderDoACompass();
  requestAnimationFrame(animationFrameLoop);
}

// ----------------------------------------------------------------------------
// KrakenSDR DoA Compass + MUSIC Pseudo-Spectrum
// ----------------------------------------------------------------------------
const doaCanvas = document.getElementById('doaCanvas');
const dCtx = doaCanvas?.getContext('2d');

function renderDoACompass() {
  if (!dCtx) return;

  // Responsive square sizing so the compass stays circular on every display format
  // (phone/tablet/desktop, portrait/landscape). Explicit px overrides the landscape
  // hud-canvas CSS (width:100%/height:220px) that would otherwise squash it to an ellipse.
  const parent = doaCanvas.parentElement;
  const availW = (parent ? parent.clientWidth : 420) - 8;
  const availH = Math.max(220, (window.innerHeight || 640) - 210);
  let size = Math.min(availW, availH, 560);
  size = Math.max(200, Math.round(size));

  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const backing = Math.round(size * dpr);
  if (doaCanvas.width !== backing || doaCanvas.height !== backing) {
    doaCanvas.width = backing;
    doaCanvas.height = backing;
  }
  doaCanvas.style.width = size + 'px';
  doaCanvas.style.height = size + 'px';
  dCtx.setTransform(dpr, 0, 0, dpr, 0, 0); // draw in CSS px, render crisp on hi-DPI

  const w = size, h = size;
  const cx = w / 2, cy = h / 2;
  const k = size / 420;                       // scale factor vs the original design size
  const R = size / 2 - Math.max(14, size * 0.075);

  dCtx.clearRect(0, 0, w, h);

  // Smooth the bearing needle (shortest angular path) for fluid motion
  let target = state.kraken_bearing || 0;
  let diff = ((target - state.kraken_smooth_bearing + 540) % 360) - 180;
  state.kraken_smooth_bearing = (state.kraken_smooth_bearing + diff * 0.2 + 360) % 360;

  const locked = state.kraken_locked && state.kraken_age < 2.0 && state.kraken_enabled;
  const accent = locked ? '#10b981' : (state.kraken_enabled ? '#f59e0b' : '#334155');

  // Outer bezel
  dCtx.beginPath();
  dCtx.arc(cx, cy, R, 0, Math.PI * 2);
  dCtx.fillStyle = '#060a14';
  dCtx.fill();
  dCtx.strokeStyle = '#1e293b';
  dCtx.lineWidth = Math.max(1, 2 * k);
  dCtx.stroke();

  // MUSIC pseudo-spectrum ring (0° at top = North, clockwise)
  const spec = state.kraken_spectrum;
  if (spec && spec.length === 360 && state.kraken_enabled) {
    for (let deg = 0; deg < 360; deg++) {
      const p = Math.max(0, Math.min(1, spec[deg]));
      if (p < 0.02) continue;
      const a = (deg - 90) * Math.PI / 180;
      const inner = R * 0.30;
      const outer = inner + p * (R * 0.66);
      dCtx.beginPath();
      dCtx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner);
      dCtx.lineTo(cx + Math.cos(a) * outer, cy + Math.sin(a) * outer);
      const hue = 200 - p * 200; // cyan(low) -> red(high)
      dCtx.strokeStyle = `hsla(${hue}, 90%, ${35 + p * 25}%, ${0.35 + p * 0.6})`;
      dCtx.lineWidth = Math.max(1.2, 2 * k);
      dCtx.stroke();
    }
  }

  // Compass rose ticks + cardinal labels
  const cards = ['N', 'E', 'S', 'W'];
  dCtx.textAlign = 'center';
  dCtx.textBaseline = 'middle';
  for (let deg = 0; deg < 360; deg += 15) {
    const a = (deg - 90) * Math.PI / 180;
    const major = (deg % 45 === 0);
    const t0 = R * (major ? 0.90 : 0.94);
    dCtx.beginPath();
    dCtx.moveTo(cx + Math.cos(a) * t0, cy + Math.sin(a) * t0);
    dCtx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
    dCtx.strokeStyle = '#334155';
    dCtx.lineWidth = major ? Math.max(1, 1.5 * k) : Math.max(0.75, k);
    dCtx.stroke();
  }
  dCtx.font = `bold ${Math.max(10, 14 * k)}px monospace`;
  for (let i = 0; i < 4; i++) {
    const a = (i * 90 - 90) * Math.PI / 180;
    const lr = R * 0.80;
    dCtx.fillStyle = (i === 0) ? '#ef4444' : '#64748b';
    dCtx.fillText(cards[i], cx + Math.cos(a) * lr, cy + Math.sin(a) * lr);
  }

  // Bearing needle
  if (state.kraken_enabled) {
    const a = (state.kraken_smooth_bearing - 90) * Math.PI / 180;
    const tipX = cx + Math.cos(a) * R * 0.80;
    const tipY = cy + Math.sin(a) * R * 0.80;
    // Confidence wedge
    const conf = Math.max(0, Math.min(100, state.kraken_confidence));
    const spread = (1 - conf / 100) * 0.6 + 0.05; // radians half-width
    dCtx.beginPath();
    dCtx.moveTo(cx, cy);
    dCtx.arc(cx, cy, R * 0.80, a - spread, a + spread);
    dCtx.closePath();
    dCtx.fillStyle = locked ? 'rgba(16,185,129,0.15)' : 'rgba(245,158,11,0.12)';
    dCtx.fill();
    // Needle line
    dCtx.beginPath();
    dCtx.moveTo(cx, cy);
    dCtx.lineTo(tipX, tipY);
    dCtx.strokeStyle = accent;
    dCtx.lineWidth = Math.max(1.5, 3 * k);
    dCtx.stroke();
    // Arrow head
    dCtx.beginPath();
    dCtx.arc(tipX, tipY, Math.max(3, 6 * k), 0, Math.PI * 2);
    dCtx.fillStyle = accent;
    dCtx.fill();
  }

  // Center hub + digital readout
  dCtx.beginPath();
  dCtx.arc(cx, cy, R * 0.28, 0, Math.PI * 2);
  dCtx.fillStyle = '#030712';
  dCtx.fill();
  dCtx.strokeStyle = accent;
  dCtx.lineWidth = Math.max(1, 1.5 * k);
  dCtx.stroke();

  dCtx.fillStyle = accent;
  dCtx.font = `bold ${Math.max(18, 30 * k)}px monospace`;
  const bText = state.kraken_enabled ? `${Math.round(state.kraken_smooth_bearing).toString().padStart(3, '0')}°` : '---°';
  dCtx.fillText(bText, cx, cy - size * 0.02);
  dCtx.font = `bold ${Math.max(9, 12 * k)}px monospace`;
  dCtx.fillStyle = '#94a3b8';
  const sub = state.kraken_enabled
    ? `${bearingCardinal(state.kraken_smooth_bearing)} · ${state.kraken_confidence.toFixed(0)}%`
    : 'STANDBY';
  dCtx.fillText(sub, cx, cy + size * 0.034);
}

// Start Engines
window.addEventListener('DOMContentLoaded', () => {
  resizeCanvases();
  connectWebSocket();
  requestAnimationFrame(animationFrameLoop);
});
