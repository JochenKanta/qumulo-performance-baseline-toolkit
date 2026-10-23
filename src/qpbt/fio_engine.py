"""Safe fio subprocess runner. Never passes a block device or a user-specified file."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
from .profiles import Workload


class FioError(RuntimeError):
    pass


def _run_fio(args: list[str], outfile: Path, timeout: int) -> dict:
    command = ["fio", *args, "--output-format=json"]
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FioError(f"fio-Zeitlimit überschritten ({timeout}s)") from exc
    except OSError as exc:
        raise FioError(f"fio konnte nicht gestartet werden: {exc}") from exc
    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(proc.stdout, encoding="utf-8")
    if proc.stderr:
        outfile.with_suffix(".stderr.txt").write_text(proc.stderr[-10000:], encoding="utf-8")
    if proc.returncode != 0:
        raise FioError(f"fio-Exitcode {proc.returncode}: {proc.stderr[-400:]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FioError(f"fio hat kein gültiges JSON erzeugt: {exc}") from exc
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise FioError("fio-JSON ohne jobs[]")
    if any(int(j.get("error", 0)) != 0 for j in jobs):
        raise FioError(f"fio meldet Job-Fehler: {[j.get('error') for j in jobs]}")
    return data


def _shared_args(dataset: Path, config: dict, numjobs: int) -> list[str]:
    w = config["workload"]
    return [
        "--name=qpbt", "--ioengine=psync", "--direct=1", "--group_reporting=1",
        "--thread=1", f"--directory={dataset}",
        "--filename_format=data.$jobnum.bin", f"--size={w['dataset_mib_per_worker']}m",
        f"--numjobs={numjobs}", "--nrfiles=1", "--eta=never",
    ]


def prepare_data(dataset: Path, config: dict, outfile: Path) -> dict:
    w = config["workload"]
    args = _shared_args(dataset, config, w["worker_count"]) + [
        "--rw=write", "--bs=1m", "--overwrite=1", "--refill_buffers=1",
        "--end_fsync=1", "--randrepeat=0", "--scramble_buffers=1",
    ]
    # Only dedicated, freshly created directory may be prepared.
    if not dataset.is_dir() or list(dataset.iterdir()):
        raise FioError("Vorbereitung erfordert ein leeres dediziertes Dataset-Verzeichnis.")
    result = _run_fio(args, outfile, timeout=max(900, int(w["worker_count"] * w["dataset_mib_per_worker"] / 3)))
    expected = [dataset / f"data.{j}.bin" for j in range(w["worker_count"])]
    for file in expected:
        if file.is_symlink() or not file.is_file() or file.stat().st_size < w["dataset_mib_per_worker"] * 1024**2:
            raise FioError(f"fio-Vorbereitung unvollständig: {file}")
    # No unknown files should be present; prevent later cleanup of arbitrary content.
    if set(dataset.iterdir()) != set(expected):
        raise FioError("Vorbereitung hat unerwartete Dateien angelegt.")
    return result


def execute_workload(dataset: Path, config: dict, work: Workload, outfile: Path) -> dict:
    w = config["workload"]
    args = _shared_args(dataset, config, work.workers) + [
        f"--rw={work.rw}", f"--bs={work.bs}",
        f"--runtime={w['measure_seconds']}", f"--ramp_time={w['warmup_seconds']}",
        "--time_based=1", "--allow_file_create=0", "--norandommap=1",
        "--randrepeat=0", "--invalidate=1",
    ]
    if "write" in work.rw or work.rw == "randrw":
        args.extend(["--end_fsync=1", "--refill_buffers=1"])
    if work.rwmixread is not None:
        args.append(f"--rwmixread={work.rwmixread}")
    timeout = w["measure_seconds"] + w["warmup_seconds"] + 180
    return _run_fio(args, outfile, timeout)


def normalize_fio(raw: dict, work: Workload) -> dict:
    jobs = raw.get("jobs", [])
    if len(jobs) != 1:
        raise FioError("Erwartet eine aggregierte fio-job-Zeile (group_reporting=1).")
    job = jobs[0]
    result = {"test_id": work.id, "name": work.title, "rw": work.rw,
              "block_size": work.bs, "workers": work.workers,
              "measure_kind": "fio", "status": "PASS", "read": {}, "write": {}}
    for direction in ("read", "write"):
        d = job.get(direction, {})
        bytes_done = int(d.get("io_bytes", 0))
        bw = float(d.get("bw_bytes", float(d.get("bw", 0)) * 1024))
        iops = float(d.get("iops", 0))
        lat = d.get("clat_ns", d.get("lat_ns", {}))
        percentiles = lat.get("percentile", {})
        def pct(target: str):
            try:
                return float(percentiles.get(target)) / 1e6
            except (ValueError, TypeError):
                return None
        result[direction] = {"io_bytes": bytes_done, "bytes_per_second": bw,
                             "iops": iops, "mean_latency_ms": (float(lat.get("mean", 0)) / 1e6 if bytes_done else None),
                             "p95_latency_ms": (pct("95.000000") if bytes_done else None),
                             "p99_latency_ms": (pct("99.000000") if bytes_done else None)}
    if not result["read"]["io_bytes"] and not result["write"]["io_bytes"]:
        raise FioError("Keine gemessene I/O-Menge in fio-Ergebnis.")
    expected_sides = ("read", "write") if work.rw == "randrw" else (
        ("read",) if "read" in work.rw else ("write",))
    for side in expected_sides:
        if not result[side]["io_bytes"] or result[side]["iops"] <= 0:
            raise FioError(f"Erwartete {side}-IOs fehlen in {work.id}.")
    # Missing percentiles must not be silently interpreted as valid baselines.
    for side in expected_sides:
        if result[side]["p95_latency_ms"] is None or result[side]["p99_latency_ms"] is None:
            raise FioError(f"fio-Latenzperzentile fehlen: {work.id}/{side}.")
    return result
