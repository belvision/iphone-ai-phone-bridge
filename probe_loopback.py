"""Bounded WASAPI output-level test. No recording, playback or network access."""
import ctypes as c
import json
import math
import time
import comtypes
from comtypes import GUID
from pycaw.pycaw import AudioUtilities, IAudioClient
from probe_hfp import IAudioCaptureClient

def main():
    dev = AudioUtilities.GetSpeakers()
    cli = dev._dev.Activate(IAudioClient._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioClient)
    fmt = cli.GetMixFormat()
    try:
        raw = c.string_at(fmt, 18 + fmt.contents.cbSize)
        subtype = GUID.from_buffer_copy(raw[24:40]) if fmt.contents.wFormatTag == 65534 else None
        if fmt.contents.wBitsPerSample != 32 or (fmt.contents.wFormatTag != 3 and str(subtype).upper() != '{00000003-0000-0010-8000-00AA00389B71}'):
            raise RuntimeError('Expected float32')
        channels, rate = fmt.contents.nChannels, fmt.contents.nSamplesPerSec
        cli.Initialize(0, 0x20000, 1000000, 0, fmt, None)
    finally:
        c.windll.ole32.CoTaskMemFree(fmt)
    capture = cli.GetService(IAudioCaptureClient._iid_).QueryInterface(IAudioCaptureClient)
    cli.Start()
    results = []
    try:
        end = time.monotonic() + 8
        next_report = time.monotonic() + .5
        frames = count = 0
        peak = squares = 0.0
        while time.monotonic() < end:
            while capture.GetNextPacketSize():
                ptr, n, flags, _, _ = capture.GetBuffer()
                try:
                    if ptr and not flags & 2:
                        samples = c.cast(ptr, c.POINTER(c.c_float))
                        for i in range(n * channels):
                            sample = samples[i]
                            peak = max(peak, abs(sample))
                            squares += sample*sample
                    frames += n
                    count += n*channels
                finally:
                    capture.ReleaseBuffer(n)
            if time.monotonic() >= next_report:
                results.append({'frames': frames, 'peak': round(peak, 5), 'rms': round(math.sqrt(squares/count),5) if count else 0})
                peak = squares = 0.0
                count = 0
                next_report = time.monotonic()+.5
            time.sleep(.01)
    finally:
        cli.Stop()
    print(json.dumps({'source': 'default playback loopback', 'rate': rate, 'channels': channels, 'timeline': results}, indent=2))

if __name__ == '__main__':
    main()
