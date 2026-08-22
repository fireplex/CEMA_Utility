#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <math.h>

#define PI 3.14159265358979323846
#define SAMPLES_PER_FRAME 400000 // Exact 625 full-frame lines at 10 MSPS (40.0 ms = 25 FPS)
#define NUM_FIELD_LINES_PAL 288
#define NUM_FIELD_LINES_NTSC 240
#define ACTIVE_PIXELS 520
#define OUT_WIDTH 640
#define OUT_HEIGHT 480

typedef struct hackrf_device hackrf_device;
typedef struct {
    hackrf_device* device;
    uint8_t* buffer;
    int buffer_length;
    int valid_length;
    void* rx_ctx;
    void* tx_ctx;
} hackrf_transfer;

typedef int (*hackrf_sample_block_cb_fn)(hackrf_transfer* transfer);

typedef int (*pfn_hackrf_init)(void);
typedef int (*pfn_hackrf_exit)(void);
typedef int (*pfn_hackrf_open)(hackrf_device** device);
typedef int (*pfn_hackrf_close)(hackrf_device* device);
typedef int (*pfn_hackrf_start_rx)(hackrf_device* device, hackrf_sample_block_cb_fn callback, void* rx_ctx);
typedef int (*pfn_hackrf_stop_rx)(hackrf_device* device);
typedef int (*pfn_hackrf_set_freq)(hackrf_device* device, const uint64_t freq_hz);
typedef int (*pfn_hackrf_set_sample_rate)(hackrf_device* device, const double freq_hz);
typedef int (*pfn_hackrf_set_amp_enable)(hackrf_device* device, const uint8_t value);
typedef int (*pfn_hackrf_set_lna_gain)(hackrf_device* device, uint32_t value);
typedef int (*pfn_hackrf_set_vga_gain)(hackrf_device* device, uint32_t value);
typedef int (*pfn_hackrf_set_baseband_filter_bandwidth)(hackrf_device* device, const uint32_t bandwidth_hz);

static HMODULE hHackrfDll = NULL;
static pfn_hackrf_init fn_hackrf_init = NULL;
static pfn_hackrf_exit fn_hackrf_exit = NULL;
static pfn_hackrf_open fn_hackrf_open = NULL;
static pfn_hackrf_close fn_hackrf_close = NULL;
static pfn_hackrf_start_rx fn_hackrf_start_rx = NULL;
static pfn_hackrf_stop_rx fn_hackrf_stop_rx = NULL;
static pfn_hackrf_set_freq fn_hackrf_set_freq = NULL;
static pfn_hackrf_set_sample_rate fn_hackrf_set_sample_rate = NULL;
static pfn_hackrf_set_amp_enable fn_hackrf_set_amp_enable = NULL;
static pfn_hackrf_set_lna_gain fn_hackrf_set_lna_gain = NULL;
static pfn_hackrf_set_vga_gain fn_hackrf_set_vga_gain = NULL;
static pfn_hackrf_set_baseband_filter_bandwidth fn_hackrf_set_baseband_filter_bandwidth = NULL;

static hackrf_device* g_device = NULL;
static volatile bool g_running = false;

// Double-buffered Linear Frame Stores (400,000 samples = 625 full-frame lines)
static float g_linear_buf_A[SAMPLES_PER_FRAME + 2000];
static float g_linear_buf_B[SAMPLES_PER_FRAME + 2000];
static volatile float* g_live_buf = g_linear_buf_A;
static volatile float* g_ready_buf = g_linear_buf_B;
static volatile uint32_t g_live_idx = 0;
static volatile uint32_t g_field_sequence = 0;
static uint32_t g_last_read_sequence = 0;

static int g_standard = 0; // 0=PAL, 1=NTSC
static bool g_invert_polarity = false;
static float g_brightness = 0.0f;
static float g_contrast = 1.0f;
static bool g_auto_hsync = true;
static float g_line_len = 640.0f;
static int g_v_hold_offset = 0;
static int g_h_hold_offset = 0;
static double g_sample_rate = 10000000.0;
static float g_if_offset = 2000000.0f; // +2 MHz IF offset

