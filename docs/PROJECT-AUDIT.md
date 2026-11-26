# Project audit and reduction priorities

Proofloom now has a reproducible archived candidate and an executable, pinned
Linux validation route. The small-proof route has been tested with actual Lean
builds, exports, Comparator and kernel checks. The RH candidate itself has not
completed those checks. This distinction governs which reductions count as
validated results.

## Current source measurements

These measurements count newline bytes and classify source lines; they are not
proof-term sizes or estimates of code that can safely be deleted. The complete
source trees are obtained separately from the public pinned mirror. The compact
[measurement artifact](../data/audit/source-summary.json) records the inputs and
classification totals.

| Metric | Original | Replayed candidate | Reduction |
|---|---:|---:|---:|
| Lean files | 175,188 | 172,315 | 2,873 |
| Total source lines | 17,374,242 | 16,323,398 | 1,050,844 |
| Import lines | 909,823 | 892,616 | 17,207 |
| Blank lines | 1,842,465 | 1,753,715 | 88,750 |
| Other source lines | 14,621,954 | 13,677,067 | 944,887 |

The total decrease is **6.05%**. It reproduces the archived cuts, substitutions
and revert masks; it is not a new shortest-proof result. Source tree fingerprint:
`422d4d7fac936e4427a1ad160cd996a5fe07cdb7afa07253bbb70f89c380ae1b`.

The candidate's remaining lines are 5.47% imports, 10.74% blanks and 83.79% other
source. There are no exact duplicate imports within the same file after replay.
There are still 134,330 files using the umbrella `import Mathlib`; narrower imports
may improve elaboration memory and scope, but can add source lines. Import count
and proof dependency count must be measured separately.

| Candidate library | Source lines | Share |
|---|---:|---:|
| Development | 11,809,933 | 72.35% |
| Formalism | 2,761,811 | 16.92% |
| GroundTheory | 977,065 | 5.99% |
| ReciprocityTheory | 527,450 | 3.23% |

Development and Formalism hold approximately 89.27% of source lines. They deserve
most of the semantic reduction investigation. Removing whitespace would produce
large cosmetic savings, not a shorter mathematical proof.

## End-to-end capability inventory

| Stage | Available now | Evidence or remaining limit |
|---|---|---|
| Input inventory | Archive hashes, counts, shape and relationship validation | Tested on the checked-in archive |
| Candidate replay | Pin/clean-source checks, Unicode spans, revert masks, protected roots, deterministic identity | Full 172,315-file text replay matches the reference |
| Runtime installation | Two fixed Linux Lean releases, pinned exporter, official checksum and archive topology checks | Installed and exercised on native ARM Linux |
| Runtime preflight | Actual binaries, compiled/native library payloads, architecture, system tools, sandbox and cgroups | Real preflight passes |
| Candidate build | Explicit current experiment, clean work copy, bound prepared dependencies | Full execution is resource-gated; RH build not run |
| Export | Correct compiler/exporter pair, read-only build tree, successful artifact publication, retained partial output | Small real exports pass |
| Statement/axiom comparison | Clean Challenge, trusted configuration, separate judge toolchain | Small valid proof accepted; mismatch, `sorry` and extra axiom rejected |
| Kernel checking | Default Lean and available paranoid kernel; strict original full profile | Small real kernels pass; full checker compatibility remains to be assessed |
| Evidence | Actual execution logs and artifacts plus separate operator-receipt import | Profile and candidate identity remain explicit; smoke is never RH evidence |
| ARCHON | Replay workflow and deterministic doctor/verify workflow, native Linux or Lima bridge | Clean-checkout Lima smoke completed with `smoke_passed`; full profile not run |
| CI | Offline tool tests plus a real Lean smoke workflow | Both workflows passed on the verified code commit; real smoke remains separate from the offline checks |
| Optimization loop | Existing historical tools and measured research candidates | Search, bounded repair and best-candidate selection are not yet integrated |

