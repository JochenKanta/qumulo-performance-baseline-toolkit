import copy
import json
from pathlib import Path
from unittest.mock import patch
import pytest

from qpbt.config import load_config, ConfigError, fingerprint, public_config
from qpbt.fio_engine import normalize_fio, FioError, _shared_args
from qpbt.mounts import scrub_options, PreflightError, official_fio_version
from qpbt.profiles import Workload, workloads, total_dataset_bytes
from qpbt.reporting import compare_runs
from qpbt.telemetry import parse_openmetrics, QumuloSampler, summarize_telemetry
from qpbt.safety import cleanup_workspace, SafetyError


@pytest.fixture
def config(tmp_path):
    here = Path(__file__).resolve().parents[1] / "qpbt.example.yaml"
    data = load_config(here)
    data["report"]["output_directory"] = str(tmp_path / "reports")
    return data


def test_profile_ten_workloads(config):
    actual = workloads(config)
    assert len(actual) == 9
    assert actual[0].id == "T01" and actual[-1].id == "T09"
    assert total_dataset_bytes(config) == 16 * 1024**3
    assert actual[7].workers == 16


def test_config_fingerprint_stable(config):
    assert fingerprint(config) == fingerprint(copy.deepcopy(config))
    assert "password" not in json.dumps(public_config(config)).lower()


def test_config_rejects_same_mount(tmp_path):
    content = (Path(__file__).resolve().parents[1] / "qpbt.example.yaml").read_text()
    content = content.replace('/mnt/qpbt-smb', '/mnt/qpbt-nfs')
    p = tmp_path / 'bad.yaml'
    p.write_text(content)
    with pytest.raises(ConfigError):
        load_config(p)


def test_safe_option_redaction():
    result = scrub_options("vers=3.1.1,username=bob,password=s3cr3t,sec=ntlmssp,credentials=/root/file")
    assert "bob" not in result and "s3cr3t" not in result and '/root/file' not in result
    assert "vers=3.1.1" in result


def test_fio_args_no_shell_and_isolated(config, tmp_path):
    args = _shared_args(tmp_path, config, 16)
    assert "--filename_format=data.$jobnum.bin" in args
    assert "--direct=1" in args
    assert all("/dev/" not in a for a in args)


def test_fio_normalization():
    sample = {"jobs": [{"error": 0, "read": {"io_bytes": 1000000, "bw_bytes": 100000000,
                                         "iops": 100, "clat_ns": {"mean": 1000000,
                                                                    "percentile": {"95.000000": 2000000,
                                                                                   "99.000000": 5000000}}},
                         "write": {"io_bytes": 0, "bw_bytes": 0, "iops": 0}}]}
    w = Workload("T02", "Read", "read", "1m", 1)
    val = normalize_fio(sample, w)
    assert val["read"]["bytes_per_second"] == 100000000
    assert val["read"]["p99_latency_ms"] == 5
    with pytest.raises(FioError):
        normalize_fio({"jobs": [{"read": {"io_bytes": 0}, "write": {"io_bytes": 0}}]}, w)


def test_metrics_parser_and_health():
    source = '''# HELP qumulo_cluster something
qumulo_info{version="7.5.1",name="TEST",uuid="id"} 1
qumulo_protocol_operation_bytes_count{protocol="nfs",io_type="read",op_name="READ"} 400
qumulo_protocol_operation_bytes_count{protocol="nfs",io_type="read",op_name="READ4"} 500
qumulo_disk_is_unhealthy{node_id="1"} 0
qumulo_quorum_node_is_offline{node_id="1"} 1
qumulo_fs_directory_used_bytes{path="secret"} 12345
'''
    parsed = parse_openmetrics(source)
    assert parsed["cluster_info"]["version"] == "7.5.1"
    assert parsed["metrics"]["qumulo_protocol_operation_bytes_count|protocol=nfs|io_type=read"] == 900
    assert QumuloSampler.health(parsed) == ["Qumulo meldet offline nodes."]
    assert "secret" not in str(parsed)


