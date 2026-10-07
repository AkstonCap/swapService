"""Offline external-launcher tests; the Docker authority is an injected boundary."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "custody_external_launcher.py"
IMAGE = "sha256:" + "a" * 64
CONTAINER = "b" * 64


def load_launcher():
    assert SCRIPT.is_file(), "external pre-execution image admission guard is missing"
    spec = importlib.util.spec_from_file_location("external_launcher", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate():
    return {
        "Id": CONTAINER, "Image": IMAGE,
        "Path": "/usr/local/bin/python3", "Args": ["/opt/swapService/swapService.py"],
        "Config": {"Env": ["STATE_DB_PATH=/var/lib/swapservice/custody.db"],
                   "WorkingDir": "/opt/swapService", "User": "1000:1000"},
        "HostConfig": {"ReadonlyRootfs": True, "Privileged": False,
                       "CapDrop": ["ALL"], "CapAdd": None, "Tmpfs": {},
                       "Mounts": [
                           {"Type": "bind", "Source": "/protected/custody", "Target": "/var/lib/swapservice",
                            "ReadOnly": False, "BindOptions": {"NonRecursive": True, "Propagation": "rprivate"}},
                           {"Type": "bind", "Source": "/protected/secrets", "Target": "/run/swapservice-secrets",
                            "ReadOnly": True, "BindOptions": {"NonRecursive": True, "Propagation": "rprivate"}},
                       ]},
        "Mounts": [
            {"Type": "bind", "Source": "/protected/custody", "Destination": "/var/lib/swapservice", "RW": True},
            {"Type": "bind", "Source": "/protected/secrets", "Destination": "/run/swapservice-secrets", "RW": False},
        ],
        "State": {"Status": "created", "Running": False},
        "NetworkSettings": {"Networks": {"bridge": {"Aliases": None}}},
        "Platform": "linux", "AppArmorProfile": "docker-default",
        "ProcessLabel": "", "MountLabel": "",
    }


def approved_config(value):
    # Independent contract: exact execution command, config, host policy and mounts.
    selected = {key: value[key] for key in (
        "Image", "Path", "Args", "Config", "HostConfig", "Mounts", "NetworkSettings",
        "Platform", "AppArmorProfile", "ProcessLabel", "MountLabel",
    )}
    raw = json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def test_unapproved_image_refuses_before_repository_execution(monkeypatch):
    launcher = load_launcher()
    value = candidate()
    approval = approved_config(value)
    value["Image"] = "sha256:" + "c" * 64
    commands = []

    def engine(args, *, attach=False):
        commands.append((args, attach))
        assert args[0] == "inspect", "rejected image reached execution"
        return json.dumps([value])

    monkeypatch.setattr(launcher, "_engine", engine)
    with pytest.raises(launcher.LaunchError, match="image"):
        launcher.launch(CONTAINER, IMAGE, approval)
    assert all(command[0][0] != "start" for command in commands)


@pytest.mark.parametrize("field", ["Path", "Args", "Config", "HostConfig", "Mounts"])
def test_unapproved_execution_configuration_cannot_start(monkeypatch, field):
    launcher = load_launcher()
    value = candidate()
    approval = approved_config(value)
    value[field] = {"unapproved": "pre-import side effect"}
    calls = []

    def engine(args, *, attach=False):
        calls.append(args)
        assert args[0] != "start"
        return json.dumps([value])

    monkeypatch.setattr(launcher, "_engine", engine)
    with pytest.raises(launcher.LaunchError):
        launcher.launch(CONTAINER, IMAGE, approval)
    assert not any(args[0] == "start" for args in calls)


def test_exact_immutable_execution_starts_once_after_two_checks(monkeypatch):
    launcher = load_launcher()
    value = candidate()
    calls = []

    def engine(args, *, attach=False):
        calls.append((args, attach))
        if args[0] == "inspect":
            return json.dumps([value])
        if args[0] == "diff":
            return ""
        assert args == ["start", "--attach", CONTAINER] and attach
        return 7  # Preserve the attached child's exit status, not a invented success.

    monkeypatch.setattr(launcher, "_engine", engine)
    assert launcher.launch(CONTAINER, IMAGE, approved_config(value)) == 7
    assert [args[0] for args, _ in calls] == ["inspect", "diff", "inspect", "diff", "start"]


@pytest.mark.parametrize("changed_file", [
    "/opt/swapService/swapService.py", "/opt/swapService/src/main.py",
    "/usr/local/bin/python3", "/usr/local/lib/python3.12/site-packages/httpx/__init__.py",
    "/usr/local/lib/python3.12/site-packages/solana/rpc/api.py",
    "/usr/local/lib/python3.12/site-packages/solders/rpc/requests.py",
    "/usr/local/lib/python3.12/os.py", "/usr/lib/libc.so.6",
    "/opt/swapService/src/__pycache__/main.cpython-312.pyc",
])
def test_changed_container_layer_cannot_execute_preimport_side_effect(monkeypatch, changed_file):
    launcher = load_launcher()
    value = candidate()
    calls = []

    def engine(args, *, attach=False):
        calls.append(args)
        assert args[0] != "start"
        return json.dumps([value]) if args[0] == "inspect" else "C " + changed_file + "\n"

    monkeypatch.setattr(launcher, "_engine", engine)
    with pytest.raises(launcher.LaunchError, match="layer"):
        launcher.launch(CONTAINER, IMAGE, approved_config(value))
    assert not any(args[0] == "start" for args in calls)


@pytest.mark.parametrize("unsafe", [
    "writable_root", "privileged", "added_capability", "missing_cap_drop", "tmpfs",
    "code_mount", "writable_secrets", "anonymous_volume", "already_executed",
])
def test_unsafe_container_is_rejected_even_with_matching_config_pin(monkeypatch, unsafe):
    launcher = load_launcher()
    value = candidate()
    if unsafe == "writable_root":
        value["HostConfig"]["ReadonlyRootfs"] = False
    elif unsafe == "privileged":
        value["HostConfig"]["Privileged"] = True
    elif unsafe == "added_capability":
        value["HostConfig"]["CapAdd"] = ["SYS_ADMIN"]
    elif unsafe == "missing_cap_drop":
        value["HostConfig"]["CapDrop"] = []
    elif unsafe == "tmpfs":
        value["HostConfig"]["Tmpfs"] = {"/opt/swapService": "rw"}
    elif unsafe == "code_mount":
        value["Mounts"][0]["Destination"] = "/opt/swapService"
    elif unsafe == "writable_secrets":
        value["Mounts"][1]["RW"] = True
    elif unsafe == "anonymous_volume":
        value["Mounts"][0]["Type"] = "volume"
    else:
        value["State"]["Status"] = "exited"
    monkeypatch.setattr(launcher, "_engine", lambda args, **kwargs:
                        json.dumps([value]) if args[0] == "inspect" else "")
    with pytest.raises(launcher.LaunchError):
        launcher.launch(CONTAINER, IMAGE, approved_config(value))


@pytest.mark.parametrize("drift", ["config", "state", "layer"])
def test_changed_evidence_between_checks_never_starts(monkeypatch, drift):
    launcher = load_launcher()
    value = candidate()
    calls = []

    def engine(args, *, attach=False):
        calls.append(args)
        assert args[0] != "start"
        if args[0] == "inspect":
            seen = copy.deepcopy(value)
            if sum(call[0] == "inspect" for call in calls) == 2:
                if drift == "config":
                    seen["Config"]["Env"].append("PYTHONPATH=/var/lib/swapservice")
                elif drift == "state":
                    seen["State"] = {"Status": "running", "Running": True}
            return json.dumps([seen])
        return "A /opt/swapService/evil.py\n" if drift == "layer" and len(calls) == 4 else ""

    monkeypatch.setattr(launcher, "_engine", engine)
    with pytest.raises(launcher.LaunchError):
        launcher.launch(CONTAINER, IMAGE, approved_config(value))


@pytest.mark.parametrize("drift", ["network", "security_label"])
@pytest.mark.parametrize("between_checks", [False, True])
def test_network_and_security_drift_is_independently_pinned(monkeypatch, drift, between_checks):
    launcher = load_launcher()
    value = candidate()
    approval = approved_config(value)
    inspected = 0

    def engine(args, *, attach=False):
        nonlocal inspected
        assert args[0] != "start"
        if args[0] == "diff":
            return ""
        inspected += 1
        changed = copy.deepcopy(value)
        if not between_checks or inspected == 2:
            if drift == "network":
                changed["NetworkSettings"]["Networks"]["unapproved"] = {"Aliases": ["rpc"]}
            else:
                changed["AppArmorProfile"] = "unconfined"
        return json.dumps([changed])

    monkeypatch.setattr(launcher, "_engine", engine)
    with pytest.raises(launcher.LaunchError):
        launcher.launch(CONTAINER, IMAGE, approval)


@pytest.mark.parametrize("bad_options", [None, {}, {"ReadOnlyNonRecursive": True},
                                        {"NonRecursive": True, "Propagation": "shared"}])
def test_readonly_secrets_requires_nonrecursive_private_bind(monkeypatch, bad_options):
    launcher = load_launcher()
    value = candidate()
    value["HostConfig"]["Mounts"][1]["BindOptions"] = bad_options
    monkeypatch.setattr(launcher, "_engine", lambda args, **kwargs:
                        json.dumps([value]) if args[0] == "inspect" else "")
    with pytest.raises(launcher.LaunchError):
        launcher.launch(CONTAINER, IMAGE, approved_config(value))


@pytest.mark.parametrize("payload", [
    "null", "{}", "[]", "[{}, {}]", "[null]", '[{"Id": "wrong"}]',
    '[{"Id": "' + CONTAINER + '", "Id": "' + CONTAINER + '"}]',
])
def test_malformed_or_duplicate_inspection_is_sanitized(monkeypatch, payload):
    launcher = load_launcher()

    def engine(args, *, attach=False):
        assert args[0] == "inspect"
        return payload

    monkeypatch.setattr(launcher, "_engine", engine)
    with pytest.raises(launcher.LaunchError, match="invalid"):
        launcher.launch(CONTAINER, IMAGE, approved_config(candidate()))


def test_duplicate_valid_inspection_refuses_before_execution(monkeypatch):
    launcher = load_launcher()
    value = candidate()
    payload = json.dumps([value]).replace('"Image":', '"Image": "' + IMAGE + '", "Image":', 1)

    def engine(args, *, attach=False):
        if args[0] == "inspect":
            return payload
        if args[0] == "diff":
            return ""
        pytest.fail("duplicate inspection reached execution")

    monkeypatch.setattr(launcher, "_engine", engine)
    with pytest.raises(launcher.LaunchError, match="invalid"):
        launcher.launch(CONTAINER, IMAGE, approved_config(value))


@pytest.mark.parametrize("which,bad_pin", [
    (0, "service"), (0, CONTAINER.upper()), (0, CONTAINER + "\n"),
    (1, "service:latest"), (1, "sha256:" + "A" * 64), (1, "sha256:" + "a" * 63),
    (2, ""), (2, "3" * 64 + "\n"),
])
def test_invalid_pins_refuse_before_engine_access(monkeypatch, which, bad_pin):
    launcher = load_launcher()
    pins = [CONTAINER, IMAGE, approved_config(candidate())]
    pins[which] = bad_pin
    monkeypatch.setattr(launcher, "_engine", lambda *args, **kwargs: pytest.fail("engine accessed"))
    with pytest.raises(launcher.LaunchError, match="pins"):
        launcher.launch(*pins)


def test_engine_uses_fixed_binary_and_clean_local_environment(monkeypatch):
    launcher = load_launcher()
    monkeypatch.setenv("DOCKER_HOST", "tcp://unapproved.example:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "unapproved")
    monkeypatch.setenv("PYTHONPATH", "/mutable")
    monkeypatch.setenv("LD_PRELOAD", "/mutable/interceptor.so")

    def run(command, **kwargs):
        assert command == ["/usr/bin/docker", "inspect", "--type=container", CONTAINER]
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
        assert kwargs["timeout"] == 15 and kwargs["stdin"] == launcher.subprocess.DEVNULL
        return launcher.subprocess.CompletedProcess(command, 0, "[]", "")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher._engine(["inspect", "--type=container", CONTAINER]) == "[]"


@pytest.mark.parametrize("failure", ["timeout", "missing_binary", "nonzero", "oversized"])
def test_engine_failures_do_not_disclose_evidence_or_credentials(monkeypatch, capsys, failure):
    launcher = load_launcher()

    def run(command, **kwargs):
        if failure == "timeout":
            raise launcher.subprocess.TimeoutExpired(command, 15, output="private-token")
        if failure == "missing_binary":
            raise FileNotFoundError("private-token")
        return launcher.subprocess.CompletedProcess(command, 1 if failure == "nonzero" else 0,
                                                    "x" * (1024 * 1024 + 1), "private-token")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.main(["--container", CONTAINER, "--approved-image", IMAGE,
                          "--approved-config", approved_config(candidate())]) == 1
    assert "private-token" not in capsys.readouterr().err


def test_external_cli_does_not_import_mutable_repository_before_refusal(tmp_path):
    import subprocess
    import sys

    marker = tmp_path / "pre-import-side-effect"
    tmp_path.joinpath("sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--container", "not-a-pin",
         "--approved-image", IMAGE, "--approved-config", approved_config(candidate())],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "canonical independent approval pins" in result.stderr
    assert not marker.exists()
