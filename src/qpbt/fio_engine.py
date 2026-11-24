"""Safe fio subprocess runner. Never passes a block device or a user-specified file.

Job options are built once and rendered either as command line arguments
(local single client) or as a job file (fio client/server mode).
"""
from __future__ import annotations
import json
import os
import subprocess
import time
from pathlib import Path
from .profiles import Workload


class FioError(RuntimeError):
    pass


Options = list[tuple[str, str]]


def _safe_value(key: str, value: str) -> str:
    value = str(value)
    if any(c in value for c in "\n\r\0") or (("$" in value) and key != "filename_format"):
        raise FioError(f"Unzulässiges Zeichen in fio-Option {key}.")
    if key in ("directory",) and ":" in value:
        # fio treats ':' as a list separator for directory/filename.
        raise FioError("fio-Verzeichnis darf keinen Doppelpunkt enthalten.")
    return value


def as_args(options: Options) -> list[str]:
    # The job name must come first; fio treats options before --name as global/no job.
    return ["--name=qpbt"] + [f"--{k}={_safe_value(k, v)}" for k, v in options]


def as_jobfile(options: Options) -> str:
    return "[qpbt]\n" + "".join(f"{k}={_safe_value(k, v)}\n" for k, v in options)


def base_options(directory: str | Path, size_mib: int, numjobs: int, filenames: list[str] | None = None) -> Options:
    opts: Options = [("ioengine", "psync"), ("direct", "1"), ("group_reporting", "1"),
                     ("thread", "1"), ("directory", str(directory))]
    if filenames:
        for name in filenames:
            if ":" in name or name.startswith("/") or ".." in name.split("/"):
                raise FioError(f"Ungültiger Datensatz-Dateiname: {name}")
        opts += [("filename", ":".join(filenames)), ("nrfiles", str(len(filenames))),
                 ("size", f"{size_mib * len(filenames)}m")]
    else:
        opts += [("filename_format", "data.$jobnum.bin"), ("nrfiles", "1"), ("size", f"{size_mib}m")]
    return opts + [("numjobs", str(numjobs))]


def prepare_options(directory: str | Path, config: dict) -> Options:
    w = config["workload"]
    return base_options(directory, w["dataset_mib_per_worker"], w["worker_count"]) + [
        ("rw", "write"), ("bs", "1m"), ("overwrite", "1"), ("refill_buffers", "1"),
        ("end_fsync", "1"), ("randrepeat", "0"), ("scramble_buffers", "1"),
    ]


def workload_options(directory: str | Path, config: dict, work: Workload,
                     filenames: list[str] | None = None) -> Options:
    """filenames: single-stream tests address the WHOLE dataset instead of one worker file."""
    w = config["workload"]
    if work.single_stream and not filenames:
        raise FioError(f"{work.id}: Single-Stream-Test benötigt die Dateiliste des Datensatzes.")
    opts = base_options(directory, w["dataset_mib_per_worker"], work.workers,
                        filenames if work.single_stream else None) + [
        ("rw", work.rw), ("bs", work.bs),
        ("runtime", str(w["measure_seconds"])), ("ramp_time", str(w["warmup_seconds"])),
        ("time_based", "1"), ("allow_file_create", "0"), ("norandommap", "1"),
        ("randrepeat", "0"), ("invalidate", "1"),
    ]
    if work.single_stream:
        opts.append(("file_service_type", "sequential" if work.sequential else "random"))
    if "write" in work.rw or work.rw == "randrw":
        opts += [("end_fsync", "1"), ("refill_buffers", "1")]
    if work.rwmixread is not None:
        opts.append(("rwmixread", str(work.rwmixread)))
    return opts


def local_dataset_files(config: dict) -> list[str]:
    return [f"data.{j}.bin" for j in range(config["workload"]["worker_count"])]


def _shared_args(dataset: Path, config: dict, numjobs: int) -> list[str]:
    return as_args(base_options(dataset, config["workload"]["dataset_mib_per_worker"], numjobs))


