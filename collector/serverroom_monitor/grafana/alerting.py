"""Grafana unified-alerting provisioning: rules, contact points, policies, templates.

Secrets reach Grafana as ``${VAR}`` placeholders in the contact-point file,
which Grafana expands from grafana-server's environment (the env file systemd
loads). Verified against Grafana 12.3: alert *rule* files are not expanded,
so ``$labels`` / ``$values`` in annotations are written as-is. Contact points
and templates must not contain any other ``$`` (the tests enforce this).
"""

from __future__ import annotations

from typing import Any

from serverroom_monitor.config import Config, format_duration
from serverroom_monitor.grafana import flux
from serverroom_monitor.grafana.flux import DATASOURCE_UID

FOLDER = "Server Room"
GROUP = "serverroom"
CONTACT_POINT = "serverroom-notify"
DEADMAN_CONTACT_POINT = "serverroom-deadman"
DEFAULT_CONTACT_POINT = "grafana-default-email"

Json = dict[str, Any]


def _query(ref: str, query: str, lookback: int) -> Json:
    return {
        "refId": ref,
        "relativeTimeRange": {"from": lookback, "to": 0},
        "datasourceUid": DATASOURCE_UID,
        "model": {"refId": ref, "query": query, "intervalMs": 1000, "maxDataPoints": 43200},
    }


def _reduce(ref: str, source: str, reducer: str = "last") -> Json:
    return {
        "refId": ref,
        "relativeTimeRange": {"from": 0, "to": 0},
        "datasourceUid": "__expr__",
        "model": {
            "refId": ref,
            "type": "reduce",
            "expression": source,
            "reducer": reducer,
            "settings": {"mode": "dropNN"},
        },
    }


def _threshold(ref: str, source: str, op: str, value: float) -> Json:
    return {
        "refId": ref,
        "relativeTimeRange": {"from": 0, "to": 0},
        "datasourceUid": "__expr__",
        "model": {
            "refId": ref,
            "type": "threshold",
            "expression": source,
            "conditions": [{"evaluator": {"type": op, "params": [value]}}],
        },
    }


def _rule(
    uid: str,
    title: str,
    data: list[Json],
    severity: str,
    summary: str,
    description: str,
    pending: float,
    # KeepLast: when data stops, threshold alerts hold their state instead of
    # sending a misleading "resolved"; the stale-data rule reports the outage.
    no_data: str = "KeepLast",
    exec_err: str = "KeepLast",
    extra_labels: dict[str, str] | None = None,
) -> Json:
    return {
        "uid": uid,
        "title": title,
        "condition": data[-1]["refId"],
        "data": data,
        "noDataState": no_data,
        "execErrState": exec_err,
        "for": format_duration(pending),
        "annotations": {"summary": summary, "description": description},
        "labels": {"severity": severity, "service": "serverroom", **(extra_labels or {})},
        "isPaused": False,
    }


def _threshold_rule(
    cfg: Config,
    uid: str,
    title: str,
    field: str,
    op: str,
    value: float,
    severity: str,
    unit: str,
    what: str,
) -> Json:
    query = flux.alert_series(cfg, "environment", field)
    word = "above" if op == "gt" else "below"
    return _rule(
        uid,
        title,
        [_query("A", query, 600), _reduce("B", "A"), _threshold("C", "B", op, value)],
        severity,
        summary=(
            f"{what} in {{{{ $labels.location }}}} is "
            f'{{{{ printf "%.1f" $values.B.Value }}}}{unit} ({severity} {word} {value:g}{unit})'
        ),
        description=f"{what} has been {word} {value:g}{unit} for at least "
        f"{format_duration(cfg.thresholds.pending_period)}.",
        pending=cfg.thresholds.pending_period,
    )