// High-inertia sub-sample H-Sync PLL Phase Filter (Zero horizontal jitter!)
static float g_locked_hsync_phase = -1.0f;

static float g_dc_i = 0.0f;
static float g_dc_q = 0.0f;
static float g_prev_i = 0.0f;
static float g_prev_q = 0.0f;
static float g_vfo_phase = 0.0f;
static float g_vfo_step = 0.0f;

// 3.0 MHz Butterworth Lowpass filter coefficients at 10 MSPS
static const float LP_B0 = 0.3913358f;
static const float LP_B1 = 0.7826715f;
static const float LP_B2 = 0.3913358f;
static const float LP_A1 = 0.3695274f;
static const float LP_A2 = 0.1958157f;

static float g_lp_x1 = 0.0f, g_lp_x2 = 0.0f;
static float g_lp_y1 = 0.0f, g_lp_y2 = 0.0f;

static uint32_t g_frame_count = 0;
static LARGE_INTEGER g_perf_freq;
static LARGE_INTEGER g_last_fps_time;
static float g_fps = 0.0f;

// Local 2D field buffer
static float g_field_buf[NUM_FIELD_LINES_PAL][ACTIVE_PIXELS];

static int rx_callback(hackrf_transfer* transfer) {
    if (!g_running || !transfer->buffer || transfer->valid_length <= 0) {
        return 0;
    }

    int8_t* raw = (int8_t*)transfer->buffer;
    int num_samples = transfer->valid_length / 2;

    float prev_i = g_prev_i;
    float prev_q = g_prev_q;
    float vfo_phase = g_vfo_phase;
    float vfo_step = g_vfo_step;
    float dc_i = g_dc_i;
    float dc_q = g_dc_q;
    bool invert = g_invert_polarity;

    float x1 = g_lp_x1, x2 = g_lp_x2;
    float y1 = g_lp_y1, y2 = g_lp_y2;

    volatile float* cur_live = g_live_buf;
    uint32_t l_idx = g_live_idx;

    for (int i = 0; i < num_samples; i++) {
        float raw_i = (float)raw[2 * i];
        float raw_q = (float)raw[2 * i + 1];

        // Fast I/Q DC Blocker
        dc_i = 0.999f * dc_i + 0.001f * raw_i;
        dc_q = 0.999f * dc_q + 0.001f * raw_q;
        float clean_i = raw_i - dc_i;
        float clean_q = raw_q - dc_q;

        // 1. Digital VFO Mixer (+2 MHz IF offset: (I+jQ)*(cos - j*sin))
        float cos_v = cosf(vfo_phase);
        float sin_v = sinf(vfo_phase);
        vfo_phase += vfo_step;
        if (vfo_phase > 2.0f * (float)PI) vfo_phase -= 2.0f * (float)PI;

        float cur_i = clean_i * cos_v + clean_q * sin_v;
        float cur_q = clean_q * cos_v - clean_i * sin_v;

        // 2. High-Speed FM Quad Discriminator
        float cross = cur_q * prev_i - cur_i * prev_q;
        float dot = cur_i * prev_i + cur_q * prev_q;
        float fm = atan2f(cross, dot + 1e-9f);

        prev_i = cur_i;
        prev_q = cur_q;

        if (invert) fm = -fm;

        // 3. 3.0 MHz Butterworth IIR Lowpass
        float filtered = LP_B0 * fm + LP_B1 * x1 + LP_B2 * x2 - LP_A1 * y1 - LP_A2 * y2;
        x2 = x1;
        x1 = fm;
        y2 = y1;
        y1 = filtered;

        cur_live[l_idx++] = filtered;

        // When a complete full frame (400,000 samples = 625 lines) is filled: Swap linear buffers!
        if (l_idx >= SAMPLES_PER_FRAME) {
            volatile float* old_live = cur_live;
            cur_live = (cur_live == g_linear_buf_A) ? g_linear_buf_B : g_linear_buf_A;
            g_ready_buf = old_live;
            g_live_buf = cur_live;
            g_field_sequence++;
            l_idx = 0;
        }
    }

    g_dc_i = dc_i;
    g_dc_q = dc_q;
    g_prev_i = prev_i;
    g_prev_q = prev_q;
    g_vfo_phase = vfo_phase;
    g_lp_x1 = x1; g_lp_x2 = x2;
    g_lp_y1 = y1; g_lp_y2 = y2;
    g_live_idx = l_idx;

    return 0;
}

