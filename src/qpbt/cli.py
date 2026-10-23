"""End user commands. Live writes require the --confirm-write switch."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from .config import load_config, ConfigError
from .runner import inspect, plan, run_real, run_simulated, output_base
from .reporting import load_json, compare_runs, create_reports
from .mounts import PreflightError
from .safety import SafetyError
from .fio_engine import FioError
from .direct_io import DirectIOError
from .doctor import inspect_host
from .validation import validate_run


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qpbt", description="Qumulo Performance Baseline Toolkit (technischer Prototyp v0.2)")
    subs = p.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor", help="Read-only Host-Prüfung ohne Qumulo-Verbindung")
    val = subs.add_parser("validate-run", help="Gespeicherten Ergebnisdatensatz offline validieren")
    val.add_argument("result", type=Path)
    for name, helptext in (("plan", "Testmatrix und Platzbedarf anzeigen (ohne Zugriff)"),
                           ("preflight", "Mounts, fio und Qumulo-API read-only prüfen"),
                           ("simulate", "Als Simulation gekennzeichneten Demo-Bericht erzeugen"),
                           ("run", "Live NFS- und SMB-Benchmark ausführen"),
                           ("status", "Gespeicherten Lauf anzeigen"),
                           ("report", "Bericht eines gespeicherten Laufs erzeugen")):
        sub = subs.add_parser(name, help=helptext)
        sub.add_argument("--config", default="qpbt.yaml", help="Konfigurationsdatei")
        if name == "run":
            sub.add_argument("--confirm-write", action="store_true", help="Explizite Freigabe für Schreibtests auf beiden Mounts")
        if name in ("status", "report"):
            sub.add_argument("--run-id", required=True)
    compare = subs.add_parser("compare", help="Zwei Baselines gegenüberstellen")
    compare.add_argument("result_a", type=Path, help="Erste result.json")
    compare.add_argument("result_b", type=Path, help="Zweite result.json")
    compare.add_argument("--out", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = inspect_host()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["passed"] else 2
        if args.command == "validate-run":
            result = validate_run(load_json(args.result))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["valid"] else 2
        if args.command == "compare":
            result = compare_runs(load_json(args.result_a), load_json(args.result_b))
            content = json.dumps(result, ensure_ascii=False, indent=2)
            if args.out:
                args.out.write_text(content + "\n", encoding="utf-8")
            else:
                print(content)
            return 0
        config = load_config(args.config)
        if args.command == "plan":
            print(json.dumps(plan(config), indent=2, ensure_ascii=False))
        elif args.command == "preflight":
            print(json.dumps(inspect(config), indent=2, ensure_ascii=False))
        elif args.command == "run":
            if not args.confirm_write:
                raise PreflightError("Live-Tests schreiben Testdateien! Bitte --confirm-write nur nach Prüfung der Mounts setzen.")
            out, run = run_real(config)
            print(f"Ergebnis: {run['status']}\nReport: {out / 'report.html'}\nRohdaten: {out / 'result.json'}")
            return 0 if run["status"] in ("PASS", "WARNING") else 1
        elif args.command == "simulate":
            out, run = run_simulated(config)
            print(f"{run['status']}: {out}\nPDF: {out / 'report.pdf'}\nHTML: {out / 'report.html'}")
        elif args.command in ("status", "report"):
            if not args.run_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in args.run_id):
                raise ValueError("Ungültige Run-ID.")
            out = output_base(config) / args.run_id
            data = load_json(out / "result.json")
            if args.command == "status":
                print(json.dumps({k: data.get(k) for k in ("run_id", "status", "created_at", "finished_at", "warnings", "errors")}, indent=2, ensure_ascii=False))
            else:
                create_reports(data, out, config["report"]["formats"])
                print(f"Berichte geschrieben nach: {out}")
        return 0
    except (ConfigError, PreflightError, SafetyError, FioError, DirectIOError, OSError, ValueError, KeyError) as exc:
        print(f"QPBT-Fehler: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
