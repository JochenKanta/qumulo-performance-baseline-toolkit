"""Static local reports; all outside inputs are HTML/PDF escaped."""
from __future__ import annotations
import html
import json
from pathlib import Path


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def numbers(test: dict) -> tuple[str, str, str, str]:
    if test.get("status") != "PASS":
        return ("—", "—", "—", "—")
    if test.get("test_id") == "T10":
        return ("—", f"{test.get('create_ops_per_s', 0):,.0f} create/s", "—", "—")
    read, write = test.get("read", {}), test.get("write", {})
    mbps = (read.get("bytes_per_second", 0) + write.get("bytes_per_second", 0)) / 1e6
    iops = read.get("iops", 0) + write.get("iops", 0)
    direction = read if read.get("io_bytes", 0) >= write.get("io_bytes", 0) else write
    def lat(key):
        val = direction.get(key)
        return f"{val:.3f}" if val is not None else "—"
    return (f"{mbps:,.1f}", f"{iops:,.0f}", lat("p95_latency_ms"), lat("p99_latency_ms"))


CLASSIFICATION_TEXT = {
    "OVER_CACHE": "Datensatz >= 2x Cluster-Cache: Lesetests erreichen überwiegend den persistenten Speicher.",
    "PARTIALLY_CACHEABLE": "Datensatz größer als der Cluster-Cache, aber < 2x: Lesewerte teilweise cachegestützt.",
    "FITS_IN_CACHE": "Datensatz passt in den Cluster-Cache: Lesewerte sind Cache-Werte, keine Disk-Werte.",
    "UNKNOWN": "Cluster-Cache nicht konfiguriert: Cache-Anteil der Lesewerte unbekannt.",
}


def _gbit(value) -> str:
    return f"{value / 1e9:,.1f} Gbit/s" if value else "—"


def extra_cells(test: dict) -> tuple[str, str]:
    clients = test.get("client_count") or (1 if test.get("measure_kind") == "fio" else "—")
    if test.get("executed_on") == "controller":
        clients = "Controller"
    util = test.get("network_utilization_pct")
    return (str(clients), f"{util:.0f} %" if isinstance(util, (int, float)) else "—")


