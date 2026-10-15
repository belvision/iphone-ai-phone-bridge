"""Bounded local audio bridge: WASAPI speaker loopback -> browser, browser -> HFP.

Nothing is recorded or sent to a cloud service here. The owner thread creates,
uses and releases all COM audio objects. Only the HFP output uses PortAudio.
"""
from __future__ import annotations

import ctypes as c
from collections import deque
import queue
import threading
import time

import comtypes  # Import on server main thread; each audio worker initializes itself.
import numpy as np
from audio_routes import MODES, output_device

WEB_RATE = 48000
PHONE_RATE = 16000
PACKET_FRAMES = WEB_RATE // 50
MAX_SESSION_SECONDS = None  # Calls end with the phone/agent or an explicit stop.
MAX_OUTPUT_FRAMES = PHONE_RATE // 5


class StreamingDecimator:
    """48 kHz float -> 16 kHz float, with persistent FIR history and phase."""

    def __init__(self):
        length = 95
        positions = np.arange(length, dtype=np.float64) - (length - 1) / 2
        cutoff = 6900 / WEB_RATE
        taps = 2 * cutoff * np.sinc(2 * cutoff * positions) * np.blackman(length)
        self.taps = (taps / taps.sum()).astype(np.float32)
        self.history = np.zeros(length - 1, dtype=np.float32)
        self.input_count = 0

    def process(self, samples):
        samples = np.asarray(samples, dtype=np.float32)
        if not samples.size:
            return np.empty(0, dtype=np.float32)
        joined = np.concatenate((self.history, samples))
        filtered = np.convolve(joined, self.taps, mode="valid")
        first = (-self.input_count) % 3
        result = filtered[first::3].copy()
        self.history = joined[-len(self.history):].copy()
        self.input_count += samples.size
        return result


class SampleQueue:
    """Bounded PCM queue; discard oldest samples instead of increasing latency."""

    def __init__(self, limit=MAX_OUTPUT_FRAMES, dtype=np.int16):
        self.limit = limit
        self.dtype = dtype
        self.parts = deque()
        self.count = 0
        self.dropped = 0
        self.lock = threading.Lock()

    def push(self, pcm):
        pcm = np.asarray(pcm, dtype=self.dtype).copy()
        if not pcm.size:
            return
        with self.lock:
            self.parts.append(pcm)
            self.count += pcm.size
            excess = self.count - self.limit
            while excess > 0:
                first = self.parts.popleft()
                removed = min(excess, first.size)
                if removed < first.size:
                    self.parts.appendleft(first[removed:])
                self.count -= removed
                self.dropped += removed
                excess -= removed

    def take(self, frames):
        result = np.zeros(frames, dtype=self.dtype)
        written = 0
        with self.lock:
            while self.parts and written < frames:
                first = self.parts.popleft()
                n = min(frames - written, first.size)
                result[written:written + n] = first[:n]
                if n < first.size:
                    self.parts.appendleft(first[n:])
                written += n
                self.count -= n
        return result, written

    def clear(self):
        with self.lock:
            self.parts.clear()
            self.count = 0

    def snapshot(self):
        with self.lock:
            return self.count, self.dropped


