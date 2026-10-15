"""Phone Link state and guarded call termination via the public Windows Calls API.

No UI automation, dialing, app registration or phone-number logging. A persistent
Windows PowerShell 5.1 sidecar supplies authoritative Dialing/Talking/Ended state.
``active`` means exactly one call in Talking state, not merely an open SCO link.
Consumers should also require ``available`` and ``audioOnComputer`` before routing
AI audio. Missing/stale/ambiguous state fails closed.
Current-call number/name are kept in memory for authorized local CRM matching;
the default public snapshot never includes these contact fields.
Explicit end_call requests use a private call token and a separate guarded
command; stopping the monitor itself never ends a phone call.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
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
        self._end_attempts: set[str] = set()

    @staticmethod
    def _unavailable(error: str) -> dict:
        return {"available": False, "active": False, "callPhase": "unknown", "callDirection": "unknown",
                "callCount": 0, "callSequence": 0, "audioOnComputer": False,
                "source": "windows-phone-call-api", "errorCode": error}

    @staticmethod
    def _parse_state(data: dict) -> dict:
        if not isinstance(data, dict): raise ValueError('Invalid phone state')
        phase = data.get("callPhase")
        if phase not in {"idle", "dialing", "incoming", "connected", "held", "ended", "lost", "multiple", "unknown", "unavailable"}:
            raise ValueError('Invalid call phase')
        count = max(0, int(data.get("callCount", 0)))
        # Explicit allow-list keeps line/call identifiers and unrelated metadata
        # outside both public snapshots and the private current-contact cache.
        state = {
            "available": data.get("available") is True,
            "active": data.get("available") is True and data.get("active") is True and phase == "connected" and count == 1,
            "callPhase": phase,
            "callDirection": data.get("callDirection") if data.get("callDirection") in {"outgoing", "incoming"} else "unknown",
            "callCount": count,
            "callSequence": max(0, int(data.get("callSequence", 0))),
            "audioOnComputer": data.get("audioOnComputer") is True,
            "source": "windows-phone-call-api",
            "errorCode": data.get("errorCode") if data.get("errorCode") in {None, "api-unavailable", "query-failed"} else "query-failed",
        }
        if state['active']:
            token = data.get('phoneCallToken')
            if isinstance(token, str) and re.fullmatch(r'[a-f0-9]{64}', token):
                state['phoneCallToken'] = token
            for key, limit in (('phoneNumber', 100), ('displayName', 200)):
                value = data.get(key, '')
                state[key] = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', value).strip()[:limit] if isinstance(value, str) else ''
        return state

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
                    state = self._parse_state(json.loads(line))
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

    def snapshot(self, include_contact: bool = False, include_control: bool = False) -> dict:
        """Public state by default; opt-in contacts only for one fresh active call."""
        with self._lock:
            state = dict(self._state)
            age = time.monotonic() - self._last_update if self._last_update else None
        if age is not None and age > self.stale_seconds:
            state.update(available=False, active=False, callPhase="unknown",
                         audioOnComputer=False, errorCode="stale")
        if not (include_contact and age is not None and 0 <= age <= self.stale_seconds
                and state.get('available') and state.get('active')
                and state.get('callPhase') == 'connected' and state.get('callCount') == 1):
            state.pop('phoneNumber', None)
            state.pop('displayName', None)
        if not (include_control and age is not None and 0 <= age <= self.stale_seconds
                and state.get('available') and state.get('active') and state.get('callCount') == 1
                and state.get('callPhase') == 'connected' and state.get('audioOnComputer')):
            state.pop('phoneCallToken', None)
        state["ageMs"] = round(age * 1000) if age is not None else None
        return state

    async def _end_command(self, token: str) -> dict:
        """One hidden, bounded command; the actual target is rechecked in WinRT."""
        executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = Path(__file__).with_name('probe_phone_state.ps1')
        process = None
        try:
            process = await asyncio.create_subprocess_exec(str(executable), '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-File', str(script), '-EndCurrentCall',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            output, _ = await asyncio.wait_for(process.communicate((token + '\n').encode('ascii')), 8)
            if process.returncode != 0 or len(output) > 4096: return {'status': 'command-failed'}
            result = json.loads(output)
            allowed = {'succeeded', 'timed-out', 'connection-lost', 'not-connected', 'not-on-computer',
                       'multiple-calls', 'wrong-call', 'invalid-target', 'unavailable', 'command-failed'}
            if not isinstance(result, dict) or result.get('operation') != 'end-call' or result.get('status') not in allowed:
                return {'status': 'command-failed'}
            return {'status': result['status']}
        except TimeoutError:
            return {'status': 'timed-out'}
        except (OSError, ValueError, TypeError):
            return {'status': 'command-failed'}
        finally:
            if process is not None and process.returncode is None:
                try: process.kill()
                except ProcessLookupError: pass
                await process.wait()

    async def end_call(self, expected_sequence: int, expected_token: str) -> dict:
        """End one explicitly identified current call, at most once per token."""
        if (type(expected_sequence) is not int or expected_sequence < 1 or not isinstance(expected_token, str)
                or not re.fullmatch(r'[a-f0-9]{64}', expected_token)):
            return {'ok': False, 'status': 'invalid-target'}
        with self._lock:
            state = self._state
            age = time.monotonic() - self._last_update if self._last_update else None
            if age is None or not 0 <= age <= self.stale_seconds: status = 'stale'
            elif not state.get('available'): status = 'unavailable'
            elif state.get('callCount', 0) > 1: status = 'multiple-calls'
            elif not state.get('active') or state.get('callPhase') != 'connected' or state.get('callCount') != 1: status = 'not-connected'
            elif not state.get('audioOnComputer'): status = 'not-on-computer'
            elif state.get('callSequence') != expected_sequence or state.get('phoneCallToken') != expected_token: status = 'wrong-call'
            elif expected_token in self._end_attempts: status = 'already-requested'
            else:
                status = ''
                self._end_attempts.add(expected_token)
        if status: return {'ok': False, 'status': status}
        requested_at = time.monotonic()
        result = await self._end_command(expected_token)
        if result['status'] != 'succeeded': return {'ok': False, 'status': result['status']}
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self._lock:
                state = self._state
                fresh = self._last_update > requested_at and 0 <= time.monotonic() - self._last_update <= self.stale_seconds
                ended = (state.get('callCount') == 0 and state.get('callPhase') in ('idle', 'ended', 'lost')
                         or state.get('callCount') == 1 and state.get('callSequence', 0) > expected_sequence)
                if fresh and state.get('available') and ended:
                    return {'ok': True, 'status': 'ended'}
            await asyncio.sleep(.05)
        return {'ok': False, 'status': 'confirmation-timeout'}

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
