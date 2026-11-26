# Run real Lean verification

Proofloom has two separate verification profiles. `smoke` checks the execution
chain on a small, repository-owned theorem and deliberate errors. `validate`
checks a reconstructed RH candidate on a sufficiently large Linux machine.
Neither profile turns the evidence recorder into an automatic mathematical
authority, and a smoke pass says nothing about the RH candidate.

The fixed tools are Lean **4.33.1** for build/export, lean4export commit
`15f6055e299ad5b89345e533cc2192f4cc00f659` built with that compiler, and Lean
**4.36.0** for Comparator and its bundled kernels. The Linux runtime records
the release checksums, actual executables, architecture and complete toolchain
payloads, including `.olean` and native libraries.

## Linux setup

Use a dedicated Linux account with a working systemd user manager and cgroup v2.
The runner requires real bubblewrap isolation; it has no unsandboxed fallback.
On Ubuntu 24.04, install prerequisites:

```sh
sudo apt-get update
sudo apt-get install -y git curl ca-certificates python3 bubblewrap zstd \
  build-essential time dbus-user-session
```

Ubuntu's AppArmor policy may restrict unprivileged user namespaces. The supplied
[Lima configuration](runtime/lima.yaml) and [smoke CI](../.github/workflows/lean-smoke.yml)
grant that permission specifically to `/usr/bin/bwrap`. The sandbox still creates
its own user, mount and network namespaces. Do not work around a failed probe by
running the proof without isolation or replacing bubblewrap with a stub.

In an SSH session, the user manager normally supplies `XDG_RUNTIME_DIR` and
`DBUS_SESSION_BUS_ADDRESS`. If the manager is absent, configure it for the dedicated
account, then reconnect:

```sh
sudo loginctl enable-linger "$(id -un)"
sudo systemctl start "user@$(id -u).service"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
systemd-run --user --scope --quiet -p MemoryMax=512M /bin/true
```

Create a new runtime directory outside the repository. Installation verifies
official release SHA256s and validates archive paths before extraction. It does
not install elan or change a global toolchain selection.

```sh
mkdir -p "$HOME/proofloom-runs"
python3 -B tools/setup_lean.py --root "$HOME/proofloom-runs/runtime"
python3 -B tools/lean_verify.py doctor \
  --runtime "$HOME/proofloom-runs/runtime/runtime.json"
```

An existing release cache can be supplied with `--downloads /path/to/releases`.
Every cached archive is checksum-verified. An existing runtime destination is
refused. Installation and real prefix hashing need several GB of disk and disk
I/O; toolchain payloads are not checked by version strings alone.

## Apple Silicon Linux backend

