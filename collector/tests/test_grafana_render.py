from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from serverroom_monitor.config import Config, load_config, parse_config
from serverroom_monitor.grafana.alerting import build_contact_points, build_policies, build_rules
from serverroom_monitor.grafana.dashboard import build_dashboard
from serverroom_monitor.grafana.render import render_all

from .conftest import REPO

EXAMPLE = REPO / "config" / "config.example.yaml"


def all_channels(**overrides: object) -> Config:
    data = {
        "location": "dc-1",
        "notifications": {
            "telegram": {"enabled": True, "chat_id": "-100123"},
            "email": {
                "enabled": True,
                "addresses": ["a@example.com", "b@example.com"],
                "smtp_host": "smtp.example.com",
                "from_address": "pi@example.com",
            },
            "webhook": {"enabled": True, "type": "discord"},
            "deadman": {"enabled": True},
        },
        **overrides,
    }
    return parse_config(data, env={})


def rules_by_uid(cfg: Config) -> dict[str, dict]:
    return {r["uid"]: r for r in build_rules(cfg)["groups"][0]["rules"]}


def threshold_of(rule: dict) -> tuple[str, float]:
    cond = rule["data"][-1]["model"]["conditions"][0]["evaluator"]
    return cond["type"], cond["params"][0]


def test_rules_follow_thresholds() -> None:
    cfg = parse_config(
        {"thresholds": {"temperature": {"warning_high": 25, "critical_high": 28}}}, env={}
    )
    rules = rules_by_uid(cfg)
    assert threshold_of(rules["sr-temp-warn"]) == ("gt", 25)
    assert threshold_of(rules["sr-temp-crit"]) == ("gt", 28)
    assert threshold_of(rules["sr-hum-low-warn"]) == ("lt", 25)
    assert threshold_of(rules["sr-hum-low-crit"]) == ("lt", 20)
    assert threshold_of(rules["sr-hum-high-warn"]) == ("gt", 60)
    assert threshold_of(rules["sr-hum-high-crit"]) == ("gt", 70)
    assert threshold_of(rules["sr-stale"]) == ("gt", 180)
    assert threshold_of(rules["sr-sensor-error"]) == ("lt", 1)


def test_rule_states_and_pending() -> None:
    rules = rules_by_uid(load_config(EXAMPLE, env={}))
    for uid in ("sr-temp-warn", "sr-temp-crit", "sr-hum-low-warn", "sr-sensor-error"):
        assert rules[uid]["for"] == "2m"
        assert rules[uid]["noDataState"] == "KeepLast"  # stale rule reports outages
    stale = rules["sr-stale"]
    assert stale["for"] == "0s"
    assert stale["noDataState"] == "Alerting"
    assert stale["execErrState"] == "Alerting"
    assert {r["labels"]["severity"] for r in rules.values()} == {"warning", "critical"}
    assert "sr-deadman" not in rules


def test_queries_scope_to_location_and_contain_no_dollar() -> None:
    cfg = all_channels()
    for rule in build_rules(cfg)["groups"][0]["rules"]:
        for d in rule["data"]:
            query = d["model"].get("query")
            if query:
                assert 'r.location == "dc-1"' in query
                assert "$" not in query


def test_annotations_use_grafana_template_variables_unescaped() -> None:
    rules = rules_by_uid(load_config(EXAMPLE, env={}))
    summary = rules["sr-temp-warn"]["annotations"]["summary"]
    assert "{{ $labels.location }}" in summary
    assert "$values.B.Value" in summary
    assert "$$" not in summary


def test_contact_points_all_channels() -> None:
    doc = build_contact_points(all_channels())
    cps = {cp["name"]: cp for cp in doc["contactPoints"]}
    receivers = {r["uid"]: r for r in cps["serverroom-notify"]["receivers"]}
    assert receivers["sr-telegram"]["settings"]["bottoken"] == "${TELEGRAM_BOT_TOKEN}"
    assert receivers["sr-telegram"]["settings"]["chatid"] == "-100123"
    assert receivers["sr-email"]["settings"]["addresses"] == "a@example.com;b@example.com"
    assert receivers["sr-webhook"]["type"] == "discord"
    assert receivers["sr-webhook"]["settings"]["url"] == "${WEBHOOK_URL}"
    assert all(not r["disableResolveMessage"] for r in receivers.values())
    assert cps["serverroom-deadman"]["receivers"][0]["settings"]["url"] == "${DEADMAN_URL}"
    assert doc["deleteContactPoints"] == [{"orgId": 1, "uid": "sr-none"}]


def test_contact_points_only_contain_secret_placeholders() -> None:
    # Grafana expands $ in this file, so nothing but ${SECRET} may use it.
    text = yaml.safe_dump(build_contact_points(all_channels()))
    dollars = re.findall(r"\$\{?[A-Za-z_]*\}?", text)
    assert set(dollars) <= {"${TELEGRAM_BOT_TOKEN}", "${WEBHOOK_URL}", "${DEADMAN_URL}"}


