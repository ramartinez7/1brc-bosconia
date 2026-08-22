#!/usr/bin/env python3
"""Repository contract test for the verification gate, CI, and cleanliness.

These checks are deterministic and need no dataset. They fail when:

* ``verify.sh`` stops being the single public gate, or stops running one of
  the correctness surfaces it is required to cover;
* ``scripts/validate.sh`` stops reusing the gate, or starts requiring the
  dataset for anything other than the full-dataset comparison;
* continuous integration stops running the gate on pull requests and on
  ``main``, or starts requiring root or package installation;
* a test writes outside the ignored scratch directories, so that a passing
  gate would no longer leave the working tree clean;
* the repository stops being standalone: a referenced repository-relative
  path does not exist, an absolute host path appears, or a source comment
  carries an experiment identifier instead of describing current behavior.

Usage: python3 tests/gate-contract-test.py
"""

import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

GATE = "verify.sh"
WORKFLOW = ".github/workflows/verify.yml"

# Entry points the public gate must keep running.
REQUIRED_GATE_STAGES = (
    "./scripts/build.sh",
    "tests/baseline-smoke.sh",
    "tests/fixture-oracle-test.sh",
    "tests/parser-fuzz.sh",
    "tests/general-input-test.py",
    "tests/strict-input-test.py",
    "tests/gate-contract-test.py",
    "tests/contract-mutation-test.py",
)

# Scratch roots the gate may write to. Everything else must stay read-only.
SCRATCH_ROOTS = (".test-work", ".oracle-cache")

# Generated build outputs are referenced by name but are never committed.
GENERATED_PATHS = ("c/c-linux", "baseline/baseline")

# These two files quote the constructs the checks forbid, so they are excluded
# from the checks that would otherwise match their own examples. Their
# forbidden constructs are synthetic and are reviewed with the checks.
SELF_REFERENTIAL_SOURCES = (
    "tests/gate-contract-test.py",
    "tests/contract-mutation-test.py",
)

TEXT_SUFFIXES = {".c", ".h", ".md", ".py", ".sh", ".yml", ".yaml", ".tsv", ""}

failures = []

# Every check reads the repository through these accessors, so the checks are
# pure functions of file contents. tests/contract-mutation-test.py substitutes
# mutated contents in memory to prove that each check still has teeth, without
# ever writing to the working tree.
OVERLAY = {}
EXECUTABLE_OVERLAY = {}


def check(condition, message):
    if not condition:
        failures.append(message)
    return bool(condition)


def read(relative):
    relative = str(relative)
    if relative in OVERLAY:
        text = OVERLAY[relative]
        if text is None:
            raise FileNotFoundError(relative)
        return text
    return (ROOT / relative).read_text(encoding="utf-8")


def exists(relative):
    relative = str(relative)
    if relative in OVERLAY:
        return OVERLAY[relative] is not None
    return (ROOT / relative).exists()


def is_executable(relative):
    relative = str(relative)
    if relative in EXECUTABLE_OVERLAY:
        return EXECUTABLE_OVERLAY[relative]
    return os.access(ROOT / relative, os.X_OK)


def repository_files():
    """Tracked files plus new files that are not ignored."""
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        names = [name for name in completed.stdout.decode().split("\0") if name]
    else:
        names = []
        for directory, subdirectories, entries in os.walk(ROOT):
            subdirectories[:] = [
                name
                for name in subdirectories
                if name != ".git" and name not in SCRATCH_ROOTS
            ]
            for entry in entries:
                names.append(
                    str(pathlib.Path(directory, entry).relative_to(ROOT))
                )
    names = {name for name in names if exists(name)}
    names.update(name for name, text in OVERLAY.items() if text is not None)
    return sorted(pathlib.Path(name) for name in names)


# --------------------------------------------------------------------------
# Minimal YAML reader
#
# A strict subset covering block mappings, block sequences, inline sequences,
# and plain scalars. It is deliberately dependency-free and keeps "on" a
# string instead of promoting it to a boolean.
# --------------------------------------------------------------------------


def parse_scalar(text):
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(item) for item in inner.split(",")]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def parse_block(lines, index, indent):
    if index < len(lines) and lines[index][0] >= indent and lines[index][1].startswith("- "):
        items = []
        item_indent = lines[index][0]
        while index < len(lines) and lines[index][0] == item_indent and lines[index][1].startswith("- "):
            rest = lines[index][1][2:]
            index += 1
            if ":" in rest and not rest.strip().startswith("["):
                nested = [(item_indent + 2, rest)]
                while index < len(lines) and lines[index][0] > item_indent:
                    nested.append(lines[index])
                    index += 1
                value, _ = parse_block(nested, 0, item_indent + 2)
                items.append(value)
            else:
                items.append(parse_scalar(rest))
        return items, index

    mapping = {}
    while index < len(lines) and lines[index][0] >= indent:
        column, text = lines[index]
        if column > indent:
            raise ValueError(f"unexpected indentation: {text!r}")
        key, _, rest = text.partition(":")
        key = parse_scalar(key)
        index += 1
        if rest.strip():
            mapping[key] = parse_scalar(rest)
            continue
        if index < len(lines) and lines[index][0] > indent:
            value, index = parse_block(lines, index, lines[index][0])
            mapping[key] = value
        else:
            mapping[key] = None
    return mapping, index


