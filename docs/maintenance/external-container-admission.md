# External container admission primitive

## Status and trust boundary

This is one narrow Batch 0 increment, **not deployment or release acceptance**. The
standalone [launcher](../../scripts/custody_external_launcher.py) imports no repository
runtime. It refuses before starting a pre-created Docker container unless an independently
approved image ID and selected execution-configuration digest match, the container is new,
its root is read-only, and its writable upper layer is pristine. All existing witness,
restore, financial, service-identity and live-chain gates remain mandatory.

The launcher does not attest itself. Independently install and protect the launcher, its
Python interpreter/stdlib, `/usr/bin/docker`, local Docker daemon/image store, host/kernel
and approval arguments **outside** the mutable service artifact. Use a protected service
unit with a clean environment and Python isolated mode (`-I`); running this script from an
operator-writable checkout does not establish independent authority. Runtime credentials
and repository code must not be able to change the launcher, pins or Docker configuration.
No image, container or approval is created by this tool.

Exclusive trusted Docker administration must span inspection through start: Docker does not
provide an atomic inspect/diff/start compare-and-swap. Another Docker administrator can
bypass the tool or change evidence after the final check. Repeated inspections contain
observable drift; they do not eliminate this trust assumption. Independently protected
image storage and host integrity, not a Docker-reported ID alone, establish byte authority.

## Exact approval contract

The independent approver supplies:

- `--container`: full 64-character lowercase hexadecimal local container ID;
- `--approved-image`: `sha256:` followed by the full 64-character lowercase hexadecimal
  Docker image configuration ID, **not** a tag, short ID or registry manifest digest;
- `--approved-config`: SHA-256 of the ASCII canonical JSON projection of one Docker
  container-inspect object containing exactly these top-level fields:

```text
Image, Path, Args, Config, HostConfig, Mounts, NetworkSettings,
Platform, AppArmorProfile, ProcessLabel, MountLabel
```

Canonical encoding uses `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=True`
and no NaN/infinity values; array order is preserved. The projection deliberately omits
volatile status, timestamps and the container ID; the supplied exact container ID is
checked separately, and both observations must have status `created` and `Running=false`.
Network endpoints/aliases/ports and the entire selected security/host/configuration objects
are pinned, not inferred from a network label. A changed candidate needs independent review,
not automatic approval of its new digest. The launcher has no digest-export/approve mode.

`Config.Env` may contain credentials. Compute/review this projection in the protected
approval domain; do not publish raw inspect JSON or pass credentials as command-line
arguments. The launcher never prints inspected evidence or Docker inspection errors.
After approval it attaches to the service's normal output, whose existing secret-safe
logging obligations remain separate.

## Container constraints

Prepare the candidate without starting it. The image must contain the repository,
interpreter, dependencies, standard library, bytecode/native libraries and intended
entrypoint in its read-only execution tree. Independently review its complete execution
closure and ensure neither custody nor secrets directories are import/plugin/executable
search roots or symlinks to executable locations. A pinned unsafe image is still unsafe.

The guard requires:

- a never-started (`created`, not running) container with the exact approved image ID;
- `ReadonlyRootfs=true`, `Privileged=false`, `CapDrop=["ALL"]`, no added capabilities;
- no user-configured tmpfs, anonymous image volumes or legacy `HostConfig.Binds`;
- only explicit bind destinations `/var/lib/swapservice` (custody data) and
  `/run/swapservice-secrets` (read-only credentials); source paths, permissions and all
  other mount options are part of the independent config approval;
- each declared and observed bind agrees on source/target/read-only permission, with
  `BindOptions.NonRecursive=true` and explicit `Propagation=rprivate`. Nested host mounts
  are excluded; writable recursive secrets mounts and shared propagation are refused;
- empty `docker diff` on both checks, including for modifications made with `docker cp`
  before first execution.

Docker still supplies runtime pseudo-filesystems such as `/proc`, `/dev` and `/dev/shm`.
These and host-bind contents are not image bytes. No claim of complete immutable execution
or mapped-memory attestation follows from a read-only root or empty upper-layer diff.

The independently protected invocation has this shape (placeholders are intentionally
not approvals):

```bash
/protected/python3 -I /protected/custody_external_launcher.py \
  --container <FULL_CONTAINER_ID> \
  --approved-image sha256:<APPROVED_DOCKER_IMAGE_ID> \
  --approved-config <INDEPENDENT_CONFIG_SHA256>
```

The tool calls fixed `/usr/bin/docker` with an empty inherited environment except a fixed
system PATH and inert HOME, so caller-controlled Docker contexts, remote daemon URLs,
proxy configuration and loader/Python environment do not redirect the Docker child.
It inspects configuration and the upper layer twice, then starts/attaches to the exact
container ID without pulls, tag resolution, command overrides or retries. Child exit status
is preserved. It never deletes containers, clears custody holds or rewrites certificates.
Do not configure an independent automatic restart route that bypasses admission.

## Verification and remaining exits

Collected [offline tests](../../tests/test_custody_external_launcher.py) inject the trusted
Docker boundary. They verify reported image/command/configuration/network/security/layer
drift, mount constraints, repeated checks, strict pins/JSON, sanitized authority failures,
clean child environment and exact-ID start ordering. An actual isolated Python subprocess
verifies CLI refusal before a mutable `sitecustomize.py` can execute. These are not real
Docker file-mutation, complete service generation, witness-contention or deployment tests.

This host denies access to `/var/run/docker.sock`; no image/container was built or started.
The actual CLI consequently refuses with a sanitized authority-unavailable message.
Before accepting Batch 0, separately establish protected installation/approval ownership,
reproducible image construction and complete closure, witness binding to the external
image authority, real Docker inspect/mount/diff semantics, a clean approved
claim/run/seal generation and the full mutation/concurrency/crash matrix. Direct
`python swapService.py` remains unchanged and is not an externally attested launch path.
Production and real funds remain blocked. See [the evaluation](../EVALUATION.md) and
[the prioritized plan](../plans/2026-09-25-recovery-admission-and-capacity-fairness.md).
