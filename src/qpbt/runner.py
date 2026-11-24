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
from .safety import make_workspace, cleanup_workspace, read_marker, SafetyError
from .telemetry import QumuloSampler, sample_client_network, summarize_telemetry, TelemetryError, node_ids
from .netcheck import run_netcheck, targets_for
from .executor import LocalExecutor


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:10]


def output_base(config: dict) -> Path:
    return Path(config["report"]["output_directory"]).expanduser().resolve()


def check_node_count(config: dict, snapshot: dict, info: dict) -> None:
    """Compare the configured node count (used for sizing) with the cluster."""
    seen = node_ids(snapshot)
    info["cluster_info"]["node_count"] = len(seen) or None
    expected = config["cluster"].get("node_count")
    if not seen:
        info["warnings"].append("Node-Anzahl aus OpenMetrics nicht ermittelbar (qumulo_quorum_node_is_offline fehlt).")
    elif expected and len(seen) != expected:
        message = f"cluster.node_count={expected}, Qumulo meldet {len(seen)} Nodes."
        if config["workload"]["dataset_sizing"] == "auto":
            raise PreflightError(message + " Auto-Sizing wäre falsch dimensioniert.")
        info["warnings"].append(message)


def apply_netcheck(config: dict, info: dict, clients: list, snapshot: dict | None) -> None:
    if not config["network"]["enabled"]:
        info["network"] = {"status": "DISABLED"}
        info["warnings"].append("Netzwerk-Vorabtest deaktiviert.")
        return
    result = run_netcheck(config, clients, snapshot)
    info["network"] = result
    for item in result["issues"]:
        info["warnings"].append(f"Netzwerk [{item['severity']}] {item['client']}: {item['detail']}")
    if result["status"] == "FAIL" and config["network"]["required"]:
        raise PreflightError("Netzwerk-Vorabtest fehlgeschlagen: "
                             + "; ".join(i["detail"] for i in result["issues"] if i["severity"] == "FAIL"))


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
            "cluster_info": {}, "warnings": [], "telemetry": "disabled", "sizing": config["sizing"]}
    snap = None
    if config["telemetry"]["enabled"]:
        try:
            snap = QumuloSampler(config).collect_once()
            problems = QumuloSampler.health(snap)
            if problems:
                raise PreflightError("Qumulo Health Check fehlgeschlagen: " + "; ".join(problems))
            info["cluster_info"] = dict(snap["cluster_info"])
            info["telemetry"] = "available"
            check_node_count(config, snap, info)
            present = set(snap["metrics"])
            for important in ("qumulo_disk_is_unhealthy", "qumulo_quorum_node_is_offline"):
                if not any(k.split("|")[0] == important for k in present):
                    info["warnings"].append(f"Health-Gauge nicht vorhanden: {important}")
        except TelemetryError as exc:
            if config["telemetry"]["required"]:
                raise PreflightError(str(exc)) from exc
            info["warnings"].append(f"Qumulo-Telemetrie nicht verfügbar: {exc}")
            info["telemetry"] = "unavailable"
            snap = None
    else:
        info["warnings"].append("Cluster-Telemetrie deaktiviert: nur Client-Baseline.")
    # Every real run must identify the actual Qumulo cluster, not merely reach a metrics URL.
    if info["telemetry"] == "available":
        missing = [k for k in ("name", "uuid", "version") if not info["cluster_info"].get(k)]
        if missing:
            raise PreflightError("OpenMetrics ohne vollständige Clusteridentität: " + ", ".join(missing))
    if not config.get("multi_client"):
        apply_netcheck(config, info, [(LocalExecutor(info["client"].get("hostname") or "controller"),
                                       targets_for(config, mounts))], snap)
    info["_snapshot"] = snap
    return info


def network_utilization(result: dict, ceiling: float | None) -> float | None:
    if not ceiling:
        return None
    rate = result.get("read", {}).get("bytes_per_second", 0) + result.get("write", {}).get("bytes_per_second", 0)
    return round(100.0 * rate / ceiling, 1)


