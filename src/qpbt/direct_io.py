"""Bounded O_DIRECT read/write/verification only inside owned run/probe."""
from __future__ import annotations
import mmap
import os
from pathlib import Path

class DirectIOError(RuntimeError):
    pass


def check_direct_io(probe_directory: Path) -> dict:
    """Write and verify 4 KiB with O_DIRECT, no pagecache fallback.

    This validates the filesystem path/OS supports O_DIRECT under the chosen
    mount; it does NOT bypass Qumulo's server-side caches or prove durability.
    """
    if not hasattr(os, "O_DIRECT"):
        raise DirectIOError("O_DIRECT wird von diesem Betriebssystem nicht angeboten.")
    if not probe_directory.is_dir() or probe_directory.is_symlink() or list(probe_directory.iterdir()):
        raise DirectIOError("Direct-I/O-Probe benötigt ein neues, leeres QPBT-Probe-Verzeichnis.")
    target = probe_directory / "direct-io.bin"
    fd = None
    expected = (bytes(range(256)) * 16)
    try:
        # Anonymous mmap supplies page-aligned memory, required by Linux O_DIRECT.
        with mmap.mmap(-1, 4096) as aligned:
            aligned[:] = expected
            fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_RDWR |
                         os.O_DIRECT | getattr(os, "O_CLOEXEC", 0), 0o600)
            written = os.write(fd, aligned)
            if written != 4096:
                raise DirectIOError(f"O_DIRECT-Probe: unvollständiger Write ({written}/4096).")
            os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            aligned[:] = b"\x00" * 4096
            received = os.readv(fd, [aligned])
            if received != 4096 or aligned[:] != expected:
                raise DirectIOError("O_DIRECT-Probe: Read-Back-Verifikation fehlgeschlagen.")
        return {"status": "PASS", "bytes": 4096,
                "meaning": "O_DIRECT client I/O supported; server cache/durability not proven"}
    except (OSError, ValueError, BufferError) as exc:
        raise DirectIOError(f"O_DIRECT fehlgeschlagen: {type(exc).__name__}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        # Only our own O_EXCL-created file is removed. Preserve root/unknown files.
        if target.is_file() and not target.is_symlink():
            target.unlink()
