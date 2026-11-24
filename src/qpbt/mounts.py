"""Read-only, fail-closed inspection of the actual network mountpoints."""
from __future__ import annotations
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath


class PreflightError(RuntimeError):
    pass


SENSITIVE_OPT = re.compile(r"(?i)^(password|pass|credentials|username|user|domain|password2|token|key)=(.+)$")
FSTYPES = {"nfs": {"nfs", "nfs4"}, "smb": {"cifs", "smb3"}}


def scrub_options(options: str) -> str:
    parts = []
    for part in options.split(","):
        if SENSITIVE_OPT.match(part):
            k = part.split("=", 1)[0]
            parts.append(f"{k}=[REDACTED]")
        else:
            parts.append(part)
    return ",".join(parts)


def parse_fio_version(version: str, exe: str = "fio") -> str:
    version = version.strip()
    if not re.fullmatch(r"fio-\d+(?:\.\d+)*.*", version):
        raise PreflightError(f"'{exe}' ist nicht der Flexible I/O Tester (erhalten: {version!r}).")
    if int(version[4:].split(".")[0]) < 3:
        raise PreflightError("fio Version 3 oder neuer erforderlich.")
    return version


def official_fio_version(binary="fio") -> str:
    exe = shutil.which(binary)
    if not exe:
        raise PreflightError("Flexible I/O Tester fio fehlt. Bitte das Linux-Paket fio installieren.")
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"fio --version konnte nicht ausgeführt werden: {exc}") from exc
    if out.returncode != 0:
        raise PreflightError(f"'{exe}' ist nicht der Flexible I/O Tester (Exitcode {out.returncode}).")
    return parse_fio_version(out.stdout, exe)


