"""Split source files into searchable chunks along function and class boundaries.

Chunking at syntax boundaries (instead of every N lines) keeps each chunk a complete unit
of meaning, which improves retrieval and lets answers cite exact line ranges.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

MAX_CHUNK_LINES = 120
MAX_CHUNK_CHARS = 6_000
TEXT_WINDOW_LINES = 60  # for languages without a parser
TEXT_WINDOW_OVERLAP = 10

CONTAINER_KINDS = frozenset({"class", "type"})
FUNCTION_VALUE_TYPES = frozenset(
    {"arrow_function", "function_expression", "function", "generator_function"}
)


@dataclass(frozen=True)
class CodeChunk:
    path: str
    language: str
    kind: str  # function, method, class, type, module, text
    symbol: str | None
    start_line: int  # 1-based, inclusive
    end_line: int
    content: str


@dataclass(frozen=True)
class LanguageSpec:
    load: Callable[[], object]
    definitions: dict[str, str]  # node type -> chunk kind
    wrappers: frozenset[str] = frozenset()  # e.g. decorators, `export`
    comments: frozenset[str] = frozenset({"comment"})
    variable_functions: bool = False  # `const f = () => ...` counts as a function


_JS_DEFINITIONS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "method_definition": "function",
    "interface_declaration": "type",
    "type_alias_declaration": "type",
    "enum_declaration": "type",
}

SPECS: dict[str, LanguageSpec] = {
    "python": LanguageSpec(
        load=tree_sitter_python.language,
        definitions={"function_definition": "function", "class_definition": "class"},
        wrappers=frozenset({"decorated_definition"}),
    ),
    "javascript": LanguageSpec(
        load=tree_sitter_javascript.language,
        definitions=_JS_DEFINITIONS,
        wrappers=frozenset({"export_statement"}),
        variable_functions=True,
    ),
    "typescript": LanguageSpec(
        load=tree_sitter_typescript.language_typescript,
        definitions=_JS_DEFINITIONS,
        wrappers=frozenset({"export_statement"}),
        variable_functions=True,
    ),
    "tsx": LanguageSpec(
        load=tree_sitter_typescript.language_tsx,
        definitions=_JS_DEFINITIONS,
        wrappers=frozenset({"export_statement"}),
        variable_functions=True,
    ),
    "java": LanguageSpec(
        load=tree_sitter_java.language,
        definitions={
            "class_declaration": "class",
            "interface_declaration": "class",
            "enum_declaration": "class",
            "record_declaration": "class",
            "method_declaration": "function",
            "constructor_declaration": "function",
        },
        comments=frozenset({"line_comment", "block_comment"}),
    ),
}


@cache
def _language(language: str) -> Language:
    # Cached for the life of the process: parsers and nodes keep raw pointers into the
    # grammar, so it must never be garbage-collected while one of them is in use.
    return Language(SPECS[language].load())


@cache
def _parser(language: str) -> Parser:
    return Parser(_language(language))


def chunk_file(path: str, language: str, text: str) -> list[CodeChunk]:
    # Split on "\n" only: str.splitlines() also breaks on \f,   and others, which would
    # put our line numbers out of step with tree-sitter's rows.
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if language not in SPECS:
        return _text_windows(path, language, lines)
    # Keep the bytes alive for the whole walk: tree-sitter nodes point into them.
    source = text.encode("utf-8")
    tree = _parser(language).parse(source)
    return _FileChunker(path, language, lines, source, SPECS[language]).run(tree.root_node)


@dataclass(frozen=True)
class _Definition:
    kind: str
    name: str
    outer: Node  # the node whose range we index, e.g. including decorators or `export`
    inner: Node  # the actual function/class node


class _FileChunker:
    def __init__(
        self, path: str, language: str, lines: list[str], source: bytes, spec: LanguageSpec
    ) -> None:
        self.path = path
        self.language = language
        self.lines = lines
        self.source = source
        self.spec = spec
        self.chunks: list[CodeChunk] = []

    def run(self, root: Node) -> list[CodeChunk]:
        self._walk(root.named_children, prefix=None, loose_kind="module")
        return self.chunks

    def _walk(self, nodes: list[Node], prefix: str | None, loose_kind: str) -> None:
        loose: list[Node] = []
        for node in nodes:
            definition = self._as_definition(node)
            if definition is None:
                loose.append(node)
                continue
            # Comments directly above a definition document it, so they belong to its chunk.
            start_row = node.start_point.row
            while (
                loose
                and loose[-1].type in self.spec.comments
                and (loose[-1].end_point.row >= start_row - 1)
            ):
                start_row = loose.pop().start_point.row
            self._flush_loose(loose, prefix, loose_kind)
            loose = []
            self._emit_definition(definition, start_row, prefix)
        self._flush_loose(loose, prefix, loose_kind)

    def _as_definition(self, node: Node) -> _Definition | None:
        if node.type in self.spec.wrappers:
            inner = node.child_by_field_name("definition") or node.child_by_field_name(
                "declaration"
            )
            if inner is None:  # e.g. `export default class {}` has no field name
                inner = next((c for c in node.named_children if self._as_definition(c)), None)
            found = self._as_definition(inner) if inner is not None else None
            return _Definition(found.kind, found.name, node, found.inner) if found else None

        if node.type in self.spec.definitions:
            name_node = node.child_by_field_name("name")
            name = self._text(name_node) if name_node is not None else "default"
            return _Definition(self.spec.definitions[node.type], name, node, node)

        if self.spec.variable_functions and node.type in (
            "lexical_declaration",
            "variable_declaration",
        ):
            declarators = [c for c in node.named_children if c.type == "variable_declarator"]
            if len(declarators) == 1:
                value = declarators[0].child_by_field_name("value")
                name_node = declarators[0].child_by_field_name("name")
                if (
                    value is not None
                    and name_node is not None
                    and value.type in FUNCTION_VALUE_TYPES
                ):
                    return _Definition("function", self._text(name_node), node, node)
        return None

    def _emit_definition(self, d: _Definition, start_row: int, prefix: str | None) -> None:
        symbol = f"{prefix}.{d.name}" if prefix else d.name
        kind = "method" if prefix and d.kind == "function" else d.kind
        end_row = _end_row(d.outer)

        if self._fits(start_row, end_row):
            self._add(kind, symbol, start_row, end_row)
            return

        body = d.inner.child_by_field_name("body")
        if kind in CONTAINER_KINDS and body is not None:
            members = body.named_children
            first_def = next((m for m in members if self._as_definition(m)), None)
            # Header: the class signature plus any docstring or fields before the first method.
            header_end = first_def.start_point.row - 1 if first_def else end_row
            self._add_windows(kind, symbol, start_row, header_end)
            if first_def is not None:
                rest = [m for m in members if m.start_point.row > header_end]
                self._walk(rest, prefix=symbol, loose_kind=kind)
            return

        self._add_windows(kind, symbol, start_row, end_row)

    def _flush_loose(self, nodes: list[Node], prefix: str | None, kind: str) -> None:
        """Group consecutive non-definition nodes (imports, constants, statements) into chunks."""
        group: list[Node] = []
        for node in nodes:
            if group and not self._fits(group[0].start_point.row, _end_row(node)):
                self._emit_loose_group(group, prefix, kind)
                group = []
            group.append(node)
        self._emit_loose_group(group, prefix, kind)

    def _emit_loose_group(self, group: list[Node], prefix: str | None, kind: str) -> None:
        # A run of nothing but comments is usually a license header or a section divider.
        if not group or all(n.type in self.spec.comments for n in group):
            return
        self._add_windows(kind, prefix, group[0].start_point.row, _end_row(group[-1]))

    def _text(self, node: Node) -> str:
        return self.source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def _fits(self, start_row: int, end_row: int) -> bool:
        if end_row - start_row + 1 > MAX_CHUNK_LINES:
            return False
        return sum(len(line) + 1 for line in self.lines[start_row : end_row + 1]) <= MAX_CHUNK_CHARS

    def _add_windows(self, kind: str, symbol: str | None, start_row: int, end_row: int) -> None:
        """Add the range as one chunk, or as several consecutive chunks if it is too big."""
        row = start_row
        while row <= end_row:
            chars = 0
            stop = row
            while stop <= end_row and stop - row < MAX_CHUNK_LINES:
                chars += len(self.lines[stop]) + 1
                if chars > MAX_CHUNK_CHARS and stop > row:
                    break
                stop += 1
            self._add(kind, symbol, row, stop - 1)
            row = stop

    def _add(self, kind: str, symbol: str | None, start_row: int, end_row: int) -> None:
        content = "\n".join(self.lines[start_row : end_row + 1])[:MAX_CHUNK_CHARS]
        if content.strip():
            self.chunks.append(
                CodeChunk(
                    path=self.path,
                    language=self.language,
                    kind=kind,
                    symbol=symbol,
                    start_line=start_row + 1,
                    end_line=end_row + 1,
                    content=content,
                )
            )


def _text_windows(path: str, language: str, lines: list[str]) -> list[CodeChunk]:
    chunks = []
    step = TEXT_WINDOW_LINES - TEXT_WINDOW_OVERLAP
    for start in range(0, max(len(lines) - TEXT_WINDOW_OVERLAP, 1), step):
        end = min(start + TEXT_WINDOW_LINES, len(lines)) - 1
        content = "\n".join(lines[start : end + 1])[:MAX_CHUNK_CHARS]
        if content.strip():
            chunks.append(
                CodeChunk(path, language, "text", None, start + 1, end + 1, content),
            )
    return chunks


def _end_row(node: Node) -> int:
    # A node that ends with a newline reports its end as column 0 of the following row.
    end = node.end_point
    return end.row - 1 if end.column == 0 and end.row > node.start_point.row else end.row
