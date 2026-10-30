"""Send a test notification to every enabled channel.

Default: ask Grafana to test each integration of the ``serverroom-notify``
contact point. That exercises exactly what real alerts use (provisioned
settings, stored secrets, templates, Grafana's network access).

``--direct``: bypass Grafana and talk to Telegram / SMTP / the webhook with
the secrets from the environment. Useful to tell "wrong token" apart from
"Grafana is misconfigured".
"""

from __future__ import annotations

import os
import smtplib
import socket
import ssl
from email.message import EmailMessage
from typing import Any

import requests

from serverroom_monitor.config import Config
from serverroom_monitor.grafana.alerting import CONTACT_POINT

TITLE = "🧪 TEST: Server room monitor"


def _message(cfg: Config) -> str:
    return (
        f"Test notification from the server room monitor in '{cfg.location}' "
        f"(host {socket.gethostname()}). If you can read this, the channel works."
    )


def _ok(name: str) -> None:
    print(f"  [ OK ] {name}")


def _fail(name: str, why: str) -> None:
    print(f"  [FAIL] {name}: {why}")


# --------------------------------------------------------------------------
# Through Grafana
# --------------------------------------------------------------------------


def _via_grafana(cfg: Config, base: str, password: str) -> int:
    auth = ("admin", password)
    try:
        resp = requests.get(
            f"{base}/api/alertmanager/grafana/config/api/v1/alerts", auth=auth, timeout=10
        )
    except requests.RequestException as exc:
        _fail("grafana", f"unreachable at {base}: {exc}")
        return 1
    if resp.status_code == 401:
        _fail("grafana", "login failed (check GRAFANA_ADMIN_PASSWORD in secrets.env)")
        return 1
    resp.raise_for_status()
    receivers = resp.json().get("alertmanager_config", {}).get("receivers", [])
    configs: list[dict[str, Any]] = next(
        (
            r.get("grafana_managed_receiver_configs", [])
            for r in receivers
            if r["name"] == CONTACT_POINT
        ),
        [],
    )
    if not configs:
        _fail(
            "grafana",
            f"contact point '{CONTACT_POINT}' has no integrations; "
            "enable a channel in config.yaml and run apply-config.sh",
        )
        return 1

    failures = 0
    for integration in configs:
        name = f"{integration['type']} ({integration.get('uid', '?')})"
        body = {
            "receivers": [
                {"name": CONTACT_POINT, "grafana_managed_receiver_configs": [integration]}
            ],
            "alert": {
                "labels": {
                    "alertname": "TestAlert",
                    "severity": "warning",
                    "location": cfg.location,
                },
                "annotations": {"summary": _message(cfg)},
            },
        }
        try:
            r = requests.post(
                f"{base}/api/alertmanager/grafana/config/api/v1/receivers/test",
                json=body,
                auth=auth,
                timeout=60,
            )
        except requests.RequestException as exc:
            _fail(name, str(exc))
            failures += 1
            continue
        errors = [
            cfg_result.get("error", "")
            for rec in (r.json().get("receivers", []) if r.content else [])
            for cfg_result in rec.get("grafana_managed_receiver_configs", [])
            if cfg_result.get("status") != "ok"
        ]
        if r.status_code == 200 and not errors:
            _ok(name)
        else:
            failures += 1
            _fail(name, "; ".join(e for e in errors if e) or f"HTTP {r.status_code} {r.text[:200]}")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# Direct
# --------------------------------------------------------------------------


def _telegram(cfg: Config, env: dict[str, str]) -> None:
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": cfg.notifications.telegram.chat_id, "text": f"{TITLE}\n{_message(cfg)}"},
        timeout=15,
    )
    data = r.json() if r.content else {}
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"HTTP {r.status_code}")


def _email(cfg: Config, env: dict[str, str]) -> None:
    e = cfg.notifications.email
    msg = EmailMessage()
    msg["Subject"] = TITLE
    msg["From"] = f"{e.from_name} <{e.from_address}>"
    msg["To"] = ", ".join(e.addresses)
    msg.set_content(_message(cfg))
    ctx = ssl.create_default_context()
    smtp: smtplib.SMTP
    if e.smtp_port == 465:
        smtp = smtplib.SMTP_SSL(e.smtp_host, e.smtp_port, timeout=20, context=ctx)
    else:
        smtp = smtplib.SMTP(e.smtp_host, e.smtp_port, timeout=20)
    with smtp:
        if e.smtp_port != 465 and e.starttls != "none":
            smtp.ehlo()
            if smtp.has_extn("starttls"):
                smtp.starttls(context=ctx)
            elif e.starttls == "mandatory":
                raise RuntimeError("server does not offer STARTTLS (starttls: mandatory)")
        if e.smtp_user:
            smtp.login(e.smtp_user, env.get("SMTP_PASSWORD", ""))
        smtp.send_message(msg)


def _webhook(cfg: Config, env: dict[str, str]) -> None:
    url = env.get("WEBHOOK_URL", "")
    if not url:
        raise RuntimeError("WEBHOOK_URL is not set")
    text = f"{TITLE}\n{_message(cfg)}"
    kind = cfg.notifications.webhook.type
    payload: dict[str, Any]
    if kind == "slack":
        payload = {"text": text}
    elif kind == "discord":
        payload = {"content": text}
    elif kind == "teams":
        payload = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                    },
                }
            ],
        }
    else:
        payload = {"status": "firing", "title": TITLE, "message": _message(cfg)}
    r = requests.post(url, json=payload, timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:200]}")


def _direct(cfg: Config) -> int:
    env = dict(os.environ)
    n = cfg.notifications
    checks = [
        ("telegram", n.telegram.enabled, _telegram),
        ("email", n.email.enabled, _email),
        ("webhook", n.webhook.enabled, _webhook),
    ]
    enabled = [(name, fn) for name, on, fn in checks if on]
    if not enabled:
        _fail("config", "no notification channel is enabled in config.yaml")
        return 1
    failures = 0
    for name, fn in enabled:
        try:
            fn(cfg, env)
            _ok(name)
        except Exception as exc:
            failures += 1
            _fail(name, str(exc))
    return 1 if failures else 0


def run_test_alerts(cfg: Config, direct: bool = False, grafana_url: str | None = None) -> int:
    if direct:
        print("Sending test notifications directly (bypassing Grafana):")
        return _direct(cfg)
    password = os.environ.get("GRAFANA_ADMIN_PASSWORD", "")
    if not password:
        print("GRAFANA_ADMIN_PASSWORD is not set (run via scripts/test-alerts.sh as root)")
        return 2
    base = (grafana_url or cfg.grafana.url).rstrip("/")
    print(f"Sending test notifications through Grafana ({base}):")
    return _via_grafana(cfg, base, password)
