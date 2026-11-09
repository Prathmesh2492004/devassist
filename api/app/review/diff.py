"""Read the unified-diff patches GitHub returns for each file in a pull request."""

import re
from dataclasses import dataclass
from typing import Literal

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

LineKind = Literal["hunk", "add", "remove", "context"]


@dataclass(frozen=True)
class DiffLine:
    kind: LineKind
    text: str
    old_line: int | None = None  # line number before the change
    new_line: int | None = None  # line number after the change


def parse_patch(patch: str) -> list[DiffLine]:
    lines: list[DiffLine] = []
    old = new = 0
    for raw in patch.split("\n"):
        header = HUNK_HEADER.match(raw)
        if header:
            old, new = int(header.group(1)), int(header.group(2))
            lines.append(DiffLine("hunk", raw))
        elif raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        elif raw.startswith("+"):
            lines.append(DiffLine("add", raw[1:], new_line=new))
            new += 1
        elif raw.startswith("-"):
            lines.append(DiffLine("remove", raw[1:], old_line=old))
            old += 1
        elif lines:  # context; GitHub sometimes drops the leading space on empty lines
            lines.append(DiffLine("context", raw[1:], old_line=old, new_line=new))
            old += 1
            new += 1
    return lines


def commentable_lines(lines: list[DiffLine]) -> set[int]:
    """New-file line numbers GitHub accepts review comments on: added and context lines."""
    return {line.new_line for line in lines if line.new_line is not None}


def added_lines(lines: list[DiffLine]) -> set[int]:
    return {line.new_line for line in lines if line.kind == "add" and line.new_line is not None}


def format_for_review(lines: list[DiffLine]) -> str:
    """Show the diff with new-file line numbers, so the model can point at exact lines."""
    width = max((len(str(line.new_line)) for line in lines if line.new_line), default=1)
    out = []
    for line in lines:
        if line.kind == "hunk":
            out.append(line.text)
        elif line.kind == "add":
            out.append(f"{line.new_line:>{width}} + {line.text}")
        elif line.kind == "remove":
            out.append(f"{'':>{width}} - {line.text}")
        else:
            out.append(f"{line.new_line:>{width}}   {line.text}")
    return "\n".join(out)
