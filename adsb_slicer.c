#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

#define MODES_GENERATOR_POLY 0x1FFF409

typedef void (*AdsbFrameCallback)(const char* hex_frame, int rssi);

static uint32_t modes_checksum(const uint8_t *msg, int bits) {
    uint32_t crc = 0;
    for (int j = 0; j < bits; j++) {
        int byte = j / 8;
        int bit = j % 8;
        int bitmode = (msg[byte] & (0x80 >> bit)) ? 1 : 0;
        if ((crc & 0x800000) ^ (bitmode ? 0x800000 : 0)) {
            crc = ((crc << 1) ^ MODES_GENERATOR_POLY) & 0xFFFFFF;
        } else {
            crc = (crc << 1) & 0xFFFFFF;
        }
    }
    return crc;
}

__declspec(dllexport) int process_iq_samples(
    const int8_t *iq_data,
    int num_samples,
    AdsbFrameCallback callback
) {
    if (num_samples < 250) return 0;

    uint32_t *mag = (uint32_t*)malloc(num_samples * sizeof(uint32_t));
    if (!mag) return 0;

    for (int i = 0; i < num_samples; i++) {
        int32_t iv = iq_data[i * 2];
        int32_t qv = iq_data[i * 2 + 1];
        mag[i] = (uint32_t)(iv * iv + qv * qv);
    }

    int frames_found = 0;
    int max_idx = num_samples - 240;

    for (int i = 0; i < max_idx; i++) {
        uint32_t p0 = mag[i];
        uint32_t p2 = mag[i + 2];
        uint32_t p7 = mag[i + 7];
        uint32_t p9 = mag[i + 9];

        uint32_t p1 = mag[i + 1];
        uint32_t p3 = mag[i + 3];
        uint32_t p4 = mag[i + 4];
        uint32_t p5 = mag[i + 5];
        uint32_t p6 = mag[i + 6];
        uint32_t p8 = mag[i + 8];

        if (p0 <= p1 || p2 <= p3 || p7 <= p6 || p9 <= p8) {
            continue;
        }

        uint32_t high_sum = p0 + p2 + p7 + p9;
        uint32_t low_sum = p1 + p3 + p4 + p5 + p6 + p8;

        if (high_sum < 250 || high_sum <= (low_sum * 2)) {
            continue;
        }

        uint8_t msg[14] = {0};
        int bit_offset = i + 16;

        for (int b = 0; b < 112; b++) {
            uint32_t s1 = mag[bit_offset + b * 2];
            uint32_t s2 = mag[bit_offset + b * 2 + 1];
            if (s1 > s2) {
                msg[b / 8] |= (0x80 >> (b % 8));
            }
        }

        int df = (msg[0] >> 3) & 0x1F;

        if (df == 17 || df == 18 || df == 19) {
            uint32_t crc = modes_checksum(msg, 112);
            if (crc == 0) {
                char hex_str[32];
                for (int h = 0; h < 14; h++) {
                    sprintf(&hex_str[h * 2], "%02X", msg[h]);
                }
                hex_str[28] = '\0';
                if (callback) {
                    callback(hex_str, (int)(high_sum / 4));
                }
                frames_found++;
                i += 240;
            }
        } else if (df == 0 || df == 4 || df == 5 || df == 11) {
            char hex_str[32];
            for (int h = 0; h < 7; h++) {
                sprintf(&hex_str[h * 2], "%02X", msg[h]);
            }
            hex_str[14] = '\0';
            if (callback) {
                callback(hex_str, (int)(high_sum / 4));
            }
            frames_found++;
            i += 120;
        }
    }

    free(mag);
    return frames_found;
}