class NativeAudio:
    def __init__(self, max_seconds=MAX_SESSION_SECONDS, output_mode="direct-hfp"):
        if output_mode not in MODES:
            raise ValueError("Unknown audio route")
        self.output_mode = output_mode
        self.output_rate = WEB_RATE if output_mode == "virtual-mic" else PHONE_RATE
        self.output_dtype = np.float32 if output_mode == "virtual-mic" else np.int16
        self.output_channels = 2 if output_mode == "virtual-mic" else 1
        self.max_seconds = max_seconds
        self.capture_packets = queue.Queue(maxsize=10)  # 200 ms at 20 ms/packet
        self.output_queue = SampleQueue(self.output_rate // 5, self.output_dtype)
        self.resampler = StreamingDecimator()
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.thread = None
        self.error = None
        self.reason = "stopped"
        self.started_at = None
        self.source_name = ""
        self.output_name = ""
        self._stats_lock = threading.Lock()
        self._input_peak = self._output_peak = 0.0
        self._captured_frames = self._rendered_frames = 0
        self._received_audio_frames = self._supplied_frames = 0
        self._received_audio_peak = 0.0  # Lifetime maximum, not reset by polling.
        self._dropped_packets = self._underruns = self._callback_statuses = 0
        self._tone_position = None
        self._last_tone = 0.0
        self._tone_lock = threading.Lock()

    @property
    def alive(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.thread is not None:
            raise RuntimeError("Audio session cannot be started twice")
        self.thread = threading.Thread(target=self._audio_thread, name="phone-audio", daemon=True)
        self.thread.start()
        if not self.ready_event.wait(8):
            self.stop("startup_timeout")
            raise RuntimeError("Audio devices did not become ready within 8 seconds")
        if self.error:
            self.stop("device_error")
            raise RuntimeError(self.error)
        if not self.alive:
            raise RuntimeError("Audio session stopped during startup")

    def stop(self, reason="stopped"):
        if not self.stop_event.is_set():
            self.reason = reason
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

    def push_audio(self, payload):
        if self.stop_event.is_set():
            return
        if not payload or len(payload) % 4:
            raise ValueError("Audio must contain float32 little-endian mono samples")
        samples = np.frombuffer(payload, dtype="<f4")
        if not np.isfinite(samples).all():
            raise ValueError("Audio contains a non-finite sample")
        samples = np.clip(samples, -1.0, 1.0)
        with self._stats_lock:
            self._received_audio_frames += samples.size
            self._received_audio_peak = max(self._received_audio_peak, float(np.max(np.abs(samples))))
        if self.output_mode == "virtual-mic":
            pcm = samples
        else:
            phone = self.resampler.process(samples)
            pcm = np.rint(np.clip(phone, -1.0, 1.0) * 32767).astype(np.int16)
        self.output_queue.push(pcm)

    def request_tone(self):
        now = time.monotonic()
        with self._tone_lock:
            if now - self._last_tone < 2:
                raise ValueError("Please wait two seconds between test tones")
            self._last_tone = now
            self._tone_position = 0
        self.output_queue.clear()

    def stats(self, reset_peaks=True):
        queued, dropped = self.output_queue.snapshot()
        with self._stats_lock:
            result = {
                "type": "level",
                "outputMode": self.output_mode,
                "outputSampleRate": self.output_rate,
                "inputPeak": round(self._input_peak, 6),
                "outputPeak": round(self._output_peak, 6),
                "capturedFrames": self._captured_frames,
                "renderedFrames": self._rendered_frames,
                "receivedAudioFrames": self._received_audio_frames,
                "receivedAudioPeak": round(self._received_audio_peak, 6),
                "suppliedFrames": self._supplied_frames,
                "queuedMs": round(1000 * queued / self.output_rate, 1),
                "droppedInputPackets": self._dropped_packets,
                "droppedOutputSamples": dropped,
                "underruns": self._underruns,
                "callbackStatuses": self._callback_statuses,
                "elapsedSeconds": round(time.monotonic() - self.started_at, 1) if self.started_at else 0,
            }
            if reset_peaks:
                self._input_peak = self._output_peak = 0.0
        return result

    def _output_callback(self, outdata, frames, timing, status):
        if self.stop_event.is_set():
            outdata[:] = bytes(len(outdata))
            return
        pcm, supplied = self.output_queue.take(frames)
        queued_supplied = supplied
        with self._tone_lock:
            tone_pos = self._tone_position
            if tone_pos is not None:
                t = (tone_pos + np.arange(frames)) / self.output_rate
                wave = np.zeros(frames)
                for start in (0.1, 0.45, 0.8):
                    phase = t - start
                    envelope = np.clip(np.minimum(phase / 0.008, (0.16 - phase) / 0.008), 0, 1)
                    wave += 0.045 * envelope * np.sin(2 * np.pi * 660 * t)
                pcm = wave.astype(np.float32) if self.output_mode == "virtual-mic" else np.rint(wave * 32767).astype(np.int16)
                supplied = frames
                self._tone_position = None if tone_pos + frames >= int(1.1 * self.output_rate) else tone_pos + frames
        output = np.repeat(pcm[:, None], self.output_channels, axis=1) if self.output_channels > 1 else pcm
        outdata[:] = output.tobytes()
        with self._stats_lock:
            self._rendered_frames += frames
            self._supplied_frames += queued_supplied
            if supplied < frames:
                self._underruns += 1
            if status:
                self._callback_statuses += 1
            if pcm.size:
                peak = float(np.max(np.abs(pcm))) if self.output_mode == "virtual-mic" else float(np.max(np.abs(pcm.astype(np.int32)))) / 32768
                self._output_peak = max(self._output_peak, peak)

    def _emit_packet(self, packet):
        try:
            self.capture_packets.put_nowait(packet)
        except queue.Full:
            try:
                self.capture_packets.get_nowait()
            except queue.Empty:
                pass
            with self._stats_lock:
                self._dropped_packets += 1
            try:
                self.capture_packets.put_nowait(packet)
            except queue.Full:
                pass

    def _audio_thread(self):
        # comtypes was imported by the server main thread; balance this worker's
        # own CoInitialize/CoUninitialize separately below.
        from comtypes import COMMETHOD, GUID, IUnknown
        from pycaw.pycaw import AudioUtilities, IAudioClient
        import sounddevice as sd

        class IAudioCaptureClient(IUnknown):
            _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
            _methods_ = [
                COMMETHOD([], c.HRESULT, "GetBuffer", (["out"], c.POINTER(c.c_void_p), "data"), (["out"], c.POINTER(c.c_uint32), "frames"), (["out"], c.POINTER(c.c_uint32), "flags"), (["out"], c.POINTER(c.c_uint64), "devicePosition"), (["out"], c.POINTER(c.c_uint64), "qpcPosition")),
                COMMETHOD([], c.HRESULT, "ReleaseBuffer", (["in"], c.c_uint32, "frames")),
                COMMETHOD([], c.HRESULT, "GetNextPacketSize", (["out"], c.POINTER(c.c_uint32), "frames")),
            ]

        client = capture = device = stream = None
        capture_started = False
        comtypes.CoInitialize()
        try:
            output_index, self.output_name = output_device(self.output_mode, sd)
            device = AudioUtilities.GetSpeakers()
            self.source_name = getattr(device, "FriendlyName", "Default Windows speakers")
            if "CABLE" in self.source_name:
                raise RuntimeError("Use normal Windows speakers for Phone Link, not CABLE Input")
            client = device._dev.Activate(IAudioClient._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioClient)
            fmt = client.GetMixFormat()
            try:
                raw = c.string_at(fmt, 18 + fmt.contents.cbSize)
                subtype = GUID.from_buffer_copy(raw[24:40]) if fmt.contents.wFormatTag == 65534 and len(raw) >= 40 else None
                if fmt.contents.wBitsPerSample != 32 or (fmt.contents.wFormatTag != 3 and str(subtype).upper() != "{00000003-0000-0010-8000-00AA00389B71}"):
                    raise RuntimeError("Default Windows playback must use IEEE float32")
                channels, rate = fmt.contents.nChannels, fmt.contents.nSamplesPerSec
                if rate != WEB_RATE or not 1 <= channels <= 8:
                    raise RuntimeError(f"Expected 48000 Hz playback with 1..8 channels, got {rate} Hz/{channels}")
                client.Initialize(0, 0x20000, 1000000, 0, fmt, None)
            finally:
                c.windll.ole32.CoTaskMemFree(fmt)
            capture = client.GetService(IAudioCaptureClient._iid_).QueryInterface(IAudioCaptureClient)
            stream = sd.RawOutputStream(device=output_index, samplerate=self.output_rate, channels=self.output_channels,
                                        dtype="float32" if self.output_mode == "virtual-mic" else "int16",
                                        blocksize=self.output_rate // 50, callback=self._output_callback)
            stream.start()
            client.Start()
            capture_started = True
            self.started_at = time.monotonic()
            self.ready_event.set()
            pending = np.empty(0, dtype=np.float32)
            while not self.stop_event.is_set():
                if self.max_seconds is not None and time.monotonic() - self.started_at >= self.max_seconds:
                    self.reason = "time_limit"
                    self.stop_event.set()
                    break
                while (not self.stop_event.is_set()
                       and (self.max_seconds is None or time.monotonic() - self.started_at < self.max_seconds)
                       and capture.GetNextPacketSize()):
                    ptr, frames, flags, _, _ = capture.GetBuffer()
                    try:
                        if flags & 2 or not ptr:
                            mono = np.zeros(frames, dtype=np.float32)
                        else:
                            values = np.ctypeslib.as_array(c.cast(ptr, c.POINTER(c.c_float)), shape=(frames * channels,))
                            mono = values.reshape(frames, channels).mean(axis=1, dtype=np.float32)
                            mono = np.nan_to_num(mono, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
                            np.clip(mono, -1.0, 1.0, out=mono)
                        with self._stats_lock:
                            self._captured_frames += frames
                            if mono.size:
                                self._input_peak = max(self._input_peak, float(np.max(np.abs(mono))))
                        pending = np.concatenate((pending, mono))
                        while pending.size >= PACKET_FRAMES:
                            self._emit_packet(pending[:PACKET_FRAMES].astype("<f4", copy=False).tobytes())
                            pending = pending[PACKET_FRAMES:]
                    finally:
                        capture.ReleaseBuffer(frames)
                self.stop_event.wait(0.005)
        except Exception as exc:
            self.error = str(exc)
            self.reason = "device_error"
            self.stop_event.set()
        finally:
            self.ready_event.set()
            if stream is not None:
                try:
                    stream.abort()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if capture_started:
                try:
                    client.Stop()
                except Exception:
                    pass
            # Releasing these references here keeps COM destruction on its owner.
            capture = client = device = stream = None
            self.output_queue.clear()
            comtypes.CoUninitialize()
