"""Config loading and strict defaults, without secret serialization."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlparse

import yaml


class ConfigError(ValueError):
    pass


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
    def only_keys(mapping: dict, allowed: set[str], where: str):
        extra = set(mapping) - allowed
        if extra:
            raise ConfigError(f"Unbekannte Konfigurationsschlüssel in {where}: {sorted(extra)}")

    only_keys(data, {"schema_version", "profile", "cluster", "targets",
                     "workload", "telemetry", "report", "safety"}, "root")
    if str(data.get("schema_version")) != "0.1":
        raise ConfigError("schema_version muss '0.1' sein.")
    if data.get("profile") != "installation-v0.1":
        raise ConfigError("Unbekanntes Testprofil; erwartet: installation-v0.1")
    targets = data.get("targets", {})
    if not isinstance(targets, dict) or set(targets) != {"nfs", "smb"}:
        raise ConfigError("targets muss genau nfs und smb enthalten.")
    for protocol in ("nfs", "smb"):
        tgt = targets.get(protocol)
        if not isinstance(tgt, dict) or not isinstance(tgt.get("mountpoint"), str) or not tgt["mountpoint"].startswith("/"):
            raise ConfigError(f"targets.{protocol}.mountpoint muss ein absoluter Pfad sein.")
        only_keys(tgt, {"mountpoint", "expected_source", "expected_version" if protocol == "nfs" else "expected_dialect"},
                  f"targets.{protocol}")
        if tgt.get("expected_source") is not None and not isinstance(tgt["expected_source"], str):
            raise ConfigError(f"targets.{protocol}.expected_source muss ein String oder null sein.")
    if targets["nfs"]["mountpoint"].rstrip("/") == targets["smb"]["mountpoint"].rstrip("/"):
        raise ConfigError("NFS- und SMB-Mountpoint dürfen nicht identisch sein.")
    nfs_ver = str(targets["nfs"].get("expected_version", "4.1"))
    if nfs_ver not in {"3", "4.1"}:
        raise ConfigError("NFS: nur Version 3 oder 4.1 im MVP.")
    smb_ver = str(targets["smb"].get("expected_dialect", "3.1.1"))
    if smb_ver not in {"3.0", "3.02", "3.0.2", "3.1.1", "3", "3.11"}:
        raise ConfigError("SMB: nur SMB3 im MVP.")
    work = data.get("workload", {})
    if not isinstance(work, dict):
        raise ConfigError("workload muss ein Mapping sein.")
    defaults = dict(warmup_seconds=10, measure_seconds=30, worker_count=16,
                    dataset_mib_per_worker=1024, sequential_block_size="1m",
                    random_block_size="4k", io_mode="direct", metadata_file_count=1000)
    only_keys(work, set(defaults), "workload")
    work = {**defaults, **work}
    # Capped inputs prevent an accidental uncontrolled write storm.
    for key, minimum, maximum in (("warmup_seconds", 0, 120), ("measure_seconds", 5, 300),
                                   ("worker_count", 2, 64), ("dataset_mib_per_worker", 64, 8192),
                                   ("metadata_file_count", 10, 100000)):
        val = work[key]
        if type(val) is not int or not minimum <= val <= maximum:
            raise ConfigError(f"workload.{key} muss zwischen {minimum} und {maximum} liegen.")
    if work["io_mode"] != "direct":
        raise ConfigError("Version 0.1 unterstützt nur direct I/O; kein stiller Fallback.")
    if work["sequential_block_size"] != "1m" or work["random_block_size"] != "4k":
        raise ConfigError("Blockgrößen für dieses versionierte Profil sind 1m und 4k.")
    data["workload"] = work
    if not isinstance(data.get("telemetry", {}), dict):
        raise ConfigError("telemetry muss ein Mapping sein.")
    only_keys(data.get("telemetry", {}), {"enabled", "required", "sample_interval_seconds"}, "telemetry")
    telemetry = {"enabled": True, "required": True, "sample_interval_seconds": 5, **data.get("telemetry", {})}
    if type(telemetry["enabled"]) is not bool or type(telemetry["required"]) is not bool:
        raise ConfigError("telemetry.enabled/required müssen Boolean sein.")
    if telemetry["required"] and not telemetry["enabled"]:
        raise ConfigError("telemetry.required=true erfordert telemetry.enabled=true.")
    if type(telemetry["sample_interval_seconds"]) is not int or not 1 <= telemetry["sample_interval_seconds"] <= 60:
        raise ConfigError("telemetry.sample_interval_seconds außerhalb 1–60.")
    data["telemetry"] = telemetry
    cluster = data.get("cluster", {})
    if not isinstance(cluster, dict):
        raise ConfigError("cluster muss ein Mapping sein.")
    only_keys(cluster, {"api_url", "api_token_env", "ca_certificate"}, "cluster")
    if telemetry["enabled"]:
        url = cluster.get("api_url", "")
        parsed = urlparse(str(url))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ConfigError("cluster.api_url muss eine HTTPS-Basisadresse ohne Credentials sein.")
        env = cluster.get("api_token_env")
        if not isinstance(env, str) or not env.isidentifier():
            raise ConfigError("cluster.api_token_env muss ein Umgebungsvariablenname sein.")
    data["cluster"] = cluster
    if not isinstance(data.get("safety", {}), dict):
        raise ConfigError("safety muss ein Mapping sein.")
    only_keys(data.get("safety", {}), {"cleanup", "free_space_multiplier"}, "safety")
    safety = {"cleanup": "after_report", "free_space_multiplier": 1.25, **data.get("safety", {})}
    if safety["cleanup"] not in {"after_report", "never"}:
        raise ConfigError("safety.cleanup muss after_report oder never sein.")
    if not isinstance(safety["free_space_multiplier"], (float, int)) or not 1.1 <= float(safety["free_space_multiplier"]) <= 3:
        raise ConfigError("safety.free_space_multiplier muss zwischen 1.1 und 3 liegen.")
    data["safety"] = safety
    if not isinstance(data.get("report", {}), dict):
        raise ConfigError("report muss ein Mapping sein.")
    only_keys(data.get("report", {}), {"output_directory", "formats"}, "report")
    report = {"output_directory": "./reports", "formats": ["json", "html", "pdf"], **data.get("report", {})}
    if not isinstance(report["output_directory"], str) or not report["output_directory"].strip():
        raise ConfigError("report.output_directory fehlt.")
    if not isinstance(report["formats"], list) or set(report["formats"]) - {"json", "html", "pdf"}:
        raise ConfigError("Unbekanntes report.formats.")
    data["report"] = report
    return data


def public_config(data: dict) -> dict:
    """Only documented nonsecret fields; no arbitrary nested credentials."""
    d = copy.deepcopy(data)
    d["cluster"] = {key: d.get("cluster", {}).get(key) for key in ("api_url", "api_token_env", "ca_certificate")}
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
               "fio_engine": "psync", "fio_io_mode": "direct",
               "protocols": ["nfs", "smb"]}
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
