#!/usr/bin/env python3
"""External admission for an independently approved local Docker container image.

Install this script and its interpreter outside the service image under independent
administration. Never import swapService code here. The Docker daemon/image store,
launcher, interpreter and host are trusted; this is not their self-attestation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from typing import Literal, overload


class LaunchError(Exception):
    """Public refusal without engine output, credentials or inspected configuration."""


@overload
def _engine(args: list[str], *, attach: Literal[False] = False) -> str: ...


@overload
def _engine(args: list[str], *, attach: Literal[True]) -> int: ...


def _engine(args: list[str], *, attach: bool = False) -> str | int:
    try:
        result = subprocess.run(
            ["/usr/bin/docker", *args],
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
            stdin=subprocess.DEVNULL, capture_output=not attach,
            text=True, timeout=None if attach else 15, check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise LaunchError("local container authority is unavailable") from exc
    if attach:
        return result.returncode
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        raise LaunchError("local container evidence is unavailable")
    return result.stdout


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError
        result[name] = value
    return result


def launch(container_id: str, image_id: str, config_sha256: str) -> int:
    if (re.fullmatch(r"[0-9a-f]{64}", container_id) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", config_sha256) is None):
        raise LaunchError("canonical independent approval pins are required")
    for _ in range(2):
        try:
            rows = json.loads(_engine(["inspect", "--type=container", container_id]),
                              object_pairs_hook=_unique_object)
            if not isinstance(rows, list) or len(rows) != 1 or rows[0]["Id"] != container_id:
                raise ValueError
            value = rows[0]
            if value["Image"] != image_id:
                raise LaunchError("container image does not match independent approval")
            selected = {key: value[key] for key in (
                "Image", "Path", "Args", "Config", "HostConfig", "Mounts", "NetworkSettings",
                "Platform", "AppArmorProfile", "ProcessLabel", "MountLabel",
            )}
            raw = json.dumps(selected, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode("ascii")
            if hashlib.sha256(raw).hexdigest() != config_sha256:
                raise LaunchError("container configuration does not match independent approval")
            host = value["HostConfig"]
            if (value["State"]["Status"] != "created"
                    or value["State"]["Running"] is not False
                    or host["ReadonlyRootfs"] is not True
                    or host["Privileged"] is not False
                    or host["CapDrop"] != ["ALL"] or host["CapAdd"] not in (None, [])
                    or host.get("Tmpfs") not in (None, {})):
                raise LaunchError("container is not a new read-only-root execution candidate")
            declared = host.get("Mounts")
            if not isinstance(declared, list) or host.get("Binds") not in (None, []):
                raise LaunchError("explicit nonrecursive bind mounts are required")
            destinations = set()
            if not isinstance(value["Mounts"], list) or len(declared) != len(value["Mounts"]):
                raise ValueError
            for mount in value["Mounts"]:
                destination = mount["Destination"]
                if (mount["Type"] != "bind" or destination in destinations
                        or not isinstance(mount["Source"], str)
                        or not mount["Source"].startswith("/")
                        or destination not in {"/var/lib/swapservice", "/run/swapservice-secrets"}
                        or type(mount["RW"]) is not bool
                        or (destination == "/run/swapservice-secrets" and mount["RW"])):
                    raise LaunchError("container has an unsupported mutable mount")
                declarations = [item for item in declared if item["Target"] == destination]
                if len(declarations) != 1:
                    raise ValueError
                specification = declarations[0]
                options = specification.get("BindOptions")
                if (specification["Type"] != "bind"
                        or specification["Source"] != mount["Source"]
                        or type(specification["ReadOnly"]) is not bool
                        or specification["ReadOnly"] is mount["RW"]
                        or not isinstance(options, dict)
                        or options.get("NonRecursive") is not True
                        or options.get("Propagation") != "rprivate"
                        or options.get("ReadOnlyNonRecursive") not in (None, False)
                        or mount.get("Propagation", "rprivate") != "rprivate"):
                    raise LaunchError("container bind mount policy is inconsistent")
                destinations.add(destination)
        except (ValueError, TypeError, KeyError) as exc:
            raise LaunchError("local container evidence is invalid") from exc
        # Even a never-started container can be changed with docker cp. Reject its
        # entire upper layer, not just a finite source/dependency inventory.
        if _engine(["diff", container_id]).strip():
            raise LaunchError("container writable layer differs from approved image")
    # No tag resolution, pull, command override, automatic restart or retry here.
    # Exclusive trusted Docker administration must span both checks and start.
    return _engine(["start", "--attach", container_id], attach=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--approved-image", required=True)
    parser.add_argument("--approved-config", required=True)
    args = parser.parse_args(argv)
    try:
        return launch(args.container, args.approved_image, args.approved_config)
    except LaunchError as exc:
        print(f"custody launcher refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
