"""Config loading and strict defaults, without secret serialization."""
from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml


class ConfigError(ValueError):
    pass


SCHEMA_VERSION = "0.2"
PROFILE = "installation-v0.2"
MIB = 1024 ** 2
GIB = 1024 ** 3
# Fixed sizing keeps the historic per-worker limit; auto sizing may need more.
MAX_FIXED_MIB_PER_WORKER = 8192
MAX_AUTO_MIB_PER_WORKER = 131072
CLIENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
# Host names / IPs. Never starts with '-', so it cannot become a CLI option.
HOSTLIKE = re.compile(r"^[A-Za-z0-9_.:\[\]%-]{1,253}$")
SSH_TARGET = re.compile(r"^(?:[A-Za-z0-9._-]{1,64}@)?[A-Za-z0-9_.:\[\]%-]{1,253}$")


def _only_keys(mapping: dict, allowed: set[str], where: str):
    extra = set(mapping) - allowed
    if extra:
        raise ConfigError(f"Unbekannte Konfigurationsschlüssel in {where}: {sorted(extra)}")


def _mapping(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} muss ein Mapping sein.")
    return value


def _hostlike(value, where: str) -> str:
    if not isinstance(value, str) or not HOSTLIKE.fullmatch(value) or value.startswith("-"):
        raise ConfigError(f"{where} muss eine IP-Adresse oder ein Hostname sein.")
    return value


def _number(value, where: str, minimum: float, maximum: float, integer: bool = False):
    ok_type = (type(value) is int) if integer else (isinstance(value, (int, float)) and not isinstance(value, bool))
    if not ok_type or not minimum <= value <= maximum:
        raise ConfigError(f"{where} muss zwischen {minimum} und {maximum} liegen.")
    return value


def _validate_target(tgt, protocol: str, where: str, version_key: bool) -> dict:
    if not isinstance(tgt, dict) or not isinstance(tgt.get("mountpoint"), str) or not tgt["mountpoint"].startswith("/"):
        raise ConfigError(f"{where}.mountpoint muss ein absoluter Pfad sein.")
    allowed = {"mountpoint", "expected_source"}
    if version_key:
        allowed.add("expected_version" if protocol == "nfs" else "expected_dialect")
    _only_keys(tgt, allowed, where)
    if tgt.get("expected_source") is not None and not isinstance(tgt["expected_source"], str):
        raise ConfigError(f"{where}.expected_source muss ein String oder null sein.")
    return tgt


def _validate_cluster(data: dict, telemetry: dict) -> dict:
    cluster = _mapping(data, "cluster")
    _only_keys(cluster, {"api_url", "api_token_env", "ca_certificate", "node_count",
                         "cache_gib_per_node", "node_addresses"}, "cluster")
    if telemetry["enabled"]:
        url = cluster.get("api_url", "")
        parsed = urlparse(str(url))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ConfigError("cluster.api_url muss eine HTTPS-Basisadresse ohne Credentials sein.")
        env = cluster.get("api_token_env")
        if not isinstance(env, str) or not env.isidentifier():
            raise ConfigError("cluster.api_token_env muss ein Umgebungsvariablenname sein.")
    if cluster.get("node_count") is not None:
        _number(cluster["node_count"], "cluster.node_count", 1, 1000, integer=True)
    if cluster.get("cache_gib_per_node") is not None:
        _number(cluster["cache_gib_per_node"], "cluster.cache_gib_per_node", 1, 1024 * 1024)
    addresses = cluster.get("node_addresses") or []
    if not isinstance(addresses, list) or len(addresses) > 1000:
        raise ConfigError("cluster.node_addresses muss eine Liste sein.")
    cluster["node_addresses"] = [_hostlike(a, "cluster.node_addresses[]") for a in addresses]
    if len(set(cluster["node_addresses"])) != len(cluster["node_addresses"]):
        raise ConfigError("cluster.node_addresses enthält Duplikate.")
    return cluster