def context_sections(run: dict) -> list[tuple[str, list[tuple[str, str]], list[list[str]] | None]]:
    """Structured (title, key/values, table) blocks shared by HTML and PDF."""
    sections = []
    sz = run.get("sizing") or {}
    if sz:
        kv = [("Modus", str(sz.get("mode"))), ("Clients x Worker", f"{sz.get('clients')} x {sz.get('workers_per_client')}"),
              ("MiB pro Worker", str(sz.get("dataset_mib_per_worker"))),
              ("Datensatz je Protokoll", f"{sz.get('dataset_gib_per_protocol')} GiB"),
              ("Cluster-Cache (konfiguriert)", f"{sz['cluster_cache_gib']} GiB ({sz.get('node_count')} Nodes x {sz.get('cache_gib_per_node')} GiB)"
               if sz.get("cluster_cache_gib") else "nicht angegeben"),
              ("Datensatz / Cache", str(sz.get("dataset_to_cache_ratio") or "—")),
              ("Einordnung", f"{sz.get('classification')}: {CLASSIFICATION_TEXT.get(sz.get('classification'), '')}")]
        sections.append(("Datensatz und Cache-Einordnung", kv, None))
    net = run.get("network") or {}
    if net.get("clients"):
        rows = [["Client", "Ziel", "Loss", "RTT avg ms", "MTU-Pfad", "Interface", "Link", "IF-MTU"]]
        for c in net["clients"]:
            for host, t in c.get("targets", {}).items():
                dev = t.get("route", {}).get("dev")
                nic = c.get("interfaces", {}).get(dev, {}) if dev else {}
                mtu = t.get("mtu_path")
                rows.append([c.get("client", ""), host,
                             f"{t.get('ping', {}).get('loss_pct')} %" if t.get("ping", {}).get("loss_pct") is not None else "—",
                             str(t.get("ping", {}).get("rtt_avg_ms") or "—"),
                             ("OK" if mtu.get("ok") else "FEHLER") + f" ({mtu.get('mtu')})" if mtu else "nicht geprüft",
                             dev or "—", f"{nic['speed_mbps'] / 1000:g} Gbit/s" if nic.get("speed_mbps") else "—",
                             str(nic.get("mtu") or "—")])
            for m in c.get("iperf3", []):
                rows.append([c.get("client", ""), "iperf3 " + m.get("direction", ""), "", "", "", "",
                             _gbit(m.get("bits_per_second")) if not m.get("error") else "Fehler", ""])
        kv = [("Status", str(net.get("status"))),
              ("Summe Client-Links", _gbit(net.get("client_link_total_bits_per_second"))),
              ("Summe Qumulo-Front-End-Links", _gbit((net.get("cluster_links") or {}).get("total_frontend_bits_per_second"))),
              ("Netzwerk-Obergrenze", f"{net['network_ceiling_bytes_per_second'] / 1e6:,.0f} MB/s"
               if net.get("network_ceiling_bytes_per_second") else "—")]
        kv += [(f"Befund {i['severity']}", f"{i['client']}: {i['detail']}") for i in net.get("issues", [])]
        sections.append(("Netzwerk-Vorabtest", kv, rows))
    elif net.get("status"):
        sections.append(("Netzwerk-Vorabtest", [("Status", str(net.get("status")))], None))
    clients = run.get("clients") or {}
    if clients:
        rows = [["Client", "fio-Server", "Host / Kernel", "fio", "NFS-Adresse", "SMB-Adresse", "NTP"]]
        for name, c in clients.items():
            host = c.get("host", {})
            rows.append([name, c.get("fio_server", ""), f"{host.get('hostname') or '—'} / {host.get('kernel') or '—'}",
                         c.get("fio_version", ""), str(c.get("mounts", {}).get("nfs", {}).get("server_address") or "—"),
                         str(c.get("mounts", {}).get("smb", {}).get("server_address") or "—"),
                         {True: "ja", False: "nein"}.get(c.get("ntp_synchronized"), "unbekannt")])
        dist = run.get("node_distribution", {}).get("per_protocol", {})
        kv = [(f"Verteilung {proto.upper()}", ", ".join(f"{addr}: {', '.join(names)}" for addr, names in m.items()))
              for proto, m in dist.items()]
        kv.append(("Single-Stream-Tests", "T01/T02/T05/T06 auf dem ersten Client, über den gesamten Datensatz"))
        kv.append(("Latenz-Perzentile", "Mehrclient-Tests: schlechtester Client (fio liefert keine aggregierten Perzentile)"))
        sections.append(("Testclients und Node-Verteilung", kv, rows))
    return sections


def write_html(run: dict, path: Path):
    h = html.escape
    simulation = bool(run.get("simulated"))
    title = "QPBT Baseline Report" + (" | SIMULATION - KEINE MESSUNG" if simulation else "")
    rows = []
    for t in run.get("tests", []):
        vals = numbers(t)
        rows.append("<tr>" + "".join(f"<td>{h(str(s))}</td>" for s in
            (t.get("protocol", ""), t.get("test_id", ""), t.get("name", ""), t.get("status", ""), *vals,
             *extra_cells(t))) + "</tr>")
    blocks = []
    for title_, kv, table in context_sections(run):
        block = f"<h2>{h(title_)}</h2><dl>" + "".join(f"<dt>{h(k)}</dt><dd>{h(v)}</dd>" for k, v in kv) + "</dl>"
        if table:
            block += ("<table><thead><tr>" + "".join(f"<th>{h(c)}</th>" for c in table[0]) + "</tr></thead><tbody>"
                      + "".join("<tr>" + "".join(f"<td>{h(c)}</td>" for c in r) + "</tr>" for r in table[1:])
                      + "</tbody></table>")
        blocks.append(block)
    problems = "".join(f"<li>{h(str(s))}</li>" for s in run.get("errors", []) + run.get("warnings", []))
    cluster_info = run.get("cluster_info", {})
    attrs = [
        ("Run-ID", run.get("run_id", "")), ("Profile", run.get("profile", "")),
        ("Status", run.get("status", "")), ("Start (UTC)", run.get("created_at", "")),
        ("Ende (UTC)", run.get("finished_at", "")),
        ("Cluster", cluster_info.get("name", "nicht ermittelt")),
        ("Qumulo Core", cluster_info.get("version", "nicht ermittelt")),
        ("Client", run.get("client", {}).get("hostname", "")),
        ("fio", run.get("fio_version", "nicht ermittelt")),
        ("Profil-Hash", run.get("profile_hash", "")),
        ("O_DIRECT NFS", run.get("direct_io_probes", {}).get("nfs", {}).get("status", "nicht durchgeführt")),
        ("O_DIRECT SMB", run.get("direct_io_probes", {}).get("smb", {}).get("status", "nicht durchgeführt")),
        ("Telemetrie", run.get("telemetry_availability", "unbekannt")),
        ("Modus", {"multi_client": f"Mehrclient ({len(run.get('clients', {}))} Clients)"}.get(run.get("mode"), "Einzelclient")),
        ("Netzwerk-Vorabtest", (run.get("network") or {}).get("status", "nicht durchgeführt")),
        ("Datensatz", (run.get("sizing") or {}).get("classification", "unbekannt")),
    ]
    dl = "".join(f"<dt>{h(k)}</dt><dd>{h(str(v or '—'))}</dd>" for k,v in attrs)
    watermark = '<div class="simulation">SIMULATION - KEINE ECHTEN QUMULO-MESSWERTE</div>' if simulation else ""
    page = f'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{h(title)}</title>