def plan(config: dict) -> dict:
    size = total_dataset_bytes(config)
    mc = config.get("multi_client")
    return {"profile": config["profile"], "protocols": ["nfs", "smb"],
            "mode": "multi_client" if mc else "single_client",
            "clients": [c["name"] for c in mc["clients"]] if mc else ["local"],
            "sizing": config["sizing"],
            "network_precheck": config["network"]["enabled"],
            "fio_tests_per_protocol": len(workloads(config)), "metadata_tests_per_protocol": 1,
            "warmup_seconds": config["workload"]["warmup_seconds"],
            "measure_seconds": config["workload"]["measure_seconds"],
            "fio_measurement_plus_warmup_seconds": estimated_seconds(config),
            "dataset_gib_per_protocol": round(size/2**30,2),
            "estimated_total_dataset_gib": round(2*size/2**30,2),
            "single_stream_tests": "T01/T02/T05/T06 laufen mit 1 Stream über den gesamten Datensatz"
                                   + (" auf dem ersten Client" if mc else ""),
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
            "cleanup": "not_attempted", "telemetry_availability": preflight.get("telemetry", "disabled"),
            "sizing": config["sizing"], "network": preflight.get("network", {"status": "NOT_RUN"}),
            "mode": "multi_client" if config.get("multi_client") else "single_client"}


def _check_mount_unchanged(config: dict, protocol: str, original: dict):
    t = config["targets"][protocol]
    new = mounted_target(t["mountpoint"], protocol,
                         str(t.get("expected_version" if protocol == "nfs" else "expected_dialect")),
                         t.get("expected_source"))
    keys = ("mountpoint", "fstype", "source", "negotiated_version", "mount_id", "mount_dev", "st_dev")
    if any(new.get(k) != original.get(k) for k in keys):
        raise SafetyError(f"{protocol}: Mountverbindung hat sich während des Laufs geändert!")


def _measure(config: dict, preflight: dict, report: dict, output: Path, protocol: str,
             work, execute) -> dict:
    """Run one workload with cluster telemetry around it; execute() returns the normalized result."""
    sampler = QumuloSampler(config) if config["telemetry"]["enabled"] and preflight["telemetry"] == "available" else None
    before = sample_client_network()
    snapshot = None
    try:
        if sampler:
            try:
                sampler.start()
            except TelemetryError as exc:
                # A short API outage during the run must not destroy the benchmark itself.
                report["warnings"].append(f"{protocol}/{work.id}: Telemetrie-Start fehlgeschlagen: {exc}")
                sampler = None
        result = execute()
    finally:
        if sampler:
            snapshot = sampler.stop()
    after = sample_client_network()
    result["protocol"] = protocol
    result["client_network_before"] = before
    result["client_network_after"] = after
    result["cache_classification"] = "client_direct_io_server_cache_uncontrolled"
    result["dataset_classification"] = config["sizing"]["classification"]
    result["network_utilization_pct"] = network_utilization(
        result, report["network"].get("network_ceiling_bytes_per_second"))
    if snapshot is not None:
        save_json(output / "telemetry" / protocol / f"{work.id}.json", snapshot)
        result["cluster_telemetry"] = summarize_telemetry(snapshot)
        if result["cluster_telemetry"]["errors"] or result["cluster_telemetry"]["sample_count"] < 2:
            report["warnings"].append(f"{protocol}/{work.id}: Telemetrie unvollständig.")
    elif sampler is None and preflight.get("telemetry") == "available":
        report["warnings"].append(f"{protocol}/{work.id}: keine Cluster-Telemetrie.")
    return result


def _fail(report: dict, exc: BaseException) -> None:
    report["status"] = "FAIL"
    report["errors"].append(f"{type(exc).__name__}: {str(exc)[:500]}")
    report["warnings"].append("Testdaten bleiben nach einem Fehlschlag zur Fehleranalyse erhalten.")


def _finalize(config: dict, report: dict, output: Path, workspaces: dict, preflight: dict) -> None:
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


def _start_run(config: dict, preflight: dict, run_id: str, base: Path) -> tuple[Path, dict]:
    output = base / run_id
    output.mkdir(parents=True, exist_ok=False)
    report = _initial_run(config, preflight, run_id, simulated=False)
    save_json(output / "preflight.json", {k: v for k, v in preflight.items() if not k.startswith("_")})
    save_json(output / "result.json", report)
    return output, report


