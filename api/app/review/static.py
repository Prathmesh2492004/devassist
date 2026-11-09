"""Rule-based checks: run Ruff's bug and security rules on a changed Python file.

A small local model misses even textbook bugs such as SQL injection. A linter never does,
costs nothing and runs in milliseconds, so known bug patterns are checked by rules and the
model is left to judge logic. Only problems on lines the pull request added are reported:
old problems in untouched code are not the author's doing.
"""

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, TypedDict

log = logging.getLogger(__name__)

Severity = Literal["bug", "risk", "suggestion"]

# Rules that point at real defects, not style, with how serious each one is. Rules not listed
# here are not run.
RULES: dict[str, Severity] = {
    # Pyflakes: code that can't work
    "F821": "bug",  # undefined name
    "F632": "bug",  # `is` comparison with a literal
    "F811": "risk",  # redefinition of an unused name
    "F601": "bug",  # dictionary key repeated with a different value
    "F841": "suggestion",  # local variable assigned but never used
    # Bugbear: common Python traps
    "B006": "bug",  # mutable default argument
    "B008": "risk",  # function call in a default argument
    "B012": "bug",  # return/break/continue inside finally swallows the exception
    "B015": "bug",  # comparison whose result is thrown away
    "B018": "risk",  # useless expression
    "B023": "bug",  # closure uses a loop variable that changes later
    "B025": "bug",  # duplicate exception handler
    # Bandit: security
    "S102": "bug",  # exec
    "S105": "risk",  # hardcoded password in a variable
    "S106": "risk",  # hardcoded password in an argument
    "S107": "risk",  # hardcoded password as a default
    "S110": "risk",  # try/except/pass hides errors
    "S113": "risk",  # HTTP request without a timeout
    "S301": "risk",  # pickle can run code from untrusted data
    "S307": "bug",  # eval
    "S324": "risk",  # weak hash (MD5/SHA1) for security
    "S501": "bug",  # TLS certificate checks turned off
    "S506": "bug",  # yaml.load without a safe loader
    "S602": "bug",  # subprocess with shell=True
    "S604": "bug",  # function call with shell=True
    "S605": "bug",  # os.system and friends with a shell
    "S608": "bug",  # SQL built from strings
    # Others
    "E722": "risk",  # bare except
    "SIM115": "risk",  # file opened without a context manager
    "PLE": "bug",  # Pylint errors
    "RUF006": "risk",  # asyncio task may be garbage-collected before it finishes
}

PYTHON_SUFFIXES = (".py", ".pyi")
# Security rules flag fake passwords and fixtures in tests, which are fine there. Seen on real
# Flask PRs: test secret keys were the only rule findings across 53 changed files.
SECURITY_PREFIX = "S"
TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class StaticFinding:
    line: int
    severity: Severity
    rule: str
    message: str


class _Location(TypedDict):
    row: int
    column: int


class _Diagnostic(TypedDict):
    code: str | None
    message: str
    location: _Location


def severity_for(code: str) -> Severity | None:
    if code in RULES:
        return RULES[code]
    prefix = code.rstrip("0123456789")
    return RULES.get(prefix)


def is_test_file(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return (
        any(part in ("test", "tests", "testing") for part in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def check_python(path: str, source: str, added: set[int]) -> list[StaticFinding]:
    """Ruff findings on the added lines of one Python file. Empty for other files."""
    if not path.endswith(PYTHON_SUFFIXES) or not added:
        return []
    rules = [r for r in RULES if not (is_test_file(path) and r.startswith(SECURITY_PREFIX))]
    command = [
        sys.executable, "-m", "ruff", "check", "-",
        "--isolated", "--no-cache", "--exit-zero", "--output-format", "json",
        "--target-version", "py312", "--stdin-filename", path,
        "--select", ",".join(rules),
    ]  # fmt: skip
    try:
        result = subprocess.run(
            command,
            input=source,
            capture_output=True,
            encoding="utf-8",  # not the OS default, which on Windows can't encode most text
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Ruff could not check %s: %s", path, exc)
        return []
    if result.returncode != 0:
        log.warning("Ruff failed on %s: %s", path, result.stderr[:500])
        return []

    diagnostics: list[_Diagnostic] = json.loads(result.stdout or "[]")
    findings = []
    for diagnostic in diagnostics:
        code, line = diagnostic["code"], diagnostic["location"]["row"]
        if code is None or line not in added:  # None means a syntax error
            continue
        severity = severity_for(code)
        if severity is not None:
            findings.append(StaticFinding(line, severity, code, diagnostic["message"]))
    return findings
