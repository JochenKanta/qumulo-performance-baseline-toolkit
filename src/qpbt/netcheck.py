"""Network pre-check from each test client towards the Qumulo nodes.

Nothing is installed or started on Qumulo nodes. The check uses standard
client-side tools only:

* ping: reachability, loss and round-trip time per node address
* ping with DF bit and a full-size payload: end-to-end MTU (jumbo frames)
* ip route get + /sys/class/net: which client NIC carries the traffic, link speed, MTU
* optional iperf3 against a separately provided iperf3 server (e.g. another test
  client on the same switch) to measure the client network path
* Qumulo OpenMetrics link speed gauges: per-node front-end link speed

The result separates network problems from storage problems before any
benchmark data is written.
"""
from __future__ import annotations

import ipaddress
import json
import re

from .executor import Executor, ExecError

DEV = re.compile(r"^[A-Za-z0-9_.:@-]{1,32}$")
PING_SUMMARY = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
PING_RTT = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms")


def parse_ping(text: str) -> dict:
    out = {"transmitted": None, "received": None, "loss_pct": None,
           "rtt_min_ms": None, "rtt_avg_ms": None, "rtt_max_ms": None}
    m = PING_SUMMARY.search(text)
    if m:
        sent, recv = int(m.group(1)), int(m.group(2))
        out.update(transmitted=sent, received=recv,
                   loss_pct=round(100.0 * (sent - recv) / sent, 1) if sent else None)
    r = PING_RTT.search(text)
    if r:
        out.update(rtt_min_ms=float(r.group(1)), rtt_avg_ms=float(r.group(2)), rtt_max_ms=float(r.group(3)))
    return out


def _is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).version == 6
    except ValueError:
        return False


def ping(ex: Executor, host: str, count: int, mtu: int | None = None) -> dict:
    cmd = ["ping", "-n", "-q", "-c", str(count), "-i", "0.2", "-W", "2"]
    if mtu:
        # Payload = MTU - IP header - ICMP header; DF set, so no fragmentation is allowed.
        header = 48 if _is_ipv6(host) else 28
        cmd += ["-M", "do", "-s", str(mtu - header)]
    res = ex.run(cmd + ["--", host.strip("[]")], timeout=count + 15)
    parsed = parse_ping(res.stdout + "\n" + res.stderr)
    parsed["exit_code"] = res.returncode
    return parsed


def route_to(ex: Executor, host: str) -> dict:
    try:
        data = json.loads(ex.check(["ip", "-j", "route", "get", host.strip("[]")], timeout=15) or "[]")
        entry = data[0] if data else {}
    except (ExecError, ValueError, IndexError) as exc:
        return {"dev": None, "error": str(exc)[:200]}
    dev = entry.get("dev")
    return {"dev": dev if isinstance(dev, str) and DEV.fullmatch(dev) else None,
            "src": entry.get("prefsrc"), "gateway": entry.get("gateway")}


def nic_props(ex: Executor, dev: str) -> dict:
    out = {"interface": dev, "speed_mbps": None, "mtu": None}
    for key, field in (("speed", "speed_mbps"), ("mtu", "mtu")):
        res = ex.run(["cat", f"/sys/class/net/{dev}/{key}"], timeout=15)
        try:
            value = int(res.stdout.strip())
            out[field] = value if value > 0 else None
        except ValueError:
            pass
    if out["speed_mbps"] is None:
        out["note"] = "Link-Speed nicht lesbar (VLAN/virtuelles Interface?)."
    return out


def iperf3(ex: Executor, server: str, seconds: int, streams: int, reverse: bool) -> dict:
    cmd = ["iperf3", "-c", server.strip("[]"), "-t", str(seconds), "-P", str(streams), "-J"]
    if reverse:
        cmd.append("-R")
    res = ex.run(cmd, timeout=seconds + 30)
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return {"error": (res.stderr or res.stdout).strip()[-300:] or "iperf3 ohne JSON-Ausgabe"}
    if data.get("error"):
        return {"error": str(data["error"])[:300]}
    end = data.get("end", {})
    received = end.get("sum_received", {})
    sent = end.get("sum_sent", {})
    return {"direction": "server->client" if reverse else "client->server",
            "bits_per_second": received.get("bits_per_second"),
            "retransmits": sent.get("retransmits")}


