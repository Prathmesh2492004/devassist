/** Parse the unified-diff patch GitHub returns for one file. Mirrors api/app/review/diff.py. */

export type DiffLine = {
  kind: "hunk" | "add" | "remove" | "context";
  text: string;
  oldLine: number | null;
  newLine: number | null;
};

const HUNK_HEADER = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

export function parsePatch(patch: string): DiffLine[] {
  const lines: DiffLine[] = [];
  let oldLine = 0;
  let newLine = 0;
  for (const raw of patch.split("\n")) {
    const header = HUNK_HEADER.exec(raw);
    if (header) {
      oldLine = Number(header[1]);
      newLine = Number(header[2]);
      lines.push({ kind: "hunk", text: raw, oldLine: null, newLine: null });
    } else if (raw.startsWith("\\")) {
      continue; // "\ No newline at end of file"
    } else if (raw.startsWith("+")) {
      lines.push({
        kind: "add",
        text: raw.slice(1),
        oldLine: null,
        newLine: newLine++,
      });
    } else if (raw.startsWith("-")) {
      lines.push({
        kind: "remove",
        text: raw.slice(1),
        oldLine: oldLine++,
        newLine: null,
      });
    } else if (lines.length > 0) {
      lines.push({
        kind: "context",
        text: raw.slice(1),
        oldLine: oldLine++,
        newLine: newLine++,
      });
    }
  }
  return lines;
}
