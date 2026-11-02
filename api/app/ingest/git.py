"""Clone public GitHub repositories."""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

FULL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


@dataclass(frozen=True)
class CloneResult:
    sha: str
    branch: str


class CloneError(Exception):
    pass


def clone_repo(full_name: str, branch: str | None, dest: Path, *, timeout: int) -> CloneResult:
    """Shallow-clone github.com/<full_name> into dest. Only the latest commit is fetched."""
    # Names are validated by the API too; check again here because they end up in a command.
    if not FULL_NAME_PATTERN.match(full_name) or ".." in full_name:
        raise CloneError(f"Invalid repository name: {full_name}")
    if branch is not None and (not BRANCH_PATTERN.match(branch) or branch.startswith("-")):
        raise CloneError(f"Invalid branch name: {branch}")

    cmd = ["git", "clone", "--depth", "1", "--single-branch", "--no-tags", "--quiet"]
    if branch:
        cmd += ["--branch", branch]
    cmd += ["--", f"https://github.com/{full_name}.git", str(dest)]
    # Never prompt for a login, and skip downloading large LFS files.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise CloneError(f"Cloning took longer than {timeout} seconds") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip().splitlines()[-1] if exc.stderr.strip() else "unknown error"
        if "not found" in detail.lower() or "could not read username" in detail.lower():
            detail = "Repository not found, or it is private. Only public repos are supported."
        raise CloneError(f"git clone failed: {detail}") from exc

    def git(*args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(dest), *args], check=True, capture_output=True, text=True
        )
        return out.stdout.strip()

    return CloneResult(
        sha=git("rev-parse", "HEAD"), branch=git("rev-parse", "--abbrev-ref", "HEAD")
    )
