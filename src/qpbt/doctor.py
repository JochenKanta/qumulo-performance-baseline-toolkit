"""Read-only local host prerequisites and safety checks."""
from __future__ import annotations
import os
import platform
import shutil
import subprocess
from .mounts import official_fio_version, PreflightError, client_info


def inspect_host() -> dict:
    checks = []
    checks.append({"check": "Linux", "status": "PASS" if platform.system()=="Linux" else "FAIL",
                   "detail": platform.platform()})
    for binary in ("findmnt", "mount.nfs", "mount.cifs", "ip"):
        path = shutil.which(binary)
        checks.append({"check": binary, "status": "PASS" if path else "WARNING",
                       "detail": path or "Nicht im PATH; ggf. nfs-common/cifs-utils/iproute2 installieren"})
    try:
        ver = official_fio_version()
        checks.append({"check": "fio", "status": "PASS", "detail": ver})
    except PreflightError as exc:
        checks.append({"check": "fio", "status": "FAIL", "detail": str(exc)})
    return {"tool": "QPBT", "kind": "READ_ONLY_LOCAL_DOCTOR", "client": client_info(),
            "checks": checks, "passed": not any(x["status"] == "FAIL" for x in checks)}