def test_webhook_types() -> None:
    for kind, grafana_type in (("slack", "slack"), ("teams", "teams"), ("generic", "webhook")):
        cfg = parse_config({"notifications": {"webhook": {"enabled": True, "type": kind}}}, env={})
        (cp,) = build_contact_points(cfg)["contactPoints"]
        assert cp["receivers"][0]["type"] == grafana_type


def test_no_channels_uses_placeholder_never_grafana_default() -> None:
    cfg = parse_config(None, env={})
    doc = build_contact_points(cfg)
    (cp,) = doc["contactPoints"]
    assert cp["name"] == "serverroom-notify"
    (placeholder,) = cp["receivers"]
    assert placeholder["uid"] == "sr-none"
    assert "no-notification-channel-configured" in placeholder["settings"]["url"]
    assert {d["uid"] for d in doc["deleteContactPoints"]} == {
        "sr-telegram",
        "sr-email",
        "sr-webhook",
        "sr-deadman",
    }
    policies = build_policies(cfg)["policies"][0]
    assert policies["receiver"] == "serverroom-notify"
    assert "grafana-default-email" not in yaml.safe_dump(build_policies(cfg))


def test_placeholder_removed_once_a_channel_is_enabled() -> None:
    doc = build_contact_points(all_channels())
    assert {"orgId": 1, "uid": "sr-none"} in doc["deleteContactPoints"]


def test_policies() -> None:
    policy = build_policies(all_channels())["policies"][0]
    assert policy["receiver"] == "serverroom-notify"
    assert policy["repeat_interval"] == "4h"
    deadman, critical = policy["routes"]
    assert deadman["object_matchers"] == [["route", "=", "deadman"]]
    assert critical["repeat_interval"] == "1h"


def test_dashboard_thresholds_follow_config() -> None:
    cfg = parse_config(
        {"thresholds": {"temperature": {"warning_high": 24, "critical_high": 26}}}, env={}
    )
    dash = build_dashboard(cfg)
    temp = next(p for p in dash["panels"] if p["title"] == "Temperature" and p["type"] == "stat")
    steps = temp["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert [s["value"] for s in steps] == [None, 24, 26]
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids))
    assert dash["uid"] == "serverroom"
    assert dash["refresh"] == "30s"


def test_dashboard_first_screen_fits_7inch_display() -> None:
    dash = build_dashboard(load_config(EXAMPLE, env={}))
    first = [p for p in dash["panels"] if p["gridPos"]["y"] < 10]
    titles = {p["title"] for p in first}
    assert {"Temperature", "Humidity", "Dew point", "Last reading"} <= titles
    assert max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in first) <= 10
    for p in dash["panels"]:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24


def test_render_all_writes_valid_files(tmp_path: Path) -> None:
    cfg = all_channels(grafana={"lan_access": True})
    paths = render_all(cfg, tmp_path, root_url="https://pi.tail1234.ts.net/")
    for path in paths:
        if path.suffix == ".yaml":
            yaml.safe_load(path.read_text())
        elif path.suffix == ".json":
            json.loads(path.read_text())
    settings = (tmp_path / "grafana-settings.env").read_text()
    assert 'GF_SERVER_HTTP_ADDR="0.0.0.0"' in settings
    assert 'GF_AUTH_ANONYMOUS_ENABLED="false"' in settings
    assert 'GF_SERVER_ROOT_URL="https://pi.tail1234.ts.net/"' in settings
    assert 'GF_SMTP_HOST="smtp.example.com:587"' in settings
    assert "PASSWORD" not in settings  # secrets never in generated files
    provider = yaml.safe_load((tmp_path / "provisioning/dashboards/serverroom.yaml").read_text())
    assert provider["providers"][0]["options"]["path"] == str(tmp_path / "dashboards")


def test_render_default_binds_localhost(tmp_path: Path) -> None:
    render_all(parse_config(None, env={}), tmp_path)
    settings = (tmp_path / "grafana-settings.env").read_text()
    assert 'GF_SERVER_HTTP_ADDR="127.0.0.1"' in settings
    assert 'GF_SECURITY_COOKIE_SECURE="true"' in settings
    # Grafana 13: the InfluxDB datasource is a plugin that must exist before startup.
    assert 'GF_PLUGINS_PREINSTALL_SYNC="influxdb"' in settings
    assert "GF_SERVER_ROOT_URL" not in settings


def test_committed_provisioning_matches_generator(tmp_path: Path) -> None:
    """grafana/ holds the rendered defaults; regenerate with scripts/render-defaults.sh."""
    render_all(
        load_config(EXAMPLE, env={}),
        tmp_path,
        deploy_root=Path("/etc/serverroom-monitor/generated"),
    )
    committed = sorted(p.relative_to(REPO / "grafana") for p in (REPO / "grafana").rglob("*.*"))
    assert committed, "grafana/ is empty"
    for rel in committed:
        assert (REPO / "grafana" / rel).read_text() == (tmp_path / rel).read_text(), (
            f"grafana/{rel} is stale: run scripts/render-defaults.sh"
        )