def check_client(ex: Executor, targets: list[str], net: dict) -> dict:
    result = {"client": ex.name, "targets": {}, "interfaces": {}, "iperf3": [], "issues": []}

    def issue(severity: str, detail: str):
        result["issues"].append({"severity": severity, "client": ex.name, "detail": detail})

    if not targets:
        issue("WARNING", "Keine Node-Adressen bekannt (cluster.node_addresses oder Mount addr=).")
    for host in targets:
        entry = {"route": route_to(ex, host)}
        try:
            entry["ping"] = ping(ex, host, net["ping_count"])
        except ExecError as exc:
            entry["ping"] = {"error": str(exc)[:200]}
        p = entry["ping"]
        if p.get("received") in (None, 0):
            issue("FAIL", f"{host}: nicht erreichbar (ICMP).")
        else:
            if p.get("loss_pct"):
                issue("WARNING", f"{host}: {p['loss_pct']} % Paketverlust.")
            if p.get("rtt_avg_ms") is not None and p["rtt_avg_ms"] > net["max_rtt_ms"]:
                issue("WARNING", f"{host}: mittlere RTT {p['rtt_avg_ms']} ms > {net['max_rtt_ms']} ms.")
        if net.get("expected_mtu"):
            try:
                df = ping(ex, host, 3, mtu=net["expected_mtu"])
            except ExecError as exc:
                df = {"error": str(exc)[:200]}
            entry["mtu_path"] = {"mtu": net["expected_mtu"], "ok": df.get("received") not in (None, 0), **df}
            if p.get("received") and not entry["mtu_path"]["ok"]:
                issue("FAIL", f"{host}: Pfad-MTU {net['expected_mtu']} nicht durchgängig (DF-Ping fehlgeschlagen).")
        dev = entry["route"].get("dev")
        if not dev:
            issue("WARNING", f"{host}: Client-Interface/Route nicht ermittelbar "
                             f"({entry['route'].get('error', 'ip route get ohne dev')}).")
        if dev and dev not in result["interfaces"]:
            result["interfaces"][dev] = nic_props(ex, dev)
        result["targets"][host] = entry
    for dev, props in result["interfaces"].items():
        if net.get("expected_mtu") and props.get("mtu") and props["mtu"] != net["expected_mtu"]:
            issue("WARNING", f"Interface {dev}: MTU {props['mtu']} statt erwartet {net['expected_mtu']}.")
        if net.get("min_client_link_gbps") and props.get("speed_mbps") is not None \
                and props["speed_mbps"] < net["min_client_link_gbps"] * 1000:
            issue("WARNING", f"Interface {dev}: {props['speed_mbps'] / 1000:g} Gbit/s < "
                             f"Minimum {net['min_client_link_gbps']} Gbit/s.")
    if net.get("iperf3_server"):
        speeds = [p["speed_mbps"] for p in result["interfaces"].values() if p.get("speed_mbps")]
        link_bps = max(speeds) * 1e6 if speeds else None
        for reverse in (False, True):
            try:
                measurement = iperf3(ex, net["iperf3_server"], net["iperf3_seconds"], net["iperf3_streams"], reverse)
            except ExecError as exc:
                measurement = {"error": str(exc)[:200]}
            result["iperf3"].append(measurement)
            if measurement.get("error"):
                issue("WARNING", f"iperf3 ({'-R' if reverse else 'send'}): {measurement['error']}")
            elif link_bps and measurement.get("bits_per_second") is not None \
                    and measurement["bits_per_second"] < net["iperf3_min_link_fraction"] * link_bps:
                issue("WARNING", f"iperf3 {measurement['direction']}: {measurement['bits_per_second'] / 1e9:.2f} Gbit/s "
                                 f"< {net['iperf3_min_link_fraction']:.0%} der Linkrate.")
    speeds = [p["speed_mbps"] for p in result["interfaces"].values() if p.get("speed_mbps")]
    result["link_bits_per_second"] = max(speeds) * 1e6 if speeds else None
    return result


def cluster_link_summary(snapshot: dict | None) -> dict:
    from .telemetry import node_link_speeds
    if not snapshot:
        return {"available": False}
    speeds = node_link_speeds(snapshot)
    frontend = {node: sum(v for role, v in roles.items() if role != "backend") for node, roles in speeds.items()}
    return {"available": bool(speeds), "per_node_bits_per_second": frontend,
            "total_frontend_bits_per_second": sum(frontend.values()) if frontend else None}


def run_netcheck(config: dict, clients: list[tuple[Executor, list[str]]], snapshot: dict | None = None) -> dict:
    net = config["network"]
    results = [check_client(ex, targets, net) for ex, targets in clients]
    issues = [i for r in results for i in r["issues"]]
    cluster = cluster_link_summary(snapshot)
    per_node = list((cluster.get("per_node_bits_per_second") or {}).values())
    if per_node and len(set(per_node)) > 1:
        issues.append({"severity": "WARNING", "client": "cluster",
                       "detail": "Qumulo-Nodes melden unterschiedliche Front-End-Linkraten: "
                                 + ", ".join(f"{v / 1e9:g}" for v in sorted(set(per_node))) + " Gbit/s."})
    client_total = sum(r["link_bits_per_second"] or 0 for r in results) or None
    cluster_total = cluster.get("total_frontend_bits_per_second")
    bounds = [b for b in (client_total, cluster_total) if b]
    status = "FAIL" if any(i["severity"] == "FAIL" for i in issues) else (
        "WARNING" if issues else "PASS")
    return {"status": status, "clients": results, "cluster_links": cluster, "issues": issues,
            "client_link_total_bits_per_second": client_total,
            "network_ceiling_bytes_per_second": min(bounds) / 8 if bounds else None,
            "note": "Netzwerk-Obergrenze = min(Summe Client-Links, Summe Qumulo-Front-End-Links); "
                    "ohne Protokoll-Overhead."}


def targets_for(config: dict, mounts: dict) -> list[str]:
    """Configured node addresses, else the addresses the client actually mounted."""
    if config["cluster"].get("node_addresses"):
        return list(config["cluster"]["node_addresses"])
    found = []
    for entry in mounts.values():
        addr = entry.get("server_address")
        if addr and addr not in found:
            found.append(addr)
    return found
