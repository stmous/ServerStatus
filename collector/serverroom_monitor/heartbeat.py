"""Dead-man's-switch pings (healthchecks.io, Uptime Kuma push, Cronitor, ...).

The external service alerts when pings *stop*, which covers the cases local
alerting can't: the Pi losing power, a kernel hang, or the network dropping.
"""

from __future__ import annotations

import logging

import requests

from serverroom_monitor.config import HeartbeatConfig

log = logging.getLogger(__name__)


class Heartbeat:
    def __init__(self, cfg: HeartbeatConfig, session: requests.Session | None = None) -> None:
        self.cfg = cfg
        self._session = session or requests.Session()
        self._last_ok: bool | None = None

    def success(self, message: str = "") -> bool:
        return self._ping(self.cfg.url, message)

    def failure(self, message: str = "") -> bool:
        if not self.cfg.fail_suffix:
            return False  # service has no failure endpoint; silence is the signal
        return self._ping(self.cfg.url.rstrip("/") + self.cfg.fail_suffix, message)

    def _ping(self, url: str, message: str) -> bool:
        if not self.cfg.enabled:
            return False
        try:
            resp = self._session.post(url, data=message.encode()[:10_000], timeout=self.cfg.timeout)
            ok = resp.status_code < 400
            if not ok:
                log.warning("heartbeat ping returned HTTP %s", resp.status_code)
        except requests.RequestException as exc:
            ok = False
            # Only log transitions so an internet outage doesn't spam the journal.
            if self._last_ok is not False:
                log.warning("heartbeat ping failed: %s", exc)
        if ok and self._last_ok is False:
            log.info("heartbeat ping recovered")
        self._last_ok = ok
        return ok

    def close(self) -> None:
        self._session.close()
