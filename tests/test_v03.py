"""Tests for 0.3: network pre-check, cluster-based sizing, multi-client mode."""
import copy
from pathlib import Path

import pytest
import yaml

from qpbt.config import load_config, ConfigError
from qpbt.executor import Executor, Result, SSHExecutor
from qpbt.fio_engine import FioError, as_jobfile, extract_json, workload_options, base_options
from qpbt.profiles import Workload, workloads
from qpbt.mounts import PreflightError

ROOT = Path(__file__).resolve().parents[1]


def write_cfg(tmp_path, mutate) -> Path:
    data = yaml.safe_load((ROOT / "qpbt.example.yaml").read_text())
    mutate(data)
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def multi(data, n=2):
    data["multi_client"] = {"clients": [
        {"name": f"client{i:02}", "address": f"10.0.0.{i}", "ssh": f"qpbt@10.0.0.{i}",
         "targets": {"nfs": {"mountpoint": "/mnt/qpbt-nfs", "expected_source": f"10.1.1.{i}:/qpbt"},
                     "smb": {"mountpoint": "/mnt/qpbt-smb", "expected_source": f"//10.1.1.{i}/qpbt"}}}
        for i in range(1, n + 1)]}


# ----------------------------------------------------------------------------- sizing

def test_auto_sizing_exceeds_cluster_cache(tmp_path):
    def m(d):
        d["cluster"].update(node_count=4, cache_gib_per_node=64)
        d["workload"]["dataset_sizing"] = "auto"
        del d["workload"]["dataset_mib_per_worker"]
        d["workload"]["cache_multiplier"] = 2.0
        multi(d, 4)
    c = load_config(write_cfg(tmp_path, m))
    # 2 x 4 x 64 GiB = 512 GiB over 4 clients x 16 workers = 8 GiB per worker
    assert c["workload"]["dataset_mib_per_worker"] == 8192
    assert c["sizing"]["dataset_gib_per_protocol"] == 512
    assert c["sizing"]["classification"] == "OVER_CACHE"


def test_auto_sizing_needs_cluster_facts(tmp_path):
    def m(d):
        d["workload"]["dataset_sizing"] = "auto"
        del d["workload"]["dataset_mib_per_worker"]
    with pytest.raises(ConfigError, match="node_count"):
        load_config(write_cfg(tmp_path, m))


def test_auto_sizing_rejects_manual_size_and_caps(tmp_path):
    def manual(d):
        d["cluster"].update(node_count=4, cache_gib_per_node=64)
        d["workload"]["dataset_sizing"] = "auto"
    with pytest.raises(ConfigError, match="nur bei dataset_sizing=fixed"):
        load_config(write_cfg(tmp_path, manual))

    def huge(d):
        d["cluster"].update(node_count=100, cache_gib_per_node=4096)
        d["workload"]["dataset_sizing"] = "auto"
        del d["workload"]["dataset_mib_per_worker"]
    with pytest.raises(ConfigError, match="Auto-Sizing"):
        load_config(write_cfg(tmp_path, huge))


def test_fixed_sizing_is_classified(tmp_path):
    c = load_config(write_cfg(tmp_path, lambda d: d["cluster"].update(node_count=4, cache_gib_per_node=64)))
    assert c["sizing"]["classification"] == "FITS_IN_CACHE"
    assert load_config(ROOT / "qpbt.example.yaml")["sizing"]["classification"] == "UNKNOWN"


def test_node_count_mismatch_blocks_auto_sizing():
    from qpbt.runner import check_node_count
    snap = {"metrics": {f"qumulo_quorum_node_is_offline|node_id={i}": 0 for i in (1, 2, 3)}}
    conf = {"cluster": {"node_count": 4}, "workload": {"dataset_sizing": "auto"}}
    with pytest.raises(PreflightError, match="meldet 3 Nodes"):
        check_node_count(conf, snap, {"cluster_info": {}, "warnings": []})
    conf["workload"]["dataset_sizing"] = "fixed"
    info = {"cluster_info": {}, "warnings": []}
    check_node_count(conf, snap, info)
    assert info["cluster_info"]["node_count"] == 3 and info["warnings"]


