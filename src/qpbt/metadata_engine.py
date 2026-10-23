"""Single-thread metadata micro-benchmark. Explicitly client-cache-sensitive."""
from pathlib import Path
from time import perf_counter


def run_metadata(directory: Path, count: int) -> dict:
    files = [directory / f"m-{i:06d}.bin" for i in range(count)]
    if list(directory.iterdir()):
        raise RuntimeError("Metadaten-Testverzeichnis nicht leer.")
    rates = {}
    completed = []
    try:
        start = perf_counter()
        for file in files:
            with file.open("xb") as f:
                f.write(b"QPBT")
            completed.append(file)
        rates["create_ops_per_s"] = count / max(perf_counter() - start, 1e-9)
        start = perf_counter()
        for file in completed:
            file.stat()
        rates["stat_ops_per_s"] = count / max(perf_counter() - start, 1e-9)
    finally:
        start = perf_counter()
        for file in completed:
            file.unlink(missing_ok=True)
        rates["delete_ops_per_s"] = len(completed) / max(perf_counter() - start, 1e-9)
    return {"test_id": "T10", "name": "Metadata Create/Stat/Delete", "rw": "metadata",
            "block_size": "4 bytes", "workers": 1, "status": "PASS",
            "measure_kind": "metadata_python_single_thread_client_cache_sensitive",
            "operations": count, **{k: round(v, 2) for k, v in rates.items()}}
