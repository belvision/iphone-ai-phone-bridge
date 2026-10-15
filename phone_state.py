"""Read-only Phone Link Bluetooth call state via the public Windows Calls API.

No UI automation, dialing, app registration or phone-number logging. A persistent
Windows PowerShell 5.1 sidecar supplies authoritative Dialing/Talking/Ended state.
``active`` means exactly one call in Talking state, not merely an open SCO link.
Consumers should also require ``available`` and ``audioOnComputer`` before routing
AI audio. Missing/stale/ambiguous state fails closed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time


class PhoneStateMonitor:
    def __init__(self, interval_ms: int = 250, stale_seconds: float = 2.0):
        self.interval_ms = max(100, min(int(interval_ms), 5000))
        self.stale_seconds = max(0.5, float(stale_seconds))
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_update = 0.0
        self._state = self._unavailable("not-started")

    @staticmethod
    def _unavailable(error: str) -> dict:
        return {"available": False, "active": False, "callPhase": "unknown", "callDirection": "unknown",
                "callCount": 0, "callSequence": 0, "audioOnComputer": False,
                "source": "windows-phone-call-api", "errorCode": error}

    def start(self) -> None:
        """Start the hidden read-only monitor once; no call is initiated."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            self._stop.clear()
            self._last_update = 0.0
            self._state = self._unavailable("starting")
            executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            script = Path(__file__).with_name("probe_phone_state.ps1")
            try:
                self._process = subprocess.Popen(
                    [str(executable), "-NoProfile", "-NonInteractive",
                     "-ExecutionPolicy", "Bypass", "-File", str(script),
                     "-IntervalMs", str(self.interval_ms)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                    errors="replace", bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                self._process = None
                self._state = self._unavailable("process-start-failed")
                return
            self._thread = threading.Thread(target=self._read, args=(self._process,),
                                            name="phone-state", daemon=True)
            self._thread.start()

    def _read(self, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                if self._stop.is_set():
                    break
                try:
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        continue
                    phase = data.get("callPhase")
                    if phase not in {"idle", "dialing", "incoming", "connected", "held", "ended", "lost", "multiple", "unknown", "unavailable"}:
                        continue
                    # Explicit allow-list avoids propagating accidental identifiers.
                    state = {
                        "available": data.get("available") is True,
                        "active": data.get("available") is True and data.get("active") is True and phase == "connected",
                        "callPhase": phase,
                        "callDirection": data.get("callDirection") if data.get("callDirection") in {"outgoing", "incoming"} else "unknown",
                        "callCount": max(0, int(data.get("callCount", 0))),
                        "callSequence": max(0, int(data.get("callSequence", 0))),
                        "audioOnComputer": data.get("audioOnComputer") is True,
                        "source": "windows-phone-call-api",
                        "errorCode": data.get("errorCode") if data.get("errorCode") in {None, "api-unavailable", "query-failed"} else "query-failed",
                    }
                except (ValueError, TypeError):
                    continue
                with self._lock:
                    if self._process is not process:
                        break
                    self._state = state
                    self._last_update = time.monotonic()
        finally:
            process.stdout.close()
            with self._lock:
                if self._process is process:
                    self._state = self._unavailable("stopped" if self._stop.is_set() else "process-exited")
                    self._last_update = 0.0

    def snapshot(self) -> dict:
        """Return a privacy-limited fresh snapshot; stale data cannot start AI."""
        with self._lock:
            state = dict(self._state)
            age = time.monotonic() - self._last_update if self._last_update else None
        if age is not None and age > self.stale_seconds:
            state.update(available=False, active=False, callPhase="unknown",
                         audioOnComputer=False, errorCode="stale")
        state["ageMs"] = round(age * 1000) if age is not None else None
        return state

    def stop(self) -> None:
        """Stop the monitor only. This never terminates the cellular call."""
        self._stop.set()
        with self._lock:
            process, thread = self._process, self._thread
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            self._process = None
            self._thread = None
            self._last_update = 0.0
            self._state = self._unavailable("stopped")
