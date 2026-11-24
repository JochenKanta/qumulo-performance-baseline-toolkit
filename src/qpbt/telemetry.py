"""Small, read-only OpenMetrics sampler using Qumulo's documented API."""
from __future__ import annotations
import json
import os
import re
import ssl
import threading
import time
from datetime import datetime, timezone
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler, HTTPRedirectHandler

METRIC_BASES = (
    "qumulo_protocol_operation_bytes", "qumulo_protocol_operations",
    "qumulo_protocol_operation_latency_seconds", "qumulo_network_interface_received_bytes",
    "qumulo_network_interface_transmitted_bytes", "qumulo_network_interface_receive_errors",
    "qumulo_network_interface_transmit_errors", "qumulo_network_interface_link_speed_bits_per_second",
    "qumulo_disk_is_unhealthy", "qumulo_quorum_node_is_offline",
    "qumulo_fs_free_bytes", "qumulo_time_is_not_synchronizing",
)
NUMERIC = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)')
LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:\\.|[^"\\])*)"')


class TelemetryError(RuntimeError):
    pass


def parse_labels(text: str | None) -> dict:
    if not text:
        return {}
    out = {}
    for m in LABEL.finditer(text):
        # Only allow known enum labels needed for summaries; never export freeform paths or users.
        if m.group(1) in {"protocol", "data_type", "io_type", "node_id", "interface", "role"}:
            # '|' and '=' are key separators internally.
            out[m.group(1)] = m.group(2)[:80].replace("|", "_").replace("=", "_")
    return out


def parse_openmetrics(text: str) -> dict:
    """Whitelisted metric aggregates. Info labels are explicitly bounded."""
    info = {}
    samples = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = NUMERIC.match(line)
        if not m:
            continue
        name, rawlabels, rawvalue = m.groups()
        if name == "qumulo_info":
            for match in LABEL.finditer(rawlabels or ""):
                k = match.group(1)
                if k in {"name", "uuid", "version", "platform", "cluster_type"}:
                    info[k] = match.group(2)[:160]
            continue
        base = next((b for b in METRIC_BASES if name == b or name in (b + "_count", b + "_sum")), None)
        if base is None:
            continue
        value = float(rawvalue)
        if abs(value) == float("inf") or value != value:
            continue
        labels = parse_labels(rawlabels)
        # Preserve protocol and io_type but avoid sample explosion: only ~10 metric families.
        # Node-scoped families keep node_id (and interface/role for NICs) so that load
        # distribution across nodes and the node count stay visible.
        dimension = "|".join(f"{k}={labels[k]}" for k in dimension_labels(base) if k in labels)
        key = f"{name}|{dimension}" if dimension else name
        samples[key] = samples.get(key, 0.0) + value
    return {"cluster_info": info, "metrics": samples}


NODE_FAMILIES = ("qumulo_disk_is_unhealthy", "qumulo_quorum_node_is_offline", "qumulo_time_is_not_synchronizing")


def dimension_labels(base: str) -> tuple[str, ...]:
    if base.startswith("qumulo_network_interface_"):
        return ("node_id", "interface", "role")
    if base in NODE_FAMILIES:
        return ("node_id",)
    return ("protocol", "data_type", "io_type")


def split_key(key: str) -> tuple[str, dict]:
    name, *parts = key.split("|")
    return name, dict(p.split("=", 1) for p in parts if "=" in p)


def node_ids(snapshot: dict) -> list[str]:
    """Distinct nodes seen in the quorum gauge (one series per node)."""
    ids = set()
    for key in snapshot.get("metrics", {}):
        name, labels = split_key(key)
        if name == "qumulo_quorum_node_is_offline" and "node_id" in labels:
            ids.add(labels["node_id"])
    return sorted(ids, key=lambda x: (len(x), x))


def node_link_speeds(snapshot: dict) -> dict:
    """Sum of link speeds per node and role (bits/s), from the documented gauge."""
    speeds: dict = {}
    for key, value in snapshot.get("metrics", {}).items():
        name, labels = split_key(key)
        if name == "qumulo_network_interface_link_speed_bits_per_second" and "node_id" in labels:
            role = labels.get("role", "unknown")
            speeds.setdefault(labels["node_id"], {}).setdefault(role, 0.0)
            speeds[labels["node_id"]][role] += value
    return speeds


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TelemetryError("Qumulo-API-Redirect verweigert, um Bearer-Token zu schützen.")


class QumuloSampler:
    def __init__(self, config: dict):
        self.conf = config
        cluster = config["cluster"]
        self.url = cluster["api_url"].rstrip("/") + "/v2/metrics/endpoints/default/data"
        self.token_env = cluster["api_token_env"]
        self.ca = cluster.get("ca_certificate")
        self.interval = config["telemetry"]["sample_interval_seconds"]
        self.samples: list[dict] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def collect_once(self) -> dict:
        token = os.getenv(self.token_env)
        if not token:
            raise TelemetryError(f"Token-Umgebungsvariable fehlt: {self.token_env}")
        try:
            sslcontext = ssl.create_default_context(cafile=self.ca) if self.ca else ssl.create_default_context()
            req = Request(self.url, headers={"Authorization": "Bearer " + token,
                                             "Accept": "application/openmetrics-text, text/plain"})
            # Ignore HTTP(S)_PROXY environment: tokens must never be sent to proxies.
            # No automatic 3xx redirect to a different host.
            opener = build_opener(ProxyHandler({}), HTTPSHandler(context=sslcontext), _NoRedirect())
            with opener.open(req, timeout=6) as response:
                # Bound the snapshot size against pathological/misconfigured targets.
                payload = response.read(8 * 1024 * 1024 + 1)
            if len(payload) > 8 * 1024 * 1024:
                raise TelemetryError("OpenMetrics-Antwort größer als 8 MiB.")
            result = parse_openmetrics(payload.decode("utf-8", "replace"))
        except TelemetryError:
            raise
        except Exception as exc:
            raise TelemetryError(f"OpenMetrics-Abfrage fehlgeschlagen: {type(exc).__name__}: {exc}") from exc
        if not result["metrics"]:
            raise TelemetryError("Keine erwarteten Qumulo-OpenMetrics-Metriken gefunden.")
        return {"timestamp": _timestamp(), "monotonic": time.monotonic(), **result}

    @staticmethod
    def health(snapshot: dict) -> list[str]:
        issues = []
        for metric, text in (("qumulo_disk_is_unhealthy", "unhealthy disks"),
                             ("qumulo_quorum_node_is_offline", "offline nodes")):
            vals = [v for k, v in snapshot["metrics"].items() if k.split("|")[0] == metric]
            if vals and any(v > 0 for v in vals):
                issues.append(f"Qumulo meldet {text}.")
        return issues

    def start(self):
        if self._thread is not None:
            raise TelemetryError("Sampler bereits gestartet.")
        # Take a before snapshot BEFORE fio starts; fail explicitly if it fails.
        self.samples = [self.collect_once()]
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.samples.append(self.collect_once())
            except Exception as exc:
                self.errors.append(str(exc))

    def stop(self) -> dict:
        if self._thread is None:
            return {"samples": [], "errors": ["Sampler wurde nicht gestartet"]}
        self._stop.set()
        self._thread.join(timeout=12)
        if self._thread.is_alive():
            self.errors.append("Sampler konnte nicht rechtzeitig beendet werden.")
        # Capture a final point so a short test still has a usable counter delta.
        try:
            self.samples.append(self.collect_once())
        except Exception as exc:
            self.errors.append(str(exc))
        return {"samples": self.samples, "errors": self.errors}


def summarize_telemetry(data: dict) -> dict:
    samples = data.get("samples", [])
    summary = {"sample_count": len(samples), "errors": list(data.get("errors", [])),
               "counter_rates_per_second": {}}
    if len(samples) < 2:
        return summary
    first, last = samples[0], samples[-1]
    duration = last["monotonic"] - first["monotonic"]
    if duration <= 0:
        return summary
    nodes: dict = {}
    for key, start in first["metrics"].items():
        name, labels = split_key(key)
        if name.endswith("_count") and key in last["metrics"]:
            delta = last["metrics"][key] - start
            if delta >= 0:
                rate = round(delta / duration, 2)
                summary["counter_rates_per_second"][key] = rate
                direction = {"qumulo_network_interface_received_bytes_count": "rx_bytes_per_second",
                             "qumulo_network_interface_transmitted_bytes_count": "tx_bytes_per_second"}.get(name)
                if direction and "node_id" in labels and labels.get("role") != "backend":
                    entry = nodes.setdefault(labels["node_id"], {"rx_bytes_per_second": 0.0, "tx_bytes_per_second": 0.0})
                    entry[direction] = round(entry[direction] + rate, 2)
            else:
                summary["errors"].append(f"Counter-Reset erkannt: {key}")
    if nodes:
        summary["node_network"] = nodes
        totals = [v["rx_bytes_per_second"] + v["tx_bytes_per_second"] for v in nodes.values()]
        busiest = max(totals)
        summary["node_balance"] = {"nodes": len(nodes),
                                   "min_to_max_ratio": round(min(totals) / busiest, 3) if busiest > 0 else None}
    return summary


def sample_client_network() -> dict:
    from pathlib import Path
    d = {}
    root = Path("/sys/class/net")
    if root.exists():
        for nic in root.iterdir():
            if nic.name == "lo":
                continue
            counters = {}
            for key in ("rx_bytes", "tx_bytes", "rx_errors", "tx_errors"):
                try:
                    counters[key] = int((nic / "statistics" / key).read_text())
                except (OSError, ValueError):
                    pass
            d[nic.name] = counters
    return d
