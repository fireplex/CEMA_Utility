"""
AeroTrack: Standalone Tactical Air Situation & Counter-UAS Radar Suite
CEMA Utility Multi-Domain Aerospace Intelligence

Hardware Integration:
1. HackRF One (1090 MHz Mode-S & ADS-B Extended Squitter).
2. Heltec WiFi LoRa 32 V3 (868/915 MHz Drone Telemetry & Pilot Link on COM6).
"""

import os
import sys
import time
import math
import json
import threading
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QUrl
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QLineEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QGroupBox, QComboBox, QDoubleSpinBox,
    QSpinBox, QCheckBox, QSplitter, QFrame, QMessageBox
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

from hackrf_adsb_thread import HackRFADSBThread
from heltec_bridge import HeltecLoraThread

class AeroTrackApp(QMainWindow):
    drone_telemetry_signal = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AeroTrack - Tactical Air Situation && Counter-UAS Radar Suite")
        self.resize(1500, 920)

        self.ref_lat = 51.5074
        self.ref_lon = -0.1278
        self.aircraft_db = {}
        self.drones_db = {}
        self.selected_icao = None

        self.adsb_thread = None
        self.heltec_bridge = None

        self.setup_ui()
        self.start_adsb_receiver()
        self.start_heltec_receiver()

        # UI Refresh Timer (10 Hz)
        self.ui_timer = QTimer()
        self.ui_timer.timeout.connect(self.refresh_airspace_ui)
        self.ui_timer.start(100)

    def setup_ui(self):
        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)
        self.setCentralWidget(main_widget)

        # Style sheet
        self.setStyleSheet("""
            QMainWindow { background-color: #0b0f19; }
            QWidget { color: #e2e8f0; font-family: 'Segoe UI', Arial, sans-serif; }
            QGroupBox {
                border: 1px solid #1e293b;
                border-radius: 6px;
                margin-top: 8px;
                font-size: 11px;
                font-weight: bold;
                color: #38bdf8;
                padding-top: 10px;
                background-color: #0d1322;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
            QLabel { font-size: 12px; }
            QPushButton {
                background-color: #1e293b;
                color: #38bdf8;
                font-weight: bold;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton:hover { background-color: #334155; border-color: #38bdf8; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background-color: #060a14;
                color: #f8fafc;
                border: 1px solid #1e293b;
                border-radius: 4px;
                padding: 4px;
            }
            QTableWidget {
                background-color: #060a14;
                gridline-color: #1e293b;
                color: #e2e8f0;
                font-family: monospace;
                font-size: 11px;
                border: 1px solid #1e293b;
            }
            QHeaderView::section {
                background-color: #0f172a;
                color: #38bdf8;
                font-weight: bold;
                padding: 4px;
                border: 1px solid #1e293b;
            }
        """)

        # Top Tactical Status HUD
        hud_layout = QHBoxLayout()
        
        self.hud_ac_badge = QLabel("AIRCRAFT: 0")
        self.hud_ac_badge.setStyleSheet("background-color: #0f172a; color: #10b981; font-weight: bold; padding: 6px 12px; border: 1px solid #10b981; border-radius: 4px; font-family: monospace; font-size: 13px;")
        
        self.hud_drone_badge = QLabel("DRONES: 0")
        self.hud_drone_badge.setStyleSheet("background-color: #0f172a; color: #a855f7; font-weight: bold; padding: 6px 12px; border: 1px solid #a855f7; border-radius: 4px; font-family: monospace; font-size: 13px;")

        self.hud_emergency_badge = QLabel("EMERGENCIES: NONE")
        self.hud_emergency_badge.setStyleSheet("background-color: #0f172a; color: #38bdf8; font-weight: bold; padding: 6px 12px; border: 1px solid #334155; border-radius: 4px; font-family: monospace; font-size: 13px;")

        self.hud_incursion_badge = QLabel("AIRSPACE HAZARD: CLEAR")
        self.hud_incursion_badge.setStyleSheet("background-color: #0f172a; color: #10b981; font-weight: bold; padding: 6px 12px; border: 1px solid #10b981; border-radius: 4px; font-family: monospace; font-size: 13px;")

        self.hud_hackrf_badge = QLabel("HACKRF 1090MHz: STANDBY")
        self.hud_hackrf_badge.setStyleSheet("background-color: #0f172a; color: #f59e0b; font-weight: bold; padding: 6px 12px; border: 1px solid #f59e0b; border-radius: 4px; font-family: monospace; font-size: 13px;")

        self.hud_heltec_badge = QLabel("HELTEC COM6: STANDBY")
        self.hud_heltec_badge.setStyleSheet("background-color: #0f172a; color: #94a3b8; font-weight: bold; padding: 6px 12px; border: 1px solid #334155; border-radius: 4px; font-family: monospace; font-size: 13px;")

        hud_layout.addWidget(self.hud_ac_badge)
        hud_layout.addWidget(self.hud_drone_badge)
        hud_layout.addWidget(self.hud_emergency_badge)
        hud_layout.addWidget(self.hud_incursion_badge)
        hud_layout.addWidget(self.hud_hackrf_badge)
        hud_layout.addWidget(self.hud_heltec_badge)
        main_layout.addLayout(hud_layout)

        # Central Splitter (Map on Left, Intel Roster on Right)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter, 1)

        # --- LEFT: Tactical Aeronautical Leaflet Map ---
        map_container = QWidget()
        map_layout = QVBoxLayout(map_container)
        map_layout.setContentsMargins(0, 0, 0, 0)

        self.map_view = QWebEngineView()
        self.init_leaflet_map()
        map_layout.addWidget(self.map_view)
        splitter.addWidget(map_container)

        # --- RIGHT: Target Intel & Control Panel ---
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        # Filter Box
        filter_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search Callsign, ICAO, or Squawk...")
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["All Air Entities", "Civilian Commercial", "Military Assets", "Low-Altitude Drones", "Emergencies Only"])
        filter_layout.addWidget(self.search_input, 2)
        filter_layout.addWidget(self.filter_combo, 1)
        right_layout.addLayout(filter_layout)

        # Live Flight Table
        self.flight_table = QTableWidget(0, 8)
        self.flight_table.setHorizontalHeaderLabels(["Callsign", "ICAO", "Alt (ft)", "Spd (kts)", "Trk", "V/S (fpm)", "Squawk", "Class"])
        self.flight_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.flight_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.flight_table.itemSelectionChanged.connect(self.on_table_selection_changed)
        right_layout.addWidget(self.flight_table, 2)

        # Target Intel Details Card
        self.intel_group = QGroupBox("Selected Air Entity Intelligence Dossier")
        intel_grid = QGridLayout(self.intel_group)
        intel_grid.setContentsMargins(8, 8, 8, 8)
        intel_grid.setSpacing(4)

        self.intel_cs_lbl = QLabel("Callsign: --")
        self.intel_icao_lbl = QLabel("ICAO Hex: --")
        self.intel_country_lbl = QLabel("Country: --")
        self.intel_class_lbl = QLabel("Category: --")
        self.intel_alt_lbl = QLabel("Altitude: --")
        self.intel_spd_lbl = QLabel("Groundspeed: --")
        self.intel_coords_lbl = QLabel("Coordinates: --")
        self.intel_status_lbl = QLabel("Status: Awaiting selection")
        self.intel_status_lbl.setStyleSheet("color: #38bdf8; font-weight: bold;")

        intel_grid.addWidget(self.intel_cs_lbl, 0, 0)
        intel_grid.addWidget(self.intel_icao_lbl, 0, 1)
        intel_grid.addWidget(self.intel_country_lbl, 1, 0)
        intel_grid.addWidget(self.intel_class_lbl, 1, 1)
        intel_grid.addWidget(self.intel_alt_lbl, 2, 0)
        intel_grid.addWidget(self.intel_spd_lbl, 2, 1)
        intel_grid.addWidget(self.intel_coords_lbl, 3, 0, 1, 2)
        intel_grid.addWidget(self.intel_status_lbl, 4, 0, 1, 2)
        right_layout.addWidget(self.intel_group, 1)

        # Hardware & RF Control Box
        ctrl_group = QGroupBox("Hardware Sensors && Airspace Settings")
        ctrl_layout = QGridLayout(ctrl_group)
        ctrl_layout.setContentsMargins(6, 6, 6, 6)
        ctrl_layout.setSpacing(6)

        self.source_combo = QComboBox()
        self.source_combo.addItems([
            "Live OpenSky Network (Real-Time Internet Airspace)",
            "HackRF One 1090 MHz SDR (Hardware RF)",
            "Local Mode-S TCP Stream (127.0.0.1:30002)",
            "Tactical Synthetic Simulation (Offline)"
        ])
        self.source_combo.currentIndexChanged.connect(self.start_adsb_receiver)

        self.ref_lat_input = QDoubleSpinBox()
        self.ref_lat_input.setRange(-90.0, 90.0)
        self.ref_lat_input.setValue(self.ref_lat)
        self.ref_lat_input.setDecimals(4)

        self.ref_lon_input = QDoubleSpinBox()
        self.ref_lon_input.setRange(-180.0, 180.0)
        self.ref_lon_input.setValue(self.ref_lon)
        self.ref_lon_input.setDecimals(4)

        self.restart_sdr_btn = QPushButton("RESTART FEED RECEIVER")
        self.restart_sdr_btn.clicked.connect(self.start_adsb_receiver)

        self.restart_heltec_btn = QPushButton("RECONNECT HELTEC COM6")
        self.restart_heltec_btn.clicked.connect(self.start_heltec_receiver)

        ctrl_layout.addWidget(QLabel("Feed Source:"), 0, 0)
        ctrl_layout.addWidget(self.source_combo, 0, 1, 1, 3)
        ctrl_layout.addWidget(QLabel("Station Lat:"), 1, 0)
        ctrl_layout.addWidget(self.ref_lat_input, 1, 1)
        ctrl_layout.addWidget(QLabel("Lon:"), 1, 2)
        ctrl_layout.addWidget(self.ref_lon_input, 1, 3)
        ctrl_layout.addWidget(self.restart_sdr_btn, 2, 0, 1, 2)
        ctrl_layout.addWidget(self.restart_heltec_btn, 2, 2, 1, 2)
        right_layout.addWidget(ctrl_group)

        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

    def init_leaflet_map(self):
        map_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8" />
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <style>
                body, html, #map {{ height: 100%; margin: 0; padding: 0; background: #060a14; }}
                .ac-label {{
                    background: rgba(15, 23, 42, 0.85);
                    border: 1px solid #38bdf8;
                    color: #f8fafc;
                    font-family: monospace;
                    font-size: 10px;
                    font-weight: bold;
                    padding: 2px 4px;
                    border-radius: 3px;
                }}
                .ac-emergency {{
                    background: rgba(239, 68, 68, 0.95);
                    border: 1px solid #ffffff;
                    color: #ffffff;
                    animation: blinker 1s linear infinite;
                }}
                @keyframes blinker {{ 50% {{ opacity: 0.3; }} }}
            </style>
        </head>
        <body>
            <div id="map"></div>
            <script>
                var map = L.map('map', {{ zoomControl: true }}).setView([{self.ref_lat}, {self.ref_lon}], 10);
                L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
                    attribution: '&copy; CartoDB &copy; OpenStreetMap',
                    maxZoom: 18
                }}).addTo(map);

                // Range Rings (10nm, 25nm, 50nm)
                var rings = [18520, 46300, 92600];
                for (var r of rings) {{
                    L.circle([{self.ref_lat}, {self.ref_lon}], {{
                        radius: r,
                        color: '#334155',
                        weight: 1,
                        fill: false,
                        dashArray: '4, 4'
                    }}).addTo(map);
                }}

                // Ground Station Marker
                L.circleMarker([{self.ref_lat}, {self.ref_lon}], {{
                    radius: 7,
                    color: '#38bdf8',
                    fillColor: '#0284c7',
                    fillOpacity: 1
                }}).bindTooltip("CEMA BASE STATION", {{ permanent: true, direction: 'top', className: 'ac-label' }}).addTo(map);

                var markers = {{}};
                var trails = {{}};
                var conflictLines = [];

                function updateAirEntity(icao, lat, lon, alt, spd, trk, callsign, is_drone, is_emergency) {{
                    var color = '#10b981'; // Green high altitude
                    if (is_emergency) {{
                        color = '#ef4444'; // Red
                    }} else if (is_drone) {{
                        color = '#a855f7'; // Purple drone
                    }} else if (alt < 10000) {{
                        color = '#f59e0b'; // Amber low altitude
                    }} else if (alt < 25000) {{
                        color = '#38bdf8'; // Cyan mid altitude
                    }}

                    var labelText = (callsign && callsign !== 'UNKNOWN') ? callsign : icao;
                    labelText += " | " + alt + "ft | " + spd + "kts";

                    if (!markers[icao]) {{
                        var marker = L.circleMarker([lat, lon], {{
                            radius: is_drone ? 6 : 8,
                            color: color,
                            fillColor: color,
                            fillOpacity: 0.8,
                            weight: 2
                        }}).addTo(map);

                        marker.bindTooltip(labelText, {{
                            permanent: true,
                            direction: 'right',
                            className: is_emergency ? 'ac-label ac-emergency' : 'ac-label'
                        }});
                        markers[icao] = marker;

                        var trail = L.polyline([[lat, lon]], {{
                            color: color,
                            weight: 2,
                            opacity: 0.6
                        }}).addTo(map);
                        trails[icao] = trail;
                    }} else {{
                        markers[icao].setLatLng([lat, lon]);
                        markers[icao].setStyle({{ color: color, fillColor: color }});
                        markers[icao].setTooltipContent(labelText);
                        
                        var latlngs = trails[icao].getLatLngs();
                        latlngs.push([lat, lon]);
                        if (latlngs.length > 30) latlngs.shift();
                        trails[icao].setLatLngs(latlngs);
                    }}
                }}

                function removeStaleEntity(icao) {{
                    if (markers[icao]) {{
                        map.removeLayer(markers[icao]);
                        delete markers[icao];
                    }}
                    if (trails[icao]) {{
                        map.removeLayer(trails[icao]);
                        delete trails[icao];
                    }}
                }}

                function drawConflictLine(lat1, lon1, lat2, lon2, text) {{
                    var line = L.polyline([[lat1, lon1], [lat2, lon2]], {{
                        color: '#ef4444',
                        weight: 3,
                        dashArray: '5, 5'
                    }}).addTo(map);
                    conflictLines.push(line);
                }}

                function clearConflicts() {{
                    for (var l of conflictLines) {{
                        map.removeLayer(l);
                    }}
                    conflictLines = [];
                }}
            </script>
        </body>
        </html>
        """
        self.map_view.setHtml(map_html)

    def start_adsb_receiver(self):
        if self.adsb_thread and self.adsb_thread.isRunning():
            self.adsb_thread.stop()
        
        self.ref_lat = self.ref_lat_input.value()
        self.ref_lon = self.ref_lon_input.value()

        mode_idx = self.source_combo.currentIndex() if hasattr(self, 'source_combo') else 0
        modes = ["live_opensky", "hackrf_sdr", "tcp_beast", "simulation"]
        mode = modes[mode_idx] if mode_idx < len(modes) else "live_opensky"

        self.aircraft_db.clear()
        
        self.adsb_thread = HackRFADSBThread(ref_lat=self.ref_lat, ref_lon=self.ref_lon, mode=mode)
        self.adsb_thread.aircraft_updated_signal.connect(self.on_aircraft_updated)
        self.adsb_thread.stats_updated_signal.connect(self.on_adsb_stats_updated)
        self.adsb_thread.start()

    def start_heltec_receiver(self):
        if self.heltec_bridge and self.heltec_bridge.isRunning():
            self.heltec_bridge.stop()
        
        self.heltec_bridge = HeltecLoraThread(port="COM6", baud=115200)
        self.heltec_bridge.gps_received.connect(self.on_heltec_drone_gps)
        self.heltec_bridge.status_changed.connect(self.on_heltec_status)
        self.heltec_bridge.start()

    def on_aircraft_updated(self, ac_dict):
        self.aircraft_db[ac_dict["icao"]] = ac_dict
        if ac_dict.get("lat") and ac_dict.get("lon"):
            js = f"if (typeof updateAirEntity === 'function') {{ updateAirEntity('{ac_dict['icao']}', {ac_dict['lat']}, {ac_dict['lon']}, {ac_dict['alt_ft']}, {ac_dict['speed_kts']}, {ac_dict['track']}, '{ac_dict['callsign']}', false, {str(ac_dict['emergency']).lower()}); }}"
            self.map_view.page().runJavaScript(js)

    def on_heltec_drone_gps(self, data):
        u3 = data.get("u3", 0)
        u4 = data.get("u4", 0)
        u5 = data.get("u5", 0)
        lat = data.get("lat", 0.0)
        lon = data.get("lon", 0.0)
        alt = data.get("alt", 0.0)
        spd = data.get("spd", 0.0)

        uid = f"DRONE-{u3:02X}:{u4:02X}:{u5:02X}"
        self.drones_db[uid] = {
            "icao": uid,
            "callsign": uid,
            "lat": lat,
            "lon": lon,
            "alt_ft": int(alt * 3.28084), # meters to feet
            "speed_kts": int(spd * 0.539957), # km/h to knots
            "track": 0.0,
            "vert_rate": 0,
            "squawk": "UAS",
            "emergency": False,
            "mil_status": "Tactical Drone",
            "country": "Local UAS",
            "last_seen": time.time()
        }
        js = f"if (typeof updateAirEntity === 'function') {{ updateAirEntity('{uid}', {lat}, {lon}, {int(alt * 3.28084)}, {int(spd * 0.539957)}, 0.0, '{uid}', true, false); }}"
        self.map_view.page().runJavaScript(js)

    def on_heltec_status(self, msg, connected):
        if connected:
            self.hud_heltec_badge.setText("HELTEC COM6: CONNECTED")
            self.hud_heltec_badge.setStyleSheet("background-color: #0f172a; color: #10b981; font-weight: bold; padding: 6px 12px; border: 1px solid #10b981; border-radius: 4px; font-family: monospace; font-size: 13px;")
        else:
            self.hud_heltec_badge.setText("HELTEC COM6: STANDBY")
            self.hud_heltec_badge.setStyleSheet("background-color: #0f172a; color: #94a3b8; font-weight: bold; padding: 6px 12px; border: 1px solid #334155; border-radius: 4px; font-family: monospace; font-size: 13px;")

    def on_adsb_stats_updated(self, stats):
        self.hud_hackrf_badge.setText(f"{stats['hardware']} [{stats['msg_rate']} msgs/s]")
        self.hud_hackrf_badge.setStyleSheet("background-color: #0f172a; color: #10b981; font-weight: bold; padding: 6px 12px; border: 1px solid #10b981; border-radius: 4px; font-family: monospace; font-size: 13px;")

    def refresh_airspace_ui(self):
        now = time.time()
        # Prune stale entries (> 30s)
        stale_ac = [k for k, v in self.aircraft_db.items() if now - v.get("last_seen", 0) > 30.0]
        for k in stale_ac:
            del self.aircraft_db[k]
            self.map_view.page().runJavaScript(f"if (typeof removeStaleEntity === 'function') {{ removeStaleEntity('{k}'); }}")

        stale_dr = [k for k, v in self.drones_db.items() if now - v.get("last_seen", 0) > 30.0]
        for k in stale_dr:
            del self.drones_db[k]
            self.map_view.page().runJavaScript(f"if (typeof removeStaleEntity === 'function') {{ removeStaleEntity('{k}'); }}")

        # Update HUD Metrics
        num_ac = len(self.aircraft_db)
        num_dr = len(self.drones_db)
        self.hud_ac_badge.setText(f"AIRCRAFT: {num_ac}")
        self.hud_drone_badge.setText(f"DRONES: {num_dr}")

        # Check for emergencies
        emergencies = [v for v in self.aircraft_db.values() if v.get("emergency")]
        if emergencies:
            em = emergencies[0]
            self.hud_emergency_badge.setText(f"ALERT: {em.get('callsign', em['icao'])} - {em.get('emergency_type', 'EMERGENCY')}")
            self.hud_emergency_badge.setStyleSheet("background-color: #7f1d1d; color: #ffffff; font-weight: bold; padding: 6px 12px; border: 1px solid #ef4444; border-radius: 4px; font-family: monospace; font-size: 13px;")
        else:
            self.hud_emergency_badge.setText("EMERGENCIES: NONE")
            self.hud_emergency_badge.setStyleSheet("background-color: #0f172a; color: #38bdf8; font-weight: bold; padding: 6px 12px; border: 1px solid #334155; border-radius: 4px; font-family: monospace; font-size: 13px;")

        # Evaluate Airspace Incursions / Conflict Alert (Drone near Low Aircraft)
        hazard_detected = False
        self.map_view.page().runJavaScript("if (typeof clearConflicts === 'function') { clearConflicts(); }")
        
        for d_id, drone in self.drones_db.items():
            d_lat = drone.get("lat")
            d_lon = drone.get("lon")
            d_alt = drone.get("alt_ft", 0)
            if not d_lat or not d_lon: continue

            for a_id, ac in self.aircraft_db.items():
                a_lat = ac.get("lat")
                a_lon = ac.get("lon")
                a_alt = ac.get("alt_ft", 0)
                if not a_lat or not a_lon: continue

                # Distance in NM
                dist_nm = math.hypot((d_lat - a_lat) * 60.0, (d_lon - a_lon) * 60.0 * math.cos(math.radians(d_lat)))
                alt_diff_ft = abs(d_alt - a_alt)

                if dist_nm < 5.0 and alt_diff_ft < 3000:
                    hazard_detected = True
                    self.map_view.page().runJavaScript(f"if (typeof drawConflictLine === 'function') {{ drawConflictLine({d_lat}, {d_lon}, {a_lat}, {a_lon}, '{dist_nm:.1f}nm'); }}")

        if hazard_detected:
            self.hud_incursion_badge.setText("AIRSPACE HAZARD: DRONE INCURSION PROXIMITY ALERT")
            self.hud_incursion_badge.setStyleSheet("background-color: #7f1d1d; color: #facc15; font-weight: bold; padding: 6px 12px; border: 1px solid #eab308; border-radius: 4px; font-family: monospace; font-size: 13px;")
        else:
            self.hud_incursion_badge.setText("AIRSPACE HAZARD: CLEAR")
            self.hud_incursion_badge.setStyleSheet("background-color: #0f172a; color: #10b981; font-weight: bold; padding: 6px 12px; border: 1px solid #10b981; border-radius: 4px; font-family: monospace; font-size: 13px;")

        # Populate Flight Table
        all_entities = list(self.aircraft_db.values()) + list(self.drones_db.values())
        query = self.search_input.text().strip().upper()
        filter_mode = self.filter_combo.currentText()

        filtered = []
        for e in all_entities:
            if query and (query not in e.get("callsign", "").upper() and query not in e.get("icao", "").upper() and query not in e.get("squawk", "").upper()):
                continue
            if filter_mode == "Civilian Commercial" and "Civil" not in e.get("mil_status", ""): continue
            if filter_mode == "Military Assets" and "Military" not in e.get("mil_status", ""): continue
            if filter_mode == "Low-Altitude Drones" and "DRONE" not in e.get("icao", ""): continue
            if filter_mode == "Emergencies Only" and not e.get("emergency"): continue
            filtered.append(e)

        self.flight_table.setRowCount(len(filtered))
        for row, e in enumerate(filtered):
            cs_item = QTableWidgetItem(e.get("callsign", "UNKNOWN"))
            icao_item = QTableWidgetItem(e.get("icao", ""))
            alt_item = QTableWidgetItem(f"{e.get('alt_ft', 0):,}")
            spd_item = QTableWidgetItem(str(e.get("speed_kts", 0)))
            trk_item = QTableWidgetItem(f"{e.get('track', 0.0):.0f}°")
            vs_item = QTableWidgetItem(f"{e.get('vert_rate', 0):+d}")
            sq_item = QTableWidgetItem(e.get("squawk", "----"))
            class_item = QTableWidgetItem(e.get("mil_status", "Civil"))

            if e.get("emergency"):
                for item in [cs_item, icao_item, alt_item, spd_item, trk_item, vs_item, sq_item, class_item]:
                    item.setForeground(Qt.GlobalColor.red)
            elif "DRONE" in e.get("icao", ""):
                for item in [cs_item, icao_item, alt_item, spd_item, trk_item, vs_item, sq_item, class_item]:
                    item.setForeground(Qt.GlobalColor.magenta)

            self.flight_table.setItem(row, 0, cs_item)
            self.flight_table.setItem(row, 1, icao_item)
            self.flight_table.setItem(row, 2, alt_item)
            self.flight_table.setItem(row, 3, spd_item)
            self.flight_table.setItem(row, 4, trk_item)
            self.flight_table.setItem(row, 5, vs_item)
            self.flight_table.setItem(row, 6, sq_item)
            self.flight_table.setItem(row, 7, class_item)

    def on_table_selection_changed(self):
        selected_rows = self.flight_table.selectedItems()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        icao_item = self.flight_table.item(row, 1)
        if not icao_item: return
        icao = icao_item.text()

        entity = self.aircraft_db.get(icao) or self.drones_db.get(icao)
        if not entity: return

        self.intel_cs_lbl.setText(f"Callsign: {entity.get('callsign')}")
        self.intel_icao_lbl.setText(f"ICAO Hex: {entity.get('icao')}")
        self.intel_country_lbl.setText(f"Country: {entity.get('country')}")
        self.intel_class_lbl.setText(f"Category: {entity.get('mil_status')}")
        self.intel_alt_lbl.setText(f"Altitude: {entity.get('alt_ft', 0):,} ft (V/S: {entity.get('vert_rate', 0):+d} fpm)")
        self.intel_spd_lbl.setText(f"Groundspeed: {entity.get('speed_kts', 0)} kts | Track: {entity.get('track', 0):.1f}°")
        self.intel_coords_lbl.setText(f"Coordinates: {entity.get('lat', 'N/A')}, {entity.get('lon', 'N/A')} (Squawk: {entity.get('squawk')})")

        if entity.get("emergency"):
            self.intel_status_lbl.setText(f"EMERGENCY ACTIVE: {entity.get('emergency_type')}")
            self.intel_status_lbl.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 13px;")
        elif "DRONE" in entity.get("icao", ""):
            self.intel_status_lbl.setText("LOCAL LOW-ALTITUDE UAS: Live Heltec LoRa/GPS Active")
            self.intel_status_lbl.setStyleSheet("color: #c084fc; font-weight: bold;")
        else:
            self.intel_status_lbl.setText("AIRSPACE ASSET: Normal Flight Dynamics")
            self.intel_status_lbl.setStyleSheet("color: #10b981; font-weight: bold;")

    def closeEvent(self, event):
        if self.adsb_thread:
            self.adsb_thread.stop()
        if self.heltec_bridge:
            self.heltec_bridge.stop()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = AeroTrackApp()
    window.show()
    sys.exit(app.exec())
