"""QPBT sequential orchestration, crash-safe partial reports, simulated path."""
from __future__ import annotations
import fcntl
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import __version__
from .config import fingerprint, public_config
from .fio_engine import FioError, prepare_data, execute_workload, normalize_fio
from .direct_io import check_direct_io, DirectIOError
from .metadata_engine import run_metadata
from .mounts import official_fio_version, client_info, preflight_mounts, mounted_target, PreflightError
from .profiles import workloads, total_dataset_bytes, estimated_seconds
from .reporting import create_reports, save_json
from .safety import make_workspace, cleanup_workspace, SafetyError
from .telemetry import QumuloSampler, sample_client_network, summarize_telemetry, TelemetryError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:10]


def output_base(config: dict) -> Path:
    return Path(config["report"]["output_directory"]).expanduser().resolve()


def inspect(config: dict) -> dict:
    if os.name != "posix" or not Path("/proc/self/mountinfo").is_file():
        raise PreflightError("Realtests benötigen einen Linux-Testclient.")
    version = official_fio_version()
    mounts = preflight_mounts(config)
    out = output_base(config)
    for value in mounts.values():
        mp = Path(value["mountpoint"])
        if out == mp or mp in out.parents:
            raise PreflightError("Report-Verzeichnis darf nicht auf dem Benchmark-Mount liegen.")
    info = {"fio_version": version, "client": client_info(), "mounts": mounts,
            "cluster_info": {}, "warnings": [], "telemetry": "disabled"}
    if config["telemetry"]["enabled"]:
        try:
            snap = QumuloSampler(config).collect_once()
            problems = QumuloSampler.health(snap)
            if problems:
                raise PreflightError("Qumulo Health Check fehlgeschlagen: " + "; ".join(problems))
            info["cluster_info"] = snap["cluster_info"]
            info["telemetry"] = "available"
            present = set(snap["metrics"])
            for important in ("qumulo_disk_is_unhealthy", "qumulo_quorum_node_is_offline"):
                if not any(k.split("|")[0] == important for k in present):
                    info["warnings"].append(f"Health-Gauge nicht vorhanden: {important}")
        except TelemetryError as exc:
            if config["telemetry"]["required"]:
                raise PreflightError(str(exc)) from exc
            info["warnings"].append(f"Qumulo-Telemetrie nicht verfügbar: {exc}")
            info["telemetry"] = "unavailable"
    else:
        info["warnings"].append("Cluster-Telemetrie deaktiviert: nur Client-Baseline.")
    # Every real run must identify the actual Qumulo cluster, not merely reach a metrics URL.
    if info["telemetry"] == "available":
        missing = [k for k in ("name", "uuid", "version") if not info["cluster_info"].get(k)]
        if missing:
            raise PreflightError("OpenMetrics ohne vollständige Clusteridentität: " + ", ".join(missing))
    return info


def plan(config: dict) -> dict:
    size = total_dataset_bytes(config)
    return {"profile": config["profile"], "protocols": ["nfs", "smb"],
            "fio_tests_per_protocol": len(workloads(config)), "metadata_tests_per_protocol": 1,
            "warmup_seconds": config["workload"]["warmup_seconds"],
            "measure_seconds": config["workload"]["measure_seconds"],
            "fio_measurement_plus_warmup_seconds": estimated_seconds(config),
            "dataset_gib_per_protocol": round(size/2**30,2),
            "estimated_total_dataset_gib": round(2*size/2**30,2),
            "note": "Zeit ohne Datensatzvorbereitung, Metadaten, Report und Cleanup; nicht garantiert."}


