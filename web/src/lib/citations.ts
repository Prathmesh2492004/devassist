import type { Source } from "./api";

export type Citation = { path: string; start: number; end: number };

// [src/app/auth.py:42-58] or [src/app/auth.py:42], but not a Markdown link [text](url).
const CITATION = /\[([^\[\]\s:]+):(\d+)(?:-(\d+))?\](?!\()/g;
const HREF_PREFIX = "#cite:";

/**
 * Turn the model's [path:start-end] citations into Markdown links that the answer renderer
 * shows as clickable chips. Fenced code blocks are left untouched.
 */
export function linkCitations(markdown: string): string {
  return markdown
    .split(/(```[\s\S]*?(?:```|$))/)
    .map((part, i) =>
      i % 2 === 1
        ? part
        : part.replace(CITATION, (_match, path: string, start: string, end?: string) => {
            const label = end && end !== start ? `${path}:${start}-${end}` : `${path}:${start}`;
            const href = `${HREF_PREFIX}${encodeURIComponent(path)}:${start}:${end ?? start}`;
            return `[${label}](${href})`;
          }),
    )
    .join("");
}

export function parseCitationHref(href: string | undefined): Citation | null {
  if (!href?.startsWith(HREF_PREFIX)) return null;
  const [path, start, end] = href.slice(HREF_PREFIX.length).split(":");
  const citation = { path: decodeURIComponent(path), start: Number(start), end: Number(end) };
  return Number.isInteger(citation.start) && Number.isInteger(citation.end) ? citation : null;
}

/** The source chunk a citation points into, or null if the model cited code it wasn't shown. */
export function findSource(sources: Source[], citation: Citation): Source | null {
  return (
    sources.find(
      (s) =>
        s.path === citation.path &&
        citation.start <= s.end_line &&
        citation.end >= s.start_line,
    ) ?? null
  );
}
