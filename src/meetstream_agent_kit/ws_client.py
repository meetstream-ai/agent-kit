"""MS-2366: a small WebSocket client (RFC 6455), standard library only.

Enough for the relay: TLS (``wss://``; plain ``ws://`` only to this computer),
one subprotocol, masked client frames, text messages up to 4 MiB with
continuation frames, ping/pong and close. Sends are thread-safe.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from typing import Final

MAX_MESSAGE: Final = 4 * 1024 * 1024
_GUID: Final = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_LOOPBACK: Final = ("127.0.0.1", "localhost", "::1")


class WebSocketError(Exception):
    """The connection failed or was closed."""


class HandshakeRejectedError(WebSocketError):
    def __init__(self, status: int) -> None:
        super().__init__(f"relay refused the connection (HTTP {status})")
        self.status = status


class ClosedError(WebSocketError):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"closed ({code}{': ' + reason if reason else ''})")
        self.code = code
        self.reason = reason


class WebSocket:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._send_lock = threading.Lock()
        self._buffer = b""
        self.closed = False
        self.last_rx = time.monotonic()  # any frame, pings included (liveness)

    # Connect ------------------------------------------------------------------

    @classmethod
    def connect(cls, url: str, headers: dict[str, str], subprotocol: str, timeout: float = 15.0) -> WebSocket:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme == "wss":
            port = parts.port or 443
        elif parts.scheme == "ws" and host in _LOOPBACK:
            port = parts.port or 80
        else:
            raise WebSocketError("the relay address must start with wss://")
        raw = socket.create_connection((host, port), timeout=timeout)
        sock: socket.socket = raw
        if parts.scheme == "wss":
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            sock = context.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        host_header = host if parts.port is None else f"{host}:{parts.port}"
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            f"Sec-WebSocket-Protocol: {subprotocol}",
        ] + [f"{k}: {v}" for k, v in headers.items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        ws = cls(sock)
        head = ws._read_until(b"\r\n\r\n", 16384)
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        try:
            status = int(status_line.split(" ")[1])
        except (IndexError, ValueError):
            sock.close()
            raise WebSocketError("not a WebSocket answer") from None
        if status != 101:
            sock.close()
            raise HandshakeRejectedError(status)
        got = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in header_lines if h)}
        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()  # noqa: S324 - RFC 6455
        if got.get("sec-websocket-accept") != accept or got.get("sec-websocket-protocol") != subprotocol:
            sock.close()
            raise WebSocketError("the relay's WebSocket answer did not check out")
        sock.settimeout(None)
        return ws

    # Reading ------------------------------------------------------------------

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buffer)))
            if not chunk:
                self.closed = True
                raise ClosedError(1006, "connection lost")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self._buffer:
            if len(self._buffer) > limit:
                raise WebSocketError("handshake answer too long")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during the handshake")
            self._buffer += chunk
        head, _, self._buffer = self._buffer.partition(marker)
        return head

    def _frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._recv_exact(2)
        fin, opcode, masked, length = bool(b1 & 0x80), b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._recv_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._recv_exact(8))
        if length > MAX_MESSAGE:
            self.close(1009, "message too big")
            raise ClosedError(1009, "message too big")
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if mask:
            payload = _mask_fast(payload, mask)
        self.last_rx = time.monotonic()
        return fin, opcode, payload

    def recv(self) -> str:
        """The next text message (blocking). Answers pings; raises ``Closed`` on close."""
        message = b""
        while True:
            fin, opcode, payload = self._frame()
            if opcode == 0x9:  # ping
                self._send(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                reason = payload[2:].decode("utf-8", "replace")
                if not self.closed:
                    self._send(0x8, payload[:2])
                self.closed = True
                raise ClosedError(code, reason)
            if opcode in (0x1, 0x2, 0x0):
                message += payload
                if len(message) > MAX_MESSAGE:
                    self.close(1009, "message too big")
                    raise ClosedError(1009, "message too big")
                if fin:
                    return message.decode("utf-8")
                continue
            self.close(1002, "bad opcode")
            raise ClosedError(1002, "bad opcode")

    # Writing ------------------------------------------------------------------

    def _send(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = (
            bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if n < 4096 else _mask_fast(payload, mask)
        )
        with self._send_lock:
            if self.closed and opcode != 0x8:
                raise ClosedError(1006, "connection closed")
            self.sock.sendall(head + mask + masked)

    def send_text(self, text: str) -> None:
        data = text.encode("utf-8")
        if len(data) > MAX_MESSAGE:
            raise WebSocketError("message too big for the relay")
        self._send(0x1, data)

    def ping(self) -> None:
        self._send(0x9, b"")

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        with contextlib.suppress(OSError):
            self._send(0x8, struct.pack("!H", code) + reason.encode("utf-8")[:120])
        self.closed = True
        with contextlib.suppress(OSError):
            self.sock.close()


def _mask_fast(payload: bytes, mask: bytes) -> bytes:
    """XOR with the 4-byte mask using one big integer (fast for images)."""
    n = len(payload)
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")
