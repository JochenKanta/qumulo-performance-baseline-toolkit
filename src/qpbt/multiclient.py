"""Multi-client baseline via fio client/server mode.

Architecture
------------
* The controller host runs qpbt. It mounts NFS + SMB itself (``targets``) and uses
  these mounts for workspace creation, verification of prepared data, the metadata
  test, the telemetry sampler and the cleanup.
* Every load client runs ``fio --server`` (started/stopped by qpbt via SSH when
  ``manage_fio_servers`` is true) and mounts the same Qumulo export, ideally via a
  different Qumulo node address. qpbt checks each client's mounts read-only via SSH.
* Each client proves that it sees the controller's run directory by reading the
  random nonce of the run marker through its own mount.
* Single-stream tests (T01/T02/T05/T06) run as one stream on the first client and
  address the dataset of ALL clients. Multi-worker tests run worker_count jobs on
  every client at the same time.

fio facts relied on (verified with fio 3.36):
* client mode prefixes generated file names with the client host unless
  ``unique_filename=0`` is set;
* a client whose job fails to parse is silently missing from ``client_stats`` while
  fio still exits 0 -> every expected client must be present with I/O;
* the "All clients" aggregate carries no latency percentiles -> qpbt aggregates
  itself and reports the WORST client percentile.
"""
from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from .executor import Executor, ExecError, executor_for
from .fio_engine import (FioError, as_jobfile, normalize_job, prepare_options, prepare_timeout,
                         run_fio_command, verify_dataset, workload_options, expected_sides)
from .mounts import (PreflightError, parse_fio_version, remote_client_info, remote_mounted_target,
                     expected_version_for)
from .profiles import Workload

IDENTITY_KEYS = ("mountpoint", "fstype", "source", "negotiated_version", "mount_id", "mount_dev", "st_dev")


@dataclass
class Client:
    name: str
    address: str
    port: int
    ssh: str
    targets: dict
    executor: Executor

    @property
    def endpoint(self) -> str:
        host = self.address.strip("[]")
        return f"ip6:{host},{self.port}" if ":" in host else f"{host},{self.port}"

    def mountpoint(self, proto: str) -> str:
        return self.targets[proto]["mountpoint"].rstrip("/") or "/"

    def run_dir(self, proto: str, workspace_id: str) -> str:
        return f"{self.mountpoint(proto)}/.qpbt/{workspace_id}"

    @property
    def pidfile(self) -> str:
        return f"/tmp/qpbt-fio-{self.port}.pid"


def load_clients(config: dict) -> list[Client]:
    mc = config["multi_client"]
    return [Client(c["name"], c["address"], c["fio_port"], c["ssh"], c["targets"],
                   executor_for(c, mc["ssh_connect_timeout"])) for c in mc["clients"]]


# --------------------------------------------------------------------------- fio servers

