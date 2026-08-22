#!/usr/bin/env python3
"""Mutation test proving the repository contract checks still have teeth.

Each mutation describes a way the verification gate, continuous integration,
scratch-directory cleanliness, or the standalone-repository rules could drift.
The mutated contents are supplied to ``tests/gate-contract-test.py`` in memory,
so this test never writes to the working tree.

A mutation that survives means the corresponding contract is no longer
enforced.

Usage: python3 tests/contract-mutation-test.py
"""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "tests" / "gate-contract-test.py"


def load_contract():
    # Importing must not leave a __pycache__ directory behind: the gate fails
    # if it changes the working tree.
    sys.dont_write_bytecode = True
    specification = importlib.util.spec_from_file_location(
        "gate_contract_test", CONTRACT
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def substitute(relative, old, new, occurrences=-1):
    """Replace every occurrence by default, so a contract cannot survive by
    being stated twice."""

    def mutate(_):
        text = (ROOT / relative).read_text(encoding="utf-8")
        if old not in text:
            raise AssertionError(f"{relative}: mutation source missing: {old!r}")
        return {str(relative): text.replace(old, new, occurrences)}, {}

    return mutate


def unexecutable(relative):
    def mutate(_):
        return {}, {str(relative): False}

    return mutate


def removal(relative):
    def mutate(_):
        return {str(relative): None}, {}

    return mutate


MUTATIONS = (
    ("the gate is deleted", removal("verify.sh")),
    ("the gate is not executable", unexecutable("verify.sh")),
    (
        "the gate drops the strict-input stage",
        substitute("verify.sh", "python3 tests/strict-input-test.py c\n", ""),
    ),
    (
        "the gate drops the parser corpus stage",
        substitute("verify.sh", "bash tests/parser-fuzz.sh", "true #"),
    ),
    (
        "the gate stops comparing the working tree",
        substitute("verify.sh", "git status --porcelain", "true"),
    ),
    (
        "the gate records the working tree only once",
        substitute("verify.sh", "git status --porcelain", "true", 1),
    ),
    (
        "the gate stops acting on the comparison",
        substitute("verify.sh", '"$STATUS_AFTER" != "$STATUS_BEFORE"', "false"),
    ),
    (
        "the gate stops removing its scratch directory",
        substitute("verify.sh", 'rm -rf -- "$WORK"', "true"),
    ),
    (
        "the gate starts requiring root",
        substitute("verify.sh", "./scripts/build.sh", "sudo ./scripts/build.sh"),
    ),
    ("continuous integration is deleted", removal(".github/workflows/verify.yml")),
    (
        "continuous integration loses the pull-request trigger",
        substitute(".github/workflows/verify.yml", "  pull_request:\n", ""),
    ),
    (
        "continuous integration loses the main-branch trigger",
        substitute(
            ".github/workflows/verify.yml", "branches: [main]", "branches: [release]"
        ),
    ),
    (
        "continuous integration stops running the gate",
        substitute(".github/workflows/verify.yml", "run: ./verify.sh", "run: make -C c"),
    ),
    (
        "continuous integration drops its timeout",
        substitute(".github/workflows/verify.yml", "    timeout-minutes: 15\n", ""),
    ),
    (
        "continuous integration installs packages",
        substitute(
            ".github/workflows/verify.yml",
            "run: ./verify.sh",
            "run: apt-get install -y gcc && ./verify.sh",
        ),
    ),
    (
        "continuous integration leaves Linux",
        substitute(".github/workflows/verify.yml", "runs-on: ubuntu-", "runs-on: macos-"),
    ),
    (
        "validation stops reusing the gate",
        substitute("scripts/validate.sh", "./verify.sh", "bash tests/parser-fuzz.sh"),
    ),
    (
        "validation re-implements a gate stage",
        substitute(
            "scripts/validate.sh",
            "./verify.sh",
            "./verify.sh\npython3 tests/general-input-test.py c",
        ),
    ),
    (
        "the scratch directory stops being ignored",
        substitute(".gitignore", ".test-work/\n", ""),
    ),
    (
        "a test writes outside the repository",
        substitute(
            "tests/fixture-oracle-test.sh",
            'WORK=$(realpath -m -- "${1:-.test-work/fixtures}")',
            "WORK=$(mktemp -d)",
        ),
    ),
    (
        "a test creates a temporary directory outside the repository",
        substitute(
            "tests/strict-input-test.py",
            'with tempfile.TemporaryDirectory(\n    prefix="strict-input-test-",\n    dir=SCRATCH,\n)',
            "with tempfile.TemporaryDirectory()",
        ),
    ),
    (
        "a source comment carries an experiment identifier",
        substitute(
            "c/main.c",
            "// The interleaved lane streams",
            "// opt-lane-prefetch: the interleaved lane streams",
        ),
    ),
    (
        "a document references a path outside the repository",
        substitute("docs/verification.md", "`scripts/build.sh`", "`c/missing-build.sh`"),
    ),
    (
        "a document leaks an absolute host path",
        substitute(
            "docs/verification.md",
            "```bash\n./verify.sh\n```",
            "```bash\n/home/example/checkout/verify.sh\n```",
        ),
    ),
    (
        "the README stops documenting the gate",
        substitute("README.md", "./verify.sh", "./run-tests.sh"),
    ),
    (
        "the verification document stops covering a stage",
        substitute("docs/verification.md", "tests/gate-contract-test.py", "a test"),
    ),
)


def main():
    contract = load_contract()
    if contract.run_checks():
        print(
            "contract mutation: the unmutated repository already fails its "
            "contract checks; run python3 tests/gate-contract-test.py",
            file=sys.stderr,
        )
        return 1

    survivors = []
    for label, mutate in MUTATIONS:
        overlay, executable_overlay = mutate(contract)
        if not contract.run_checks(overlay, executable_overlay):
            survivors.append(label)
            print(f"contract mutation: SURVIVED: {label}", file=sys.stderr)

    if survivors:
        print(
            f"contract mutation: FAIL ({len(survivors)} of {len(MUTATIONS)} "
            "mutations undetected)",
            file=sys.stderr,
        )
        return 1

    print(f"contract mutations: PASS ({len(MUTATIONS)} detected)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
