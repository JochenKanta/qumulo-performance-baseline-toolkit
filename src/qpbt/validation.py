"""Postrun quality gate. Never classify simulated results as real evidence."""
from __future__ import annotations

EXPECTED = {(proto, f"T{n:02}") for proto in ("nfs", "smb") for n in range(1, 11)}


def validate_run(report: dict) -> dict:
    errors = []
    warnings = []
    if report.get("simulated"):
        errors.append("Simulation: keine Qumulo-Messung/Installationsabnahme.")
    if report.get("status") not in ("PASS", "WARNING"):
        errors.append(f"Laufstatus: {report.get('status', '<fehlt>')}.")
    seen = set()
    for result in report.get("tests", []):
        key = (result.get("protocol"), result.get("test_id"))
        if key in seen:
            errors.append(f"Doppelter Testfall: {key}.")
        seen.add(key)
        if result.get("status") != "PASS":
            errors.append(f"Test fehlgeschlagen: {key}.")
        if key[1] != "T10" and result.get("measure_kind") != "fio":
            if not report.get("simulated"):
                errors.append(f"Kein fio-Ergebnis für {key}.")
    for key in sorted(EXPECTED - seen):
        errors.append(f"Fehlender Testfall: {key}.")
    for key in seen - EXPECTED:
        warnings.append(f"Unbekannter Testfall: {key}.")
    if not report.get("profile_hash"):
        errors.append("Profilfingerprint fehlt.")
    for proto in ("nfs", "smb"):
        if not report.get("simulated") and report.get("direct_io_probes", {}).get(proto, {}).get("status") != "PASS":
            errors.append(f"Direct-I/O-Probe für {proto} fehlt oder ist fehlgeschlagen.")
    if not report.get("simulated") and not all(report.get("cluster_info", {}).get(k) for k in ("name", "uuid", "version")):
        errors.append("Clusteridentität unvollständig.")
    if report.get("warnings"):
        warnings.extend(report["warnings"])
    return {"run_id": report.get("run_id"), "valid": not errors, "errors": errors,
            "warnings": warnings, "expected_cases": len(EXPECTED),
            "present_cases": len(seen), "assessment": "LAB_NOT_VALIDATED" if not errors else "INVALID"}
