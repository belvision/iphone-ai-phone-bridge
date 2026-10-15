"""Two local output routes and a reversible virtual-microphone lease.

Only the known VB-CABLE pair can be selected. No arbitrary device IDs are
accepted from HTTP. Device changes are restored unless the user changed them.
"""
import threading
import time
import warnings
import json
from pathlib import Path

import comtypes
from pycaw.pycaw import AudioUtilities
from pycaw.constants import EDataFlow, ERole

MODES = {"direct-hfp", "virtual-mic"}
ROLES = (ERole.eConsole, ERole.eMultimedia, ERole.eCommunications)
RESTORE_FILE = Path(__file__).resolve().parent / ".mic-restore.json"


def cable_output_device():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        devices = AudioUtilities.GetAllDevices()
    matches = [d for d in devices if d._dev.GetState() == 1
               and (d.FriendlyName or "").startswith("CABLE Output")
               and "VB-Audio" in (d.FriendlyName or "")]
    if len(matches) != 1:
        raise RuntimeError("VB-CABLE не найден. Установите официальный драйвер и перезагрузите компьютер.")
    return matches[0]


def output_device(mode, sd):
    if mode not in MODES:
        raise ValueError("Неизвестный способ передачи звука.")
    apis = sd.query_hostapis()
    matches = []
    for i, dev in enumerate(sd.query_devices()):
        api = apis[dev["hostapi"]]["name"]
        if mode == "direct-hfp":
            good = "Hands-Free HF Audio" in dev["name"] and "WDM-KS" in api and dev["max_output_channels"] >= 1
        else:
            good = dev["name"].startswith("CABLE Input") and "VB-Audio" in dev["name"] and "WASAPI" in api and dev["max_output_channels"] >= 2
        if good:
            matches.append((i, dev["name"]))
    if len(matches) != 1:
        raise RuntimeError("Не найден единственный выход " + ("iPhone Hands-Free." if mode == "direct-hfp" else "CABLE Input. После установки перезапустите программу."))
    return matches[0]


class MicrophoneLease:
    def __init__(self):
        self.lock = threading.RLock()
        self.original = None
        self.target = None
        self.updated = 0
        self.error = None
        if RESTORE_FILE.is_file():
            try:
                saved = json.loads(RESTORE_FILE.read_text(encoding="utf-8"))
                ids = [saved["target"], *saved["original"].values()]
                if len(ids) != 4 or not all(isinstance(i, str) and i.startswith("{0.0.1.") and len(i) < 160 for i in ids):
                    raise ValueError("Invalid microphone backup")
                self.original = {r: saved["original"][r.name] for r in ROLES}
                self.target = saved["target"]
            except (OSError, ValueError, KeyError, TypeError):
                self.error = "Не удалось прочитать резервную копию микрофона. Проверьте настройки звука Windows."

    def _defaults(self):
        enum = AudioUtilities.GetDeviceEnumerator()
        return {r: enum.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, r.value).GetId() for r in ROLES}

    def prepare(self, mode):
        with self.lock:
            comtypes.CoInitialize()
            try:
                if mode not in MODES:
                    raise ValueError("Неизвестный способ передачи звука.")
                if mode == "direct-hfp":
                    self._restore()
                    return
                target = cable_output_device().id
                speakers = AudioUtilities.GetSpeakers()
                if "CABLE" in (speakers.FriendlyName or ""):
                    raise RuntimeError("Оставьте динамики Windows обычными: CABLE Input нельзя использовать для звука собеседника.")
                if self.original is None:
                    self.original = self._defaults()
                    self.target = target
                    try:
                        RESTORE_FILE.write_text(json.dumps({"target": target, "original": {r.name: v for r, v in self.original.items()}}), encoding="utf-8")
                    except OSError:
                        self.original = self.target = None
                        raise RuntimeError("Не удалось сохранить прежние настройки микрофона; переключение отменено.")
                elif self.target != target:
                    raise RuntimeError("Устройство VB-CABLE изменилось. Сначала остановите помощника.")
                try:
                    AudioUtilities.SetDefaultDevice(target, roles=list(ROLES))
                    if any(value != target for value in self._defaults().values()):
                        raise RuntimeError("Windows не переключила микрофон на VB-CABLE.")
                except Exception:
                    self._restore()
                    raise
                self.updated = time.monotonic()
                self.error = None
            finally:
                speakers = None
                comtypes.CoUninitialize()

    def _restore(self):
        if self.original is None:
            return
        current = self._defaults()
        failures = []
        for role, old_id in self.original.items():
            if current[role] == self.target and old_id != self.target:
                try:
                    AudioUtilities.SetDefaultDevice(old_id, roles=[role])
                except Exception as exc:
                    failures.append(str(exc))
        if failures:
            self.error = "Не удалось вернуть прежний микрофон Windows. Проверьте настройки звука."
            raise RuntimeError(self.error)
        self.original = self.target = None
        RESTORE_FILE.unlink(missing_ok=True)

    def restore(self):
        with self.lock:
            comtypes.CoInitialize()
            try:
                self._restore()
            finally:
                comtypes.CoUninitialize()

    def heartbeat(self):
        with self.lock:
            if self.original is not None:
                self.updated = time.monotonic()

    def expired(self):
        with self.lock:
            return self.original is not None and time.monotonic() - self.updated > 30

    def ready(self):
        with self.lock:
            comtypes.CoInitialize()
            try:
                return self.target is not None and all(v == self.target for v in self._defaults().values())
            finally:
                comtypes.CoUninitialize()

    def status(self):
        with self.lock:
            comtypes.CoInitialize()
            try:
                try:
                    cable_output_device()
                    installed = True
                except RuntimeError:
                    installed = False
                return {"virtualMicInstalled": installed, "virtualMicPrepared": self.ready(), "error": self.error}
            finally:
                comtypes.CoUninitialize()