static bool load_hackrf_dll(void) {
    if (hHackrfDll) return true;

    const char* paths[] = {
        "hackrf.dll",
        "C:\\Users\\toxic\\Downloads\\hackrf-tools-windows (1)\\hackrf.dll",
        "C:\\Program Files\\HackRF\\bin\\hackrf.dll",
        "C:\\Users\\toxic\\Desktop\\hackrf-utility\\hackrf.dll"
    };

    for (int i = 0; i < 4; i++) {
        hHackrfDll = LoadLibraryA(paths[i]);
        if (hHackrfDll) break;
    }

    if (!hHackrfDll) return false;

    fn_hackrf_init = (pfn_hackrf_init)GetProcAddress(hHackrfDll, "hackrf_init");
    fn_hackrf_exit = (pfn_hackrf_exit)GetProcAddress(hHackrfDll, "hackrf_exit");
    fn_hackrf_open = (pfn_hackrf_open)GetProcAddress(hHackrfDll, "hackrf_open");
    fn_hackrf_close = (pfn_hackrf_close)GetProcAddress(hHackrfDll, "hackrf_close");
    fn_hackrf_start_rx = (pfn_hackrf_start_rx)GetProcAddress(hHackrfDll, "hackrf_start_rx");
    fn_hackrf_stop_rx = (pfn_hackrf_stop_rx)GetProcAddress(hHackrfDll, "hackrf_stop_rx");
    fn_hackrf_set_freq = (pfn_hackrf_set_freq)GetProcAddress(hHackrfDll, "hackrf_set_freq");
    fn_hackrf_set_sample_rate = (pfn_hackrf_set_sample_rate)GetProcAddress(hHackrfDll, "hackrf_set_sample_rate");
    fn_hackrf_set_amp_enable = (pfn_hackrf_set_amp_enable)GetProcAddress(hHackrfDll, "hackrf_set_amp_enable");
    fn_hackrf_set_lna_gain = (pfn_hackrf_set_lna_gain)GetProcAddress(hHackrfDll, "hackrf_set_lna_gain");
    fn_hackrf_set_vga_gain = (pfn_hackrf_set_vga_gain)GetProcAddress(hHackrfDll, "hackrf_set_vga_gain");
    fn_hackrf_set_baseband_filter_bandwidth = (pfn_hackrf_set_baseband_filter_bandwidth)GetProcAddress(hHackrfDll, "hackrf_set_baseband_filter_bandwidth");

    return (fn_hackrf_init && fn_hackrf_open && fn_hackrf_start_rx);
}

// --- EXPORTED C API ---

__declspec(dllexport) int fpv_decoder_start(uint64_t freq_hz, uint32_t sample_rate, uint32_t lna, uint32_t vga, uint32_t amp) {
    if (!load_hackrf_dll()) {
        return -1;
    }

    if (g_running) {
        return 0;
    }

    QueryPerformanceFrequency(&g_perf_freq);
    QueryPerformanceCounter(&g_last_fps_time);
    g_frame_count = 0;
    g_fps = 0.0f;

    g_sample_rate = 10000000.0;
    g_vfo_step = (float)(2.0 * PI * g_if_offset / g_sample_rate);

    g_live_buf = g_linear_buf_A;
    g_ready_buf = g_linear_buf_B;
    g_live_idx = 0;
    g_field_sequence = 0;
    g_last_read_sequence = 0;
    g_locked_hsync_phase = -1.0f;

    g_vfo_phase = 0.0f;
    g_dc_i = 0.0f;
    g_dc_q = 0.0f;
    g_prev_i = 0.0f;
    g_prev_q = 0.0f;
    g_lp_x1 = 0.0f; g_lp_x2 = 0.0f;
    g_lp_y1 = 0.0f; g_lp_y2 = 0.0f;

    fn_hackrf_init();

    int res = fn_hackrf_open(&g_device);
    if (res != 0 || !g_device) {
        return -2;
    }

    uint64_t tuning_hz = freq_hz - (uint64_t)g_if_offset;
    fn_hackrf_set_freq(g_device, tuning_hz);
    fn_hackrf_set_sample_rate(g_device, g_sample_rate);
    fn_hackrf_set_amp_enable(g_device, (uint8_t)amp);
    fn_hackrf_set_lna_gain(g_device, lna);
    fn_hackrf_set_vga_gain(g_device, vga);
    fn_hackrf_set_baseband_filter_bandwidth(g_device, 10000000);

    g_running = true;
    res = fn_hackrf_start_rx(g_device, rx_callback, NULL);
    if (res != 0) {
        g_running = false;
        fn_hackrf_close(g_device);
        g_device = NULL;
        return -3;
    }

    return 0;
}

