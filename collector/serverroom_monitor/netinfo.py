"""Best-effort IP address discovery for the OLED."""

from __future__ import annotations

import socket
import subprocess
import time


def primary_ipv4() -> str | None:
    """The source address the kernel would use for outbound traffic.

    Connecting a UDP socket sends no packets; it only selects a route.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, never actually contacted
            addr: str = s.getsockname()[0]
            return addr
    except OSError:
        return None


class TailscaleIP:
    """Cached ``tailscale ip -4`` lookup (refreshed every few minutes)."""

    def __init__(self, ttl: float = 300.0) -> None:
        self._ttl = ttl
        self._value: str | None = None
        self._fetched = -1e9

    def get(self) -> str | None:
        now = time.monotonic()
        if now - self._fetched >= self._ttl:
            self._fetched = now
            try:
                out = subprocess.run(
                    ["tailscale", "ip", "-4"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                lines = out.stdout.split()
                self._value = lines[0] if out.returncode == 0 and lines else None
            except (OSError, subprocess.SubprocessError):
                self._value = None
        return self._value