def run_real(config: dict) -> tuple[Path, dict]:
    if config.get("multi_client"):
        return run_multi(config)
    run_id = make_id()
    base = output_base(config)
    with local_lock(base):
        # No customer data is written before the entire preflight succeeds.
        preflight = inspect(config)
        output, report = _start_run(config, preflight, run_id, base)
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
                    result = _measure(config, preflight, report, output, protocol, work,
                                      lambda: normalize_fio(execute_workload(
                                          path / "dataset", config, work,
                                          output / "fio" / protocol / f"{work.id}.json"), work))
                    report["tests"].append(result)
                    save_json(output / "result.json", report)
                _check_mount_unchanged(config, protocol, preflight["mounts"][protocol])
                result = run_metadata(path / "metadata", config["workload"]["metadata_file_count"])
                result["protocol"] = protocol
                report["tests"].append(result)
                save_json(output / "result.json", report)
            report["status"] = "WARNING" if report["warnings"] else "PASS"
        except (Exception, KeyboardInterrupt) as exc:
            _fail(report, exc)
        finally:
            _finalize(config, report, output, workspaces, preflight)
        return output, report


def inspect_any(config: dict) -> dict:
    """Preflight for either mode; never writes benchmark data."""
    if not config.get("multi_client"):
        return inspect(config)
    from . import multiclient as mc
    managed = config["multi_client"]["manage_fio_servers"]
    info, clients = mc.inspect_multi(config, inspect, apply_netcheck)
    if not managed:
        mc.check_servers(clients)
    info["fio_servers"] = "managed_by_qpbt" if managed else "external"
    return info


def run_multi(config: dict) -> tuple[Path, dict]:
    from . import multiclient as mc
    run_id = make_id()
    base = output_base(config)
    with local_lock(base):
        preflight, clients = mc.inspect_multi(config, inspect, apply_netcheck)
        names = [c.name for c in clients]
        output, report = _start_run(config, preflight, run_id, base)
        report["clients"] = {n: {k: v for k, v in preflight["clients"][n].items() if k != "warnings"} for n in names}
        report["node_distribution"] = preflight["node_distribution"]
        workspaces = {}
        managed = config["multi_client"]["manage_fio_servers"]
        started = False
        try:
            if managed:
                mc.start_servers(clients)
                started = True
                report["fio_servers"] = "managed_by_qpbt"
            else:
                mc.check_servers(clients)
                report["fio_servers"] = "external"

            def check_all(protocol):
                _check_mount_unchanged(config, protocol, preflight["mounts"][protocol])
                for c in clients:
                    mc.check_client_mount_unchanged(config, c, protocol, preflight["clients"][c.name]["mounts"][protocol])

            for protocol in ("nfs", "smb"):
                check_all(protocol)
                workspace_id = f"{run_id}-{protocol}"
                path = make_workspace(config["targets"][protocol]["mountpoint"], workspace_id, clients=names)
                workspaces[protocol] = (path, workspace_id)
                nonce = read_marker(path)["nonce"]
                for c in clients:
                    mc.verify_remote_marker(c, protocol, workspace_id, nonce)
                check_all(protocol)
                probes = {"controller": check_direct_io(path / "probe")}
                for c in clients:
                    (path / "probe" / c.name).mkdir(mode=0o700)
                probes["clients"] = mc.direct_io_probe(clients, protocol, workspace_id,
                                                       output / "fio" / protocol / "PROBE.json")
                probes["status"] = "PASS"
                report.setdefault("direct_io_probes", {})[protocol] = probes
                save_json(output / "result.json", report)
                mc.prepare_all(clients, config, protocol, workspace_id, path,
                               output / "fio" / protocol / "PREPARE.json")
                for work in workloads(config):
                    check_all(protocol)
                    participants, jobs = mc.workload_jobs(clients, config, protocol, workspace_id, work)
                    outfile = output / "fio" / protocol / f"{work.id}.json"
                    result = _measure(config, preflight, report, output, protocol, work,
                                      lambda: mc.normalize_multi(
                                          mc.run_client_jobs(jobs, outfile, mc.workload_timeout(config, len(jobs))),
                                          work, participants))
                    result["participating_clients"] = [c.name for c in participants]
                    report["tests"].append(result)
                    save_json(output / "result.json", report)
                check_all(protocol)
                result = run_metadata(path / "metadata", config["workload"]["metadata_file_count"])
                result["protocol"] = protocol
                result["executed_on"] = "controller"
                report["tests"].append(result)
                save_json(output / "result.json", report)
            report["status"] = "WARNING" if report["warnings"] else "PASS"
        except (Exception, KeyboardInterrupt) as exc:
            _fail(report, exc)
        finally:
            if started:
                problems = mc.stop_servers(clients)
                if problems:
                    report["warnings"].append("fio-Server nicht sauber beendet: " + "; ".join(problems))
                    if report["status"] == "PASS":
                        report["status"] = "WARNING"
            _finalize(config, report, output, workspaces, preflight)
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