def load_yaml(relative):
    lines = []
    for raw in read(relative).splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), stripped))
    document, _ = parse_block(lines, 0, 0)
    return document


# --------------------------------------------------------------------------
# Gate contract
# --------------------------------------------------------------------------


def check_gate():
    if not check(exists(GATE), f"missing public gate: {GATE}"):
        return
    check(is_executable(GATE), f"{GATE} is not executable")
    text = read(GATE)
    for stage in REQUIRED_GATE_STAGES:
        check(stage in text, f"{GATE} no longer runs {stage}")
        target = stage.lstrip("./")
        check(exists(target), f"{GATE} references a missing file: {target}")
    check(
        text.count("git status --porcelain") >= 2
        and '"$STATUS_AFTER" != "$STATUS_BEFORE"' in text,
        f"{GATE} no longer compares the working tree before and after the run",
    )
    check(
        'rm -rf -- "$WORK"' in text,
        f"{GATE} no longer removes its scratch directory",
    )
    check("sudo" not in text, f"{GATE} must not require root")


def check_validate_reuse():
    if not check(exists("scripts/validate.sh"), "missing scripts/validate.sh"):
        return
    text = read("scripts/validate.sh")
    check(f"./{GATE}" in text, f"scripts/validate.sh no longer runs ./{GATE}")
    for stage in REQUIRED_GATE_STAGES:
        if not stage.startswith("tests/"):
            continue
        check(
            stage not in text,
            f"scripts/validate.sh must reuse the gate instead of running {stage}",
        )
    check(
        '"$DATA"' in text,
        "scripts/validate.sh no longer performs the full-dataset comparison",
    )


def check_ci():
    if not check(exists(WORKFLOW), f"missing CI workflow: {WORKFLOW}"):
        return
    workflow = load_yaml(WORKFLOW)
    triggers = workflow.get("on")
    if not check(isinstance(triggers, dict), f"{WORKFLOW} has no trigger mapping"):
        return
    check("pull_request" in triggers, f"{WORKFLOW} does not run on pull requests")
    push = triggers.get("push")
    branches = push.get("branches") if isinstance(push, dict) else None
    check(
        isinstance(branches, list) and "main" in branches,
        f"{WORKFLOW} does not run on pushes to main",
    )
    jobs = workflow.get("jobs") or {}
    if not check(isinstance(jobs, dict) and jobs, f"{WORKFLOW} defines no jobs"):
        return
    for name, job in jobs.items():
        runner = str(job.get("runs-on", ""))
        check(runner.startswith("ubuntu-"), f"{WORKFLOW}: job {name} does not run on Linux")
        timeout = job.get("timeout-minutes")
        check(
            isinstance(timeout, int) and 0 < timeout <= 20,
            f"{WORKFLOW}: job {name} needs a timeout-minutes bound of at most 20",
        )
        steps = job.get("steps") or []
        commands = [str(step.get("run", "")) for step in steps if isinstance(step, dict)]
        uses = [str(step.get("uses", "")) for step in steps if isinstance(step, dict)]
        check(
            any(entry.startswith("actions/checkout@") for entry in uses),
            f"{WORKFLOW}: job {name} does not check out the repository",
        )
        check(
            any(command.strip() == f"./{GATE}" for command in commands),
            f"{WORKFLOW}: job {name} does not run ./{GATE}",
        )
        joined = "\n".join(commands)
        check("sudo" not in joined, f"{WORKFLOW}: job {name} must not require root")
        check(
            "apt-get" not in joined and "pip install" not in joined,
            f"{WORKFLOW}: job {name} must not install packages",
        )


# --------------------------------------------------------------------------
# Cleanliness contract
# --------------------------------------------------------------------------


def check_scratch_is_ignored():
    if not check(exists(".gitignore"), "missing .gitignore"):
        return
    ignore = read(".gitignore")
    for root in SCRATCH_ROOTS:
        check(f"{root}/" in ignore, f".gitignore does not ignore {root}/")
    for generated in GENERATED_PATHS:
        check(generated in ignore, f".gitignore does not ignore {generated}")
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return
    for root in SCRATCH_ROOTS:
        ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", f"{root}/probe"],
            check=False,
        )
        check(ignored.returncode == 0, f"Git does not ignore {root}/")