# ----------------------------------------------------------------------------- config

def test_multi_client_config_validation(tmp_path):
    c = load_config(write_cfg(tmp_path, multi))
    assert [x["fio_port"] for x in c["multi_client"]["clients"]] == [8765, 8765]
    assert c["sizing"]["clients"] == 2

    def dup(d):
        multi(d)
        d["multi_client"]["clients"][1]["name"] = "client01"
    with pytest.raises(ConfigError, match="Doppelter"):
        load_config(write_cfg(tmp_path, dup))

    def option_injection(d):
        multi(d)
        d["multi_client"]["clients"][0]["ssh"] = "-oProxyCommand=touch /tmp/pwned"
    with pytest.raises(ConfigError, match="ssh"):
        load_config(write_cfg(tmp_path, option_injection))

    def same_endpoint(d):
        multi(d)
        d["multi_client"]["clients"][1]["address"] = "10.0.0.1"
    with pytest.raises(ConfigError, match="doppelt"):
        load_config(write_cfg(tmp_path, same_endpoint))

    def unknown_key(d):
        multi(d)
        d["multi_client"]["clients"][0]["password"] = "x"
    with pytest.raises(ConfigError, match="Unbekannte"):
        load_config(write_cfg(tmp_path, unknown_key))


def test_old_schema_is_rejected_with_hint(tmp_path):
    with pytest.raises(ConfigError, match="0.2"):
        load_config(write_cfg(tmp_path, lambda d: d.update(schema_version="0.1")))


# ----------------------------------------------------------------------------- executor

def test_ssh_executor_quotes_remote_command():
    ex = SSHExecutor("c1", "qpbt@10.0.0.1")
    argv = ex.argv(["cat", "--", "/mnt/x; rm -rf /"])
    assert argv[:3] == ["ssh", "-o", "BatchMode=yes"]
    assert argv[-1] == "cat -- '/mnt/x; rm -rf /'"
    assert "ForwardAgent=no" in argv


# ----------------------------------------------------------------------------- netcheck

class FakeExecutor(Executor):
    kind = "fake"

    def __init__(self, responses):
        super().__init__("fake")
        self.responses = responses
        self.calls = []

    def run(self, command, timeout=30):
        self.calls.append(command)
        for key, value in self.responses.items():
            if key in " ".join(command):
                return value
        return Result(1, "", "unknown")


def ping_ok(rtt="0.1"):
    return Result(0, f"3 packets transmitted, 3 received, 0% packet loss\nrtt min/avg/max/mdev = {rtt}/{rtt}/{rtt}/0.0 ms\n", "")


NET = {"enabled": True, "required": True, "expected_mtu": 9000, "ping_count": 3, "max_rtt_ms": 1.0,
       "min_client_link_gbps": 25, "iperf3_server": None, "iperf3_seconds": 3, "iperf3_streams": 1,
       "iperf3_min_link_fraction": 0.7}


def test_parse_ping():
    from qpbt.netcheck import parse_ping
    out = parse_ping("10 packets transmitted, 7 received, 30% packet loss, time 9ms\n"
                     "rtt min/avg/max/mdev = 0.100/0.200/0.900/0.050 ms")
    assert out["loss_pct"] == 30.0 and out["rtt_avg_ms"] == 0.2 and out["rtt_max_ms"] == 0.9


