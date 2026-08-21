# CEMA Advanced RF Tracking System: Operator's Manual

The CEMA RF Tracking Application is a standalone, real-time Signals Intelligence (SIGINT) platform designed for tactical Electronic Warfare operators. It interfaces directly with a HackRF Software Defined Radio to provide high-speed RF tracking, anomaly detection, and automated target intelligence.

---

## Core Operating Modes

The application relies on a dual-engine architecture to hunt and analyze signals.

### 1. Sweep Mode (Wideband Hunter)
> [!NOTE]
> **Use Case:** Broad spectrum reconnaissance and Frequency Hopper detection.

In Sweep mode, the SDR sweeps rapidly across a massive swath of spectrum (e.g., 30 MHz to 500 MHz). It sacrifices fine resolution to give you a macro-view of the RF battlespace. This is your primary tool for finding where the enemy is operating.

### 2. Stare Mode (Narrowband Analyzer)
> [!NOTE]
> **Use Case:** Deep, high-resolution analysis of a specific target.

In Stare mode, the SDR locks onto a specific 2 MHz chunk of spectrum and streams raw I/Q phase data. The DSP engine kicks in, identifying the exact modulation, bandwidth, and physical fingerprint of whatever is transmitting inside that 2 MHz window.

### 3. The "Hunter-Killer" Transition
> [!TIP]
> If you are in **Sweep Mode** and see an enemy transmission spike on the graph, simply **Left-Click** directly on that spike. 
> 
> The application will instantly abort the sweep, automatically tune the SDR to the exact frequency you clicked, and engage **Stare Mode** to analyze the target.

---

## User Interface & Graphs

### Real-Time Spectrum (FFT) & Masking
The top graph displays the live RF environment. 
*   **Panning & Zooming:** By default, you can click and drag the graph to pan, or right-click and drag to zoom.
*   **Mask Mode:** If you are operating in a noisy environment (like a city full of FM radio stations), click the **[MASK MODE]** toggle. You can now left-click and drag grey exclusion zones over continuous civilian signals. The intelligence algorithms will completely ignore any signals originating from inside a mask.

### Waterfall Spectrogram
The bottom-left graph acts as a scrolling history of the FFT. The color intensity corresponds to signal strength, allowing you to visually see the duration and bandwidth of past transmissions even after they have stopped. Use the **WF Sens** slider to adjust the color contrast.

### Constellation Graph (Phase Diagram)
The bottom-right graph plots the raw I/Q phase data of the center frequency. This is used by advanced operators to visually decode the modulation scheme of a target (e.g., seeing a perfect circle for FM, or 4 distinct clusters for QPSK).

---

## (Intel DB)

The core power of the CEMA application lies in the right-hand **Intel DB** tab. The software is constantly running automated algorithms in the background to profile enemy targets.

### 1. Watchlist (Drone & Link Matching)
> [!IMPORTANT]
> **Requirement:** Must be in **Stare Mode**

The software mathematically calculates the active bandwidth and modulation of live signals. It cross-references this against `watchlist.json`. If an enemy drone video link or tactical data burst powers up, the software will instantly throw a **RED ALERT** on the screen.
*   Click **EDIT WATCHLIST.JSON** to open the database file and manually add new threat signatures based on intelligence bulletins.

### 2. Hardware Fingerprinting
> [!IMPORTANT]
> **Requirement:** Must be in **Stare Mode**

Every physical radio has microscopic hardware imperfections. When an enemy keys their radio (Push-To-Talk), the software captures the split-second transient spike and hashes it into a unique Hex ID (e.g., `0x4A2B`).
*   **Tracking:** Even if the enemy changes their frequency, the physical hardware fingerprint stays the same. You will see `0x4A2B` appear at the new frequency.
*   **Renaming:** **Right-click** any fingerprint in the list to assign it a tactical name (e.g., "Alpha Squad Leader").

### 3. Network Topology (Command Mapping)
> [!TIP]
> **Tactical Value:** Identify high-value targets for Electronic Attack.

The software tracks the timing of all fingerprinted transmissions. If Radio B transmits within 5 seconds of Radio A finishing its transmission, the software deduces a "Call and Response" link. Over time, it maps out a visual command tree. 
*   If a specific radio initiates replies from multiple subordinates, it is automatically flagged with a 👑 icon, identifying the likely HV radio.

### 4. Frequency Hopping (FHSS) Tracker
> [!IMPORTANT]
> **Requirement:** Must be in **Sweep Mode**

When in Sweep Mode, the algorithm hunts for transient, bursty peaks across the entire spectrum. If it detects a rapid sequence of hops, it calculates the hop-rate and logs the hostile network.

---

## 🎛️ Hardware Tuning (SDR Parameters)

| Parameter | Function | Tactical Advice |
| :--- | :--- | :--- |
| **LNA Gain (0-40)** | Hardware RF amplifier right at the antenna. | Increase to hear faint, distant signals. **Warning:** Setting this too high in an urban environment will cause the SDR to clip and distort the entire spectrum. |
| **VGA Gain (0-62)** | Intermediate Frequency (IF) Baseband amplifier. | Used for fine-tuning the signal amplitude before it is digitized. Keep around 40 for general use. |
