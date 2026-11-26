<div align="center">

# Proofloom

### Reproducible Lean proof experiments.

[![Offline tooling](https://github.com/Jack-mi/proofloom/actions/workflows/tooling.yml/badge.svg)](https://github.com/Jack-mi/proofloom/actions/workflows/tooling.yml)
[![Lean smoke](https://github.com/Jack-mi/proofloom/actions/workflows/lean-smoke.yml/badge.svg)](https://github.com/Jack-mi/proofloom/actions/workflows/lean-smoke.yml)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB)](#start-an-experiment)
[![Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-596273)](LICENSE)

[Start an experiment](#start-an-experiment) · [Run Lean checks](docs/RUNTIME.md) · [Project audit](docs/PROJECT-AUDIT.md) · [Understand the evidence](#understand-the-evidence)

</div>

![Proofloom project illustration](docs/assets/proofloom-hero.webp)

Proofloom makes large Lean proof-reduction experiments reproducible. It inventories an archived snapshot, reconstructs a candidate from a pinned source checkout, and keeps external validation results attached to the exact inputs and candidate they describe.

The bundled snapshot concerns the claimed Lean proof of the Riemann Hypothesis. Its unfinished reduction provides a concrete dataset for investigating dependencies, reproducing transformations, and preserving useful evidence between experiments.

## Follow the experiment

**Inventory the inputs.** Read the actual archive counts, file sizes and SHA256 digests before constructing a candidate. The snapshot's identity includes both transformation inputs and explanatory lists.

**Replay the reduction.** Apply archived cuts and theorem substitutions, honor restored modules, and rewrite imports through retained dependencies. Check the source pin, clean state, required files and character offsets before publishing a new output directory. The supplied source and archived inputs remain unchanged.

**Keep the evidence.** Record external build, export, Comparator and checker attempts with their tool, log and artifact identities. Preserve failures and interrupted attempts alongside later results. Changes to inputs, tools, candidates or artifacts produce an explicit stale result.

**Expose the outcome.** Commands return structured JSON. Invalid inputs and failed build commands return nonzero; a valid report can honestly describe an unverified or stale experiment. The optional workflow follows the same contract.

## Start an experiment

Requires **Python 3.9+** and **Git**. The Python CLI uses the standard library.

```sh
git clone https://github.com/Jack-mi/proofloom.git
cd proofloom
python3 tools/proofloom.py inventory
```

Supply a clean checkout of the [public source mirror](https://github.com/Deicyde/riemann-hypothesis) at commit `8b83d5e7b319ebc7b3be6e59ce3411ba2b09d116`. Choose a new output directory outside both checkouts; its parent must already exist.

```sh
python3 tools/proofloom.py replay \
  --source "/path/to/clean-mirror" \
  --output "/path/to/new-experiment"

python3 tools/proofloom.py report \
  --experiment "/path/to/new-experiment"
```

Replay reads proof source as text and runs this project's transformation tool. It does not compile the Lean submission. Compilation and verification require a properly isolated environment; the [experiment guide](docs/EXPERIMENTS.md) describes that continuation.

An experiment contains:

```text
new-experiment/
├── candidate/       Reconstructed source and copied project configuration
├── manifest.json    Source, snapshot, tool and candidate identities
└── evidence/        Individually recorded external validation attempts
```

## Understand the evidence

The current archived reconstruction contains **172,315 Lean files**, **16,323,398 newline-based source lines** and **968,561,834 source bytes**. Its definition applies archived cuts and substitutions with revert masks, without the unavailable package-aware import-trimming step. The guide gives the complete reconstruction fingerprint and provenance limitations.

**This candidate's proof checks have not run.** It has not been rebuilt with Lean, checked with Comparator, or verified by independent whole-proof kernels in this project. Each proof stage begins as `not_run`.

The offline tooling suite covers replay determinism, Unicode offsets, restored modules, import visibility, input and destination refusal, build failure propagation, stale evidence, partial-export retries, interrupted record writes, runtime integrity and resource controls. These tests establish tooling behavior; they do not establish proof acceptance.

The [Linux validation runtime](docs/RUNTIME.md) has also completed a real small-proof smoke: Lean 4.33.1 builds/exports, Lean 4.36.0 compares the statements and axioms, and its default/paranoid kernels check the proof. Wrong statements, `sorry`, extra axioms and malformed exports are rejected. This checks the verification chain, independently of the unverified RH candidate.

After performing external validation, record the actual result:

```sh
python3 tools/proofloom.py record \
  --experiment "/path/to/new-experiment" \
  --receipt "/path/to/actual-validation-receipt.json"
```

Receipts are operator-recorded external evidence. The recorder checks their shape and file bindings, preserves `passed`, `failed`, `interrupted` and `unsupported` outcomes, and never executes receipt commands. This milestone always reports `proof_accepted: false`; a passing receipt cannot certify a proof. See the [receipt contract and verification guide](docs/EXPERIMENTS.md#identity-and-external-evidence).

## Optional workflow

With ARCHON 0.14 installed, the `proofloom-replay` workflow coordinates inventory, replay and a current identity report. Its nodes exchange typed results and take paths through environment inputs. It has no automatic proof builds or schedules.

```sh
archon validate workflows proofloom-replay --workflow-source "$PWD" --json

archon workflow run proofloom-replay --workflow-source "$PWD" --no-worktree \
  --input "source=/path/to/clean-mirror" \
  --input "output=/path/to/new-experiment"
```

`proofloom-verify` performs runtime preflight and bounded verification on native Linux
or a named Lima guest. Its default profile is the small proof; full RH verification
requires explicit inputs and a large memory budget. See [runtime setup](docs/RUNTIME.md#archon-orchestration).

## Contributor checks

Run the same offline checks used by CI:

```sh
python3 tools/validate_tooling.py
git diff --check
```

The checks exercise synthetic fixtures and validate Python, Shell and C syntax. They fetch no proof source and perform no full Lean build.

## Research and provenance

The research notes preserve the context behind the bundled dataset. Use [EXPERIMENTS.md](docs/EXPERIMENTS.md) for the active tooling instructions; [NOTICE](NOTICE) records source attribution and the changes made for this distribution.

- [Proof map](docs/PROOF-MAP.md): the structure and principal dependencies of the claimed proof.
- [Reduction research](docs/MINIFY.md): the archived reduction work and candidates for shorter mathematical routes.
- [Pruning method](docs/PRUNING.md): source tracing, its assumptions and the failures it exposed.
- [Original build reference](docs/BUILD-ORIGINAL.md): the historical build environment and procedure.
- [Published mathematical results](docs/LITERATURE.md): statements mapped from the formalization.

The underlying Lean formalization was published by Levent Alpöge and is obtained from the pinned public mirror. Its source is not included here and was published without a license. This project's [Apache-2.0 license](LICENSE) covers the tools, data and documentation; it does not license the external proof source. Existing authorship and license notices are retained.
