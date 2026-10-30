"""InfluxDB 2.x writer using the plain HTTP API."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import requests

from serverroom_monitor.config import InfluxConfig

log = logging.getLogger(__name__)


class WriteError(Exception):
    """Base class for write failures."""


class RetryableWriteError(WriteError):
    """Server unreachable, overloaded, or misconfigured auth: keep the data."""


class RejectedWriteError(WriteError):
    """The server rejected the data itself (HTTP 400/413/422): retrying won't help."""


class InfluxWriter:
    def __init__(self, cfg: InfluxConfig, session: requests.Session | None = None) -> None:
        self.cfg = cfg
        self._session = session or requests.Session()
        self._url = f"{cfg.url}/api/v2/write"
        self._params = {"org": cfg.org, "bucket": cfg.bucket, "precision": "s"}
        self._headers = {
            "Authorization": f"Token {cfg.token}",
            "Content-Type": "text/plain; charset=utf-8",
        }

    def write(self, lines: Sequence[str]) -> None:
        if not lines:
            return
        body = "\n".join(lines).encode()
        try:
            resp = self._session.post(
                self._url,
                params=self._params,
                headers=self._headers,
                data=body,
                timeout=self.cfg.timeout,
            )
        except requests.RequestException as exc:
            raise RetryableWriteError(f"InfluxDB unreachable: {exc}") from exc
        if resp.status_code == 204:
            return
        detail = resp.text[:300].strip()
        if resp.status_code in (400, 413, 422):
            raise RejectedWriteError(
                f"InfluxDB rejected {len(lines)} point(s): HTTP {resp.status_code} {detail}"
            )
        # 401/403/404 are configuration problems (token, bucket); they are
        # fixable, so keep buffering rather than losing data.
        raise RetryableWriteError(f"InfluxDB write failed: HTTP {resp.status_code} {detail}")

    def close(self) -> None:
        self._session.close()