def _decode(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


def parse_mountinfo(text: str, path: str) -> dict | None:
    """Last exact mountinfo entry for a mountpoint (the one visible on top)."""
    match = None
    for line in text.splitlines():
        fields = line.split()
        if len(fields) > 6 and _decode(fields[4]) == str(path):
            match = {"mount_id": fields[0], "mount_dev": fields[2], "mount_root": _decode(fields[3])}
    return match


def _mount_identity(path: Path) -> dict:
    """Match an exact mountinfo mountpoint, not just its filesystem type.

    Linux mount IDs change if the mount disappears and is recreated. A device
    number alone is insufficient for network mounts, so keep both.
    """
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        match = parse_mountinfo(text, str(path))
        if match is not None:
            match["st_dev"] = os.stat(path).st_dev
    except OSError as exc:
        raise PreflightError(f"Linux-Mount-ID nicht lesbar: {exc}") from exc
    if match is None:
        raise PreflightError(f"Kein eigenständiger /proc/self/mountinfo-Eintrag für {path}.")
    return match


def mount_address(options: str) -> str | None:
    """Server address the kernel actually connected to (NFS/CIFS addr= option)."""
    m = re.search(r"(?:^|,)addr=([^,]+)", options or "")
    return m.group(1) if m else None


def evaluate_findmnt(entry: dict, path: str, proto: str, expected_version: str,
                     expected_source: str | None, who: str = "") -> dict:
    """Protocol, version and source checks shared by local and remote clients."""
    prefix = f"{who}: " if who else ""
    if str(PurePosixPath(entry.get("target", ""))) != str(PurePosixPath(path)):
        raise PreflightError(f"{prefix}{proto}: {path} ist kein eigenständiger Mount (tatsächlich {entry.get('target')}).")
    fstype = entry.get("fstype", "").lower()
    if fstype not in FSTYPES[proto]:
        raise PreflightError(f"{prefix}{proto}: unerwarteter Dateisystemtyp '{fstype}' – erwartet {FSTYPES[proto]}.")
    opts = entry.get("options", "") or ""
    version_match = re.search(r"(?:^|,)vers=([^,]+)", opts)
    actual = version_match.group(1) if version_match else None
    if proto == "nfs":
        if actual == "4" and re.search(r"(?:^|,)minorversion=1(?:,|$)", opts):
            actual = "4.1"
        if actual != expected_version:
            raise PreflightError(f"{prefix}NFS-Version nicht wie erwartet: Ist={actual!r}, Soll={expected_version!r}.")
    else:
        # Exact SMB dialect is included in CIFS mount options on supported Linux kernels.
        aliases = {"3.11": "3.1.1", "3.0.2": "3.02", "3": "3.0"}
        if actual is None:
            raise PreflightError(f"{prefix}SMB-Dialekt konnte aus findmnt nicht sicher ermittelt werden.")
        if aliases.get(actual, actual) != aliases.get(expected_version, expected_version):
            raise PreflightError(f"{prefix}SMB-Dialekt nicht wie erwartet: Ist={actual!r}, Soll={expected_version!r}.")
    source = entry.get("source", "")
    if expected_source and source != expected_source:
        raise PreflightError(f"{prefix}{proto}: falsche Mount-Quelle: {source!r} statt {expected_source!r}.")
    return dict(mountpoint=str(path), fstype=fstype, source=source,
                options=scrub_options(opts), negotiated_version=actual,
                server_address=mount_address(opts))


def mounted_target(path: str, proto: str, expected_version: str, expected_source: str | None = None) -> dict:
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise PreflightError(f"{proto}: Mountpoint existiert nicht: {p}")
    if p.is_symlink() or p.resolve() != p.absolute():
        raise PreflightError(f"{proto}: Mountpoint darf keine Symlinks enthalten: {p}")
    if p == Path("/"):
        raise PreflightError("Root-Filesystem darf niemals als Benchmarkziel verwendet werden.")
    try:
        proc = subprocess.run(["findmnt", "--json", "--target", str(p),
                               "--output", "TARGET,FSTYPE,SOURCE,OPTIONS"],
                              capture_output=True, text=True, timeout=8, check=True)
        entry = json.loads(proc.stdout)["filesystems"][0]
    except (OSError, ValueError, KeyError, IndexError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"{proto}: findmnt fehlgeschlagen: {exc}") from exc
    result = evaluate_findmnt(entry, str(p), proto, expected_version, expected_source)
    if not os.access(p, os.R_OK | os.W_OK | os.X_OK):
        raise PreflightError(f"{proto}: Testbenutzer hat nicht genügend Rechte auf {p}.")
    return {**result, **_mount_identity(p)}


def remote_mounted_target(executor, path: str, proto: str, expected_version: str,
                          expected_source: str | None) -> dict:
    """Same checks as mounted_target(), executed read-only on a test client."""
    from .executor import ExecError
    who = executor.name
    p = PurePosixPath(path)
    if not p.is_absolute() or str(p) == "/" or ".." in p.parts:
        raise PreflightError(f"{who}: {proto}: ungültiger Mountpoint {path}")
    try:
        raw = executor.check(["findmnt", "--json", "--target", str(p), "--output",
                              "TARGET,FSTYPE,SOURCE,OPTIONS"], timeout=20)
        entry = json.loads(raw)["filesystems"][0]
        result = evaluate_findmnt(entry, str(p), proto, expected_version, expected_source, who)
        identity = parse_mountinfo(executor.check(["cat", "/proc/self/mountinfo"], timeout=20), str(p))
        if identity is None:
            raise PreflightError(f"{who}: kein eigenständiger mountinfo-Eintrag für {p}.")
        identity["st_dev"] = int(executor.check(["stat", "-c", "%d", "--", str(p)], timeout=20).strip())
        if executor.run(["test", "-r", str(p), "-a", "-w", str(p), "-a", "-x", str(p)], timeout=20).returncode != 0:
            raise PreflightError(f"{who}: {proto}: Testbenutzer hat nicht genügend Rechte auf {p}.")
    except (ExecError, ValueError, KeyError, IndexError) as exc:
        raise PreflightError(f"{who}: {proto}: Mountprüfung fehlgeschlagen: {exc}") from exc
    return {**result, **identity}


def client_info() -> dict:
    nics = []
    sysnet = Path("/sys/class/net")
    if sysnet.exists():
        for p in sorted(sysnet.iterdir()):
            if p.name == "lo":
                continue
            info = {"interface": p.name}
            for key in ("speed", "operstate", "mtu"):
                try:
                    info[key] = (p / key).read_text().strip()
                except OSError:
                    info[key] = None
            nics.append(info)
    return {"hostname": platform.node(), "kernel": platform.release(),
            "platform": platform.platform(), "cpu_count": os.cpu_count(), "interfaces": nics}


def remote_client_info(executor) -> dict:
    from .executor import ExecError
    info = {"name": executor.name, "access": executor.describe()}
    for key, cmd in (("hostname", ["uname", "-n"]), ("kernel", ["uname", "-r"]), ("cpu_count", ["nproc"])):
        try:
            info[key] = executor.check(cmd, timeout=20).strip()[:200]
        except ExecError as exc:
            info[key] = None
            info.setdefault("errors", []).append(str(exc)[:200])
    return info


def free_space_needed(config: dict) -> float:
    from .profiles import total_dataset_bytes
    # Both protocols may expose the same Qumulo free-space pool: be conservative.
    return 2 * total_dataset_bytes(config) * float(config["safety"]["free_space_multiplier"])


def preflight_mounts(config: dict) -> dict:
    mounts = {}
    for proto in ("nfs", "smb"):
        tgt = config["targets"][proto]
        if not tgt.get("expected_source"):
            raise PreflightError(
                f"{proto}: expected_source muss für Live-Tests den EXAKTEN "
                "findmnt-SOURCE-Wert enthalten. Erst 'findmnt -T <mountpoint> -o SOURCE' prüfen."
            )
        mounts[proto] = mounted_target(
            tgt["mountpoint"], proto,
            str(tgt.get("expected_version" if proto == "nfs" else "expected_dialect", "4.1" if proto == "nfs" else "3.1.1")),
            tgt.get("expected_source"))
    needed = free_space_needed(config)
    for proto, ent in mounts.items():
        stat = os.statvfs(ent["mountpoint"])
        free = stat.f_bavail * stat.f_frsize
        ent["available_bytes"] = free
        if free < needed:
            raise PreflightError(f"{proto}: zu wenig freier Speicher: {free / 2**30:.1f} GiB; "
                                 f"für beide Protokolle konservativ gefordert {needed / 2**30:.1f} GiB.")
    return mounts


def expected_version_for(config: dict, proto: str) -> str:
    t = config["targets"][proto]
    return str(t.get("expected_version", "4.1")) if proto == "nfs" else str(t.get("expected_dialect", "3.1.1"))
