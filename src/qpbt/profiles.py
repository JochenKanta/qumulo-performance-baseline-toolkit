"""Versioned, protocol-independent I/O test matrix."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Workload:
    id: str
    title: str
    rw: str
    bs: str
    workers: int
    rwmixread: int | None = None


def workloads(conf: dict) -> list[Workload]:
    w = conf["workload"]
    n = w["worker_count"]
    return [
        Workload("T01", "Sequential Write 1", "write", "1m", 1),
        Workload("T02", "Sequential Read 1", "read", "1m", 1),
        Workload("T03", f"Sequential Write {n}", "write", "1m", n),
        Workload("T04", f"Sequential Read {n}", "read", "1m", n),
        Workload("T05", "Random Read 1", "randread", "4k", 1),
        Workload("T06", "Random Write 1", "randwrite", "4k", 1),
        Workload("T07", f"Random Read {n}", "randread", "4k", n),
        Workload("T08", f"Random Write {n}", "randwrite", "4k", n),
        Workload("T09", f"Random Mixed 70/30 {n}", "randrw", "4k", n, 70),
    ]


def total_dataset_bytes(conf: dict) -> int:
    w = conf["workload"]
    return w["worker_count"] * w["dataset_mib_per_worker"] * 1024 * 1024


def estimated_seconds(conf: dict) -> int:
    work = conf["workload"]
    return 2 * 9 * (work["warmup_seconds"] + work["measure_seconds"])
