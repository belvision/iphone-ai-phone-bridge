"""Send one quiet test tone only to the observed HFP kernel-streaming output."""
import json
import argparse
import math
import time
import numpy as np
import sounddevice as sd

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--count', type=int, default=1, choices=[1, 2, 3])
    args = parser.parse_args()
    hosts = sd.query_hostapis()
    candidates = [(i, d) for i, d in enumerate(sd.query_devices()) if d['max_output_channels'] and 'Hands-Free HF Audio' in d['name'] and hosts[d['hostapi']]['name'] == 'Windows WDM-KS']
    if len(candidates) != 1:
        raise RuntimeError('Expected one unambiguous HFP WDM-KS output')
    i, device = candidates[0]
    sent = callbacks = 0
    statuses = []
    def callback(out, frames, timing, status):
        nonlocal sent, callbacks
        if status:
            statuses.append(str(status))
        t = (np.arange(frames)+sent)/16000
        phase = np.mod(t-.5, 1)
        active = (t >= .5) & (t < .5+args.count) & (phase < .4)
        env = np.where(active, np.maximum(0, np.minimum(1, np.minimum(phase/.02,(.4-phase)/.02))), 0)
        samples = (np.sin(t*(2*math.pi*660))*env*.08*32767).astype(np.int16)
        out[:] = samples.tobytes()
        sent += frames
        callbacks += 1
    info = {'device': i}
    try:
        with sd.RawOutputStream(device=i, samplerate=16000, channels=1, dtype='int16', blocksize=320, callback=callback):
            time.sleep(args.count+1)
        info.update(frames=sent, callbacks=callbacks, status=statuses[-5:])
    except Exception as exc:
        info['error'] = str(exc)
    print(json.dumps(info, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