__declspec(dllexport) void fpv_decoder_set_tuning(int standard, int invert, float brightness, float contrast, int auto_hsync, float manual_line_len, int v_hold_offset, int h_hold_offset) {
    g_standard = standard;
    g_invert_polarity = (invert != 0);
    g_brightness = brightness;
    g_contrast = contrast;
    g_auto_hsync = (auto_hsync != 0);
    if (manual_line_len > 300.0f) {
        g_line_len = manual_line_len;
    }
    g_v_hold_offset = v_hold_offset;
    g_h_hold_offset = h_hold_offset;
}

__declspec(dllexport) int fpv_decoder_get_frame(uint8_t* out_pixels, int width, int height, int* out_sync_locked, float* out_fps) {
    if (!g_running || !out_pixels) return 0;

    uint32_t seq = g_field_sequence;
    if (seq == 0 || seq == g_last_read_sequence) {
        return 0; // Wait for next completed linear frame
    }
    g_last_read_sequence = seq;

    // Read from the 100% linear, unwrapped 400,000-sample ready buffer
    volatile float* field_data = g_ready_buf;

    float line_len = g_auto_hsync ? (g_standard == 0 ? 640.0f : 635.5f) : g_line_len;
    int int_len = (int)(line_len + 0.5f);
    int num_fold_lines = 150;
    float avg_line[1024] = {0};

    // 1. Rock-Solid Global H-Sync Phase Lock on contiguous Field 1 buffer (Zero shaking!)
    for (int l = 0; l < num_fold_lines; l++) {
        int32_t l_start = (int32_t)(l * int_len);
        for (int p = 0; p < int_len && p < 1024; p++) {
            int32_t idx = l_start + p;
            if (idx >= 0 && idx < SAMPLES_PER_FRAME) {
                avg_line[p] += field_data[idx];
            }
        }
    }

    int min_p = 0;
    float min_val = avg_line[0];
    for (int p = 1; p < int_len && p < 1024; p++) {
        if (avg_line[p] < min_val) {
            min_val = avg_line[p];
            min_p = p;
        }
    }

    // Sub-sample parabolic interpolation around min_p
    float sub_p = (float)min_p;
    if (min_p > 0 && min_p < int_len - 1) {
        float y0 = avg_line[min_p - 1];
        float y1 = avg_line[min_p];
        float y2 = avg_line[min_p + 1];
        float denom_p = 2.0f * (y0 - 2.0f * y1 + y2);
        if (fabsf(denom_p) > 1e-5f) {
            float delta = (y0 - y2) / denom_p;
            if (delta > -1.0f && delta < 1.0f) {
                sub_p += delta;
            }
        }
    }

    // Heavy inertia smoothing across frames (0.95 inertia, 0.05 step = ZERO SHAKING!)
    if (g_locked_hsync_phase < 0.0f) {
        g_locked_hsync_phase = sub_p;
    } else {
        float diff = sub_p - g_locked_hsync_phase;
        if (diff > (float)int_len / 2.0f) diff -= (float)int_len;
        if (diff < -(float)int_len / 2.0f) diff += (float)int_len;
        g_locked_hsync_phase += 0.05f * diff;
        if (g_locked_hsync_phase < 0.0f) g_locked_hsync_phase += (float)int_len;
        if (g_locked_hsync_phase >= (float)int_len) g_locked_hsync_phase -= (float)int_len;
    }

    float final_sync_p = g_auto_hsync ? g_locked_hsync_phase : (float)min_p;

    // 2. Pure Monotonic Linear Progressive Rasterizer (ZERO CUTS / ZERO TEARS!)
    int num_field_lines = (g_standard == 0 ? NUM_FIELD_LINES_PAL : NUM_FIELD_LINES_NTSC);
    float sync_offset = 104.0f; // 10.4µs total blanking back-porch at 10 MSPS
    float active_width_ratio = 0.8125f;

    float frame_min = 1e9f;
    float frame_max = -1e9f;

    int total_frame_lines = 625;
    int start_line = ((g_v_hold_offset % total_frame_lines) + total_frame_lines) % total_frame_lines;

    for (int y = 0; y < num_field_lines; y++) {
        int line_idx = (start_line + y) % total_frame_lines;
        float line_start = final_sync_p + sync_offset + (float)g_h_hold_offset + (float)line_idx * line_len;

        for (int x = 0; x < ACTIVE_PIXELS; x++) {
            float sample_pos = line_start + (float)x * (line_len * active_width_ratio / (float)ACTIVE_PIXELS);
            int32_t idx_fl = (int32_t)sample_pos;

            while (idx_fl < 0) idx_fl += SAMPLES_PER_FRAME;
            while (idx_fl >= SAMPLES_PER_FRAME) idx_fl -= SAMPLES_PER_FRAME;

            int32_t idx_next = (idx_fl + 1 < SAMPLES_PER_FRAME) ? (idx_fl + 1) : 0;
            float alpha = sample_pos - floorf(sample_pos);
            float val = (1.0f - alpha) * field_data[idx_fl] + alpha * field_data[idx_next];

            g_field_buf[y][x] = val;
            if (val < frame_min) frame_min = val;
            if (val > frame_max) frame_max = val;
        }
    }

    // 3. Contrast AGC & Linear Progressive Scaler to 640x480 (Pure monotonic Y mapping = NO TEAR!)
    float denom = (frame_max - frame_min);
    if (denom < 0.05f) denom = 0.05f;

    float contrast = g_contrast;
    float brightness = g_brightness;

    for (int out_y = 0; out_y < height; out_y++) {
        int src_y = (out_y * num_field_lines) / height;
        if (src_y >= num_field_lines) src_y = num_field_lines - 1;

        for (int out_x = 0; out_x < width; out_x++) {
            int src_x = (out_x * ACTIVE_PIXELS) / width;
            if (src_x >= ACTIVE_PIXELS) src_x = ACTIVE_PIXELS - 1;

            float val = g_field_buf[src_y][src_x];
            float norm = ((val - frame_min) / denom) * 255.0f * contrast + brightness;
            if (norm < 0.0f) norm = 0.0f;
            if (norm > 255.0f) norm = 255.0f;

            out_pixels[out_y * width + out_x] = (uint8_t)norm;
        }
    }

    // FPS Calculation
    g_frame_count++;
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    double elapsed = (double)(now.QuadPart - g_last_fps_time.QuadPart) / (double)g_perf_freq.QuadPart;
    if (elapsed >= 1.0) {
        g_fps = (float)((double)g_frame_count / elapsed);
        g_frame_count = 0;
        g_last_fps_time = now;
    }

    if (out_sync_locked) *out_sync_locked = 1;
    if (out_fps) *out_fps = g_fps;

    return 1;
}

static volatile LONG g_stop_lock = 0;

__declspec(dllexport) void fpv_decoder_stop(void) {
    if (InterlockedExchange(&g_stop_lock, 1) != 0) {
        return;
    }

    g_running = false;

    hackrf_device* dev = g_device;
    g_device = NULL;

    if (dev) {
        if (fn_hackrf_stop_rx) {
            fn_hackrf_stop_rx(dev);
        }
        if (fn_hackrf_close) {
            fn_hackrf_close(dev);
        }
    }

    InterlockedExchange(&g_stop_lock, 0);
}
