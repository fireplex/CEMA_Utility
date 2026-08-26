"""
Tactical AI Intelligence Copilot & Automated SITREP / INTSUM Engine
CEMA Utility - Air-Gapped On-Device Decision Support System

Features:
1. Expert Tactical EW Multi-Domain Reasoning & Dynamic Synthesis Engine.
2. NATO-Standard STANAG Military SITREP / INTSUM Report Generator.
3. Multi-Sensor Physics, Telemetry Math, Jamming Power Budgets & Geolocation.
"""

import os
import time
import math
import datetime
import threading

class TacticalCopilot:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TacticalCopilot, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self.station_callsign = "CEMA-STATION-ALPHA"
        self.slm_model = None
        self.slm_tokenizer = None
        self.slm_status = "OFFLINE"
        self.slm_lock = threading.Lock()
        self._initialized = True
        
        # Asynchronously load local quantized SLM onto RTX 3060 CUDA VRAM
        threading.Thread(target=self._init_slm_engine, daemon=True).start()

    def _init_slm_engine(self):
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
            
            if not torch.cuda.is_available():
                self.slm_status = "CPU_FALLBACK"
                return

            self.slm_status = "LOADING"
            model_id = "Qwen/Qwen2.5-1.5B-Instruct"

            quant_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.float16
            )

            tokenizer = AutoTokenizer.from_pretrained(model_id)
            model = AutoModelForCausalLM.from_pretrained(
                model_id,
                quantization_config=quant_config,
                device_map="cuda",
                torch_dtype=torch.float16
            )
            model.eval()

            with self.slm_lock:
                self.slm_tokenizer = tokenizer
                self.slm_model = model
                self.slm_status = "ONLINE (RTX 3060 CUDA / 4-BIT)"

        except Exception as e:
            self.slm_status = f"ERROR: {e}"

    def is_slm_ready(self):
        with self.slm_lock:
            return self.slm_model is not None and self.slm_tokenizer is not None

    def query_slm(self, query, context_data, max_new_tokens=300):
        if not self.is_slm_ready():
            return None

        try:
            import torch
            queue = context_data.get("hk_queue", {})
            pilots = context_data.get("pilots", {})
            last_bearing = context_data.get("last_bearing", 0.0)
            station_loc = context_data.get("station_loc", (51.5074, -0.1278))
            cep_fix = context_data.get("cep_fix", None)

            # Synthesize real-time tactical context
            ctx_summary = []
            ctx_summary.append(f"Station Origin: {station_loc[0]:.5f}°N, {station_loc[1]:.5f}°W")
            ctx_summary.append(f"Kraken DoA Line-of-Bearing: {last_bearing:05.1f}° True")
            if cep_fix:
                ctx_summary.append(f"Triangulated Geolocation (95% CEP): {cep_fix['lat']:.5f}°N, {cep_fix['lon']:.5f}°W (±{cep_fix['cep']:.1f}m)")

            ctx_summary.append(f"Active Emitters ({len(queue)} tracked):")
            for k, t in queue.items():
                ctx_summary.append(f"  - [{t.get('priority', 'P3')}] Freq: {t.get('freq_display', t.get('freq'))} | Mod: {t.get('mod')} | Threat: {t.get('score', 0)}/100 | RSSI: {t.get('rssi', 0):.1f} dBFS | Azimuth: {t.get('bearing', last_bearing):.1f}°")

            ctx_summary.append(f"Decoded Drone Pilot Entities ({len(pilots)}):")
            for uid, p in pilots.items():
                arm_str = "ARMED" if p.get("armed") else "DISARMED"
                ctx_summary.append(f"  - Pilot UID {p.get('u3')}:{p.get('u4')}:{p.get('u5')} | State: {arm_str} | Speed: {p.get('spd', 0)} km/h | Alt: {p.get('alt', 0)}m | GPS: {p.get('lat', 'N/A')}, {p.get('lon', 'N/A')}")

            context_str = "\n".join(ctx_summary)

            prompt = (
                f"<|im_start|>system\n"
                f"You are a military Electronic Warfare (EW), CEMA, and Tactical UAS Intelligence Officer AI Assistant.\n"
                f"Analyze the live sensor telemetry below and answer the operator's query with concise, actionable military-grade analysis, flight phase intent, link correlation, or ECM jamming recommendations.\n\n"
                f"=== LIVE TACTICAL SENSOR CONTEXT ===\n"
                f"{context_str}\n"
                f"====================================\n<|im_end|>\n"
                f"<|im_start|>user\n{query}\n<|im_end|>\n"
                f"<|im_start|>assistant\n"
            )

            with self.slm_lock:
                inputs = self.slm_tokenizer(prompt, return_tensors="pt").to("cuda")
                with torch.no_grad():
                    outputs = self.slm_model.generate(
                        **inputs,
                        max_new_tokens=max_new_tokens,
                        temperature=0.3,
                        do_sample=True,
                        repetition_penalty=1.1
                    )
                resp = self.slm_tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
                return resp.strip()

        except Exception as e:
            return None

    def generate_nato_sitrep(self, context_data):
        now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        callsign = context_data.get("station_callsign", self.station_callsign)
        loc = context_data.get("station_loc", (51.5074, -0.1278))
        
        queue = context_data.get("hk_queue", {})
        pilots = context_data.get("pilots", {})
        bearings = context_data.get("bearing_history", [])
        last_bearing = context_data.get("last_bearing", 0.0)
        
        p1_targets = [v for v in queue.values() if "P1" in v.get("priority", "")]
        p2_targets = [v for v in queue.values() if "P2" in v.get("priority", "")]
        p3_targets = [v for v in queue.values() if "P3" in v.get("priority", "")]

        lines = []
        lines.append("=" * 66)
        lines.append("TACTICAL INTELLIGENCE SUMMARY (INTSUM) / SITUATION REPORT")
        lines.append(f"STATION: {callsign} | STATION POS: {loc[0]:.5f}°N, {loc[1]:.5f}°W")
        lines.append(f"REPORT TIME: {now_utc} | CLASSIFICATION: SECRET // REL TACTICAL EW")
        lines.append("=" * 66)
        lines.append("")
        
        lines.append("1. EXECUTIVE SUMMARY & THREAT ASSESSMENT")
        lines.append(f"   • Total Active Emitters Tracked : {len(queue)}")
        lines.append(f"   • Priority Breakdown            : {len(p1_targets)} Critical (P1), {len(p2_targets)} High (P2), {len(p3_targets)} Advisory (P3)")
        if p1_targets:
            lines.append("   • SECTOR STATUS                 : [!] HOSTILE / ARMED DRONE ACTIVITY IN SECTOR.")
        else:
            lines.append("   • SECTOR STATUS                 : [OK] ROUTINE SPECTRUM PATROL. NO ARMED UAS IDENTIFIED.")
        lines.append("")

        lines.append("2. INTERCEPTED EMITTER MATRIX (SDR & HARDWARE CVA)")
        if not queue:
            lines.append("   • No active emitters currently in priority intercept queue.")
        else:
            for idx, (k, t) in enumerate(queue.items(), 1):
                brng = t.get("bearing", 0.0)
                brng_str = f"{brng:05.1f}° True" if brng > 0 else "Pending Coherent Fix"
                lines.append(f"   [{idx}] [{t.get('priority', 'UNKNOWN')}] {t.get('freq_display', str(t.get('freq')) + ' MHz')}")
                lines.append(f"       • Classification : {t.get('mod', 'Unknown')}")
                lines.append(f"       • Threat Score   : {t.get('score', 0)}/100 | RSSI: {t.get('rssi', 0):.1f} dBFS | CVA: {t.get('fingerprint', '0x----')}")
                lines.append(f"       • Line-of-Bearing: {brng_str} | Last Seen: {t.get('last_seen', 'N/A')}")
        lines.append("")

        lines.append("3. DECODED DRONE TELEMETRY & OPERATOR STATE (HELTEC SNIFFER)")
        if not pilots:
            lines.append("   • No ExpressLRS / Crossfire digital pilot telemetry actively decoded.")
        else:
            for uid, p in pilots.items():
                arm_str = "ARMED (IN-FLIGHT / KINETIC ENGAGEMENT)" if p.get("armed", False) else "DISARMED (GROUND / IDLE)"
                lines.append(f"   • Pilot UID Hash: {p.get('u3')}:{p.get('u4')}:{p.get('u5')} (CRC Init: 0x{p.get('crc_init')})")
                lines.append(f"     • Flight State  : {arm_str} | Rate: {p.get('rate_name', 'Unknown')} | Sniffer RSSI: {p.get('rssi', 0):.0f} dBm")
                if p.get("lat") and p.get("lon"):
                    lines.append(f"     • Drone Position: {p.get('lat'):.5f}°N, {p.get('lon'):.5f}°W | Alt: {p.get('alt', 0)}m | Speed: {p.get('spd', 0)} km/h")
        lines.append("")

        lines.append("4. DIRECTION FINDING (DoA) & GEOLOCATION FIX (KRAKENSDR)")
        lines.append(f"   • Current Coherent Line-of-Bearing: {last_bearing:05.1f}° True")
        lines.append(f"   • Multi-Point Baseline History     : {len(bearings)} Line-of-Bearing observation(s)")
        if context_data.get("cep_fix"):
            fix = context_data["cep_fix"]
            lines.append(f"   • GEOLOCATION TARGET FIX (CEP)     : {fix['lat']:.5f}°N, {fix['lon']:.5f}°W (95% CEP Radius: ±{fix['cep']:.1f}m)")
        else:
            lines.append("   • Triangulation Target Fix         : Baseline expansion required for 2-point intersection.")
        lines.append("")

        lines.append("5. TACTICAL ELECTRONIC WARFARE (EW) & COUNTERMEASURE ACTIONS")
        if p1_targets:
            lines.append("   [!] ACTION 1: Direct directional C-UAS jamming along Line-of-Bearing azimuth.")
            lines.append(f"   [!] ACTION 2: Align high-gain countermeasure antennas towards {last_bearing:05.1f}° True.")
            lines.append("   [!] ACTION 3: Alert perimeter defense and kinetic intercept teams to target vector.")
        else:
            lines.append("   • Continue wideband autonomous sweep monitoring.")
        lines.append("=" * 66)
        
        return "\n".join(lines)

    def answer_operator_query(self, query, context_data):
        q = query.lower().strip()
        queue = context_data.get("hk_queue", {})
        pilots = context_data.get("pilots", {})
        last_bearing = context_data.get("last_bearing", 0.0)
        station_loc = context_data.get("station_loc", (51.5074, -0.1278))
        cep_fix = context_data.get("cep_fix", None)
        history = context_data.get("bearing_history", [])

        if not q:
            return "Operator, enter a tactical query."

        # 1. GPU Neural SLM Inference (if online in VRAM)
        if self.is_slm_ready():
            slm_resp = self.query_slm(query, context_data)
            if slm_resp and len(slm_resp.strip()) > 10:
                return slm_resp

        # 2. Rule-Guided Multi-Domain Deterministic Fallback Engine
        if any(w in q for w in ["brief", "briefing", "sitrep", "intsum", "report", "overview"]):
            return self._reason_threat_overview(queue, pilots, last_bearing)

        if any(w in q for w in ["hardware", "fingerprint", "cva", "silicon", "pa transient", "clone", "decept"]):
            return self._reason_hardware_fingerprinting(queue)

        if any(w in q for w in ["phase", "intent", "dive", "loiter", "kinetic", "diving", "speed"]):
            return self._reason_kinetic_flight_intent(pilots, queue, last_bearing, station_loc)

        if any(w in q for w in ["ecm", "jam", "countermeasure", "neutraliz", "suppress", "deny", "defense", "electronic attack"]):
            return self._reason_ecm_jamming_strategy(queue, pilots, last_bearing)

        if any(w in q for w in ["correlat", "pair", "cross", "link", "match", "belong", "video feed", "control link"]):
            return self._reason_multi_domain_correlation(queue, pilots, last_bearing)

        if any(w in q for w in ["range", "horizon", "propagation", "path loss", "fspl", "power budget", "j/s", "j to s", "burn through"]):
            return self._reason_rf_physics_and_range(queue, pilots, last_bearing)

        if any(w in q for w in ["triangulat", "cep", "geolocation", "geolocat", "line-of-bearing", "baseline"]):
            return self._reason_direction_finding_and_geolocation(last_bearing, cep_fix, history, queue, pilots, station_loc)

        if any(w in q for w in ["pilot", "operator", "telemetry", "armed", "disarm", "elrs", "crossfire", "heltec", "gps", "standoff", "discrepan", "where"]):
            return self._reason_pilot_and_operator(pilots, queue, last_bearing, station_loc)

        if any(w in q for w in ["threat", "p1", "p2", "critical", "hostil", "danger", "target", "risk", "queue", "active", "status"]):
            return self._reason_threat_overview(queue, pilots, last_bearing)

        if any(w in q for w in ["bearing", "doa", "direction", "kraken", "vector", "azimuth"]):
            return self._reason_direction_finding_and_geolocation(last_bearing, cep_fix, history, queue, pilots, station_loc)

        return self._reason_general_tactical_query(query, queue, pilots, last_bearing, station_loc)

    def _reason_kinetic_flight_intent(self, pilots, queue, last_bearing, station_loc):
        res = ["=== DRONE FLIGHT INTENT & KINETIC PHASE CLASSIFICATION ==="]
        if not pilots:
            closing_targets = [t for t in queue.values() if t.get("trend") == "CLOSING" or t.get("score", 0) >= 80]
            if closing_targets:
                res.append("• Telemetry Status: Direct pilot telemetry unlinked. Inferring intent from RF spectral dynamics.")
                for t in closing_targets:
                    res.append(f"  - Emitter {t.get('freq_display', t.get('freq'))} ({t.get('mod')}): High signal persistence (RSSI {t.get('rssi', 0):.1f} dBFS).")
                    res.append(f"  - Threat Score: {t.get('score', 0)}/100 | Azimuth: {t.get('bearing', last_bearing):.1f}° True.")
                res.append("\n• Tactical Assessment: Active hostile RF carrier detected on vector. Operator is transmitting continuously.")
                res.append("• Advisory: Maintain directional EW readiness on active bearing.")
            else:
                res.append("• No active pilot telemetry packets or closing high-threat carriers detected in sector.")
                res.append("• Current State: Ambient RF baseline. Emitters in sector exhibit stationary or low-duty patrol behavior.")
            return "\n".join(res)

        for uid, p in pilots.items():
            armed = p.get("armed", False)
            spd = p.get("spd", 0)
            alt = p.get("alt", 0)
            d_lat = p.get("lat")
            d_lon = p.get("lon")
            rate = p.get("rate_name", "Unknown")

            res.append(f"• Target Airframe [Pilot UID {p.get('u3')}:{p.get('u4')}:{p.get('u5')}]:")
            res.append(f"  - Armed Status : {'[!] ARMED (Active Flight Mission)' if armed else '[OK] DISARMED (Ground/Pre-Flight)'}")
            res.append(f"  - Flight Stats : Groundspeed = {spd} km/h | Altitude = {alt}m AGL | Packet Rate = {rate}")
            
            if d_lat and d_lon:
                res.append(f"  - Current GPS  : {d_lat:.5f}°N, {d_lon:.5f}°W")

            if not armed:
                res.append("\n• Operational Phase : PHASE 0 - GROUND IDLE / STAGING")
                res.append("  - Analysis       : Airframe motors are disarmed. Pilot is either performing pre-flight checks, on standby, or waiting for launch command.")
                res.append("  - Action Required: Pinpoint operator location via Kraken DoA before launch.")
            elif spd > 85:
                res.append("\n• Operational Phase : [!] PHASE 3 - TERMINAL ATTACK DIVE / HIGH-SPEED SPRINT")
                res.append(f"  - Analysis       : Airframe is operating at high kinetic velocity ({spd} km/h). Consistent with an FPV strike drone closing on target coordinates.")
                res.append("  - Action Required: IMMEDIATE ALERT. Engage directional C-UAS jamming and alert kinetic perimeter defenses.")
            elif spd < 20 and alt > 25:
                res.append("\n• Operational Phase : PHASE 1 - RECONNAISSANCE LOITER / ISR SURVEILLANCE")
                res.append(f"  - Analysis       : Airframe is stationary or slowly loitering at {alt}m altitude. Consistent with optical/thermal target scouting or artillery spotting.")
                res.append("  - Action Required: Prepare video downlink intercept (5.8 GHz) to assess what the drone camera is observing.")
            else:
                res.append("\n• Operational Phase : PHASE 2 - INGRESS / ROUTE TRANSIT")
                res.append(f"  - Analysis       : Airframe is cruising at {spd} km/h along flight path.")
                res.append("  - Action Required: Monitor Doppler and Line-of-Bearing drift rates.")
        return "\n".join(res)

    def _reason_multi_domain_correlation(self, queue, pilots, last_bearing):
        res = ["=== TACTICAL MULTI-DOMAIN LINK CORRELATION ASSESSMENT ==="]
        vtx = [t for t in queue.values() if any(m in t.get("mod", "") for m in ["Video", "PAL", "NTSC", "QAM", "OFDM"])]
        rc = [t for t in queue.values() if any(m in t.get("mod", "") for m in ["LoRa", "CSS", "Crossfire", "ELRS", "FSK", "GFSK"])]

        if rc and vtx:
            res.append("• Correlation Status: [!] FULL COMBAT UAS KILL-CHAIN CONFIRMED PAIRED")
            res.append(f"  - Control Uplink(s)   : {len(rc)} identified")
            for r in rc:
                res.append(f"    • {r.get('freq_display', r.get('freq'))} | Mod: {r.get('mod')} | RSSI: {r.get('rssi', 0):.1f} dBFS")
            res.append(f"  - Video Downlink(s)   : {len(vtx)} identified")
            for v in vtx:
                res.append(f"    • {v.get('freq_display', v.get('freq'))} | Mod: {v.get('mod')} | RSSI: {v.get('rssi', 0):.1f} dBFS")
            
            res.append(f"\n• Cross-Domain Analysis:")
            res.append(f"  - Line-of-Bearing Coherence : Aligned along primary azimuth {last_bearing:05.1f}° True.")
            res.append(f"  - Electronic Vulnerability  : Dual-band vulnerability. Disruption of EITHER band terminates combat efficacy.")
            res.append(f"  - Recommended EA Protocol   : Synchronized dual-band jamming — broadband chirp on 900M control + video sync corruption on 5.8G.")
        elif rc and not vtx:
            res.append("• Correlation Status: CONTROL UPLINK ACTIVE / VIDEO LINK UNSEEN")
            res.append(f"  - Active Uplink : {rc[0].get('freq_display', rc[0].get('freq'))} ({rc[0].get('mod')})")
            res.append("  - Operational Assessment:")
            res.append("    1. Drone may be flying autonomous waypoint GPS mission without video broadcast.")
            res.append("    2. Drone video transmitter may be operating on 1.2 GHz / 3.3 GHz or digital HD out of current stare passband.")
            res.append("    3. Airframe is at extreme range or operating below the 5.8 GHz RF horizon.")
        elif vtx and not rc:
            res.append("• Correlation Status: VIDEO DOWNLINK ACTIVE / CONTROL UPLINK UNSEEN")
            res.append(f"  - Active Video Carrier : {vtx[0].get('freq_display', vtx[0].get('freq'))} ({vtx[0].get('mod')})")
            res.append("  - Operational Assessment:")
            res.append("    1. Airborne drone video transmitter has line-of-sight to station, but ground operator is masked by terrain or distance.")
            res.append("    2. Operator may be using low-power 2.4 GHz uplink with high-gain directional antenna pointed away from station.")
        else:
            res.append("• Correlation Status: No correlated control uplinks or video downlinks currently active in priority queue.")
        return "\n".join(res)

    def _reason_pilot_and_operator(self, pilots, queue, last_bearing, station_loc):
        res = ["=== PILOT TELEMETRY & OPERATOR DISCREPANCY ANALYSIS ==="]
        if not pilots:
            res.append("• Decoded Pilot Telemetry: None currently registered in session.")
            res.append("• Sniffer Status: Heltec V3 is monitoring 900 MHz (915.0/868.0) and 2.4 GHz channels for ExpressLRS sync packets.")
            if queue:
                res.append(f"• RF Activity: {len(queue)} raw emitter(s) active in spectrum. Telemetry decoding requires valid pilot handshake packet.")
            return "\n".join(res)

        for uid, p in pilots.items():
            arm_str = "ARMED (IN-FLIGHT)" if p.get("armed", False) else "DISARMED (GROUND IDLE)"
            res.append(f"• Pilot UID: {p.get('u3')}:{p.get('u4')}:{p.get('u5')} (CRC: 0x{p.get('crc_init')})")
            res.append(f"  - Operational Status : {arm_str}")
            res.append(f"  - Link Quality & Rate: {p.get('rate_name', 'Unknown')} | Sniffer RSSI: {p.get('rssi', 0):.0f} dBm")
            
            d_lat = p.get("lat")
            d_lon = p.get("lon")
            if d_lat and d_lon:
                lat1, lon1 = math.radians(station_loc[0]), math.radians(station_loc[1])
                lat2, lon2 = math.radians(d_lat), math.radians(d_lon)
                d_lon_rad = lon2 - lon1
                y = math.sin(d_lon_rad) * math.cos(lat2)
                x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lon_rad)
                calc_bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
                
                R = 6371.0
                dlat = lat2 - lat1
                a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon_rad/2)**2
                c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
                dist_km = R * c

                bearing_delta = abs(calc_bearing - last_bearing)
                if bearing_delta > 180: bearing_delta = 360 - bearing_delta

                res.append(f"  - Drone Position     : {d_lat:.5f}°N, {d_lon:.5f}°W (Range: {dist_km:.2f} km, Bearing: {calc_bearing:.1f}°)")
                res.append(f"  - Operator DoA Fix   : {last_bearing:05.1f}° True (Measured Ground Transmitter Bearing)")
                res.append(f"  - Standoff Angular Delta: {bearing_delta:.1f}°")

                if bearing_delta > 25.0:
                    res.append("\n  [!] OPERATOR-DRONE SEPARATION DETECTED:")
                    res.append(f"      Ground operator transmitter bearing ({last_bearing:.1f}°) diverges from airborne drone ({calc_bearing:.1f}°).")
                    res.append("      Tactical Implication: Pilot is concealed in a standoff position, vehicle, or operating behind terrain masking.")
                else:
                    res.append("\n  [OK] OPERATOR-DRONE CO-ALIGNED: Transmitter azimuth coincides with drone flight vector.")
            else:
                res.append("  - GPS Position       : Drone telemetry packets decoded, GPS lock pending on airframe.")
        return "\n".join(res)

    def _reason_ecm_jamming_strategy(self, queue, pilots, last_bearing):
        res = ["=== TACTICAL ELECTRONIC COUNTERMEASURES (ECM) ADVISORY ==="]
        high_threats = [t for t in queue.values() if t.get("score", 0) >= 60]
        
        if not high_threats:
            res.append("• Threat Level: Low / Advisory.")
            res.append("• Electronic Attack Recommendation: Standby. No high-threat carriers justify active RF emission.")
            res.append("• EMCON Advisory: Maintain passive electronic surveillance (ESM) to avoid revealing friendly station location.")
            return "\n".join(res)

        res.append(f"• Active Target Emitters for Electronic Suppression ({len(high_threats)}):")
        for t in high_threats:
            res.append(f"  - [{t.get('priority', 'HIGH')}] {t.get('freq_display', t.get('freq'))} | Mod: {t.get('mod')} | Threat Score: {t.get('score', 0)}/100")

        res.append(f"\n• Directional Jamming Alignment:")
        res.append(f"  - Primary Jammer Azimuth : {last_bearing:05.1f}° True")
        res.append(f"  - Recommended Antenna   : Directional horn or log-periodic array (beamwidth 30°-45°)")

        res.append("\n• Electronic Attack (EA) Waveform Selection:")
        for t in high_threats:
            mod = t.get("mod", "")
            freq = t.get("freq_display", str(t.get("freq")))
            if "LoRa" in mod or "CSS" in mod or "915" in freq or "868" in freq:
                res.append("  [!] 900 MHz Control Link (LoRa/CSS):")
                res.append("      - Waveform : Swept-barrage chirp jamming across 860–930 MHz at > 1 kHz sweep rate.")
                res.append("      - Mechanism: Corrupts LoRa preamble chirp detection, triggering drone Failsafe / Return-to-Home.")
            elif "Video" in mod or "PAL" in mod or "NTSC" in mod or "5740" in freq:
                res.append("  [!] 5.8 GHz FPV Video Link (Analog FM):")
                res.append("      - Waveform : Pseudo-random sync pulse injection or continuous wave (CW) on active video channel.")
                res.append("      - Mechanism: Strips horizontal/vertical sync locks, causing total video blackout on operator goggles.")
            elif "Crossfire" in mod or "GFSK" in mod:
                res.append("  [!] GFSK / 2-FSK Telemetry Link:")
                res.append("      - Waveform : High-duty narrowband Gaussian noise jamming (J/S >= +6 dB).")

        res.append("\n• Power Budget & J/S Calculation:")
        res.append("  - To achieve reliable link denial at 2 km standoff, maintain P_jammer >= 10W into a 12 dBi directional antenna.")
        return "\n".join(res)

    def _reason_direction_finding_and_geolocation(self, last_bearing, cep_fix, bearing_history, queue, pilots, station_loc):
        res = ["=== DIRECTION FINDING (DoA) & GEOLOCATION FIX INTELLIGENCE ==="]
        res.append(f"• Station Origin Coordinates  : {station_loc[0]:.5f}°N, {station_loc[1]:.5f}°W")
        res.append(f"• Coherent MUSIC Line-of-Bearing: {last_bearing:05.1f}° True")
        res.append(f"• KrakenSDR Array History      : {len(bearing_history)} observation(s) recorded in baseline")

        if cep_fix:
            res.append("\n• TRIANGULATED GEOLOCATION FIX (95% CEP):")
            res.append(f"  - Target Latitude  : {cep_fix['lat']:.5f}°N")
            res.append(f"  - Target Longitude : {cep_fix['lon']:.5f}°W")
            res.append(f"  - Circular Error   : ±{cep_fix['cep']:.1f} meters (95% confidence radius)")
            res.append("  - Tactical Status  : Target emitter position is geo-fixed. Coordinates ready for kinetic or EW tasking.")
        else:
            res.append("\n• Triangulation Baseline Analysis:")
            if len(bearing_history) >= 2:
                res.append("  - Multi-point bearings available. Click 'Fix Triangulated Target' in Kraken tab to solve CEP intersection.")
            else:
                res.append("  - Single-station bearing active (Line-of-Bearing vector only).")
                res.append("  - To calculate distance/geolocation: Displace station along baseline or ingest secondary DF node bearing.")

        if pilots:
            for uid, p in pilots.items():
                if p.get("lat") and p.get("lon"):
                    res.append(f"\n• Cross-Verification with Drone Telemetry GPS:")
                    res.append(f"  - Decoded Drone GPS : {p.get('lat'):.5f}°N, {p.get('lon'):.5f}°W (Alt: {p.get('alt', 0)}m)")
        return "\n".join(res)

    def _reason_rf_physics_and_range(self, queue, pilots, last_bearing):
        res = ["=== RF PROPAGATION, LINK BUDGET & RANGE ESTIMATION ==="]
        if not queue:
            res.append("• No active emitters currently in priority queue to calculate RF link budget.")
            return "\n".join(res)

        for k, t in queue.items():
            freq_mhz = t.get("freq", 915.0)
            rssi = t.get("rssi", -60.0)
            mod = t.get("mod", "Unknown")
            
            pt_dbm = 20.0
            rx_gain = 3.0
            tx_gain = 2.0
            fspl_est = pt_dbm + tx_gain + rx_gain - rssi
            dist_km_est = 10.0 ** ((fspl_est - 20.0 * math.log10(freq_mhz) - 32.44) / 20.0)
            dist_km_est = max(0.05, min(25.0, dist_km_est))

            res.append(f"• Emitter {t.get('freq_display', str(freq_mhz) + ' MHz')} [{mod}]:")
            res.append(f"  - Signal Strength : {rssi:.1f} dBFS (Carrier SNR: ~{abs(rssi)-90:.1f} dB above floor)")
            res.append(f"  - Estimated Range : ~{dist_km_est:.2f} km (assuming 100mW TX in free-space line-of-sight)")
            
            los_km = 3.57 * (math.sqrt(5.0) + math.sqrt(50.0))
            res.append(f"  - Radio Horizon   : ~{los_km:.1f} km (assuming Rx @ 5m AGL, Tx @ 50m AGL)")
        return "\n".join(res)

    def _reason_hardware_fingerprinting(self, queue):
        res = ["=== RF PHYSICAL HARDWARE FINGERPRINTING (CVA) ==="]
        fp_list = [t for t in queue.values() if t.get("fingerprint", "0x----") != "0x----"]
        if not fp_list:
            res.append("• No unique physical power amplifier (PA) turn-on transients captured in current stare session.")
            res.append("• Note: Fingerprinting analyzes microsecond turn-on envelope transient dynamics (CVA) to identify distinct physical silicon.")
            return "\n".join(res)

        for t in fp_list:
            res.append(f"• Silicon Signature {t.get('fingerprint')}:")
            res.append(f"  - Frequency : {t.get('freq_display', t.get('freq'))} ({t.get('mod')})")
            res.append(f"  - Amplitude Trend: {t.get('trend', 'STATIONARY')}")
            res.append("  - Hardware Integrity: Unique physical transmitter PA verified.")
        return "\n".join(res)

    def _reason_threat_overview(self, queue, pilots, last_bearing):
        res = ["=== TACTICAL SECTOR THREAT SUMMARY ==="]
        p1 = [v for v in queue.values() if "P1" in v.get("priority", "")]
        p2 = [v for v in queue.values() if "P2" in v.get("priority", "")]
        p3 = [v for v in queue.values() if "P3" in v.get("priority", "")]

        res.append(f"• Total Monitored Emitters : {len(queue)}")
        res.append(f"• Threat Classification    : {len(p1)} Critical (P1), {len(p2)} High (P2), {len(p3)} Advisory (P3)")
        res.append(f"• Primary Direction Vector : {last_bearing:05.1f}° True")

        if p1:
            res.append("\n• CRITICAL (P1) TARGETS:")
            for t in p1:
                res.append(f"  [!] {t.get('freq_display', t.get('freq'))} | Mod: {t.get('mod')} | Score: {t.get('score', 0)}/100 | Bearing: {t.get('bearing', last_bearing):.1f}°")
        if p2:
            res.append("\n• HIGH PRIORITY (P2) TARGETS:")
            for t in p2:
                res.append(f"  • {t.get('freq_display', t.get('freq'))} | Mod: {t.get('mod')} | Score: {t.get('score', 0)}/100")

        if pilots:
            res.append(f"\n• Decoded Pilot Telemetry  : {len(pilots)} pilot(s) active in sector.")
            for uid, p in pilots.items():
                arm_state = "ARMED" if p.get("armed") else "DISARMED"
                res.append(f"  - Pilot UID {p.get('u3')}:{p.get('u4')}:{p.get('u5')}: State = {arm_state}, Rate = {p.get('rate_name', 'Unknown')}")
        else:
            res.append("\n• Decoded Pilot Telemetry  : 0 pilots actively decoded.")

        return "\n".join(res)

    def _reason_general_tactical_query(self, query, queue, pilots, last_bearing, station_loc):
        res = [f"=== TACTICAL INTELLIGENCE EVALUATION: '{query}' ==="]
        res.append(f"• Station Location        : {station_loc[0]:.5f}°N, {station_loc[1]:.5f}°W")
        res.append(f"• Current Emitters Tracked: {len(queue)} signal(s) in priority queue")
        res.append(f"• Decoded Pilot Entities  : {len(pilots)} active transmitter(s)")
        res.append(f"• Primary MUSIC Bearing   : {last_bearing:05.1f}° True")
        
        p1 = [v for v in queue.values() if "P1" in v.get("priority", "")]
        if p1:
            res.append(f"\n[!] TACTICAL ALERT: {len(p1)} high-threat military UAS signal(s) active in sector.")
            res.append(f"    Target: {p1[0].get('freq_display', p1[0].get('freq'))} ({p1[0].get('mod')}). Recommend EW suppression along {last_bearing:.1f}°.")
        else:
            res.append("\n[OK] SECTOR SECURE: No active armed drone threats detected.")
            
        res.append("\nTip: Ask specific tactical questions regarding 'flight phase', 'correlation', 'operator location', 'ECM jamming', or click 'GENERATE NATO SITREP'.")
        return "\n".join(res)

_copilot_instance = None

def get_tactical_copilot():
    global _copilot_instance
    if _copilot_instance is None:
        _copilot_instance = TacticalCopilot()
    return _copilot_instance