def test_netcheck_detects_mtu_and_link_problems():
    from qpbt.netcheck import run_netcheck
    ex = FakeExecutor({
        "-M do": Result(1, "3 packets transmitted, 0 received, 100% packet loss", ""),
        "ping": ping_ok("2.5"),
        "route get": Result(0, '[{"dev":"ens1f0","prefsrc":"10.0.0.5"}]', ""),
        "/speed": Result(0, "10000\n", ""),
        "/mtu": Result(0, "1500\n", ""),
    })
    snap = {"metrics": {"qumulo_network_interface_link_speed_bits_per_second|node_id=1|interface=eth0|role=frontend": 1e11,
                        "qumulo_network_interface_link_speed_bits_per_second|node_id=2|interface=eth0|role=frontend": 1e11,
                        "qumulo_network_interface_link_speed_bits_per_second|node_id=1|interface=eth1|role=backend": 1e11}}
    result = run_netcheck({"network": NET}, [(ex, ["10.1.1.1"])], snap)
    details = " ".join(i["detail"] for i in result["issues"])
    assert result["status"] == "FAIL"
    assert "Pfad-MTU 9000" in details and "MTU 1500" in details and "RTT" in details and "Gbit/s" in details
    # min(client 10 Gbit/s, cluster front-end 200 Gbit/s), backend excluded
    assert result["network_ceiling_bytes_per_second"] == 10e9 / 8
    assert result["cluster_links"]["total_frontend_bits_per_second"] == 2e11


def test_netcheck_unreachable_is_fail_and_blocks_when_required():
    from qpbt.runner import apply_netcheck
    ex = FakeExecutor({"ping": Result(1, "3 packets transmitted, 0 received, 100% packet loss", "")})
    info = {"warnings": []}
    with pytest.raises(PreflightError, match="nicht erreichbar"):
        apply_netcheck({"network": NET}, info, [(ex, ["10.9.9.9"])], None)
    info = {"warnings": []}
    apply_netcheck({"network": {**NET, "required": False}}, info, [(ex, ["10.9.9.9"])], None)
    assert info["network"]["status"] == "FAIL" and any("nicht erreichbar" in w for w in info["warnings"])


def test_netcheck_targets_fall_back_to_mount_addresses():
    from qpbt.netcheck import targets_for
    mounts = {"nfs": {"server_address": "10.1.1.2"}, "smb": {"server_address": "10.1.1.2"}}
    assert targets_for({"cluster": {"node_addresses": []}}, mounts) == ["10.1.1.2"]
    assert targets_for({"cluster": {"node_addresses": ["a", "b"]}}, mounts) == ["a", "b"]


# ----------------------------------------------------------------------------- fio jobs

def test_jobfile_rejects_injection():
    with pytest.raises(FioError):
        as_jobfile(base_options("/mnt/x\nexec_prerun=touch /tmp/pwned", 64, 1))
    with pytest.raises(FioError):
        as_jobfile(base_options("/mnt/${HOME}", 64, 1))
    with pytest.raises(FioError, match="Doppelpunkt"):
        as_jobfile(base_options("/mnt/a:/mnt/b", 64, 1))
    assert "filename_format=data.$jobnum.bin" in as_jobfile(base_options("/mnt/x", 64, 1))


def test_single_stream_spans_whole_dataset():
    conf = load_config(ROOT / "qpbt.example.yaml")
    t05 = [w for w in workloads(conf) if w.id == "T05"][0]
    with pytest.raises(FioError):
        workload_options("/mnt/x", conf, t05)
    files = [f"data.{i}.bin" for i in range(16)]
    opts = dict(workload_options("/mnt/x", conf, t05, files))
    assert opts["filename"] == ":".join(files) and opts["size"] == f"{16 * 1024}m"
    assert opts["file_service_type"] == "random" and opts["numjobs"] == "1"
    with pytest.raises(FioError):
        base_options("/mnt/x", 64, 1, ["../escape.bin"])


def test_extract_json_skips_client_progress_lines():
    raw = "<vm> qpbt: (g=0): rw=read\n<vm> Starting 2 threads\n\n{\n \"client_stats\": []\n}\n"
    assert extract_json(raw) == {"client_stats": []}
    with pytest.raises(FioError):
        extract_json("fio: failed to connect\n")


# ----------------------------------------------------------------------------- multi-client parsing

def _client(name, port, host="10.0.0.1"):
    from qpbt.multiclient import Client
    return Client(name, host, port, "local", {}, None)


