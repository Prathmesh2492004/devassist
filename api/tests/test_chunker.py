from pathlib import Path

from app.ingest.chunker import MAX_CHUNK_LINES, CodeChunk, chunk_file
from app.ingest.files import iter_source_files


def _summary(chunks: list[CodeChunk]) -> list[tuple[str, str | None, int, int]]:
    return [(c.kind, c.symbol, c.start_line, c.end_line) for c in chunks]


PYTHON = '''\
import os

MAX_RETRIES = 3


# Charges the card, retrying on network errors.
@retry(times=MAX_RETRIES)
def charge(card):
    return gateway.charge(card)


class Payments:
    """Payment operations."""

    def refund(self, payment_id):
        return gateway.refund(payment_id)
'''


def test_python_splits_on_functions_and_classes_with_exact_lines() -> None:
    chunks = chunk_file("billing.py", "python", PYTHON)

    assert _summary(chunks) == [
        ("module", None, 1, 3),
        ("function", "charge", 6, 9),  # includes the comment and decorator above it
        ("class", "Payments", 12, 16),
    ]
    assert chunks[1].content.startswith("# Charges the card")
    assert chunks[1].content.endswith("return gateway.charge(card)")


def test_large_class_is_split_into_header_and_methods() -> None:
    methods = "\n".join(f"    def method_{i}(self):\n" + "        x = 1\n" * 30 for i in range(6))
    source = f'class Big:\n    """Docs."""\n    limit = 5\n\n{methods}'

    chunks = chunk_file("big.py", "python", source)

    assert chunks[0].kind == "class" and chunks[0].symbol == "Big"
    assert chunks[0].start_line == 1 and chunks[0].end_line == 4
    assert [c.symbol for c in chunks[1:]] == [f"Big.method_{i}" for i in range(6)]
    assert all(c.kind == "method" for c in chunks[1:])


def test_long_function_is_split_into_consecutive_windows() -> None:
    body = "".join(f"    step_{i}()\n" for i in range(300))
    chunks = chunk_file("long.py", "python", f"def long():\n{body}")

    assert len(chunks) == 3
    assert all(c.symbol == "long" and c.kind == "function" for c in chunks)
    assert all(c.end_line - c.start_line + 1 <= MAX_CHUNK_LINES for c in chunks)
    # Windows are contiguous: no line is lost or duplicated.
    assert [c.start_line for c in chunks[1:]] == [c.end_line + 1 for c in chunks[:-1]]
    assert chunks[-1].end_line == 301


def test_typescript_handles_exports_arrow_functions_and_types() -> None:
    source = """\
import { db } from "./db";

export interface User {
  id: string;
}

export const getUser = async (id: string): Promise<User> => {
  return db.find(id);
};

export default class UserService {
  load(id: string) {
    return getUser(id);
  }
}
"""
    chunks = chunk_file("users.ts", "typescript", source)

    assert _summary(chunks) == [
        ("module", None, 1, 1),
        ("type", "User", 3, 5),
        ("function", "getUser", 7, 9),
        ("class", "UserService", 11, 15),
    ]


def test_tsx_component() -> None:
    source = "export function Button({ label }: Props) {\n  return <button>{label}</button>;\n}\n"
    assert _summary(chunk_file("Button.tsx", "tsx", source)) == [("function", "Button", 1, 3)]


def test_java_methods_get_class_prefix_when_class_is_large() -> None:
    methods = "\n".join(
        f"  public void m{i}() {{\n" + "    run();\n" * 40 + "  }" for i in range(4)
    )
    source = f"package a;\n\npublic class Service {{\n  private int x;\n\n{methods}\n}}\n"

    chunks = chunk_file("Service.java", "java", source)

    assert [c.symbol for c in chunks] == [None, "Service", *[f"Service.m{i}" for i in range(4)]]
    assert chunks[2].kind == "method"


def test_unsupported_language_uses_overlapping_line_windows() -> None:
    text = "\n".join(f"line {i}" for i in range(1, 121))
    chunks = chunk_file("README.md", "markdown", text)

    assert _summary(chunks) == [
        ("text", None, 1, 60),
        ("text", None, 51, 110),
        ("text", None, 101, 120),
    ]


def test_comment_only_runs_are_dropped() -> None:
    source = "# Copyright 2026 Example Corp\n# Licensed under MIT\n\ndef f():\n    pass\n"
    assert _summary(chunk_file("f.py", "python", source)) == [("function", "f", 4, 5)]


def test_windows_line_endings_keep_line_numbers_and_strip_cr() -> None:
    source = "import os\r\n\r\ndef f():\r\n    return 1\r\n"
    chunks = chunk_file("f.py", "python", source)

    assert _summary(chunks) == [("module", None, 1, 1), ("function", "f", 3, 4)]
    assert "\r" not in chunks[1].content


def test_chunking_many_files_in_one_process_is_stable() -> None:
    """Regression test: tree-sitter 0.26.0 crashed after a handful of files in a row."""
    api_root = Path(__file__).resolve().parents[1]
    files = list(iter_source_files(api_root / "app", max_file_bytes=300_000, max_files=1000))
    assert len(files) > 5
    for _ in range(10):
        for f in files:
            chunk_file(f.path, f.language, f.text)