def server_reachable(client: Client, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((client.address.strip("[]"), client.port), timeout=timeout):
            return True
    except OSError:
        return False


def start_servers(clients: list[Client], wait_seconds: int = 15) -> list[str]:
    """Start one fio server per client, bound to the configured test-network address."""
    started = []
    for c in clients:
        if server_reachable(c, 1.0):
            raise PreflightError(f"{c.name}: auf {c.address}:{c.port} läuft bereits ein Dienst. "
                                 "Fremden fio-Server beenden oder manage_fio_servers=false setzen.")
        bind = c.endpoint
        try:
            c.executor.check(["fio", f"--server={bind}", f"--daemonize={c.pidfile}"], timeout=30)
        except ExecError as exc:
            raise PreflightError(f"{c.name}: fio-Server konnte nicht gestartet werden: {exc}") from exc
        started.append(c.name)
    deadline = time.monotonic() + wait_seconds
    pending = list(clients)
    while pending and time.monotonic() < deadline:
        pending = [c for c in pending if not server_reachable(c, 1.0)]
        if pending:
            time.sleep(0.5)
    if pending:
        raise PreflightError("fio-Server nicht erreichbar: " + ", ".join(f"{c.name} ({c.address}:{c.port})" for c in pending))
    return started


def stop_servers(clients: list[Client]) -> list[str]:
    problems = []
    for c in clients:
        try:
            res = c.executor.run(["pkill", "-F", c.pidfile], timeout=20)
            if res.returncode not in (0, 1):
                problems.append(f"{c.name}: pkill Exitcode {res.returncode}")
            c.executor.run(["rm", "-f", "--", c.pidfile], timeout=20)
        except ExecError as exc:
            problems.append(f"{c.name}: {exc}")
    return problems


def check_servers(clients: list[Client]) -> None:
    missing = [c for c in clients if not server_reachable(c)]
    if missing:
        raise PreflightError("fio-Server nicht erreichbar (fio --server auf den Clients starten): "
                             + ", ".join(f"{c.name} ({c.address}:{c.port})" for c in missing))


# --------------------------------------------------------------------------- preflight

def node_distribution(clients_info: dict, node_addresses: list[str]) -> dict:
    """Which Qumulo address each client actually mounted (kernel addr= option)."""
    out = {"per_protocol": {}, "warnings": []}
    for proto in ("nfs", "smb"):
        mapping: dict = {}
        for name, info in clients_info.items():
            addr = info["mounts"][proto].get("server_address") or "unbekannt"
            mapping.setdefault(addr, []).append(name)
        out["per_protocol"][proto] = mapping
        counts = [len(v) for v in mapping.values()]
        available = len(node_addresses) if node_addresses else None
        if "unbekannt" in mapping:
            out["warnings"].append(f"{proto}: Server-Adresse (addr=) für {mapping['unbekannt']} nicht ermittelbar.")
        if len(clients_info) > 1 and len(mapping) == 1:
            out["warnings"].append(f"{proto}: alle Clients nutzen dieselbe Qumulo-Adresse "
                                   f"{next(iter(mapping))} - Last liegt auf einer Node.")
        elif counts and max(counts) - min(counts) > 1:
            out["warnings"].append(f"{proto}: ungleichmäßige Verteilung der Clients auf Qumulo-Adressen: "
                                   + ", ".join(f"{a}={len(c)}" for a, c in mapping.items()))
        if available and len(clients_info) >= available and len(mapping) < available:
            out["warnings"].append(f"{proto}: {len(mapping)} von {available} Node-Adressen genutzt.")
        if node_addresses:
            unknown = [a for a in mapping if a not in node_addresses and a != "unbekannt"]
            if unknown:
                out["warnings"].append(f"{proto}: Adresse(n) {unknown} nicht in cluster.node_addresses.")
    return out


def inspect_client(config: dict, client: Client) -> dict:
    ex = client.executor
    info = {"name": client.name, "fio_server": f"{client.address}:{client.port}", "access": ex.describe(),
            "warnings": []}
    try:
        info["fio_version"] = parse_fio_version(ex.check(["fio", "--version"], timeout=20), f"{client.name}:fio")
    except ExecError as exc:
        raise PreflightError(f"{client.name}: fio nicht ausführbar: {exc}") from exc
    info["host"] = remote_client_info(ex)
    info["mounts"] = {}
    for proto in ("nfs", "smb"):
        tgt = client.targets[proto]
        if not tgt.get("expected_source"):
            raise PreflightError(f"{client.name}: {proto}.expected_source muss den exakten findmnt-SOURCE-Wert enthalten.")
        info["mounts"][proto] = remote_mounted_target(ex, tgt["mountpoint"], proto,
                                                      expected_version_for(config, proto), tgt["expected_source"])
    try:
        ntp = ex.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=20).stdout.strip()
    except ExecError:
        ntp = ""
    info["ntp_synchronized"] = {"yes": True, "no": False}.get(ntp)
    if info["ntp_synchronized"] is not True:
        info["warnings"].append(f"{client.name}: NTP-Synchronisation nicht bestätigt ({ntp or 'unbekannt'}).")
    return info


def inspect_multi(config: dict, inspect_controller, apply_netcheck) -> tuple[dict, list[Client]]:
    from .netcheck import targets_for
    info = inspect_controller(config)
    snapshot = info.pop("_snapshot", None)
    clients = load_clients(config)
    info["clients"] = {}
    for client in clients:
        cinfo = inspect_client(config, client)
        info["clients"][client.name] = cinfo
        info["warnings"].extend(cinfo["warnings"])
    versions = {c["fio_version"] for c in info["clients"].values()} | {info["fio_version"]}
    if len(versions) > 1:
        info["warnings"].append(f"Unterschiedliche fio-Versionen: {sorted(versions)}")
    info["node_distribution"] = node_distribution(info["clients"], config["cluster"].get("node_addresses") or [])
    info["warnings"].extend(info["node_distribution"]["warnings"])
    apply_netcheck(config, info, [(c.executor, targets_for(config, info["clients"][c.name]["mounts"]))
                                  for c in clients], snapshot)
    info["_snapshot"] = snapshot
    return info, clients