def build_rules(cfg: Config) -> Json:
    t = cfg.thresholds
    rules = [
        _threshold_rule(
            cfg,
            "sr-temp-warn",
            "Temperature high (warning)",
            "temperature",
            "gt",
            t.temperature_warning_high,
            "warning",
            " °C",
            "Temperature",
        ),
        _threshold_rule(
            cfg,
            "sr-temp-crit",
            "Temperature high (critical)",
            "temperature",
            "gt",
            t.temperature_critical_high,
            "critical",
            " °C",
            "Temperature",
        ),
        _threshold_rule(
            cfg,
            "sr-hum-low-warn",
            "Humidity low (warning)",
            "humidity",
            "lt",
            t.humidity_warning_low,
            "warning",
            " %RH",
            "Humidity",
        ),
        _threshold_rule(
            cfg,
            "sr-hum-low-crit",
            "Humidity low (critical)",
            "humidity",
            "lt",
            t.humidity_critical_low,
            "critical",
            " %RH",
            "Humidity",
        ),
        _threshold_rule(
            cfg,
            "sr-hum-high-warn",
            "Humidity high (warning)",
            "humidity",
            "gt",
            t.humidity_warning_high,
            "warning",
            " %RH",
            "Humidity",
        ),
        _threshold_rule(
            cfg,
            "sr-hum-high-crit",
            "Humidity high (critical)",
            "humidity",
            "gt",
            t.humidity_critical_high,
            "critical",
            " %RH",
            "Humidity",
        ),
        # Stale data: no pending period on top of stale_after (that *is* the
        # debounce). If the query fails (InfluxDB down) or finds nothing in
        # 6 h, the rule fires too.
        _rule(
            "sr-stale",
            "No data from sensor",
            [
                _query("A", flux.age_seconds(cfg, "-6h"), 6 * 3600),
                _reduce("B", "A"),
                _threshold("C", "B", "gt", t.stale_after),
            ],
            "critical",
            summary=(
                f"No sensor data from {cfg.location} for "
                "{{ with $values.B }}{{ humanizeDuration .Value }}"
                "{{ else }}over 6 hours (or InfluxDB is unreachable){{ end }}"
            ),
            description=(
                f"The last reading is older than {format_duration(t.stale_after)}. Check the "
                "collector (journalctl -u serverroom-monitor), InfluxDB and the sensor wiring."
            ),
            pending=0,
            no_data="Alerting",
            exec_err="Alerting",
        ),
        _rule(
            "sr-sensor-error",
            "Sensor error",
            [
                _query("A", flux.alert_series(cfg, "collector_status", "sensor_ok"), 600),
                _reduce("B", "A"),
                _threshold("C", "B", "lt", 1),
            ],
            "critical",
            summary="Sensor in {{ $labels.location }} is failing to deliver valid readings",
            description=(
                f"The collector could not get a valid reading for {cfg.sensor.failure_threshold}"
                " consecutive cycles (I2C errors or out-of-range values). Check wiring; run "
                "/opt/serverroom-monitor/scripts/test-sensor.py."
            ),
            pending=t.pending_period,
        ),
    ]
    if cfg.notifications.deadman.enabled:
        rules.append(
            _rule(
                "sr-deadman",
                "Deadman (always firing)",
                [
                    {
                        "refId": "A",
                        "relativeTimeRange": {"from": 0, "to": 0},
                        "datasourceUid": "__expr__",
                        "model": {"refId": "A", "type": "math", "expression": "1"},
                    }
                ],
                "none",
                summary="Grafana alerting is alive",
                description="Always firing on purpose; routed only to the deadman URL.",
                pending=0,
                extra_labels={"route": "deadman"},
            )
        )
    return {
        "apiVersion": 1,
        "groups": [{"orgId": 1, "name": GROUP, "folder": FOLDER, "interval": "1m", "rules": rules}],
    }


TEMPLATES = r"""
{{ define "serverroom.title" -}}
{{ if eq .Status "firing" }}{{ if eq .CommonLabels.severity "critical" }}🔴 CRITICAL{{ else }}🟠 WARNING{{ end }}{{ else }}✅ RESOLVED{{ end }}: {{ .CommonLabels.alertname }}
{{- end }}

{{ define "serverroom.message" -}}
{{ range .Alerts.Firing }}🔥 {{ .Annotations.summary }}
{{ end }}{{ range .Alerts.Resolved }}✅ Resolved: {{ .Annotations.summary }}
{{ end }}
{{- end }}

{{ define "serverroom.text" -}}
{{ template "serverroom.title" . }}
{{ template "serverroom.message" . }}
{{- end }}
""".strip()