def gate_sources():
    """The gate itself plus every script it can run."""
    sources = [pathlib.Path(GATE), pathlib.Path("scripts/build.sh")]
    sources.append(pathlib.Path("scripts/validate.sh"))
    sources += sorted((ROOT / "tests").glob("*.sh"))
    sources += sorted((ROOT / "tests").glob("*.py"))
    return [
        source if not source.is_absolute() else source.relative_to(ROOT)
        for source in sources
    ]


def check_hermetic_scratch():
    for relative in gate_sources():
        if str(relative) in SELF_REFERENTIAL_SOURCES or not exists(relative):
            continue
        text = read(relative)
        check("/tmp" not in text, f"{relative} writes outside the repository (/tmp)")
        check(
            not re.search(r"\bmktemp\b", text),
            f"{relative} uses mktemp instead of an ignored scratch directory",
        )
        for match in re.finditer(r"TemporaryDirectory\(", text):
            depth = 1
            index = match.end()
            while index < len(text) and depth:
                depth += (text[index] == "(") - (text[index] == ")")
                index += 1
            check(
                "dir=" in text[match.end() : index],
                f"{relative} creates a temporary directory outside the repository",
            )


# --------------------------------------------------------------------------
# Standalone-repository contract
# --------------------------------------------------------------------------

PATH_TOKEN = re.compile(
    r"(?<![\w./-])((?:tests|scripts|docs|baseline|c|\.github)/[A-Za-z0-9_.*/-]+)"
)
ABSOLUTE_HOST_PATH = re.compile(r"(?<![\w-])/(?:home|Users|root)/[A-Za-z0-9._-]+")
EXPERIMENT_SLUG = re.compile(r"//.*\b(?:opt|exp|sim|cand|bench)-[a-z0-9]+-[a-z0-9-]+")


def resolves(token):
    if token in GENERATED_PATHS:
        return True
    if any(token.startswith(f"{root}/") for root in SCRATCH_ROOTS):
        return True
    if "*" in token:
        return any(ROOT.glob(token))
    return exists(token)


def check_standalone_paths():
    for relative in repository_files():
        if relative.suffix not in TEXT_SUFFIXES:
            continue
        if str(relative) in SELF_REFERENTIAL_SOURCES:
            continue
        try:
            text = read(relative)
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for match in PATH_TOKEN.finditer(text):
            token = match.group(1).rstrip(".,;:")
            if resolves(token):
                continue
            failures.append(f"{relative}: references a path outside this repository: {token}")
        for match in ABSOLUTE_HOST_PATH.finditer(text):
            failures.append(f"{relative}: contains an absolute host path: {match.group(0)}")


def check_source_comment_policy():
    if not check(exists("c/main.c"), "missing c/main.c"):
        return
    text = read("c/main.c")
    for match in EXPERIMENT_SLUG.finditer(text):
        failures.append(
            "c/main.c: comment carries an experiment identifier instead of "
            f"describing current behavior: {match.group(0).strip()}"
        )


def check_documentation():
    for required in ("README.md", "docs/verification.md"):
        if not check(exists(required), f"missing {required}"):
            return
    readme = read("README.md")
    check(f"./{GATE}" in readme, f"README.md does not document ./{GATE}")
    check(
        "docs/verification.md" in readme,
        "README.md does not link the verification documentation",
    )
    verification = read("docs/verification.md")
    check(f"./{GATE}" in verification, f"docs/verification.md does not document ./{GATE}")
    for stage in REQUIRED_GATE_STAGES:
        target = stage.lstrip("./")
        if target == "scripts/build.sh":
            continue
        check(
            target in verification,
            f"docs/verification.md does not document {target}",
        )


CHECKS = (
    check_gate,
    check_validate_reuse,
    check_ci,
    check_scratch_is_ignored,
    check_hermetic_scratch,
    check_standalone_paths,
    check_source_comment_policy,
    check_documentation,
)


def run_checks(overlay=None, executable_overlay=None):
    """Run every contract check and return the failure messages."""
    global failures, OVERLAY, EXECUTABLE_OVERLAY
    failures = []
    OVERLAY = dict(overlay or {})
    EXECUTABLE_OVERLAY = dict(executable_overlay or {})
    try:
        for contract in CHECKS:
            contract()
        return list(failures)
    finally:
        OVERLAY = {}
        EXECUTABLE_OVERLAY = {}


def main():
    problems = run_checks()
    if problems:
        for problem in problems:
            print(f"contract: FAIL: {problem}", file=sys.stderr)
        return 1
    print("repository contracts: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