def _entry(host, port, iops, p99, error=0, jobname="qpbt"):
    side = {"io_bytes": 4096 * iops, "bw_bytes": 4096 * iops, "iops": iops,
            "clat_ns": {"mean": 100000, "percentile": {"95.000000": p99 / 2, "99.000000": p99}}}
    return {"jobname": jobname, "hostname": host, "port": port, "error": error,
            "read": side, "write": {"io_bytes": 0, "iops": 0}}


def test_multi_aggregation_sums_and_uses_worst_percentile():
    from qpbt.multiclient import normalize_multi
    clients = [_client("a", 8765, "10.0.0.1"), _client("b", 8765, "10.0.0.2")]
    data = {"client_stats": [_entry("10.0.0.1", 8765, 1000, 2e6), _entry("10.0.0.2", 8765, 3000, 8e6),
                             {"jobname": "All clients", "hostname": "10.0.0.2", "port": 8765,
                              "read": {}, "write": {}}]}
    work = Workload("T07", "Random Read", "randread", "4k", 16)
    r = normalize_multi(data, work, clients)
    assert r["read"]["iops"] == 4000 and r["client_count"] == 2
    assert r["read"]["p99_latency_ms"] == 8.0
    assert r["latency_percentile_aggregation"] == "max_over_clients"
    assert r["client_balance_min_to_max"] == round(1000 / 3000, 3)


def test_multi_missing_or_failed_client_is_fatal():
    from qpbt.multiclient import client_entries
    clients = [_client("a", 8765, "10.0.0.1"), _client("b", 8765, "10.0.0.2")]
    # fio exits 0 when a client's job is dropped: must not pass silently
    with pytest.raises(FioError, match="fehlt für Client b"):
        client_entries({"client_stats": [_entry("10.0.0.1", 8765, 10, 1e6)]}, clients)
    with pytest.raises(FioError, match="Fehler 5"):
        client_entries({"client_stats": [_entry("10.0.0.1", 8765, 10, 1e6),
                                         _entry("10.0.0.2", 8765, 10, 1e6, error=5)]}, clients)


def test_multi_single_stream_uses_first_client_and_all_files():
    from qpbt.multiclient import workload_jobs, Client
    conf = load_config(ROOT / "qpbt.example.yaml")
    targets = {"nfs": {"mountpoint": "/mnt/n"}, "smb": {"mountpoint": "/mnt/s"}}
    clients = [Client(n, "10.0.0.1", p, "local", targets, None) for n, p in (("c1", 8765), ("c2", 8766))]
    t02 = [w for w in workloads(conf) if w.id == "T02"][0]
    participants, jobs = workload_jobs(clients, conf, "nfs", "RUN-nfs", t02)
    assert [c.name for c in participants] == ["c1"]
    opts = dict(jobs[0][1])
    assert opts["directory"] == "/mnt/n/.qpbt/RUN-nfs/dataset"
    assert opts["filename"].split(":")[16] == "c2/data.0.bin" and len(opts["filename"].split(":")) == 32
    t04 = [w for w in workloads(conf) if w.id == "T04"][0]
    participants, jobs = workload_jobs(clients, conf, "nfs", "RUN-nfs", t04)
    assert len(jobs) == 2 and dict(jobs[1][1])["directory"].endswith("/dataset/c2")


def test_node_distribution_warns_when_all_clients_hit_one_node():
    from qpbt.multiclient import node_distribution
    info = {n: {"mounts": {"nfs": {"server_address": "10.1.1.1"}, "smb": {"server_address": a}}}
            for n, a in (("c1", "10.1.1.1"), ("c2", "10.1.1.2"))}
    out = node_distribution(info, ["10.1.1.1", "10.1.1.2"])
    assert any("dieselbe Qumulo-Adresse" in w for w in out["warnings"])
    assert out["per_protocol"]["smb"] == {"10.1.1.1": ["c1"], "10.1.1.2": ["c2"]}


# ----------------------------------------------------------------------------- safety