def _validate_network(data: dict) -> dict:
    net = _mapping(data, "network")
    defaults = {"enabled": True, "required": False, "expected_mtu": None, "ping_count": 10,
                "max_rtt_ms": 1.0, "min_client_link_gbps": None, "iperf3_server": None,
                "iperf3_seconds": 10, "iperf3_streams": 4, "iperf3_min_link_fraction": 0.7}
    _only_keys(net, set(defaults), "network")
    net = {**defaults, **net}
    for key in ("enabled", "required"):
        if type(net[key]) is not bool:
            raise ConfigError(f"network.{key} muss Boolean sein.")
    if net["required"] and not net["enabled"]:
        raise ConfigError("network.required=true erfordert network.enabled=true.")
    if net["expected_mtu"] is not None:
        _number(net["expected_mtu"], "network.expected_mtu", 576, 9216, integer=True)
    _number(net["ping_count"], "network.ping_count", 3, 100, integer=True)
    _number(net["max_rtt_ms"], "network.max_rtt_ms", 0.05, 100)
    if net["min_client_link_gbps"] is not None:
        _number(net["min_client_link_gbps"], "network.min_client_link_gbps", 1, 800)
    if net["iperf3_server"] is not None:
        _hostlike(net["iperf3_server"], "network.iperf3_server")
    _number(net["iperf3_seconds"], "network.iperf3_seconds", 3, 120, integer=True)
    _number(net["iperf3_streams"], "network.iperf3_streams", 1, 32, integer=True)
    _number(net["iperf3_min_link_fraction"], "network.iperf3_min_link_fraction", 0.1, 1.0)
    return net


def _validate_multi_client(data: dict) -> dict | None:
    if data.get("multi_client") is None:
        return None
    mc = _mapping(data, "multi_client")
    _only_keys(mc, {"fio_port", "manage_fio_servers", "ssh_connect_timeout", "clients"}, "multi_client")
    mc = {"fio_port": 8765, "manage_fio_servers": True, "ssh_connect_timeout": 10, **mc}
    _number(mc["fio_port"], "multi_client.fio_port", 1024, 65535, integer=True)
    _number(mc["ssh_connect_timeout"], "multi_client.ssh_connect_timeout", 2, 120, integer=True)
    if type(mc["manage_fio_servers"]) is not bool:
        raise ConfigError("multi_client.manage_fio_servers muss Boolean sein.")
    clients = mc.get("clients")
    if not isinstance(clients, list) or not 1 <= len(clients) <= 64:
        raise ConfigError("multi_client.clients muss 1 bis 64 Clients enthalten.")
    names, endpoints = set(), set()
    out = []
    for i, client in enumerate(clients):
        where = f"multi_client.clients[{i}]"
        if not isinstance(client, dict):
            raise ConfigError(f"{where} muss ein Mapping sein.")
        _only_keys(client, {"name", "address", "fio_port", "ssh", "targets"}, where)
        name = client.get("name")
        if not isinstance(name, str) or not CLIENT_NAME.fullmatch(name):
            raise ConfigError(f"{where}.name: nur A-Z, a-z, 0-9, '-' und '_' (max. 32 Zeichen).")
        if name in names:
            raise ConfigError(f"Doppelter Clientname: {name}")
        names.add(name)
        address = _hostlike(client.get("address"), f"{where}.address")
        port = client.get("fio_port", mc["fio_port"])
        _number(port, f"{where}.fio_port", 1024, 65535, integer=True)
        if (address, port) in endpoints:
            raise ConfigError(f"{where}: fio-Server {address}:{port} ist doppelt konfiguriert.")
        endpoints.add((address, port))
        ssh = client.get("ssh")
        if ssh != "local" and (not isinstance(ssh, str) or not SSH_TARGET.fullmatch(ssh) or ssh.startswith("-")):
            raise ConfigError(f"{where}.ssh muss 'local' oder ein SSH-Ziel (user@host) sein.")
        targets = client.get("targets")
        if not isinstance(targets, dict) or set(targets) != {"nfs", "smb"}:
            raise ConfigError(f"{where}.targets muss genau nfs und smb enthalten.")
        for protocol in ("nfs", "smb"):
            _validate_target(targets[protocol], protocol, f"{where}.targets.{protocol}", version_key=False)
        if targets["nfs"]["mountpoint"].rstrip("/") == targets["smb"]["mountpoint"].rstrip("/"):
            raise ConfigError(f"{where}: NFS- und SMB-Mountpoint dürfen nicht identisch sein.")
        out.append({"name": name, "address": address, "fio_port": port, "ssh": ssh,
                    "targets": {p: dict(targets[p]) for p in ("nfs", "smb")}})
    mc["clients"] = out
    return mc


