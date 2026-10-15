"""Read-only Windows audio diagnostics; does not record or send audio."""
import ctypes
import json
import warnings

import comtypes
from pycaw.pycaw import AudioUtilities, IAudioClient
from pycaw.api.audiopolicy import IAudioSessionControl2

warnings.filterwarnings('ignore', category=UserWarning)

def failure(exc):
    return {'error': str(exc), 'hresult': hex(getattr(exc, 'hresult', 0) & 0xffffffff)}

def main():
    enum = AudioUtilities.GetDeviceEnumerator()
    devices = AudioUtilities.GetAllDevices()
    ids = {d.id: d.FriendlyName for d in devices}
    # HFP endpoints observed on this computer; test explicit access even if hidden.
    ids.setdefault('{0.0.1.00000000}.{7A0303B9-EA7D-4754-8509-C71BB6669D25}', 'iPhone HF capture (explicit)')
    ids.setdefault('{0.0.0.00000000}.{37F2C6E2-53E9-4DA6-ADCB-24CBAD8665C1}', 'iPhone HF render (explicit)')
    result = []
    for endpoint_id, name in ids.items():
        row = {'name': name, 'id': endpoint_id, 'enumerated': any(d.id.lower() == endpoint_id.lower() for d in devices)}
        try:
            dev = enum.GetDevice(endpoint_id)
            row['state'] = dev.GetState()
            client = dev.Activate(IAudioClient._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioClient)
            fmt = client.GetMixFormat()
            try:
                row['mix_format'] = {k: getattr(fmt.contents, k) for k in ('nChannels', 'nSamplesPerSec', 'wBitsPerSample', 'wFormatTag')}
            finally:
                ctypes.windll.ole32.CoTaskMemFree(fmt)
            row['period_100ns'] = client.GetDevicePeriod()
        except Exception as exc:
            row.update(failure(exc))
        result.append(row)
    sessions = []
    for d in devices:
        try:
            se = d.AudioSessionManager.GetSessionEnumerator()
            for i in range(se.GetCount()):
                ctl = se.GetSession(i).QueryInterface(IAudioSessionControl2)
                sessions.append({'device': d.FriendlyName, 'pid': ctl.GetProcessId(), 'state': ctl.GetState()})
        except Exception:
            pass
    print(json.dumps({'endpoints': result, 'sessions': sessions}, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