The supplied template uses Lima 2.3 with native Apple Virtualization, four CPUs,
6 GiB RAM and a 60 GiB guest disk. It mounts only this repository read-only and
an explicit work directory writable. It does not mount the host home directory
or load its SSH public keys. Install [Lima](https://lima-vm.io/docs/installation/),
then start this named guest:

```sh
mkdir -p "$HOME/proofloom-runs/work"
limactl start -y --name=proofloom \
  --param "repository=$PWD" \
  --param "work=$HOME/proofloom-runs/work" \
  docs/runtime/lima.yaml
limactl shell proofloom -- python3 -B /proofloom/tools/setup_lean.py \
  --root /proofloom-work/runtime
```

Runtime and output paths supplied through a backend are **Linux guest paths**.
The fixed bridge takes each path as an argument, not as shell code:

```sh
python3 -B tools/linux_backend.py doctor --backend lima \
  --verifier /proofloom/tools/lean_verify.py \
  --runtime /proofloom-work/runtime/runtime.json

python3 -B tools/linux_backend.py smoke --backend lima \
  --verifier /proofloom/tools/lean_verify.py \
  --runtime /proofloom-work/runtime/runtime.json \
  --output /proofloom-work/smoke-1 \
  --timeout 120 --memory-mib 2048 --jobs 2
```

Use `--limactl /absolute/path/to/limactl` when it is not on PATH. For a custom
installation, supply `--lima-home /path/to/instances` (workflow input `lima_home`)
so the named guest remains resolvable across detached runs. Stop the local
VM when it is no longer needed with `limactl stop proofloom`.

## What smoke checks

The trusted Challenge states natural-number addition commutativity. The valid
solution uses `Nat.add_comm`; no Mathlib download is needed for this smoke.
Challenge is built and exported before a solution is compiled. Comparator checks
the actual statement and permitted axioms, then runs the default Lean kernel and,
when present, the bundled paranoid kernel.

Four deliberately bad cases must be rejected: a different statement, `sorry`, an
extra axiom, and a malformed export. A failure to run the valid case prevents a
negative result from being presented as successful proof rejection.

```sh
python3 -B tools/lean_verify.py smoke \
  --runtime /path/to/runtime/runtime.json \
  --output /path/to/new-smoke-run \
  --timeout 120 --memory-mib 2048 --jobs 2
```

The verified local run completed 15 real stages. Both normal and paranoid kernels
accepted the valid proof; all four negatives were rejected. The result is
`smoke_passed`, `smoke_profile: true`, `proof_accepted: false`.
The [Lean smoke CI](../.github/workflows/lean-smoke.yml) reproduces this check
separately from offline Python/Shell/C tests.

## Full RH candidate preparation

The current reconstructed candidate has **172,315 files and 16,323,398 lines**.
Its full Lean validation remains unexecuted. The full profile requires at least
256 GiB visible Linux RAM and a memory allowance of at least 262,144 MiB. This is
a conservative execution precondition based on the historical export/checking
workloads, not an assurance that every checker fits. Prepare substantially more
disk than the sources: approximately 600 GiB is a useful starting allowance for
two trusted dependency copies, compiled modules and a potentially 61.6 GB export.
The 6 GiB local VM must refuse this profile.

Use the pinned [mirror](https://github.com/Deicyde/riemann-hypothesis) at
`8b83d5e7b319ebc7b3be6e59ce3411ba2b09d116`. Keep the replay source pristine.
Prepare another clean pinned checkout for Challenge, using the build toolchain's
direct Lake binary to fetch the trusted Mathlib cache before network isolation.
The full process does not fetch packages during a proof build.

```sh
BUILD_LAKE="/path/to/runtime/lean-4.33.1-linux/bin/lake"
BUILD_BIN="$(dirname "$BUILD_LAKE")"
CLEAN="/path/to/separate-clean-pinned-mirror"
EXPERIMENT="/path/to/replayed-experiment"

(cd "$CLEAN" && PATH="$BUILD_BIN:$PATH" "$BUILD_LAKE" exe cache get)
```

For ARM, the prefix suffix is `linux_aarch64`. Copy the **trusted prepared package
directory** into the candidate; do not run a network-enabled preparation command
from untrusted build outputs. The `.lake` cache is outside the text-candidate
identity and receives its own full-validation identity:

```sh
test ! -e "$EXPERIMENT/candidate/.lake/packages"
mkdir -p "$EXPERIMENT/candidate/.lake"
cp -a "$CLEAN/.lake/packages" "$EXPERIMENT/candidate/.lake/packages"
```

Each Git package must match its pinned commit, be clean, contain no ignored extra
Lean source, and have its prepared compiled cache. Full validation compares the
complete native/Lean build cache and compiled package configuration to the clean
copy. A `lakefile.olean` or native plugin cannot differ while keeping the `.olean`
subdirectory identical. Preparations that leave a package without its required
cache fail rather than causing a silent download or rebuild outside policy.

```sh
python3 -B tools/lean_verify.py validate \
  --runtime /path/to/runtime/runtime.json \
  --experiment "$EXPERIMENT" --challenge "$CLEAN" \
  --output /path/to/new-full-run \
  --stage all --timeout 86400 --memory-mib 262144 --jobs 8
```

`--stage build` stops after clean Challenge build/export and the candidate build;
`export` adds candidate export; `compare` and `all` also run Comparator. The
strict original checker configuration is preserved. An incompatible checker can
cause an unsuccessful aggregate comparison; inspect the individual logs and do
not interpret that as a counterexample to RH. A different compatibility profile
would need its own explicit configuration identity.

The executor copies only bound source, six project configuration files and
prepared packages into a separate work directory. It never imports an unbound
candidate-root `.lake/build` cache. The source and configuration of that work
copy are checked after build/export, dependencies and runtime payloads are
rechecked, and only export files enter the judge. Full validation outputs real
stage JSON/logs and artifacts in the run directory; it does not invent passing
receipts for the existing text experiment.

## Outcomes, controls and current limits

Every stage uses a real systemd user scope: cgroup `MemoryMax`, no swap,
`CPUQuota`, and at most 512 tasks. `--jobs` controls allocated CPUs and Lean
threads rather than being a Lake scheduler flag. A timeout stops the recorded
scope and process group, retaining logs and partial exports. A failed export is
never renamed as the successful artifact. The requested memory limit is read
back from the resource manager; a virtual-address-space limit is not substituted
for physical memory control.

`result.json` describes the profile, runtime, budget and completed stages.
Unexecuted stages have no successful execution record. Each executed stage has
an exit code, command, elapsed time, log digest and any produced export digest.
Observed resource properties establish the configured limits; a sampled
`MemoryPeak` is not presented as the final measured peak RSS. OOM may currently
surface as status 137 or a signal with failure logs rather than a dedicated
`resource_limit` outcome. The cause must be inspected, not converted into a
mathematical refutation.

Outputs are new run directories. Completed validation is not resumed inside this
first executor; a new attempt uses a new output and preserves prior evidence.
Checkpointed full-build reuse and individually structured checker verdicts are
next improvements. The existing `record` command remains an explicit import
of operator-recorded evidence; see [EXPERIMENTS.md](EXPERIMENTS.md).

## ARCHON orchestration

`proofloom-replay` continues to reconstruct text candidates. `proofloom-verify`
adds deterministic runtime preflight and bounded validation. Model nodes are not
used to hold a long build open, guess process ownership or infer a pass from prose.

```sh
archon validate workflows proofloom-verify --workflow-source "$PWD" --json
archon workflow run proofloom-verify --workflow-source "$PWD" --no-worktree \
  --input backend=lima --input profile=smoke \
  --input verifier=/proofloom/tools/lean_verify.py \
  --input runtime=/proofloom-work/runtime/runtime.json \
  --input output=/proofloom-work/archon-smoke-1 \
  --input timeout=120 --input memory_mib=2048 --input jobs=2
```

For full verification, use `profile=full` with explicit `experiment`, `challenge`,
`memory_mib=262144` and a suitable timeout on the large Linux backend. No workflow
creates a paid machine, enables a schedule, or buys a larger resource allowance.