def _receivers(cfg: Config) -> list[Json]:
    n = cfg.notifications
    title = '{{ template "serverroom.title" . }}'
    message = '{{ template "serverroom.message" . }}'
    receivers: list[Json] = []
    if n.telegram.enabled:
        receivers.append(
            {
                "uid": "sr-telegram",
                "type": "telegram",
                "disableResolveMessage": False,
                "settings": {
                    "bottoken": "${TELEGRAM_BOT_TOKEN}",
                    "chatid": n.telegram.chat_id,
                    "message": '{{ template "serverroom.text" . }}',
                    "parse_mode": "None",
                },
            }
        )
    if n.email.enabled:
        receivers.append(
            {
                "uid": "sr-email",
                "type": "email",
                "disableResolveMessage": False,
                "settings": {
                    "addresses": ";".join(n.email.addresses),
                    "singleEmail": True,
                    "subject": title,
                    "message": message,
                },
            }
        )
    if n.webhook.enabled:
        kind = n.webhook.type
        settings: Json
        if kind == "slack":
            settings = {"url": "${WEBHOOK_URL}", "title": title, "text": message}
        elif kind == "discord":
            settings = {
                "url": "${WEBHOOK_URL}",
                "title": title,
                "message": message,
                "use_discord_username": False,
            }
        elif kind == "teams":
            settings = {"url": "${WEBHOOK_URL}", "title": title, "message": message}
        else:
            settings = {
                "url": "${WEBHOOK_URL}",
                "httpMethod": "POST",
                "title": title,
                "message": message,
            }
        receivers.append(
            {
                "uid": "sr-webhook",
                "type": "webhook" if kind == "generic" else kind,
                "disableResolveMessage": False,
                "settings": settings,
            }
        )
    return receivers


def build_contact_points(cfg: Config) -> Json:
    contact_points: list[Json] = []
    receivers = _receivers(cfg)
    if receivers:
        contact_points.append({"orgId": 1, "name": CONTACT_POINT, "receivers": receivers})
    if cfg.notifications.deadman.enabled:
        contact_points.append(
            {
                "orgId": 1,
                "name": DEADMAN_CONTACT_POINT,
                "receivers": [
                    {
                        "uid": "sr-deadman",
                        "type": "webhook",
                        "disableResolveMessage": True,
                        "settings": {"url": "${DEADMAN_URL}", "httpMethod": "POST"},
                    }
                ],
            }
        )
    doc: Json = {
        "apiVersion": 1,
        "templates": [{"orgId": 1, "name": "serverroom", "template": TEMPLATES}],
        "contactPoints": contact_points,
    }
    # Remove channels that were disabled since the last render.
    disabled = {"sr-telegram", "sr-email", "sr-webhook", "sr-deadman"} - {
        r["uid"] for cp in contact_points for r in cp["receivers"]
    }
    if disabled:
        doc["deleteContactPoints"] = [{"orgId": 1, "uid": uid} for uid in sorted(disabled)]
    return doc


def build_policies(cfg: Config) -> Json:
    n = cfg.notifications
    receiver = CONTACT_POINT if n.any_enabled else DEFAULT_CONTACT_POINT
    routes: list[Json] = []
    if n.deadman.enabled:
        routes.append(
            {
                "receiver": DEADMAN_CONTACT_POINT,
                "object_matchers": [["route", "=", "deadman"]],
                "group_wait": "0s",
                "group_interval": "1m",
                "repeat_interval": "5m",
                "continue": False,
            }
        )
    routes.append(
        {
            "receiver": receiver,
            "object_matchers": [["severity", "=", "critical"]],
            "repeat_interval": format_duration(n.repeat_interval_critical),
        }
    )
    return {
        "apiVersion": 1,
        "policies": [
            {
                "orgId": 1,
                "receiver": receiver,
                "group_by": ["alertname", "location"],
                "group_wait": format_duration(n.group_wait),
                "group_interval": format_duration(n.group_interval),
                "repeat_interval": format_duration(n.repeat_interval_warning),
                "routes": routes,
            }
        ],
    }