def extract_json(stdout: str) -> dict:
    """fio client mode prints '<host> ...' progress lines before the JSON document."""
    text = stdout.lstrip()
    if not text.startswith("{"):
        index = stdout.find("\n{")
        if index < 0:
            raise FioError("fio hat kein JSON ausgegeben.")
        text = stdout[index + 1:]
    try:
        data, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        raise FioError(f"fio hat kein gültiges JSON erzeugt: {exc}") from exc
    return data


def run_fio_command(args: list[str], outfile: Path, timeout: int) -> dict:
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
    return extract_json(proc.stdout)


def _run_fio(args: list[str], outfile: Path, timeout: int) -> dict:
    data = run_fio_command(args, outfile, timeout)
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise FioError("fio-JSON ohne jobs[]")
    if any(int(j.get("error", 0)) != 0 for j in jobs):
        raise FioError(f"fio meldet Job-Fehler: {[j.get('error') for j in jobs]}")
    return data


def prepare_timeout(config: dict, files: int) -> int:
    w = config["workload"]
    return max(900, int(files * w["dataset_mib_per_worker"] / 3))


def verify_dataset(dataset: Path, expected_names: list[str], size_mib: int, attempts: int = 10) -> None:
    """Expected files only, each at least the nominal size.

    Files written by OTHER clients may become visible with a short delay
    (NFS attribute cache). Opening a file revalidates its attributes
    (close-to-open consistency), so retry a few times before failing.
    """
    expected = {dataset / name for name in expected_names}
    minimum = size_mib * 1024 ** 2
    last = ""
    for attempt in range(attempts):
        try:
            missing = []
            for file in sorted(expected):
                if file.is_symlink():
                    raise FioError(f"Symlink im Datensatz: {file}")
                fd = os.open(file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    if os.fstat(fd).st_size < minimum:
                        missing.append(file.name)
                finally:
                    os.close(fd)
            present = {p for d in {f.parent for f in expected} for p in d.iterdir()}
            if present - expected:
                raise FioError("Vorbereitung hat unerwartete Dateien angelegt: "
                               + ", ".join(sorted(p.name for p in present - expected))[:300])
            if not missing:
                return
            last = f"zu klein: {missing[:5]}"
        except FileNotFoundError as exc:
            last = f"fehlt: {exc.filename}"
        time.sleep(min(1 + attempt, 5))
    raise FioError(f"fio-Vorbereitung unvollständig ({last}).")


def prepare_data(dataset: Path, config: dict, outfile: Path) -> dict:
    w = config["workload"]
    # Only dedicated, freshly created directory may be prepared.
    if not dataset.is_dir() or list(dataset.iterdir()):
        raise FioError("Vorbereitung erfordert ein leeres dediziertes Dataset-Verzeichnis.")
    result = _run_fio(as_args(prepare_options(dataset, config)), outfile,
                      timeout=prepare_timeout(config, w["worker_count"]))
    verify_dataset(dataset, local_dataset_files(config), w["dataset_mib_per_worker"], attempts=1)
    return result


def execute_workload(dataset: Path, config: dict, work: Workload, outfile: Path) -> dict:
    w = config["workload"]
    files = local_dataset_files(config) if work.single_stream else None
    timeout = w["measure_seconds"] + w["warmup_seconds"] + 180
    return _run_fio(as_args(workload_options(dataset, config, work, files)), outfile, timeout)


def normalize_fio(raw: dict, work: Workload) -> dict:
    jobs = raw.get("jobs", [])
    if len(jobs) != 1:
        raise FioError("Erwartet eine aggregierte fio-job-Zeile (group_reporting=1).")
    return normalize_job(jobs[0], work)


def normalize_job(job: dict, work: Workload) -> dict:
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
    for side in expected_sides(work):
        if not result[side]["io_bytes"] or result[side]["iops"] <= 0:
            raise FioError(f"Erwartete {side}-IOs fehlen in {work.id}.")
    # Missing percentiles must not be silently interpreted as valid baselines.
    for side in expected_sides(work):
        if result[side]["p95_latency_ms"] is None or result[side]["p99_latency_ms"] is None:
            raise FioError(f"fio-Latenzperzentile fehlen: {work.id}/{side}.")
    return result


def expected_sides(work: Workload) -> tuple[str, ...]:
    return ("read", "write") if work.rw == "randrw" else (("read",) if "read" in work.rw else ("write",))