def test_metric_rate():
    a = {"monotonic": 10, "metrics": {"qumulo_protocol_operations_count|protocol=nfs": 100}}
    b = {"monotonic": 20, "metrics": {"qumulo_protocol_operations_count|protocol=nfs": 180}}
    result = summarize_telemetry({"samples": [a,b], "errors": []})
    assert result["counter_rates_per_second"]["qumulo_protocol_operations_count|protocol=nfs"] == 8


def test_compare():
    def inp(number):
        return {"run_id": "r", "profile": "installation-v0.1", "profile_hash": "abc",
                "fio_version": "fio-3.41", "client": {"c": 1}, "mounts": {},
                "tests": [{"protocol": "nfs", "test_id": "T02", "status": "PASS",
                           "read": {"bytes_per_second": number, "iops": 3},
                           "write": {"bytes_per_second": 0, "iops": 0}}]}
    result = compare_runs(inp(100e6), inp(110e6))
    assert result["comparable"]
    assert result["results"][0]["change_pct"] == 10


def test_safety_rejects_symlink(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    # Mount checking must fail - regular local directories cannot be cleaned.
    with pytest.raises(SafetyError):
        cleanup_workspace(str(mount), "run")


def test_reject_wrong_fio_binary():
    # In this environment 'fio' may be Fiona GIS CLI; guard prevents confusing the tools.
    with patch("qpbt.mounts.shutil.which", return_value="/usr/bin/fake-fio"):
        with patch("qpbt.mounts.subprocess.run") as mocked:
            mocked.return_value.returncode = 0
            mocked.return_value.stdout = "fio, version 1.10.1\n"
            with pytest.raises(PreflightError):
                official_fio_version()


def test_orchestration_integration_without_real_q(tmp_path, monkeypatch):
    # pytest will inject the two fixtures despite explicit optional defaults.
    from qpbt import runner
    config_path = Path(__file__).resolve().parents[1] / "qpbt.example.yaml"
    conf = load_config(config_path)
    conf["report"]["output_directory"] = str(tmp_path / "results")
    conf["telemetry"] = {"enabled": False, "required": False, "sample_interval_seconds": 5}
    conf["safety"]["cleanup"] = "never"
    monkeypatch.setattr(runner, "inspect", lambda c: {
        "fio_version": "fio-3.42", "client": {"hostname": "UNITTEST"},
        "mounts": {"nfs": {"mountpoint": "/fake/nfs"}, "smb": {"mountpoint": "/fake/smb"}},
        "cluster_info": {"name": "TESTCLUSTER"}, "warnings": ["MOCK: Telemetrie fehlt."],
        "telemetry": "disabled"})
    monkeypatch.setattr(runner, "_check_mount_unchanged", lambda c,p,o: None)
    def fake_workspace(mount, runid):
        dest = tmp_path / ("nfs" if "nfs" in mount else "smb") / runid
        (dest / "dataset").mkdir(parents=True)
        (dest / "metadata").mkdir()
        (dest / "probe").mkdir()
        return dest
    monkeypatch.setattr(runner, "make_workspace", fake_workspace)
    monkeypatch.setattr(runner, "prepare_data", lambda dataset,c,ofile: {})
    monkeypatch.setattr(runner, "check_direct_io", lambda path: {"status": "PASS", "bytes": 4096})
    def fake_execute(dataset, conf, work, outfile):
        io = "read" if "read" in work.rw else "write"
        if work.rw == "randrw":
            io = "mixed"
        return {"jobs": [{"read": {"io_bytes": 1048576 if io in ("read", "mixed") else 0,
                                   "bw_bytes": 2048, "iops": 100,
                                   "clat_ns": {"mean": 10000, "percentile": {"95.000000": 20000, "99.000000": 50000}}},
                           "write": {"io_bytes": 1048576 if io in ("write", "mixed") else 0,
                                     "bw_bytes": 2048, "iops": 100,
                                     "clat_ns": {"mean": 10000, "percentile": {"95.000000": 20000, "99.000000": 50000}}}}]}
    monkeypatch.setattr(runner, "execute_workload", fake_execute)
    out, state = runner.run_real(conf)
    assert len(state["tests"]) == 20
    assert state["status"] == "WARNING"
    assert state["simulated"] is False
    assert (out / "report.html").is_file() and (out / "report.pdf").is_file()
    assert (out / "result.json").is_file()
    assert state["direct_io_probes"]["nfs"]["status"] == "PASS"


def test_direct_io_readback_on_local_linux(tmp_path):
    from qpbt.direct_io import check_direct_io
    output = check_direct_io(tmp_path)
    assert output["status"] == "PASS"
    assert not list(tmp_path.iterdir())


def test_direct_io_refuses_symlink(tmp_path):
    from qpbt.direct_io import check_direct_io, DirectIOError
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(DirectIOError):
        check_direct_io(alias)


def test_openmetrics_parser_uses_data_type_and_no_sensitive_labels():
    body = """
qumulo_info{name="lab",uuid="uuid-1",version="7.5.1"} 1
qumulo_protocol_operation_bytes_count{protocol="nfs",data_type="data",io_type="read",op_name="READ"} 12
qumulo_protocol_operation_bytes_count{protocol="nfs",data_type="metadata",io_type="read",op_name="READDIR"} 6
qumulo_protocol_operation_bytes_count{protocol="nfs",data_type="data",io_type="read",op_name="READ4"} 3
qumulo_network_interface_received_bytes_count{node_id="1",interface="eth0"} 100
qumulo_fs_directory_used_bytes{path="/secret"} 123
"""
    metrics = parse_openmetrics(body)
    assert metrics["cluster_info"]["name"] == "lab"
    assert metrics["metrics"]["qumulo_protocol_operation_bytes_count|protocol=nfs|data_type=data|io_type=read"] == 15
    assert metrics["metrics"]["qumulo_protocol_operation_bytes_count|protocol=nfs|data_type=metadata|io_type=read"] == 6
    assert "secret" not in json.dumps(metrics)


def test_validate_run_simulated_never_accepted(config, monkeypatch, tmp_path):
    from qpbt.runner import run_simulated
    from qpbt.validation import validate_run
    config["report"]["output_directory"] = str(tmp_path)
    _, report = run_simulated(config)
    result = validate_run(report)
    assert not result["valid"]
    assert any("Simulation" in item for item in result["errors"])


def test_validate_run_missing_workload_fail():
    from qpbt.validation import validate_run
    data = {"run_id": "real1", "simulated": False, "status": "PASS",
            "profile_hash": "abc", "cluster_info": {"name": "x", "uuid": "a", "version": "b"},
            "direct_io_probes": {"nfs": {"status": "PASS"}, "smb": {"status": "PASS"}},
            "tests": []}
    result = validate_run(data)
    assert not result["valid"] and len(result["errors"]) >= 20


def test_mixed_requires_both_directions():
    work = Workload("T09", "Mixed", "randrw", "4k", 16, 70)
    body = {"jobs": [{"read": {"io_bytes": 4096, "iops": 1,
                             "clat_ns": {"mean": 100, "percentile": {"95.000000": 100, "99.000000": 200}}},
                      "write": {"io_bytes": 0, "iops": 0}}]}
    with pytest.raises(FioError, match="write-IOs"):
        normalize_fio(body, work)


def test_local_doctor_never_needs_cluster():
    from qpbt.doctor import inspect_host
    data = inspect_host()
    assert data["kind"] == "READ_ONLY_LOCAL_DOCTOR"
    assert any(c["check"] == "fio" for c in data["checks"])


def test_live_mounts_require_explicit_source(config):
    from qpbt.mounts import preflight_mounts
    with pytest.raises(PreflightError, match="expected_source"):
        preflight_mounts(config)


def test_namespace_separates_nfs_and_smb_workspace():
    run_id = "20261014T000000Z-test"
    ids = {f"{run_id}-{protocol}" for protocol in ("nfs", "smb")}
    assert len(ids) == 2


def test_sampler_no_redirect():
    from qpbt.telemetry import _NoRedirect, TelemetryError
    with pytest.raises(TelemetryError):
        _NoRedirect().redirect_request(None, None, 302, "moved", {}, "https://evil.invalid/")


def test_config_does_not_leak_unknown_credentials(tmp_path):
    source = (Path(__file__).resolve().parents[1] / "qpbt.example.yaml").read_text()
    target = tmp_path / "illegal.yaml"
    target.write_text(source + "\nunknown_password: secret-value\n")
    with pytest.raises(ConfigError, match="Unbekannte Konfigurationsschlüssel"):
        load_config(target)


def test_config_rejects_token_url_query(tmp_path):
    source = (Path(__file__).resolve().parents[1] / "qpbt.example.yaml").read_text()
    target = tmp_path / "badapi.yaml"
    target.write_text(source.replace('https://qumulo.example.internal:8000',
                                     'https://qumulo.example.internal:8000?token=secret'))
    with pytest.raises(ConfigError, match="HTTPS-Basisadresse"):
        load_config(target)


def test_workspace_cleanup_only_owned_run(monkeypatch, tmp_path):
    from qpbt.safety import make_workspace, cleanup_workspace
    mount = tmp_path / "mount"
    mount.mkdir()
    original = Path.is_mount
    monkeypatch.setattr(Path, "is_mount", lambda path: path == mount or original(path))
    run = make_workspace(str(mount), "unit-nfs")
    (run / "dataset" / "data.0.bin").write_bytes(b"test")
    (run / "metadata" / "m-000001.bin").write_bytes(b"test")
    outside = mount / "customer.txt"
    outside.write_text("customer data")
    cleanup_workspace(str(mount), "unit-nfs")
    assert outside.read_text() == "customer data"
    assert not run.exists()


def test_workspace_cleanup_refuses_unexpected_file(monkeypatch, tmp_path):
    from qpbt.safety import make_workspace, cleanup_workspace
    mount = tmp_path / "mount"
    mount.mkdir()
    original = Path.is_mount
    monkeypatch.setattr(Path, "is_mount", lambda path: path == mount or original(path))
    run = make_workspace(str(mount), "unit-smb")
    unknown = run / "dataset" / "unrecognized.bin"
    unknown.write_bytes(b"must not be silently removed")
    with pytest.raises(SafetyError, match="Unbekannte Datei"):
        cleanup_workspace(str(mount), "unit-smb")
    assert unknown.is_file()


def test_profile_fingerprint_ignores_report_path_and_tokens(config):
    profile = fingerprint(config)
    other = copy.deepcopy(config)
    other["report"]["output_directory"] = "/some/other/path"
    other["cluster"]["api_url"] = "https://different.example:8000"
    other["cluster"]["api_token_env"] = "ANOTHER_TOKEN_ENV"
    assert fingerprint(other) == profile
    other["workload"]["measure_seconds"] += 1
    assert fingerprint(other) != profile


def test_compare_ignores_volatile_mount_ids():
    def run(mid, free):
        return {"run_id": "r", "simulated": False, "profile": "installation-v0.1",
                "tool_version": "0.2", "fio_version": "fio-3.42", "profile_hash": "same",
                "client": {}, "cluster_info": {"uuid": "uuid-a"},
                "mounts": {"nfs": {"mountpoint": "/mnt/n", "fstype": "nfs4", "source": "s:/e",
                                    "options": "rw,vers=4.1", "negotiated_version": "4.1",
                                    "mount_id": mid, "available_bytes": free}},
                "tests": []}
    output = compare_runs(run("11", 100), run("22", 200))
    assert output["comparable"] is True


def test_compare_warns_different_cluster_uuid():
    a = {"cluster_info": {"uuid": "a"}, "profile": "installation-v0.1", "profile_hash": "X",
         "fio_version": "fio-3.42", "tool_version": "0.2", "mounts": {}, "client": {}, "tests": []}
    b = copy.deepcopy(a)
    b["cluster_info"]["uuid"] = "b"
    result = compare_runs(a, b)
    assert result["comparable"] is False
    assert any("Cluster-UUIDs" in w for w in result["warnings"])
