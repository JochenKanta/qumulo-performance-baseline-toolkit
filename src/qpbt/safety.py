"""Isolated target workspace: never touch anything outside a unique marked run."""
from __future__ import annotations
import json
import os
import shutil
import stat
from pathlib import Path


class SafetyError(RuntimeError):
    pass


def _check_not_link(path: Path):
    if path.is_symlink():
        raise SafetyError(f"Symlink in Testbereich ist unzulässig: {path}")


def make_workspace(mountpoint: str, run_id: str) -> Path:
    if not run_id or not all(c.isascii() and (c.isalnum() or c in "-_") for c in run_id):
        raise SafetyError("Unsichere Run-ID.")
    mount = Path(mountpoint)
    if mount.is_symlink() or not mount.is_mount():
        raise SafetyError("Workspace darf nur auf einem echten, eigenen Mountpoint angelegt werden.")
    root = mount / ".qpbt"
    _check_not_link(root)
    if root.is_mount():
        raise SafetyError(".qpbt darf kein eigener Mountpoint sein.")
    if not root.exists():
        root.mkdir(mode=0o700)
    if not root.is_dir() or os.stat(root).st_dev != os.stat(mount).st_dev:
        raise SafetyError(f"Reservierter Name .qpbt ist kein sicheres Verzeichnis: {root}")
    run = root / run_id
    run.mkdir(mode=0o700, exist_ok=False)
    (run / "marker.json").write_text(json.dumps({"run_id": run_id, "owner": "qpbt", "schema": "0.1"}), encoding="utf-8")
    (run / "dataset").mkdir(mode=0o700)
    (run / "metadata").mkdir(mode=0o700)
    (run / "probe").mkdir(mode=0o700)
    return run


def cleanup_workspace(mountpoint: str, run_id: str) -> None:
    mount = Path(mountpoint)
    if mount.is_symlink() or not mount.is_mount():
        raise SafetyError("Bereinigung abgebrochen: Mountpoint ist nicht mehr derselbe Mounttyp.")
    root = mount / ".qpbt"
    run = root / run_id
    for p in (root, run):
        _check_not_link(p)
        if p.is_mount() or os.stat(p).st_dev != os.stat(mount).st_dev:
            raise SafetyError(f"Mountwechsel innerhalb des Workspace: {p}")
    if run.parent != root or run_id in ("", ".", ".."):
        raise SafetyError("Pfadvalidierung fehlgeschlagen.")
    marker = run / "marker.json"
    if marker.is_symlink() or not marker.is_file():
        raise SafetyError("Marker fehlt oder ist unsicher; automatische Bereinigung verweigert.")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SafetyError(f"Marker ist defekt: {exc}") from exc
    if payload != {"run_id": run_id, "owner": "qpbt", "schema": "0.1"}:
        raise SafetyError("Marker stimmt nicht mit Run-ID überein.")
    # No symlinks/special files and no unexpected names, even deep inside the tree.
    allowed_files = {"marker.json", "dataset", "metadata", "probe"}
    if {p.name for p in run.iterdir()} - allowed_files:
        raise SafetyError("Unbekannte Dateien im Testverzeichnis: Bereinigung verweigert.")
    for current, dirs, files in os.walk(run, followlinks=False):
        cp = Path(current)
        for name in dirs + files:
            p = cp / name
            st = p.lstat()
            if p.is_mount():
                raise SafetyError(f"Zusätzlicher Mount im Testbereich: {p}")
            if stat.S_ISLNK(st.st_mode) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise SafetyError(f"Unzulässiger Dateityp in Testdaten: {p}")
        if cp.name == "dataset":
            for name in files:
                if not name.startswith("data.") or not name.endswith(".bin") or not name[5:-4].isdigit():
                    raise SafetyError(f"Unbekannte Datei im Dataset: {name}")
        if cp.name == "probe":
            if any(name != "direct-io.bin" for name in files):
                raise SafetyError("Unbekannte Datei im Direct-I/O-Probe-Verzeichnis.")
        if cp.name == "metadata":
            for name in files:
                if not name.startswith("m-") or not name.endswith(".bin") or not name[2:-4].isdigit():
                    raise SafetyError(f"Unbekannte Datei im Metadaten-Test: {name}")
    # Python rmtree on Linux uses fd-based protections against directory symlink races.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise SafetyError("Sichere Bereinigung wird auf diesem Betriebssystem nicht unterstützt.")
    shutil.rmtree(run)
    try:
        root.rmdir()
    except OSError:
        pass  # Other QPBT runs or reserved data; retain .qpbt.