<style>
:root{{--teal:#007c86;--border:#d8e3e8;--bg:#f5f8fa;--text:#193444}}
*{{box-sizing:border-box}}body{{font-family:system-ui,Arial,sans-serif;color:var(--text);background:var(--bg);margin:0;line-height:1.5}}
main{{max-width:1100px;margin:36px auto;padding:28px 36px;background:white;border-radius:16px;box-shadow:0 8px 30px #102e4314}}
h1{{letter-spacing:-.02em;margin:0}}.eyebrow{{font-size:12px;color:var(--teal);font-weight:700;letter-spacing:.12em}}
p.note{{font-size:13px;color:#53707c}}dl{{display:grid;grid-template-columns:160px 1fr;gap:4px 14px;background:#f6fafb;padding:18px;border-radius:10px}}
dt{{color:#627580}}dd{{margin:0;font-weight:600;overflow-wrap:anywhere}}h2{{margin-top:30px}}table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;background:#edf6f7;color:#174351}}td,th{{padding:10px;border-bottom:1px solid var(--border)}}tr:nth-child(even){{background:#fafdfe}}
.simulation{{padding:13px;margin:20px 0;background:#ffe4bf;color:#633609;border:2px solid #e5a950;font-weight:800;text-align:center}}
footer{{margin-top:35px;font-size:12px;color:#69808b}}@media(max-width:800px){{main{{margin:0;padding:16px;overflow:auto}}dl{{grid-template-columns:110px 1fr}}table{{min-width:780px}}}}
@media print{{body{{background:white}}main{{margin:0;box-shadow:none}}}}
</style></head><body><main><div class="eyebrow">QUMULO PERFORMANCE BASELINE TOOLKIT / 0.3 PROTOTYPE</div><h1>{h(title)}</h1>{watermark}
<p>Versioniertes Installationsprofil für getrennte, sequentielle NFS- und SMB-Messungen.</p><dl>{dl}</dl>
<h2>Testresultate</h2><table><thead><tr><th>Protokoll</th><th>ID</th><th>Workload</th><th>Status</th><th>MB/s</th><th>IOPS / Meta</th><th>P95 ms</th><th>P99 ms</th><th>Clients</th><th>Netz-Auslastung</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
{''.join(blocks)}
<h2>Hinweise / Fehler</h2><ul>{problems or '<li>Keine gemeldeten Fehler oder Warnungen.</li>'}</ul>
<p class="note">Durchsatz: dezimale MB/s (10^6 Bytes/s). Ein PASS bedeutet technische Testausführung, <strong>nicht</strong> die Einhaltung eines Qumulo-Performance-SLA. Metadata-Stat kann vom Linux-Clientcache profitieren. Netz-Auslastung = Durchsatz / min(Summe Client-Links, Summe Qumulo-Front-End-Links). Qumulo-Cache und bestätigt persistente Schreibvorgänge werden in diesem Prototyp nicht isoliert nachgewiesen.</p>
<footer>QPBT prototypischer Installations-Baseline-Bericht. Kein Nachweis der maximalen Cluster-Aggregatleistung. Rohdaten und Telemetrie befinden sich im Run-Verzeichnis.</footer>
</main></body></html>'''
    path.write_text(page, encoding="utf-8")


def write_pdf(run: dict, path: Path):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, KeepTogether
    from reportlab.lib.colors import HexColor
    esc = html.escape
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="QTitle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=18,
                              leading=23, spaceAfter=10, textColor=HexColor("#154453")))
    styles.add(ParagraphStyle(name="QSmall", parent=styles["Normal"], fontSize=8.5, leading=12))
    styles.add(ParagraphStyle(name="QCell", parent=styles["Normal"], fontSize=7.5, leading=9.5))
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=14*mm, bottomMargin=14*mm)
    story = [Paragraph("QUMULO PERFORMANCE BASELINE TOOLKIT", styles["QSmall"]),
             Paragraph("Installation Baseline - Profil v0.2 (QPBT 0.3)", styles["QTitle"])]
    if run.get("simulated"):
        story.append(Paragraph('<font color="red"><b>SIMULATION - KEINE ECHTEN MESSWERTE</b></font>', styles["Heading2"]))
    meta = [("Run ID", run.get("run_id", "")), ("Status", run.get("status", "")),
            ("Profil", run.get("profile", "")),
            ("Qumulo", run.get("cluster_info", {}).get("name", "n/a")),
            ("Version", run.get("cluster_info", {}).get("version", "n/a")),
            ("Client", run.get("client", {}).get("hostname", "")),
            ("Start UTC", run.get("created_at", "")), ("End UTC", run.get("finished_at", ""))]
    for i in range(0, len(meta), 2):
        story.append(Paragraph(" | ".join(f"<b>{esc(k)}:</b> {esc(str(v))}" for k,v in meta[i:i+2]), styles["QSmall"]))
    story += [Spacer(1, 6*mm)]
    data = [["Proto", "Test", "Workload", "Status", "MB/s", "IOPS / Meta", "P95 ms", "P99 ms", "Clients", "Netz"]]
    for t in run.get("tests", []):
        values = numbers(t)
        data.append([str(t.get("protocol", "")), str(t.get("test_id", "")), str(t.get("name", "")),
                     str(t.get("status", "")), *values, *extra_cells(t)])
    cells = [[Paragraph(esc(v), styles["QCell"]) for v in row] for row in data]
    grid = Table(cells, repeatRows=1, colWidths=[18*mm, 13*mm, 58*mm, 20*mm, 24*mm, 30*mm, 22*mm, 22*mm, 20*mm, 20*mm])
    grid.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), HexColor("#dbecef")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, HexColor("#f4f9fa")]),
        ("LINEBELOW", (0,0), (-1,-1), .2, HexColor("#c8d8df")),
        ("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 7),
        ("RIGHTPADDING", (0,0), (-1,-1), 5),
        ("TOPPADDING", (0,0), (-1,-1), 1.5), ("BOTTOMPADDING", (0,0), (-1,-1), 1.5),
    ]))
    story += [grid]
    for title_, kv, table in context_sections(run):
        story += [Spacer(1, 5*mm), Paragraph(esc(title_), styles["Heading3"])]
        for k, v in kv:
            story.append(Paragraph(f"<b>{esc(k)}:</b> {esc(v)}", styles["QSmall"]))
        if table:
            widths = [267*mm / len(table[0])] * len(table[0])
            sub = Table([[Paragraph(esc(c), styles["QCell"]) for c in r] for r in table], repeatRows=1, colWidths=widths)
            sub.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), HexColor("#dbecef")),
                                     ("LINEBELOW", (0,0), (-1,-1), .2, HexColor("#c8d8df")),
                                     ("VALIGN", (0,0), (-1,-1), "TOP")]))
            story += [Spacer(1, 2*mm), sub]
    story += [Spacer(1, 6*mm), Paragraph("Warnungen und Fehler", styles["Heading3"])]
    issues = run.get("errors", []) + run.get("warnings", [])
    if not issues:
        story.append(Paragraph("Keine gemeldeten Warnungen oder Fehler.", styles["QSmall"]))
    else:
        for item in issues[:25]:
            story.append(Paragraph("- " + esc(str(item)), styles["QSmall"]))
    story.append(Spacer(1, 6*mm))
    story.append(Paragraph("PASS zeigt die erfolgreiche Ausfuehrung des Testfalls an, keine Performance-SLA. "
                           "Die Tests sind keine Aussage zur maximalen Qumulo-Clusterleistung oder zu serverseitigen Cold-Cache-Messungen.", styles["QSmall"]))
    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(HexColor("#56727c"))
        canvas.drawString(15*mm, 9*mm, "QPBT 0.3 technical prototype")
        canvas.drawRightString(282*mm, 9*mm, f"Seite {document.page}")
        canvas.restoreState()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def create_reports(run: dict, outdir: Path, formats: list[str]):
    save_json(outdir / "result.json", run)
    if "html" in formats:
        write_html(run, outdir / "report.html")
    if "pdf" in formats:
        write_pdf(run, outdir / "report.pdf")


def compare_runs(one: dict, two: dict) -> dict:
    first = {(t.get("protocol"), t.get("test_id")): t for t in one.get("tests", []) if t.get("status") == "PASS"}
    second = {(t.get("protocol"), t.get("test_id")): t for t in two.get("tests", []) if t.get("status") == "PASS"}
    warnings = []
    if one.get("simulated") or two.get("simulated"):
        warnings.append("Mindestens eine Baseline ist simuliert.")
    if (one.get("sizing") or {}).get("clients") != (two.get("sizing") or {}).get("clients"):
        warnings.append("Unterschiedliche Anzahl Testclients - Ergebnisse nicht direkt vergleichbar.")
    for name in ("profile", "profile_hash", "fio_version", "tool_version"):
        if one.get(name) != two.get(name):
            warnings.append(f"Abweichung in {name} - Ergebnisse nicht direkt vergleichbar.")
    a_uuid = one.get("cluster_info", {}).get("uuid")
    b_uuid = two.get("cluster_info", {}).get("uuid")
    if a_uuid and b_uuid and a_uuid != b_uuid:
        warnings.append("Unterschiedliche Qumulo-Cluster-UUIDs; Ergebnisse nicht direkt vergleichbar.")
    if one.get("client") != two.get("client"):
        warnings.append("Unterschiedliche Client-Hardware oder Software; Vergleich nur eingeschränkt.")
    # mount_id/st_dev/free_bytes are transient. Only semantic mount properties
    # should decide whether profiles are comparable after remount/reboot.
    def comparable_mounts(run):
        fields = ("mountpoint", "fstype", "source", "negotiated_version", "options")
        return {p: {k: value.get(k) for k in fields} for p, value in run.get("mounts", {}).items()}
    if comparable_mounts(one) != comparable_mounts(two):
        warnings.append("Abweichende Mount-Konfiguration; Vergleich nur eingeschränkt.")
    rows = []
    for key in sorted(set(first) & set(second)):
        a, b = first[key], second[key]
        if key[1] == "T10":
            va, vb, unit = a.get("create_ops_per_s", 0), b.get("create_ops_per_s", 0), "create/s"
        else:
            va = a.get("read", {}).get("iops", 0) + a.get("write", {}).get("iops", 0)
            vb = b.get("read", {}).get("iops", 0) + b.get("write", {}).get("iops", 0)
            unit = "IOPS" if key[1] in {"T05", "T06", "T07", "T08", "T09"} else "MB/s"
            if unit == "MB/s":
                va = (a.get("read", {}).get("bytes_per_second", 0) + a.get("write", {}).get("bytes_per_second", 0)) / 1e6
                vb = (b.get("read", {}).get("bytes_per_second", 0) + b.get("write", {}).get("bytes_per_second", 0)) / 1e6
        rows.append({"protocol": key[0], "test": key[1], "unit": unit,
                     "baseline_a": round(va, 2), "baseline_b": round(vb, 2),
                     "change_pct": round(100*(vb-va)/va, 2) if va > 0 else None})
    return {"baseline_a": one.get("run_id"), "baseline_b": two.get("run_id"),
            "warnings": warnings, "comparable": not warnings, "results": rows}
