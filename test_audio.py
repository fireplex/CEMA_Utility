import subprocess, numpy as np, queue, sounddevice as sd, time
q = queue.Queue(maxsize=2000)

def cb(outdata, frames, time_info, status):
    chunk = np.zeros(frames, dtype=np.float32)
    idx = 0
    while idx < frames:
        if 'rem' not in cb.__dict__ or len(cb.rem) == 0:
            try: cb.rem = q.get_nowait()
            except: break
        take = min(frames-idx, len(cb.rem))
        chunk[idx:idx+take] = cb.rem[:take]
        cb.rem = cb.rem[take:]
        idx += take
    outdata[:,0] = chunk

try:
    p = subprocess.Popen(['hackrf_transfer','-r','-','-f','95900000','-l','14','-g','20','-a','1','-s','20000000'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stream = sd.OutputStream(device=3, samplerate=47607, channels=1, dtype='float32', callback=cb)
    stream.start()
    
    print('Started SDR. Waiting 3 seconds to gather stats...')
    t = time.time()
    total_blocks = 0
    while time.time() - t < 3:
        raw = p.stdout.read(32768)
        if len(raw) == 32768:
            d = np.frombuffer(raw, dtype=np.int8).astype(np.float32)
            ib = d[0::2] - np.mean(d[0::2])
            qb = d[1::2] - np.mean(d[1::2])
            iq = ib + 1j * qb
            cc = iq[1:] * np.conj(iq[:-1])
            fm_full = np.angle(cc)
            dec = np.mean(fm_full[:16224].reshape(-1, 416), axis=1) * 50.0
            
            if total_blocks == 100:
                print(f"Sample block max value: {np.max(np.abs(dec)):.4f}")
            
            q.put(dec)
            total_blocks += 1
            
    print(f"Total blocks processed: {total_blocks}")
    print(f"Final queue size: {q.qsize()}")
except Exception as e:
    import traceback
    traceback.print_exc()
finally:
    if 'p' in locals(): p.terminate()