def check_client_mount_unchanged(config: dict, client: Client, proto: str, original: dict) -> None:
    from .safety import SafetyError
    tgt = client.targets[proto]
    now = remote_mounted_target(client.executor, tgt["mountpoint"], proto,
                                expected_version_for(config, proto), tgt["expected_source"])
    if any(now.get(k) != original.get(k) for k in IDENTITY_KEYS):
        raise SafetyError(f"{client.name}/{proto}: Mountverbindung hat sich während des Laufs geändert!")


def verify_remote_marker(client: Client, proto: str, workspace_id: str, nonce: str, attempts: int = 6) -> None:
    """The client must read the controller's marker nonce through its own mount."""
    from .safety import SafetyError
    path = f"{client.run_dir(proto, workspace_id)}/marker.json"
    last = ""
    for attempt in range(attempts):
        try:
            payload = json.loads(client.executor.check(["cat", "--", path], timeout=20))
            if payload.get("nonce") == nonce and payload.get("run_id") == workspace_id:
                return
            last = "Nonce stimmt nicht überein"
        except (ExecError, ValueError) as exc:
            last = str(exc)[:200]
        time.sleep(min(1 + attempt, 3))
    raise SafetyError(f"{client.name}/{proto}: sieht das Run-Verzeichnis nicht ({last}). "
                      "Mount zeigt nicht auf dasselbe Qumulo-Verzeichnis wie der Controller?")


# --------------------------------------------------------------------------- fio execution

def run_client_jobs(jobs: list[tuple[Client, list]], outfile: Path, timeout: int) -> dict:
    """Write one job file per client (kept for audit) and run fio in client mode."""
    args = []
    for client, options in jobs:
        jobfile = outfile.with_name(f"{outfile.stem}.{client.name}.fio")
        jobfile.parent.mkdir(parents=True, exist_ok=True)
        jobfile.write_text(as_jobfile(options + [("unique_filename", "0")]), encoding="utf-8")
        args += [f"--client={client.endpoint}", str(jobfile)]
    return run_fio_command(args, outfile, timeout)


def client_entries(data: dict, clients: list[Client]) -> dict:
    stats = data.get("client_stats")
    if not isinstance(stats, list) or not stats:
        raise FioError("fio-Client-JSON ohne client_stats[]")
    entries = [s for s in stats if s.get("jobname") != "All clients"]
    out = {}
    for client in clients:
        host = client.address.strip("[]")
        matches = [s for s in entries if s.get("port") == client.port and s.get("hostname") in (host, client.address)]
        if not matches:
            same_port = [c for c in clients if c.port == client.port]
            if len(same_port) == 1:
                matches = [s for s in entries if s.get("port") == client.port]
        if not matches:
            raise FioError(f"fio-Ergebnis fehlt für Client {client.name} - Job dort nicht gelaufen.")
        if len(matches) > 1:
            raise FioError(f"Mehrdeutiges fio-Ergebnis für Client {client.name}.")
        if int(matches[0].get("error", 0)) != 0:
            raise FioError(f"fio meldet Fehler {matches[0].get('error')} auf Client {client.name}.")
        out[client.name] = matches[0]
    if len(entries) != len(clients):
        raise FioError(f"Erwartet {len(clients)} Client-Ergebnisse, erhalten {len(entries)}.")
    return out