The verified code commit is `8a7cf6bbe59ef31446b12e6763e6f29204f4a510`.
Its [offline CI run](https://github.com/Jack-mi/proofloom/actions/runs/45861958210)
passed on Python 3.9 and 3.14, and its
[real Lean smoke CI run](https://github.com/Jack-mi/proofloom/actions/runs/45861958065)
passed on Ubuntu 24.04. The offline suite contains 57 tests plus Python, Shell
and C syntax checks. The clean local ARCHON run
`8d01f6d4f7cae893722c43458034f389` completed both doctor and verify nodes, with
15 real execution stages, a valid proof accepted by both kernels and all four
negative cases rejected. Its checkout stayed at the verified commit throughout.
These results establish the runtime and orchestration path; they do not establish
the RH candidate's validity.

## Improvements made on this path

The new Linux executor operates on the supplied experiment rather than wrapping
the historical early-fork `build-linux.sh`. Build/export cannot read the judge
workspace or host home. Tools and dependency packages are mounted read-only, with
one writable work tree and no network. The work tree's source/configuration are
checked before and after execution; unbound root build caches are not copied.

Runtime validation now includes `.olean` and native libraries, not only the Lake
shim or a `--version` string. Package checks include Git revision/clean state,
ignored extra source refusal, the entire compiled/native cache and compiled Lake
configuration. Candidate and clean Challenge packages must agree.

Real execution exposed an important resource error: `RLIMIT_AS` capped virtual
address space and stopped Lean from creating threads before proving anything.
The executor now uses systemd cgroup `MemoryMax`, disabled swap, CPU quota and
CPU affinity. Limits are queried back from the resource manager. A no-network,
narrow-mount probe is required, including the system `which` link needed by
Comparator and empty directories used by its nested sandbox.

Large-build cleanup has also changed. A `.setup.json` is removed only after the
matching ordinary module `.trace` is newer. File age alone does not prove that a
module has finished. Cleanup refuses symlink escape and preserves the actual
build exit status. Evidence publication is atomic; interrupted writes do not
block later records, and partial failed exports do not lock later successful
attempts to incomplete bytes.

## Resources and execution scope

The local Apple Silicon machine has 16 GiB physical RAM. Its dedicated Linux VM
has four CPUs, 6 GiB RAM and a 60 GiB disk. This is enough for the owned small-proof
smoke with a 2 GiB cgroup allowance. It is not enough for a historical RH export
that reached about 163 GB RAM, or independent checkers that used roughly 80–236 GB.

The full profile currently requires at least 256 GiB visible Linux RAM and an
explicit budget. Approximately 600 GiB disk is a starting capacity estimate, not
a quota or fit guarantee. No paid machine has been created. [RUNTIME.md](RUNTIME.md)
provides the same installation and validation commands for a prepared large Linux
host. Source preparation, trusted cache preparation and immutable inputs must
remain distinct from running the submitted code.

## Next experiments, in order

1. **Obtain a buildable, validated baseline.** Run the full profile on a suitable
   host. If the archived candidate fails, preserve the original errors and fix
   one root cause per batch. The original code can serve as the build reference;
   the archived candidate must earn its own result.
2. **Produce the current dependency data.** Export and index the actual passing
   candidate; regenerate package import/declaration dumps rather than relying on
   missing historical intermediates. Parameterize `reach.py` outputs and retire
   fixed-host repair loops from active operation.
3. **Try low-risk mechanical reductions.** Trim transitive imports with the
   complete package graph. Substitute already-visible identical theorem
   statements with shorter verified proofs. Build changed modules and downstream
   consumers before a full check. No new import scope is added by default.
4. **Choose proofs by dependency cost.** Compare identical statements by proof
   size, source lines and dependency closure. The current historical selector
   prioritizes build order; its "smallest proof" description should not be
   mistaken for a verified global minimum.
5. **Investigate shorter mathematical routes.** Start with heavy dependency
   steps in Development/Formalism. Historical dominator weights identify useful
   targets, such as the theta step carrying 37.75% of expression nodes; that is
   not 37.75% of source lines or an automatically removable fraction. Check
   whether a cheaper existing theorem or a necessary special case replaces a
   general construction.

File bundling and lemma inlining may improve navigation or build scheduling.
Their file/declaration savings must not be reported as the same percentage of
proof-content reduction. Simple formatting savings should be reported separately.

## How ARCHON should control optimization

A reduction run should own one hypothesis and one immutable parent candidate.
Deterministic nodes compute the transformation, identities and metrics; models
judge causes and propose changes from a small error/dependency slice. Models do
not read a 61.6 GB export into context or act as proof kernels.

For each batch: establish inputs, generate the candidate, build affected modules,
perform at most a small explicit number of repairs, then pass the full validation
gate. Keep failure artifacts, the rollback target and time/memory budget. Promote
a candidate to "best" only if its target statements, axioms and actual proof
checks pass and its measured objective improves.

The current executor starts new run directories and does not reuse full build
checkpoints. Durable stage resume, separate structured checker compatibility/OOM
outcomes, automatic execution-to-receipt import, disk quotas and dependency-cost
candidate selection remain concrete next increments. These are separate from
having a working validation environment and should be delivered with their own
failure-path tests.

The useful optimization objective is a smaller **validated** proof and lower
build/check cost, not simply the lowest newline count. A build failure does not
refute RH; a smoke pass does not prove it.