def _fake_mount(monkeypatch, tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    original = Path.is_mount
    monkeypatch.setattr(Path, "is_mount", lambda path: path == mount or original(path))
    return mount


def test_multi_layout_cleanup(monkeypatch, tmp_path):
    from qpbt.safety import make_workspace, cleanup_workspace, read_marker
    mount = _fake_mount(monkeypatch, tmp_path)
    run = make_workspace(str(mount), "unit-nfs", clients=["c1", "c2"])
    assert len(read_marker(run)["nonce"]) == 32
    (run / "dataset" / "c1" / "data.0.bin").write_bytes(b"x")
    (run / "probe" / "c2").mkdir()
    (run / "probe" / "c2" / "direct-io.bin").write_bytes(b"x")
    cleanup_workspace(str(mount), "unit-nfs")
    assert not run.exists()


def test_multi_layout_refuses_unknown_client_dir(monkeypatch, tmp_path):
    from qpbt.safety import make_workspace, cleanup_workspace, SafetyError
    mount = _fake_mount(monkeypatch, tmp_path)
    run = make_workspace(str(mount), "unit-smb", clients=["c1"])
    (run / "dataset" / "intruder").mkdir()
    with pytest.raises(SafetyError, match="Unbekannte Datei"):
        cleanup_workspace(str(mount), "unit-smb")
    assert run.exists()


# ----------------------------------------------------------------------------- telemetry

def test_telemetry_node_awareness():
    from qpbt.telemetry import parse_openmetrics, node_ids, summarize_telemetry
    def body(t):
        return "\n".join([
            'qumulo_quorum_node_is_offline{node_id="1"} 0', 'qumulo_quorum_node_is_offline{node_id="2"} 0',
            f'qumulo_network_interface_received_bytes_count{{node_id="1",interface="eth0",role="frontend"}} {300 * t}',
            f'qumulo_network_interface_received_bytes_count{{node_id="2",interface="eth0",role="frontend"}} {100 * t}',
            f'qumulo_network_interface_received_bytes_count{{node_id="2",interface="eth1",role="backend"}} {900 * t}'])
    a = {"monotonic": 0, **parse_openmetrics(body(0))}
    b = {"monotonic": 10, **parse_openmetrics(body(10))}
    assert node_ids(a) == ["1", "2"]
    s = summarize_telemetry({"samples": [a, b], "errors": []})
    assert s["node_network"]["1"]["rx_bytes_per_second"] == 300
    assert s["node_network"]["2"]["rx_bytes_per_second"] == 100  # backend excluded
    assert s["node_balance"] == {"nodes": 2, "min_to_max_ratio": round(100 / 300, 3)}


# ----------------------------------------------------------------------------- validation

def test_validate_multi_client_requires_all_clients():
    from qpbt.validation import validate_run
    tests = []
    for proto in ("nfs", "smb"):
        for n in range(1, 10):
            workers = 1 if n in (1, 2, 5, 6) else 16
            parts = ["c1"] if workers == 1 else ["c1", "c2"]
            tests.append({"protocol": proto, "test_id": f"T{n:02}", "status": "PASS", "measure_kind": "fio",
                          "workers": workers, "participating_clients": parts, "clients": {c: {} for c in parts}})
        tests.append({"protocol": proto, "test_id": "T10", "status": "PASS"})
    report = {"run_id": "r", "simulated": False, "status": "PASS", "mode": "multi_client", "profile_hash": "x",
              "cluster_info": {"name": "n", "uuid": "u", "version": "v"}, "clients": {"c1": {}, "c2": {}},
              "network": {"status": "PASS"}, "sizing": {"classification": "OVER_CACHE"},
              "direct_io_probes": {p: {"status": "PASS", "clients": {"c1": {"status": "PASS"}, "c2": {"status": "PASS"}}}
                                   for p in ("nfs", "smb")},
              "tests": tests}
    assert validate_run(report)["valid"], validate_run(report)["errors"]
    broken = copy.deepcopy(report)
    broken["tests"][3]["participating_clients"] = ["c1"]
    del broken["tests"][3]["clients"]["c2"]
    assert any("unvollständig" in e for e in validate_run(broken)["errors"])
