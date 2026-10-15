"""Fixed-destination Retell HTTPS relay using the owner's encrypted HTTP proxy.

This is not an open proxy: only the web-call control and gateway routes used by
the local adapter are accepted, and credentials never appear in browser JSON.
"""
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from connection_store import protected

NETWORK_FILE = Path(os.environ["LOCALAPPDATA"]) / "RetellPhoneBridge" / "network.dpapi"
RETELL_ORIGIN = "https://api.retellai.com"


def save_proxy(value, path=NETWORK_FILE):
    parsed = urlsplit(value.get("address", ""))
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid configured proxy")
    if value.get("country") != "US" or not isinstance(value.get("username"), str) or not isinstance(value.get("password"), str):
        raise ValueError("A verified US proxy is required")
    data = protected(json.dumps(value).encode())
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="network-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


def load_proxy(path=NETWORK_FILE):
    path = Path(path)
    if not path.is_file(): return None
    payload = path.read_bytes()
    if len(payload) > 16384: raise ValueError("Invalid proxy configuration")
    return json.loads(protected(payload, decrypt=True))


def allowed_request(method, path):
    if method == "POST" and path == "/v3/create-web-call": return True
    if method == "POST" and re.fullmatch(r"/v2/stop-call/call_[A-Za-z0-9_-]+", path): return True
    base = r"/webrtc-proxy/call_[A-Za-z0-9_-]+/v1/webrtc/sessions"
    return (method == "POST" and re.fullmatch(base, path) is not None
            or method in ("PATCH", "DELETE") and re.fullmatch(base + r"/[A-Za-z0-9_-]+", path) is not None)


class RetellRelay:
    def __init__(self):
        self.proxy = None
        self.session = None

    async def start(self):
        self.proxy = load_proxy()
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25, connect=15))

    async def close(self):
        if self.session: await self.session.close()

    def status(self):
        return {"proxyEnabled": self.proxy is not None, "country": "US" if self.proxy else None}

    async def handle(self, request):
        path = "/" + request.match_info["tail"]
        if not allowed_request(request.method, path):
            raise web.HTTPNotFound()
        if not self.proxy or not self.session:
            return web.json_response({"error_message": "Американский прокси Retell не настроен."}, status=503)
        headers = {}
        for name in ("Authorization", "Content-Type", "X-Retell-Client-JS-SDK-Version", "g-recaptcha-response"):
            if name in request.headers: headers[name] = request.headers[name]
        # Retell public keys enforce their allowed browser domain even when
        # control requests travel through the owner's HTTP proxy.
        if request.headers.get("Origin"):
            headers["Origin"] = request.headers["Origin"]
            headers["Referer"] = request.headers["Origin"] + "/"
        proxy_headers = {"Proxy-Authorization": aiohttp.encode_basic_auth(self.proxy["username"], self.proxy["password"])}
        try:
            async with self.session.request(request.method, RETELL_ORIGIN + path,
                    data=await request.read(), headers=headers, proxy=self.proxy["address"], proxy_headers=proxy_headers,
                    allow_redirects=False) as upstream:
                chunks, size = [], 0
                async for chunk in upstream.content.iter_chunked(16384):
                    size += len(chunk)
                    if size > 262144: raise ValueError("Oversized Retell response")
                    chunks.append(chunk)
                body = b"".join(chunks)
                outgoing = {"Content-Type": upstream.headers.get("Content-Type", "application/json")}
                for name in ("X-Retell-Client-JS-SDK-Min-Version", "X-Retell-Client-JS-SDK-Recommended-Version"):
                    if name in upstream.headers: outgoing[name] = upstream.headers[name]
                return web.Response(status=upstream.status, body=body, headers=outgoing)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            # Raw exception strings may include proxy credentials; never expose them.
            return web.json_response({"error_message": "Нет соединения с Retell через американский прокси. Проверьте подключение."}, status=502)
