"""Tiny sd_notify(3) implementation (no libsystemd binding required)."""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)


class SystemdNotifier:
    def __init__(self, environ: dict[str, str] | None = None) -> None:
        env = dict(os.environ) if environ is None else environ
        self._addr = env.get("NOTIFY_SOCKET", "")
        usec = env.get("WATCHDOG_USEC", "")
        pid = env.get("WATCHDOG_PID", "")
        self.watchdog_interval: float | None = None
        if usec.isdigit() and (not pid or pid == str(os.getpid())):
            self.watchdog_interval = int(usec) / 1_000_000
        self._sock: socket.socket | None = None
        if self._addr:
            if self._addr.startswith("@"):
                self._addr = "\0" + self._addr[1:]  # abstract namespace
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)

    @property
    def enabled(self) -> bool:
        return self._sock is not None

    def notify(self, state: str) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendto(state.encode(), self._addr)
        except OSError as exc:
            log.debug("sd_notify failed: %s", exc)

    def ready(self, status: str = "") -> None:
        self.notify("READY=1" + (f"\nSTATUS={status}" if status else ""))

    def status(self, status: str) -> None:
        self.notify(f"STATUS={status}")

    def watchdog(self) -> None:
        self.notify("WATCHDOG=1")

    def stopping(self) -> None:
        self.notify("STOPPING=1")