def resolve_sizing(data: dict) -> dict:
    """Determine dataset size per worker; classify against the cluster cache.

    auto: total dataset per protocol >= cache_multiplier x node_count x cache_gib_per_node,
    so that read tests cannot be served entirely from the cluster cache.
    """
    work, cluster = data["workload"], data["cluster"]
    clients = len(data["multi_client"]["clients"]) if data.get("multi_client") else 1
    workers = work["worker_count"]
    nodes, cache = cluster.get("node_count"), cluster.get("cache_gib_per_node")
    cache_bytes = nodes * cache * GIB if nodes and cache else None
    if work["dataset_sizing"] == "auto":
        if not nodes or not cache:
            raise ConfigError("dataset_sizing=auto erfordert cluster.node_count und cluster.cache_gib_per_node.")
        target = work["cache_multiplier"] * cache_bytes
        per_worker = max(64, math.ceil(target / (clients * workers) / MIB))
        if per_worker > MAX_AUTO_MIB_PER_WORKER:
            raise ConfigError(f"Auto-Sizing ergibt {per_worker} MiB pro Worker (max. {MAX_AUTO_MIB_PER_WORKER}). "
                              "Mehr Clients/Worker verwenden oder dataset_sizing=fixed.")
        work["dataset_mib_per_worker"] = per_worker
    total = clients * workers * work["dataset_mib_per_worker"] * MIB
    if cache_bytes is None:
        classification = "UNKNOWN"
    elif total >= 2 * cache_bytes:
        classification = "OVER_CACHE"
    elif total > cache_bytes:
        classification = "PARTIALLY_CACHEABLE"
    else:
        classification = "FITS_IN_CACHE"
    return {"mode": work["dataset_sizing"], "clients": clients, "workers_per_client": workers,
            "dataset_mib_per_worker": work["dataset_mib_per_worker"],
            "dataset_gib_per_protocol": round(total / GIB, 2),
            "node_count": nodes, "cache_gib_per_node": cache,
            "cluster_cache_gib": round(cache_bytes / GIB, 2) if cache_bytes else None,
            "cache_multiplier": work["cache_multiplier"] if work["dataset_sizing"] == "auto" else None,
            "dataset_to_cache_ratio": round(total / cache_bytes, 2) if cache_bytes else None,
            "classification": classification}


