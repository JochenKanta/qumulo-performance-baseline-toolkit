"""Isolated target workspace: never touch anything outside a unique marked run."""
from __future__ import annotations
import json
import os
import re
import secrets
import shutil
import stat
from pathlib import Path


class SafetyError(RuntimeError):
    pass


def is_mountpoint(path: Path) -> bool:
    """st_dev based check plus an exact /proc/self/mountinfo entry (bind mounts share st_dev)."""
    if path.is_mount():
        return True
    try:
        from .mounts import parse_mountinfo
        return parse_mountinfo(Path("/proc/self/mountinfo").read_text(encoding="utf-8"), str(path)) is not None
    except OSError:
        return False


def _check_not_link(path: Path):
    if path.is_symlink():
        raise SafetyError(f"Symlink in Testbereich ist unzulässig: {path}")


CLIENT_DIR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def _numbered(name: str, prefix: str) -> bool:
    return name.startswith(prefix) and name.endswith(".bin") and name[len(prefix):-4].isdigit()


def make_workspace(mountpoint: str, run_id: str, clients: list[str] | None = None) -> Path:
    """Create the isolated run directory. With clients: per-client dataset subdirectories.

    The marker carries a random nonce. Remote test clients read it through their own
    mount to prove they see exactly this directory on the same Qumulo filesystem.
    """
    if not run_id or not all(c.isascii() and (c.isalnum() or c in "-_") for c in run_id):
        raise SafetyError("Unsichere Run-ID.")
    clients = list(clients or [])
    if any(not CLIENT_DIR.fullmatch(c) for c in clients) or len(set(clients)) != len(clients):
        raise SafetyError("Unsichere oder doppelte Clientnamen.")
    mount = Path(mountpoint)
    if mount.is_symlink() or not is_mountpoint(mount):
        raise SafetyError("Workspace darf nur auf einem echten, eigenen Mountpoint angelegt werden.")
    root = mount / ".qpbt"
    _check_not_link(root)
    if is_mountpoint(root):
        raise SafetyError(".qpbt darf kein eigener Mountpoint sein.")
    if not root.exists():
        root.mkdir(mode=0o700)
    if not root.is_dir() or os.stat(root).st_dev != os.stat(mount).st_dev:
        raise SafetyError(f"Reservierter Name .qpbt ist kein sicheres Verzeichnis: {root}")
    run = root / run_id
    run.mkdir(mode=0o700, exist_ok=False)
    marker = {"run_id": run_id, "owner": "qpbt", "schema": "0.2", "clients": clients,
              "nonce": secrets.token_hex(16)}
    (run / "marker.json").write_text(json.dumps(marker), encoding="utf-8")
    (run / "dataset").mkdir(mode=0o700)
    (run / "metadata").mkdir(mode=0o700)
    (run / "probe").mkdir(mode=0o700)
    for client in clients:
        (run / "dataset" / client).mkdir(mode=0o700)
    return run


def read_marker(run: Path) -> dict:
    marker = run / "marker.json"
    if marker.is_symlink() or not marker.is_file():
        raise SafetyError("Marker fehlt oder ist unsicher; automatische Bereinigung verweigert.")
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SafetyError(f"Marker ist defekt: {exc}") from exc


def _check_layout(rel: tuple[str, ...], is_dir: bool, clients: set[str]) -> None:
    """Whitelist of every path the toolkit itself creates inside a run directory."""
    name = rel[-1]
    if len(rel) == 1:
        ok = (name == "marker.json" and not is_dir) or (name in {"dataset", "metadata", "probe"} and is_dir)
    elif len(rel) == 2 and rel[0] == "dataset":
        ok = (is_dir and name in clients) or (not is_dir and _numbered(name, "data."))
    elif len(rel) == 2 and rel[0] == "probe":
        ok = (is_dir and name in clients) or (not is_dir and name == "direct-io.bin")
    elif len(rel) == 2 and rel[0] == "metadata":
        ok = not is_dir and _numbered(name, "m-")
    elif len(rel) == 3 and rel[0] == "dataset" and rel[1] in clients:
        ok = not is_dir and _numbered(name, "data.")
    elif len(rel) == 3 and rel[0] == "probe" and rel[1] in clients:
        ok = not is_dir and name == "direct-io.bin"
    else:
        ok = False
    if not ok:
        area = {"dataset": "im Dataset", "probe": "im Direct-I/O-Probe-Verzeichnis",
                "metadata": "im Metadaten-Test"}.get(rel[0], "im Testverzeichnis")
        raise SafetyError(f"Unbekannte Datei {area}: {'/'.join(rel)} – Bereinigung verweigert.")


def cleanup_workspace(mountpoint: str, run_id: str) -> None:
    mount = Path(mountpoint)
    if mount.is_symlink() or not is_mountpoint(mount):
        raise SafetyError("Bereinigung abgebrochen: Mountpoint ist nicht mehr derselbe Mounttyp.")
    root = mount / ".qpbt"
    run = root / run_id
    for p in (root, run):
        _check_not_link(p)
        if is_mountpoint(p) or os.stat(p).st_dev != os.stat(mount).st_dev:
            raise SafetyError(f"Mountwechsel innerhalb des Workspace: {p}")
    if run.parent != root or run_id in ("", ".", ".."):
        raise SafetyError("Pfadvalidierung fehlgeschlagen.")
    payload = read_marker(run)
    if payload.get("run_id") != run_id or payload.get("owner") != "qpbt" or payload.get("schema") not in ("0.1", "0.2"):
        raise SafetyError("Marker stimmt nicht mit Run-ID überein.")
    clients = payload.get("clients", [])
    if not isinstance(clients, list) or any(not isinstance(c, str) or not CLIENT_DIR.fullmatch(c) for c in clients):
        raise SafetyError("Marker enthält ungültige Clientnamen.")
    # No symlinks/special files and no unexpected names, even deep inside the tree.
    for current, dirs, files in os.walk(run, followlinks=False):
        cp = Path(current)
        for name in dirs + files:
            p = cp / name
            st = p.lstat()
            if is_mountpoint(p):
                raise SafetyError(f"Zusätzlicher Mount im Testbereich: {p}")
            if stat.S_ISLNK(st.st_mode) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise SafetyError(f"Unzulässiger Dateityp in Testdaten: {p}")
            _check_layout(p.relative_to(run).parts, stat.S_ISDIR(st.st_mode), set(clients))
    # Python rmtree on Linux uses fd-based protections against directory symlink races.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise SafetyError("Sichere Bereinigung wird auf diesem Betriebssystem nicht unterstützt.")
    shutil.rmtree(run)
    try:
        root.rmdir()
    except OSError:
        pass  # Other QPBT runs or reserved data; retain .qpbt.