def aggregate(per_client: dict, work: Workload) -> dict:
    """Sum throughput/IOPS; IOPS-weighted mean latency; worst-client P95/P99."""
    any_result = next(iter(per_client.values()))
    result = {k: any_result[k] for k in ("test_id", "name", "rw", "block_size")}
    result.update(workers=work.workers, measure_kind="fio", status="PASS",
                  latency_percentile_aggregation="max_over_clients")
    for side in ("read", "write"):
        parts = [r[side] for r in per_client.values()]
        iops = sum(p["iops"] for p in parts)
        weighted = [p["mean_latency_ms"] * p["iops"] for p in parts if p.get("mean_latency_ms") is not None]
        p95 = [p["p95_latency_ms"] for p in parts if p.get("p95_latency_ms") is not None]
        p99 = [p["p99_latency_ms"] for p in parts if p.get("p99_latency_ms") is not None]
        result[side] = {"io_bytes": sum(p["io_bytes"] for p in parts),
                        "bytes_per_second": sum(p["bytes_per_second"] for p in parts),
                        "iops": iops,
                        "mean_latency_ms": (sum(weighted) / iops) if weighted and iops else None,
                        "p95_latency_ms": max(p95) if p95 else None,
                        "p99_latency_ms": max(p99) if p99 else None}
    return result


def normalize_multi(data: dict, work: Workload, clients: list[Client]) -> dict:
    entries = client_entries(data, clients)
    per_client = {name: normalize_job(entry, work) for name, entry in entries.items()}
    result = aggregate(per_client, work)
    result["client_count"] = len(per_client)
    result["clients"] = {name: {side: r[side] for side in expected_sides(work)} for name, r in per_client.items()}
    rates = [sum(r[s]["bytes_per_second"] for s in expected_sides(work)) for r in per_client.values()]
    result["client_balance_min_to_max"] = round(min(rates) / max(rates), 3) if max(rates) > 0 else None
    return result


def direct_io_probe(clients: list[Client], proto: str, workspace_id: str, outfile: Path) -> dict:
    """4 KiB-aligned O_DIRECT write + verified read-back on every client (64 KiB)."""
    jobs = []
    for c in clients:
        jobs.append((c, [("ioengine", "psync"), ("direct", "1"), ("thread", "1"),
                         ("directory", f"{c.run_dir(proto, workspace_id)}/probe/{c.name}"),
                         ("filename", "direct-io.bin"), ("size", "64k"), ("bs", "4k"), ("rw", "write"),
                         ("verify", "crc32c"), ("do_verify", "1"), ("verify_fatal", "1"),
                         ("numjobs", "1"), ("group_reporting", "1")]))
    data = run_client_jobs(jobs, outfile, timeout=120)
    entries = client_entries(data, clients)
    result = {}
    for name, entry in entries.items():
        written = int(entry.get("write", {}).get("io_bytes", 0))
        verified = int(entry.get("read", {}).get("io_bytes", 0))
        if written != 65536 or verified != 65536:
            raise FioError(f"{name}: O_DIRECT-Probe unvollständig (write={written}, verify-read={verified}).")
        result[name] = {"status": "PASS", "bytes": 65536,
                        "meaning": "O_DIRECT write + crc32c read-back on client; server cache not proven"}
    return result


def prepare_all(clients: list[Client], config: dict, proto: str, workspace_id: str,
                controller_run: Path, outfile: Path) -> dict:
    w = config["workload"]
    jobs = [(c, prepare_options(f"{c.run_dir(proto, workspace_id)}/dataset/{c.name}", config)) for c in clients]
    data = run_client_jobs(jobs, outfile, timeout=prepare_timeout(config, w["worker_count"] * len(clients)))
    client_entries(data, clients)
    names = [f"{c.name}/data.{j}.bin" for c in clients for j in range(w["worker_count"])]
    verify_dataset(controller_run / "dataset", names, w["dataset_mib_per_worker"])
    return data


def workload_jobs(clients: list[Client], config: dict, proto: str, workspace_id: str,
                  work: Workload) -> tuple[list[Client], list]:
    if work.single_stream:
        first = clients[0]
        files = [f"{c.name}/data.{j}.bin" for c in clients for j in range(config["workload"]["worker_count"])]
        return [first], [(first, workload_options(f"{first.run_dir(proto, workspace_id)}/dataset", config, work, files))]
    return clients, [(c, workload_options(f"{c.run_dir(proto, workspace_id)}/dataset/{c.name}", config, work))
                     for c in clients]


def workload_timeout(config: dict, clients: int) -> int:
    w = config["workload"]
    return w["measure_seconds"] + w["warmup_seconds"] + 180 + 5 * clients
