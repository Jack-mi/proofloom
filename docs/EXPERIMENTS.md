# Reproduce the archived candidate

From this checkout, with Python 3.9+ and Git:

```sh
python3 tools/proofloom.py inventory
python3 tools/proofloom.py replay --source /path/to/clean-mirror --output /path/to/new-experiment
python3 tools/proofloom.py report --experiment /path/to/new-experiment
python3 tools/proofloom.py record --experiment /path/to/new-experiment --receipt /path/to/receipt.json
```

The mirror must be a clean checkout at
`8b83d5e7b319ebc7b3be6e59ce3411ba2b09d116`. Replay reads text and runs only
this repository's `prune.py`; it never compiles or executes proof code. The output must
not exist, and must be outside both the source and this repository, without containing either.
Its parent must exist. Paths containing spaces, quotes and shell metacharacters are supported.
All commands emit one JSON object on success; diagnostics go to stderr. Invalid input
fails nonzero. A valid stale report exits zero with `outcome: stale` and
`proof_accepted: false`.

`--archive-dir DIR` on inventory/replay selects an explicit snapshot, without relaxing
the source pin. The default is `data/min` relative to the tool, independent of cwd.
Replay validates inputs first, builds in a unique sibling temporary directory, rechecks
identities, then publishes the experiment. Failures remove only that temporary directory.
It preserves the supplied source and archive. Do not concurrently publish to the same destination.

## What is reconstructed

The archive contains 172,315 kept modules, 175,188 cut-map modules and 90,693 raw cuts;
41,668 swap-map modules contain 43,069 raw swaps. The explanatory merged-theorem TSV
contains 43,266 rows. All six input files are hashed and inventoried, including the
build-needed and merged-theorem lists, which are explanatory and are not reapplied.

The 900 modules in `files-kept-as-they-were.txt` receive empty cuts and no swaps before
span validation. This masks 194 cuts in 15 modules and 7 swaps in 7 modules. Active
spans use decoded Python character offsets, including Unicode, and must have valid
bounds and no overlaps. Kept/swap/revert membership and all expected source/configuration
files are checked. Main, Statements and Challenge have no archived transformations
and remain byte-identical; the six project configuration files are copied unchanged.

Dropped local imports expand to the frontier of kept modules and external package
imports through `prune.py`. Package import dumps used by the historical import-trimming
step are absent. This baseline performs **no package-aware import trim**, attribute
cleanup or merging. `make-fork.sh` remains the separate historical early-fork entry point;
its expected fingerprint is not this snapshot's fingerprint. Do not launch the historical
remote repair scripts to reproduce this baseline.

The real text-only reconstruction measured:

| Metric | Archived reconstruction |
|---|---:|
| Lean files | 172,315 |
| Newline-based source lines | 16,323,398 |
| Source bytes | 968,561,834 |

`candidate/src` tree SHA256:
`422d4d7fac936e4427a1ad160cd996a5fe07cdb7afa07253bbb70f89c380ae1b`.
This matches the reference using archived cuts/swaps, revert masks and existing prune.
It is distinct from README's final historical 172,500-file / 15.95M-line candidate.
No round71 identity is established, and these measurements establish no proof validity
or build-time improvement.

## Identity and external evidence

The output has `candidate/src`, six copied configuration files, `manifest.json` and
`evidence/`. A versioned manifest binds archive file digests/counts, the clean source
commit and Git tree, actual source tree, transformation tool digests (`proofloom.py`
and `prune.py`), protected configuration digests, and candidate contents/counts.
Canonical JSON binds these components into `candidate_id`. Paths and timestamps are
provenance, outside that identity. The source-tree digest uses `tools/tree_hash.py`'s
algorithm: relative paths sorted as strings, hashing each path + NUL + the SHA256 bytes
of its file. Lines count newline bytes, rather than adding a line for an unterminated file.

`report` and `record` recompute identities, including the supplied source and archive.
Keep those inputs available. Changed or unavailable inputs, candidate/configuration,
or replay tools make the experiment stale. Reporting also rehashes evidence logs,
executables and artifacts; unavailable or changed files make that evidence stale.
Hashing is bounded streaming, but repeatedly reading a 61.6 GB export still costs
61.6 GB of disk I/O per pass. Recording performs a second identity/artifact check before
writing. Immutable, distinctly identified attempts preserve earlier failures. Records are written to a same-directory temporary file and published only after a complete write, without replacing an existing record. The latest
attempt determines each displayed stage; stale attempts remain visible.

Every stage initially says `not_run`: build, export, Comparator/axioms and each named
independent checker (`leanchecker-paranoid`, `nanoda`, `con-leche`, `con-ron`, `Tenet`,
`tenet-hs`, `tenet-jvm`). Interrupted and unsupported attempts retain those statuses.
This milestone always reports `proof_accepted: false`, even if every stage has a
submitted passing receipt. Receipts are **operator-recorded external evidence**;
the recorder validates bindings and shapes, never runs their commands and cannot
establish that their statements about verification are true.

A receipt is a JSON object with exactly these required fields:

```json
{
  "version": 1,
  "candidate_id": "<candidate_id from report>",
  "stage": "build",
  "outcome": "passed",
  "exit_code": 0,
  "tool": {"name": "lake", "version": "<actual version>", "executable": "/absolute/path/to/lake"},
  "command": ["lake", "build", "Main"],
  "log": "/absolute/path/to/actual-build.log",
  "artifacts": {}
}
```

This is a shape illustration: substitute real results, executable and logs after running
verification. A passed stage requires actual exit code zero and all required files;
exit zero alone does not suffice. Outcomes are `passed`, `failed`, `interrupted`, or
`unsupported`; `exit_code` may be null when no exit status is available. Optional
`elapsed_seconds` and `peak_rss_bytes` are included only when actually measured.
Use `checker` with an individually named checker for `stage: checker`.

Required artifact names for passed receipts:

| Stage | `artifacts` keys |
|---|---|
| build | none (log and executable still required) |
| export | `solution_export` |
| comparator | `challenge_export`, `solution_export`, `comparison_config` |
| checker | `solution_export` |

Values are readable file paths. Failed/interrupted/unsupported attempts may omit artifacts
that were not produced, but still need actual tool and log provenance. The comparison
configuration must match the copied config. Current passed export, Comparator and checker
attempts must agree on the solution export digest. Failed, interrupted, unsupported or
stale attempts do not lock later successful evidence to a partial export. Keep each attempt's
artifacts at separate immutable paths so earlier failures remain available for inspection.
Relative receipt paths resolve from the recorder's cwd and are stored as absolute paths.

## Expensive verification later

**No full Lean rebuild, Comparator/axiom check, or independent whole-proof checker was
run for this archived candidate.** Offline tests, replay fingerprints and ARCHON probes
are tooling evidence only.

Prepare a suitable fresh Linux machine with enough disk/RAM, outside this host and
repository. Follow the pinned mirror's README and verification notes to install Lean
4.33.1, its pinned packages and `lean4export` v4.33.0 built with Lean 4.33.1. Prepare the
trusted Mathlib cache before disabling networking. Obtain a clean Challenge export
separately, before compiling the submitted candidate. Keep the original replay source
read-only; use a separate clean pinned clone for Challenge preparation.

The pinned mirror's actual commands are:

```sh
# In the separate clean pinned clone, with Lean 4.33.1 and lean4export prepared:
cd "$CLEAN"
lake exe cache get
lake build Challenge
lake env lean4export Challenge -- $(cat export-targets.txt) > challenge.export

# In EXPERIMENT/candidate, with Lean 4.33.1:
# Fetch the trusted cache during preparation, before entering the sandbox.
lake exe cache get
# Execute BOTH commands below inside a no-network sandbox:
lake build Main
lake env lean4export Main -- $(cat "$CLEAN/export-targets.txt") > solution.export

# With Lean 4.36.0 active, from an empty judging directory containing solution.export:
lake comparator --config "$CLEAN/comparator.json" --challenge-from-export "$CLEAN/challenge.export" --solution-from-export solution.export
```

Use a no-network container/read-only root or the existing Linux bubblewrap pattern
(`--unshare-net`, read-only root, candidate and scratch writable) for build and solution
export. Check the sandbox probe succeeds first. Capture real status, logs, version output
and exports; receipt argv should reflect what actually ran, including sandbox wrappers
when applicable. Copy only validation artifacts out for judging. Comparator uses the
untouched config's five targets (`riemannHypothesis`, `riemannZeta_eq_zero_re`,
`LFunction_ne_zero_of_half_lt_re`, `LFunction_eq_zero_re`,
`LFunction_eq_zero_re_of_isPrimitive`) and three permitted axioms (`propext`,
`Quot.sound`, `Classical.choice`). Record each independent checker separately from its
real outputs. An incompatible checker stays unsupported; a stopped check stays interrupted.
The recorder does not substitute for examining those results.

## ARCHON and contributor checks

With installed ARCHON 0.14, from this checkout:

```sh
archon validate workflows proofloom-replay --workflow-source "$PWD" --json
archon workflow run proofloom-replay --workflow-source "$PWD" --no-worktree \
  --input "source=/path/to/clean-mirror" --input "output=/path/to/new-experiment"
python3 tools/validate_tooling.py
git diff --check
```

The project workflow has deterministic inventory → replay → report bash nodes, typed
JSON outputs, environment-delivered inputs and no provider/model configuration. Optional
`archive_dir` defaults to an empty value, which omits the flag. There are no automatic
builds, schedules or queues. `--dry-run --exec-code` executes these text-only nodes;
it is a workflow/schema/quoting check, never proof verification. The implementation's
bounded probe used a temporary synthetic Git source and process-local test pin injection;
there is no production CLI pin bypass. Installed 0.14 workflow validation and that probe
are recorded separately from Factory implementation/review and proof validation.

The final report node uses `always_run: true` to recompute identity on resume. A resumed
run may reuse completed inventory/replay nodes: use standalone CLI `report` or a fresh
report invocation for current evidence after external validation. Cached node success is
not fresh validation evidence.

Offline unittest fixtures contain only synthetic source and fake executable PATH stubs.
CI runs these tooling tests and Python/Shell/C syntax checks on Ubuntu with Python 3.9
and a current supported Python. It never fetches or compiles the original proof.
