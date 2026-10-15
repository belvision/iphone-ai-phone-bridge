"""Explicit local VB-CABLE tone test. No cloud call or saved recording."""
import argparse
import json
import urllib.request
import numpy as np
import sounddevice as sd
from native_audio import NativeAudio


def get(path):
    with urllib.request.urlopen("http://localhost:8766" + path, timeout=5) as response:
        return json.load(response)


def command(action):
    request = urllib.request.Request("http://localhost:8766/audio-route",
        data=json.dumps({"action": action, "mode": "virtual-mic"}).encode(),
        headers={"Content-Type": "application/json", "Origin": "http://localhost:8766"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-local-test", action="store_true")
    args = parser.parse_args()
    if not args.run_local_test:
        raise SystemExit("Specify --run-local-test; this opens VB-CABLE and briefly changes the Windows microphone.")
    if get("/health")["audioActive"] or get("/phone-state").get("callPhase") not in ("idle", "ended"):
        raise SystemExit("Stop audio and finish the cellular call before the local test.")
    apis = sd.query_hostapis()
    capture_devices = [i for i, d in enumerate(sd.query_devices()) if d["name"].startswith("CABLE Output")
                       and "VB-Audio" in d["name"] and "WASAPI" in apis[d["hostapi"]]["name"] and d["max_input_channels"] >= 2]
    if len(capture_devices) != 1:
        raise SystemExit("Expected one WASAPI CABLE Output input; reboot after installing the driver.")
    bridge = None
    try:
        assert command("prepare")["virtualMicPrepared"], "Windows did not select the virtual microphone"
        with sd.RawInputStream(device=capture_devices[0], samplerate=48000, channels=2, dtype="float32", blocksize=960) as stream:
            bridge = NativeAudio(max_seconds=3, output_mode="virtual-mic")
            bridge.start()
            bridge.request_tone()
            energy = frames = overflows = 0
            peak = 0.0
            for _ in range(100):
                data, overflow = stream.read(960)
                samples = np.frombuffer(data, dtype=np.float32)
                energy += float(np.sum(samples * samples))
                frames += samples.size
                peak = max(peak, float(np.max(np.abs(samples))))
                overflows += int(overflow)
            stats = bridge.stats(reset_peaks=False)
            result = {"virtualToneReceived": peak > 0.01, "receivedPeak": round(peak, 5),
                      "receivedRms": round(float(np.sqrt(energy / frames)), 5),
                      "captureOverflows": overflows, "outputSampleRate": stats["outputSampleRate"],
                      "outputFrames": stats["renderedFrames"], "droppedOutputSamples": stats["droppedOutputSamples"]}
            if not result["virtualToneReceived"]:
                raise RuntimeError("The test tone did not reach the virtual microphone")
    finally:
        if bridge: bridge.stop("user_stop")
        restored = command("reset")
        if restored["virtualMicPrepared"]:
            raise RuntimeError("Virtual microphone remained prepared after test")
    print(json.dumps({**result, "normalMicrophoneRestored": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