def load_config(path: str | Path) -> dict:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ConfigError(f"Konfiguration nicht gefunden: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Ungültige YAML-Konfiguration: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Die YAML-Wurzel muss ein Mapping sein.")
    # Reject unsupported keys instead of copying arbitrary text/credentials into reports.
    _only_keys(data, {"schema_version", "profile", "cluster", "targets", "network", "multi_client",
                      "workload", "telemetry", "report", "safety"}, "root")
    if str(data.get("schema_version")) != SCHEMA_VERSION:
        raise ConfigError(f"schema_version muss '{SCHEMA_VERSION}' sein (siehe CHANGELOG für die Migration von 0.1).")
    if data.get("profile") != PROFILE:
        raise ConfigError(f"Unbekanntes Testprofil; erwartet: {PROFILE}")
    data["schema_version"] = SCHEMA_VERSION
    targets = data.get("targets", {})
    if not isinstance(targets, dict) or set(targets) != {"nfs", "smb"}:
        raise ConfigError("targets muss genau nfs und smb enthalten.")
    for protocol in ("nfs", "smb"):
        _validate_target(targets.get(protocol), protocol, f"targets.{protocol}", version_key=True)
    if targets["nfs"]["mountpoint"].rstrip("/") == targets["smb"]["mountpoint"].rstrip("/"):
        raise ConfigError("NFS- und SMB-Mountpoint dürfen nicht identisch sein.")
    nfs_ver = str(targets["nfs"].get("expected_version", "4.1"))
    if nfs_ver not in {"3", "4.1"}:
        raise ConfigError("NFS: nur Version 3 oder 4.1 im MVP.")
    smb_ver = str(targets["smb"].get("expected_dialect", "3.1.1"))
    if smb_ver not in {"3.0", "3.02", "3.0.2", "3.1.1", "3", "3.11"}:
        raise ConfigError("SMB: nur SMB3 im MVP.")

    work = _mapping(data, "workload")
    defaults = dict(warmup_seconds=10, measure_seconds=30, worker_count=16,
                    dataset_sizing="fixed", dataset_mib_per_worker=1024, cache_multiplier=2.0,
                    sequential_block_size="1m", random_block_size="4k", io_mode="direct",
                    metadata_file_count=1000)
    _only_keys(work, set(defaults), "workload")
    sizing = work.get("dataset_sizing", "fixed")
    if sizing not in {"fixed", "auto"}:
        raise ConfigError("workload.dataset_sizing muss fixed oder auto sein.")
    if sizing == "auto" and "dataset_mib_per_worker" in work:
        raise ConfigError("workload.dataset_mib_per_worker nur bei dataset_sizing=fixed angeben.")
    if sizing == "fixed" and "cache_multiplier" in work:
        raise ConfigError("workload.cache_multiplier nur bei dataset_sizing=auto angeben.")
    work = {**defaults, **work}
    # Capped inputs prevent an accidental uncontrolled write storm.
    for key, minimum, maximum in (("warmup_seconds", 0, 120), ("measure_seconds", 5, 300),
                                   ("worker_count", 2, 64), ("dataset_mib_per_worker", 64, MAX_FIXED_MIB_PER_WORKER),
                                   ("metadata_file_count", 10, 100000)):
        _number(work[key], f"workload.{key}", minimum, maximum, integer=True)
    _number(work["cache_multiplier"], "workload.cache_multiplier", 1.0, 10.0)
    if work["io_mode"] != "direct":
        raise ConfigError("Dieses Profil unterstützt nur direct I/O; kein stiller Fallback.")
    if work["sequential_block_size"] != "1m" or work["random_block_size"] != "4k":
        raise ConfigError("Blockgrößen für dieses versionierte Profil sind 1m und 4k.")
    data["workload"] = work

    telemetry = _mapping(data, "telemetry")
    _only_keys(telemetry, {"enabled", "required", "sample_interval_seconds"}, "telemetry")
    telemetry = {"enabled": True, "required": True, "sample_interval_seconds": 5, **telemetry}
    if type(telemetry["enabled"]) is not bool or type(telemetry["required"]) is not bool:
        raise ConfigError("telemetry.enabled/required müssen Boolean sein.")
    if telemetry["required"] and not telemetry["enabled"]:
        raise ConfigError("telemetry.required=true erfordert telemetry.enabled=true.")
    _number(telemetry["sample_interval_seconds"], "telemetry.sample_interval_seconds", 1, 60, integer=True)
    data["telemetry"] = telemetry
    data["cluster"] = _validate_cluster(data, telemetry)
    data["network"] = _validate_network(data)
    data["multi_client"] = _validate_multi_client(data)

    safety = _mapping(data, "safety")
    _only_keys(safety, {"cleanup", "free_space_multiplier"}, "safety")
    safety = {"cleanup": "after_report", "free_space_multiplier": 1.25, **safety}
    if safety["cleanup"] not in {"after_report", "never"}:
        raise ConfigError("safety.cleanup muss after_report oder never sein.")
    _number(safety["free_space_multiplier"], "safety.free_space_multiplier", 1.1, 3)
    data["safety"] = safety
    report = _mapping(data, "report")
    _only_keys(report, {"output_directory", "formats"}, "report")
    report = {"output_directory": "./reports", "formats": ["json", "html", "pdf"], **report}
    if not isinstance(report["output_directory"], str) or not report["output_directory"].strip():
        raise ConfigError("report.output_directory fehlt.")
    if not isinstance(report["formats"], list) or set(report["formats"]) - {"json", "html", "pdf"}:
        raise ConfigError("Unbekanntes report.formats.")
    data["report"] = report
    data["sizing"] = resolve_sizing(data)
    return data


def client_count(data: dict) -> int:
    return len(data["multi_client"]["clients"]) if data.get("multi_client") else 1


def public_config(data: dict) -> dict:
    """Only documented nonsecret fields; no arbitrary nested credentials."""
    d = copy.deepcopy(data)
    d["cluster"] = {key: d.get("cluster", {}).get(key) for key in
                    ("api_url", "api_token_env", "ca_certificate", "node_count",
                     "cache_gib_per_node", "node_addresses")}
    for protocol in ("nfs", "smb"):
        d["targets"][protocol] = {k: v for k, v in d["targets"][protocol].items()
                                   if k in {"mountpoint", "expected_version", "expected_dialect", "expected_source"}}
    return d


def fingerprint(data: dict) -> str:
    import hashlib
    # Fingerprint the workload semantics, not local report paths, CA locations,
    # secrets or endpoints. Mount/cluster/client comparison happens separately.
    profile = {"schema_version": data["schema_version"],
               "profile": data["profile"],
               "workload": data["workload"],
               "client_count": client_count(data),
               "fio_engine": "psync", "fio_io_mode": "direct",
               "single_stream_spans_dataset": True,
               "protocols": ["nfs", "smb"]}
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