@contextmanager
def local_lock(base: Path):
    base.mkdir(parents=True, exist_ok=True)
    lockfile = base / ".qpbt.lock"
    fd = os.open(lockfile, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PreflightError("Ein anderer QPBT-Lauf ist auf diesem Client aktiv.") from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _initial_run(config: dict, preflight: dict, run_id: str, simulated: bool) -> dict:
    return {"run_id": run_id, "created_at": now(), "finished_at": None,
            "tool_version": __version__, "profile": config["profile"],
            "profile_hash": fingerprint(config), "simulated": simulated,
            "status": "RUNNING", "fio_version": preflight.get("fio_version"),
            "client": preflight.get("client", {}), "mounts": preflight.get("mounts", {}),
            "cluster_info": preflight.get("cluster_info", {}),
            "config": public_config(config), "tests": [],
            "warnings": list(preflight.get("warnings", [])), "errors": [],
            "cleanup": "not_attempted", "telemetry_availability": preflight.get("telemetry", "disabled")}


def _check_mount_unchanged(config: dict, protocol: str, original: dict):
    t = config["targets"][protocol]
    new = mounted_target(t["mountpoint"], protocol,
                         str(t.get("expected_version" if protocol == "nfs" else "expected_dialect")),
                         t.get("expected_source"))
    keys = ("mountpoint", "fstype", "source", "negotiated_version", "mount_id", "mount_dev", "st_dev")
    if any(new.get(k) != original.get(k) for k in keys):
        raise SafetyError(f"{protocol}: Mountverbindung hat sich während des Laufs geändert!")


def run_real(config: dict) -> tuple[Path, dict]:
    run_id = make_id()
    base = output_base(config)
    with local_lock(base):
        # No customer data is written before the entire preflight succeeds.
        preflight = inspect(config)
        output = base / run_id
        output.mkdir(parents=True, exist_ok=False)
        report = _initial_run(config, preflight, run_id, simulated=False)
        save_json(output / "preflight.json", preflight)
        save_json(output / "result.json", report)
        workspaces = {}
        try:
            for protocol in ("nfs", "smb"):
                _check_mount_unchanged(config, protocol, preflight["mounts"][protocol])
                # Namespace by protocol even if NFS + SMB expose the identical directory.
                workspace_id = f"{run_id}-{protocol}"
                path = make_workspace(config["targets"][protocol]["mountpoint"], workspace_id)
                workspaces[protocol] = (path, workspace_id)
                _check_mount_unchanged(config, protocol, preflight["mounts"][protocol])
                report.setdefault("direct_io_probes", {})[protocol] = check_direct_io(path / "probe")
                save_json(output / "result.json", report)
                prepare_data(path / "dataset", config, output / "fio" / protocol / "PREPARE.json")
                for work in workloads(config):
                    _check_mount_unchanged(config, protocol, preflight["mounts"][protocol])
                    sampler = QumuloSampler(config) if config["telemetry"]["enabled"] and preflight["telemetry"] == "available" else None
                    before = sample_client_network()
                    snapshot = None
                    try:
                        if sampler:
                            sampler.start()
                        raw = execute_workload(path / "dataset", config, work,
                                               output / "fio" / protocol / f"{work.id}.json")
                        result = normalize_fio(raw, work)
                    finally:
                        if sampler:
                            snapshot = sampler.stop()
                    after = sample_client_network()
                    result["protocol"] = protocol
                    result["client_network_before"] = before
                    result["client_network_after"] = after
                    result["cache_classification"] = "client_direct_io_server_cache_uncontrolled"
                    if snapshot is not None:
                        save_json(output / "telemetry" / protocol / f"{work.id}.json", snapshot)
                        result["cluster_telemetry"] = summarize_telemetry(snapshot)
                        if result["cluster_telemetry"]["errors"] or result["cluster_telemetry"]["sample_count"] < 2:
                            report["warnings"].append(f"{protocol}/{work.id}: Telemetrie unvollständig.")
                    report["tests"].append(result)
                    save_json(output / "result.json", report)
                _check_mount_unchanged(config, protocol, preflight["mounts"][protocol])
                result = run_metadata(path / "metadata", config["workload"]["metadata_file_count"])
                result["protocol"] = protocol
                report["tests"].append(result)
                save_json(output / "result.json", report)
            report["status"] = "WARNING" if report["warnings"] else "PASS"
        except (Exception, KeyboardInterrupt) as exc:
            report["status"] = "FAIL"
            report["errors"].append(f"{type(exc).__name__}: {str(exc)[:500]}")
            report["warnings"].append("Testdaten bleiben nach einem Fehlschlag zur Fehleranalyse erhalten.")
        finally:
            report["finished_at"] = now()
            save_json(output / "result.json", report)
            try:
                create_reports(report, output, config["report"]["formats"])
            except Exception as exc:
                report["errors"].append(f"Reporter: {type(exc).__name__}: {str(exc)[:250]}")
                report["status"] = "FAIL"
                save_json(output / "result.json", report)
            # Only clean finished, successful runs. Unknown/mutated targets remain untouched.
            if report["status"] in {"PASS", "WARNING"} and config["safety"]["cleanup"] == "after_report":
                try:
                    for protocol in ("nfs", "smb"):
                        if protocol in workspaces:
                            _check_mount_unchanged(config, protocol, preflight["mounts"][protocol])
                            cleanup_workspace(config["targets"][protocol]["mountpoint"], workspaces[protocol][1])
                    report["cleanup"] = "completed"
                except Exception as exc:
                    report["cleanup"] = "incomplete"
                    report["status"] = "WARNING"
                    report["warnings"].append(f"Cleanup manuell prüfen: {type(exc).__name__}: {exc}")
            elif report["status"] == "FAIL":
                report["cleanup"] = "skipped_after_failure"
            else:
                report["cleanup"] = "disabled"
            try:
                create_reports(report, output, config["report"]["formats"])
            except Exception as exc:
                report["status"] = "FAIL"
                report["errors"].append(f"Final Report-Fehler: {type(exc).__name__}: {str(exc)[:250]}")
                save_json(output / "result.json", report)
        return output, report


def run_simulated(config: dict) -> tuple[Path, dict]:
    run_id = "SIMULATION-" + make_id()
    out = output_base(config) / run_id
    out.mkdir(parents=True, exist_ok=False)
    preflight = {"fio_version": "SIMULATED", "client": {"hostname": "SIMULATED-CLIENT"},
                 "mounts": {}, "cluster_info": {"name": "SIMULATED-QUMULO", "version": "SIMULATED"},
                 "warnings": ["SIMULATION: Es wurde keine Verbindung zu einem Qumulo-Cluster hergestellt."],
                 "telemetry": "simulation"}
    report = _initial_run(config, preflight, run_id, simulated=True)
    for i, proto in enumerate(("nfs", "smb")):
        for j, w in enumerate(workloads(config)):
            baseline = 1040 * (0.93 if proto == "smb" else 1.0)
            if w.bs == "4k":
                iops = 3500 * (w.workers if w.workers > 1 else 1) * (0.9 if proto == "smb" else 1)
                mb = iops * 4096
            else:
                mb = baseline * 1e6 * (1.06 if w.workers > 1 else 1)
                iops = mb / (1024 * 1024)
            results = {"read": {"io_bytes": 0, "bytes_per_second": 0, "iops": 0,
                                "mean_latency_ms": None, "p95_latency_ms": None, "p99_latency_ms": None},
                       "write": {"io_bytes": 0, "bytes_per_second": 0, "iops": 0,
                                 "mean_latency_ms": None, "p95_latency_ms": None, "p99_latency_ms": None}}
            sides = ["read", "write"] if w.rw == "randrw" else (["read"] if "read" in w.rw else ["write"])
            for side in sides:
                frac = (.7 if side == "read" else .3) if len(sides) == 2 else 1
                results[side] = {"io_bytes": int(mb * frac * 30), "bytes_per_second": round(mb * frac),
                                 "iops": round(iops * frac, 2), "mean_latency_ms": 0.31,
                                 "p95_latency_ms": 0.76, "p99_latency_ms": 1.32}
            report["tests"].append({"test_id": w.id, "name": w.title, "rw": w.rw,
                                     "block_size": w.bs, "workers": w.workers, "protocol": proto,
                                     "status": "PASS", "measure_kind": "SIMULATED_FIO", **results})
        report["tests"].append({"test_id": "T10", "name": "Metadata Create/Stat/Delete",
                                 "status": "PASS", "protocol": proto, "measure_kind": "SIMULATED_METADATA",
                                 "create_ops_per_s": 8800 if proto == "nfs" else 7100})
    report["status"] = "SIMULATION"
    report["cleanup"] = "not_applicable"
    report["finished_at"] = now()
    create_reports(report, out, config["report"]["formats"])
    return out, report
