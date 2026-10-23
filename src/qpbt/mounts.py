"""Read-only, fail-closed inspection of the actual network mountpoints."""
from __future__ import annotations
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path


class PreflightError(RuntimeError):
    pass


SENSITIVE_OPT = re.compile(r"(?i)^(password|pass|credentials|username|user|domain|password2|token|key)=(.+)$")


def scrub_options(options: str) -> str:
    parts = []
    for part in options.split(","):
        if SENSITIVE_OPT.match(part):
            k = part.split("=", 1)[0]
            parts.append(f"{k}=[REDACTED]")
        else:
            parts.append(part)
    return ",".join(parts)


def official_fio_version(binary="fio") -> str:
    exe = shutil.which(binary)
    if not exe:
        raise PreflightError("Flexible I/O Tester fio fehlt. Bitte das Linux-Paket fio installieren.")
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"fio --version konnte nicht ausgeführt werden: {exc}") from exc
    version = out.stdout.strip()
    if out.returncode != 0 or not re.fullmatch(r"fio-\d+(?:\.\d+)*.*", version):
        raise PreflightError(f"'{exe}' ist nicht der Flexible I/O Tester (erhalten: {version!r}).")
    major = int(version[4:].split(".")[0])
    if major < 3:
        raise PreflightError("fio Version 3 oder neuer erforderlich.")
    return version



def _mount_identity(path: Path) -> dict:
    """Match an exact mountinfo mountpoint, not just its filesystem type.

    Linux mount IDs change if the mount disappears and is recreated. A device
    number alone is insufficient for network mounts, so keep both.
    """
    def decode(value: str) -> str:
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)

    match = None
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) > 6 and decode(fields[4]) == str(path):
                    match = {"mount_id": fields[0], "mount_dev": fields[2],
                             "mount_root": decode(fields[3]), "st_dev": os.stat(path).st_dev}
    except OSError as exc:
        raise PreflightError(f"Linux-Mount-ID nicht lesbar: {exc}") from exc
    if match is None:
        raise PreflightError(f"Kein eigenständiger /proc/self/mountinfo-Eintrag für {path}.")
    return match

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
        import json
        mounts = json.loads(proc.stdout)["filesystems"]
        entry = mounts[0]
    except (OSError, ValueError, KeyError, IndexError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"{proto}: findmnt fehlgeschlagen: {exc}") from exc
    mount_target = Path(entry["target"])
    if mount_target != p:
        raise PreflightError(f"{proto}: {p} ist kein eigenständiger Mount (tatsächlich {mount_target}).")
    fstype = entry.get("fstype", "").lower()
    types = {"nfs": {"nfs", "nfs4"}, "smb": {"cifs", "smb3"}}
    if fstype not in types[proto]:
        raise PreflightError(f"{proto}: unerwarteter Dateisystemtyp '{fstype}' – erwartet {types[proto]}.")
    opts = entry.get("options", "")
    version_match = re.search(r"(?:^|,)vers=([^,]+)", opts)
    actual = version_match.group(1) if version_match else None
    if proto == "nfs":
        if actual == "4" and re.search(r"(?:^|,)minorversion=1(?:,|$)", opts):
            actual = "4.1"
        if actual != expected_version:
            raise PreflightError(f"NFS-Version nicht wie erwartet: Ist={actual!r}, Soll={expected_version!r}.")
    else:
        # Exact SMB dialect is included in CIFS mount options on supported Linux kernels.
        aliases = {"3.11": "3.1.1", "3.0.2": "3.02", "3": "3.0"}
        if actual is None:
            raise PreflightError("SMB-Dialekt konnte aus findmnt nicht sicher ermittelt werden.")
        if aliases.get(actual, actual) != aliases.get(expected_version, expected_version):
            raise PreflightError(f"SMB-Dialekt nicht wie erwartet: Ist={actual!r}, Soll={expected_version!r}.")
    source = entry.get("source", "")
    if expected_source and source != expected_source:
        raise PreflightError(f"{proto}: falsche Mount-Quelle: {source!r} statt {expected_source!r}.")
    if not os.access(p, os.R_OK | os.W_OK | os.X_OK):
        raise PreflightError(f"{proto}: Testbenutzer hat nicht genügend Rechte auf {p}.")
    return dict(mountpoint=str(p), fstype=fstype, source=source,
                options=scrub_options(opts), negotiated_version=actual,
                **_mount_identity(p))


def client_info() -> dict:
    nics = []
    sysnet = Path("/sys/class/net")
    if sysnet.exists():
        for p in sorted(sysnet.iterdir()):
            if p.name == "lo":
                continue
            info = {"interface": p.name}
            for key in ("speed", "operstate"):
                try:
                    info[key] = (p / key).read_text().strip()
                except OSError:
                    info[key] = None
            nics.append(info)
    return {"hostname": platform.node(), "kernel": platform.release(),
            "platform": platform.platform(), "cpu_count": os.cpu_count(), "interfaces": nics}


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
    # Both paths point at network FS, but may expose the same Qumulo free-space pool.
    from .profiles import total_dataset_bytes
    needed = 2 * total_dataset_bytes(config) * float(config["safety"]["free_space_multiplier"])
    for proto, ent in mounts.items():
        stat = os.statvfs(ent["mountpoint"])
        free = stat.f_bavail * stat.f_frsize
        ent["available_bytes"] = free
        if free < needed:
            raise PreflightError(f"{proto}: zu wenig freier Speicher: {free / 2**30:.1f} GiB; "
                                 f"für beide Protokolle konservativ gefordert {needed / 2**30:.1f} GiB.")
    return mounts
