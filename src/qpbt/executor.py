"""Run read-only helper commands locally or on a test client via SSH.

Commands are always given as argv lists. For SSH they are quoted with
shlex.join, so the remote shell never interprets substitutions or operators.
SSH runs with BatchMode (no password prompts) and never forwards agents.
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass


class ExecError(RuntimeError):
    pass


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


class Executor:
    kind = "abstract"

    def __init__(self, name: str):
        self.name = name

    def argv(self, command: list[str]) -> list[str]:
        raise NotImplementedError

    def run(self, command: list[str], timeout: int = 30) -> Result:
        if not command or any(not isinstance(a, str) for a in command):
            raise ExecError("Ungültiger Befehl.")
        try:
            proc = subprocess.run(self.argv(command), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ExecError(f"{self.name}: Zeitlimit {timeout}s für {command[0]} überschritten.") from exc
        except OSError as exc:
            raise ExecError(f"{self.name}: {command[0]} nicht ausführbar: {exc}") from exc
        if self.kind == "ssh" and proc.returncode == 255:
            raise ExecError(f"{self.name}: SSH-Verbindung fehlgeschlagen: {proc.stderr.strip()[-300:]}")
        return Result(proc.returncode, proc.stdout, proc.stderr)

    def check(self, command: list[str], timeout: int = 30) -> str:
        res = self.run(command, timeout)
        if res.returncode != 0:
            raise ExecError(f"{self.name}: '{command[0]}' Exitcode {res.returncode}: {res.stderr.strip()[-300:]}")
        return res.stdout

    def describe(self) -> str:
        return self.kind


class LocalExecutor(Executor):
    kind = "local"

    def argv(self, command: list[str]) -> list[str]:
        return list(command)


class SSHExecutor(Executor):
    kind = "ssh"

    def __init__(self, name: str, target: str, connect_timeout: int = 10):
        super().__init__(name)
        if not target or target.startswith("-"):
            raise ExecError("Ungültiges SSH-Ziel.")
        self.target = target
        self.connect_timeout = connect_timeout

    def argv(self, command: list[str]) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}",
                "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
                "-T", self.target, "--", shlex.join(command)]

    def describe(self) -> str:
        return f"ssh:{self.target}"


def executor_for(client: dict, connect_timeout: int = 10) -> Executor:
    if client.get("ssh") == "local":
        return LocalExecutor(client["name"])
    return SSHExecutor(client["name"], client["ssh"], connect_timeout)
