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


RECEIVERS_API = "/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers"
LEGACY_TEST_API = "/api/alertmanager/grafana/config/api/v1/receivers/test"


def _test_alert(cfg: Config) -> dict[str, Any]:
    return {
        "labels": {"alertname": "TestAlert", "severity": "warning", "location": cfg.location},
        "annotations": {"summary": _message(cfg)},
    }


def _report(name: str, error: str | None) -> int:
    if error is None:
        _ok(name)
        return 0
    _fail(name, error)
    return 1


def _test_integration(
    cfg: Config, base: str, auth: tuple[str, str], receiver: str, integration: dict[str, Any]
) -> str | None:
    """Test one integration with Grafana >= 13's receivers API. None = success."""
    body = {"integration": integration, "alert": _test_alert(cfg)}
    try:
        r = requests.post(
            f"{base}{RECEIVERS_API}/{receiver}/test", json=body, auth=auth, timeout=60
        )
    except requests.RequestException as exc:
        return str(exc)
    result = r.json() if r.content else {}
    if r.status_code == 200 and result.get("status") == "success":
        return None
    return str(result.get("error") or f"HTTP {r.status_code} {r.text[:200]}")


def _test_integration_legacy(
    cfg: Config, base: str, auth: tuple[str, str], integration: dict[str, Any]
) -> str | None:
    """Grafana <= 12: the Alertmanager-config test endpoint (removed in 13)."""
    body = {
        "receivers": [{"name": CONTACT_POINT, "grafana_managed_receiver_configs": [integration]}],
        "alert": _test_alert(cfg),
    }
    try:
        r = requests.post(f"{base}{LEGACY_TEST_API}", json=body, auth=auth, timeout=60)
    except requests.RequestException as exc:
        return str(exc)
    errors = [
        c.get("error", "")
        for rec in (r.json().get("receivers", []) if r.content else [])
        for c in rec.get("grafana_managed_receiver_configs", [])
        if c.get("status") != "ok"
    ]
    if r.status_code == 200 and not errors:
        return None
    return "; ".join(e for e in errors if e) or f"HTTP {r.status_code} {r.text[:200]}"


def _integrations(base: str, auth: tuple[str, str]) -> tuple[str, list[dict[str, Any]]] | None:
    """Return (api, integrations of serverroom-notify), trying Grafana 13's API first."""
    r = requests.get(f"{base}{RECEIVERS_API}", auth=auth, timeout=10)
    if r.status_code == 401:
        raise PermissionError
    if r.status_code == 200:
        for item in r.json().get("items", []):
            if item["spec"]["title"] == CONTACT_POINT:
                integrations = item["spec"]["integrations"]
                return item["metadata"]["name"], integrations
        return None
    # Older Grafana: read the Alertmanager config instead.
    r = requests.get(f"{base}/api/alertmanager/grafana/config/api/v1/alerts", auth=auth, timeout=10)
    if r.status_code == 401:
        raise PermissionError
    r.raise_for_status()
    for rec in r.json().get("alertmanager_config", {}).get("receivers", []):
        if rec["name"] == CONTACT_POINT:
            return "", rec.get("grafana_managed_receiver_configs", [])
    return None


def _via_grafana(cfg: Config, base: str, password: str) -> int:
    auth = ("admin", password)
    try:
        found = _integrations(base, auth)
    except PermissionError:
        _fail("grafana", "login failed (check GRAFANA_ADMIN_PASSWORD in secrets.env)")
        return 1
    except requests.RequestException as exc:
        _fail("grafana", f"unreachable at {base}: {exc}")
        return 1
    integrations = [i for i in (found[1] if found else []) if i.get("uid") != "sr-none"]
    if not found or not integrations:
        _fail(
            "grafana",
            "no notification channel is enabled; enable one in config.yaml and run apply-config.sh",
        )
        return 1
    receiver = found[0]
    failures = 0
    for integration in integrations:
        name = f"{integration['type']} ({integration.get('uid', '?')})"
        if receiver:
            error = _test_integration(cfg, base, auth, receiver, integration)
        else:
            error = _test_integration_legacy(cfg, base, auth, integration)
        failures += _report(name, error)
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
