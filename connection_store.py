"""Persist only a Retell public web-call key, encrypted for this Windows user."""
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import re
import tempfile
import threading


class Blob(c.Structure):
    _fields_ = [("size", w.DWORD), ("data", c.POINTER(c.c_ubyte))]


def protected(data, *, decrypt=False):
    crypt = c.WinDLL("crypt32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    buffer = c.create_string_buffer(data)
    source = Blob(len(data), c.cast(buffer, c.POINTER(c.c_ubyte)))
    output = Blob()
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [c.POINTER(Blob), c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, w.DWORD, c.POINTER(Blob)]
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [c.POINTER(Blob), w.LPCWSTR, c.c_void_p, c.c_void_p, c.c_void_p, w.DWORD, c.POINTER(Blob)]
    fn.restype = w.BOOL
    if not fn(c.byref(source), None, None, None, None, 1, c.byref(output)):
        raise OSError("Windows не смогла расшифровать подключение." if decrypt else "Windows не смогла зашифровать подключение.")
    try:
        return c.string_at(output.data, output.size)
    finally:
        if output.data:
            c.memset(output.data, 0, output.size)
            kernel.LocalFree(c.cast(output.data, c.c_void_p))
        c.memset(buffer, 0, len(buffer))


def validated_connection(value):
    if not isinstance(value, dict):
        raise ValueError("Некорректное подключение.")
    key, agent = value.get("key"), value.get("agentId")
    if not isinstance(key, str) or not re.fullmatch(r"public_key_[A-Za-z0-9_-]{1,240}", key):
        raise ValueError("Сохранить можно только Public Key, начинающийся с public_key_.")
    if not isinstance(agent, str) or not re.fullmatch(r"agent_[A-Za-z0-9_-]{1,240}", agent):
        raise ValueError("Проверьте ID агента.")
    mode = value.get("audioMode", "direct-hfp")
    if mode not in ("direct-hfp", "virtual-mic"):
        raise ValueError("Неизвестный способ передачи звука.")
    return {"key": key, "agentId": agent, "audioMode": mode}


class ConnectionStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else Path(os.environ["LOCALAPPDATA"]) / "RetellPhoneBridge" / "connection.dpapi"
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.is_file():
                return None
            payload = self.path.read_bytes()
            if len(payload) > 16384:
                raise ValueError("Сохранённое подключение повреждено.")
            try:
                return validated_connection(json.loads(protected(payload, decrypt=True)))
            except (ValueError, UnicodeError):
                raise ValueError("Сохранённое подключение повреждено.") from None

    def save(self, value):
        value = validated_connection(value)
        with self.lock:
            payload = protected(json.dumps(value).encode("utf-8"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix="connection-", suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
