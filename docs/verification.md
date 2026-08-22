# Verification

`./verify.sh` is the single command that runs every correctness surface of this
repository that does not need the one-billion-row dataset.

```bash
./verify.sh
```

It requires a C toolchain, `make`, and Python 3. It does not require root
privileges, network access, or a generated dataset. It writes only inside
`.test-work/`, removes that directory before exiting, and compares
`git status` before and after, failing if the gate itself changed the working
tree. A clean clone therefore stays clean, and a checkout with work in
progress is not reported as a failure.

The gate is budgeted to finish in under five minutes on a modern desktop or CI
runner, which is why the continuous-integration job declares a bounded
`timeout-minutes`. Every stage is deterministic: no case is sampled from the
clock, the environment, or a dataset.

## Stages

| Stage | Entry point | What it proves |
| --- | --- | --- |
| Build | `scripts/build.sh` | The optimized program and the independent oracle compile with the repository's flags. |
| Oracle smoke | `tests/baseline-smoke.sh` | The oracle reproduces a checked-in expectation and rejects an invalid temperature. |
| Fixtures and sanitizer regression | `tests/fixture-oracle-test.sh` | Byte-exact output for the dense sentinel/direct path, the collision fallback, long-key fallback, the exhaustive temperature domain, and page-aligned EOF records; then the same inputs through ASan and UBSan builds of the worker loops. |
| Parser and EOF corpus | `tests/parser-fuzz.sh` | 102 deterministic cases compared against the oracle at one and several threads, plus a guard-page sanitizer harness for out-of-contract input. See [`parser-fuzz-corpus.md`](parser-fuzz-corpus.md). |
| General-input cardinality | `tests/general-input-test.py` | Dense escape at 414 names, 3,000 and 16,384 generic names, oversized output, multi-segment merges, and bounded rejection above capacity. See [`general-input.md`](general-input.md). |
| Strict input and runtime envelope | `tests/strict-input-test.py` | `ONEBRC_STRICT=1` rejects every out-of-contract record class with exit `2`, empty stdout, and a deterministic diagnostic; `NTHREADS` rejects hostile values; the non-strict cold paths stay bounded under sanitizers. |
| Repository contracts | `tests/gate-contract-test.py` | The gate, `scripts/validate.sh`, CI, scratch-directory cleanliness, standalone paths, and this documentation have not drifted apart. |
| Contract mutations | `tests/contract-mutation-test.py` | Each contract check still fails when the contract it protects is removed. |

Every stage is also runnable on its own; the gate exists so that none of them
can be forgotten.

## Full validation

```bash
./scripts/validate.sh measurements_1B.txt
```

`scripts/validate.sh` runs the gate first and then adds the one comparison that
does need data: the optimized program against the independent C oracle over a
complete dataset, byte for byte. The oracle output is cached under
`.oracle-cache/`, keyed by dataset and oracle-source hash.

The dataset is required only for that final comparison. Everything else lives
in `./verify.sh`.

## Continuous integration

[`.github/workflows/verify.yml`](../.github/workflows/verify.yml) runs
`./verify.sh` on Linux for every pull request and every push to `main`. The
workflow checks out the repository and runs the gate; it installs nothing and
uses no elevated privileges, which keeps continuous integration and a local
clean clone identical.

## Drift protection

`tests/gate-contract-test.py` fails when:

- `verify.sh` is missing, is not executable, drops a stage, or stops comparing
  the working tree before and after the run;
- `scripts/validate.sh` stops reusing the gate or re-implements one of its
  stages;
- the workflow stops triggering on pull requests or on `main`, stops running
  the gate, drops its timeout bound, or starts installing packages or using
  `sudo`;
- a gate script writes outside the ignored scratch directories, or the
  scratch directories stop being ignored;
- a tracked file references a repository-relative path that does not exist, or
  an absolute host path;
- a source comment in `c/main.c` carries an experiment identifier instead of
  describing current behavior.

`tests/contract-mutation-test.py` keeps those checks honest. It applies each
drift above to an in-memory copy of the repository and fails if any of them
goes undetected, so a check cannot silently stop enforcing its contract. It
never writes to the working tree.
