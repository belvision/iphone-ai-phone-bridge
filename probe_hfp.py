"""Bounded HFP WASAPI probe. Output is silence; input only produces level statistics."""
import ctypes as c
import argparse
import json
import math
import time
import comtypes
from comtypes import COMMETHOD, GUID, IUnknown
from pycaw.pycaw import AudioUtilities, IAudioClient

CAPTURE = '{0.0.1.00000000}.{7A0303B9-EA7D-4754-8509-C71BB6669D25}'
RENDER = '{0.0.0.00000000}.{37F2C6E2-53E9-4DA6-ADCB-24CBAD8665C1}'

class AudioClientProperties(c.Structure):
    _fields_ = [('cbSize', c.c_uint32), ('bIsOffload', c.c_int32), ('eCategory', c.c_int32), ('Options', c.c_int32)]

class IAudioClient2(IAudioClient):
    _iid_ = GUID('{726778CD-F60A-4EDA-82DE-E47610CD78AA}')
    _methods_ = [
        COMMETHOD([], c.HRESULT, 'IsOffloadCapable', (['in'], c.c_int32, 'category'), (['out'], c.POINTER(c.c_int32), 'capable')),
        COMMETHOD([], c.HRESULT, 'SetClientProperties', (['in'], c.POINTER(AudioClientProperties), 'properties')),
        COMMETHOD([], c.HRESULT, 'GetBufferSizeLimits', (['in'], c.c_void_p, 'format'), (['in'], c.c_int32, 'eventDriven'), (['out'], c.POINTER(c.c_int64), 'min'), (['out'], c.POINTER(c.c_int64), 'max')),
    ]

class IAudioCaptureClient(IUnknown):
    _iid_ = GUID('{C8ADBD64-E71E-48A0-A4DE-185C395CD317}')
    _methods_ = [
        COMMETHOD([], c.HRESULT, 'GetBuffer', (['out'], c.POINTER(c.c_void_p), 'data'), (['out'], c.POINTER(c.c_uint32), 'frames'), (['out'], c.POINTER(c.c_uint32), 'flags'), (['out'], c.POINTER(c.c_uint64), 'devicePosition'), (['out'], c.POINTER(c.c_uint64), 'qpcPosition')),
        COMMETHOD([], c.HRESULT, 'ReleaseBuffer', (['in'], c.c_uint32, 'frames')),
        COMMETHOD([], c.HRESULT, 'GetNextPacketSize', (['out'], c.POINTER(c.c_uint32), 'frames')),
    ]

class IAudioRenderClient(IUnknown):
    _iid_ = GUID('{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}')
    _methods_ = [
        COMMETHOD([], c.HRESULT, 'GetBuffer', (['in'], c.c_uint32, 'frames'), (['out'], c.POINTER(c.c_void_p), 'data')),
        COMMETHOD([], c.HRESULT, 'ReleaseBuffer', (['in'], c.c_uint32, 'frames'), (['in'], c.c_uint32, 'flags')),
    ]

def open_client(endpoint_id, communications=False):
    dev = AudioUtilities.GetDeviceEnumerator().GetDevice(endpoint_id)
    client = dev.Activate(IAudioClient._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioClient)
    if communications:
        props = AudioClientProperties(c.sizeof(AudioClientProperties), 0, 3, 0)
        client.QueryInterface(IAudioClient2).SetClientProperties(c.byref(props))
    fmt = client.GetMixFormat()
    try:
        raw = c.string_at(fmt, 18 + fmt.contents.cbSize)
        subtype = GUID.from_buffer_copy(raw[24:40]) if fmt.contents.wFormatTag == 65534 else None
        if fmt.contents.nChannels != 1 or fmt.contents.nSamplesPerSec != 16000 or fmt.contents.wBitsPerSample != 32:
            raise RuntimeError('Unexpected HFP mix format; refusing to interpret samples')
        if fmt.contents.wFormatTag != 3 and str(subtype).upper() != '{00000003-0000-0010-8000-00AA00389B71}':
            raise RuntimeError('HFP format is not IEEE float32')
        client.Initialize(0, 0, 1000000, 0, fmt, None)
    finally:
        c.windll.ole32.CoTaskMemFree(fmt)
    return client

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=2)
    parser.add_argument('--tone', action='store_true', help='Send one quiet 0.4s 660 Hz test beep')
    parser.add_argument('--communications', action='store_true')
    args = parser.parse_args()
    if not 0 < args.seconds <= 60:
        raise SystemExit('seconds must be 0..60')
    opened = []
    info = {}
    try:
        for label, endpoint in [('render', RENDER), ('capture', CAPTURE)]:
            try:
                cli = open_client(endpoint, args.communications)
                opened.append(cli)
                info[label] = {'initialized': True, 'buffer_frames': cli.GetBufferSize()}
                if label == 'render':
                    output = cli.GetService(IAudioRenderClient._iid_).QueryInterface(IAudioRenderClient)
                    output.GetBuffer(cli.GetBufferSize())
                    output.ReleaseBuffer(cli.GetBufferSize(), 2)  # AUDCLNT_BUFFERFLAGS_SILENT
                else:
                    capture = cli.GetService(IAudioCaptureClient._iid_).QueryInterface(IAudioCaptureClient)
                cli.Start()
                info[label]['started'] = True
            except Exception as exc:
                info.setdefault(label, {}).update(error=str(exc), hresult=hex(getattr(exc, 'hresult', 0) & 0xffffffff))
        if info.get('capture', {}).get('started'):
            frames = packets = active_packets = rendered_frames = 0
            peak = 0.0
            timeline = []
            start = time.monotonic()
            next_report = start + .5
            deadline = start + args.seconds
            while time.monotonic() < deadline:
                if info.get('render', {}).get('started'):
                    n = opened[0].GetBufferSize() - opened[0].GetCurrentPadding()
                    if n:
                        ptr = output.GetBuffer(n)
                        if args.tone:
                            samples = c.cast(ptr, c.POINTER(c.c_float))
                            for i in range(n):
                                t = (rendered_frames + i) / 16000
                                # A smooth, quiet burst, then silence.
                                phase = t - .5
                                env = min(1, phase/.02, (.4-phase)/.02) if 0 <= phase < .4 else 0
                                samples[i] = .04 * max(0, env) * math.sin(2*math.pi*660*t)
                            output.ReleaseBuffer(n, 0)
                        else:
                            output.ReleaseBuffer(n, 2)
                        rendered_frames += n
                while capture.GetNextPacketSize():
                    data, n, flags, _, _ = capture.GetBuffer()
                    try:
                        if data and not flags & 2:
                            values = c.cast(data, c.POINTER(c.c_float))
                            peak = max(peak, max(abs(values[i]) for i in range(n)))
                            active_packets += 1
                        frames += n
                        packets += 1
                    finally:
                        capture.ReleaseBuffer(n)
                time.sleep(0.01)
                now = time.monotonic()
                if now >= next_report:
                    timeline.append({'t': round(now-start, 1), 'frames': frames, 'peak': round(peak, 6)})
                    next_report = now + .5
            info['capture'].update(frames=frames, packets=packets, non_silent_packets=active_packets, peak=round(peak, 6), timeline=timeline)
            info['render'].update(frames=rendered_frames, tone_requested=args.tone)
    finally:
        for cli in reversed(opened):
            try:
                cli.Stop()
            except Exception:
                pass
        print(json.dumps(info, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
