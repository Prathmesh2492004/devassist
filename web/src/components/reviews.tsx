"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  api,
  parsePullRequest,
  type Finding,
  type Repo,
  type Review,
  type ReviewedFile,
  type ReviewSummary,
  type Severity,
} from "@/lib/api";
import { parsePatch, type DiffLine } from "@/lib/diff";

const POLL_MS = 2000;

const SEVERITY_STYLE: Record<Severity, string> = {
  bug: "border-bad/40 bg-bad/10 text-bad",
  risk: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
  suggestion: "border-accent/40 bg-accent/10 text-accent",
};

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : "Could not reach the API.";
}

function isActive(review: ReviewSummary): boolean {
  return review.status === "queued" || review.status === "running";
}

export function Reviews({ repoId }: { repoId: number }) {
  const [repo, setRepo] = useState<Repo | null>(null);
  const [reviews, setReviews] = useState<ReviewSummary[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refreshList = useCallback(async () => {
    const list = await api.listReviews(repoId);
    setReviews(list);
    return list;
  }, [repoId]);

  useEffect(() => {
    Promise.all([api.getRepo(repoId), refreshList()])
      .then(([r, list]) => {
        setRepo(r);
        setSelectedId((id) => id ?? list[0]?.id ?? null);
      })
      .catch((err) => setError(errorMessage(err)));
  }, [repoId, refreshList]);

  // Load the selected review, and keep polling while the worker is still on it.
  useEffect(() => {
    if (selectedId === null) {
      setReview(null);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const next = await api.getReview(selectedId);
        if (cancelled) return;
        setReview(next);
        if (isActive(next)) {
          timer = setTimeout(load, POLL_MS);
        } else {
          refreshList().catch(() => undefined);
        }
      } catch (err) {
        if (!cancelled) setError(errorMessage(err));
      }
    };
    load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [selectedId, refreshList]);

  async function start(prNumber: number) {
    setError(null);
    const created = await api.createReview(repoId, prNumber);
    await refreshList();
    setSelectedId(created.id);
  }

  async function remove(id: number) {
    try {
      await api.deleteReview(id);
      const list = await refreshList();
      if (id === selectedId) setSelectedId(list[0]?.id ?? null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  const indexed = repo !== null && repo.last_indexed_sha !== null && repo.chunk_count > 0;
  const current = review && review.id === selectedId ? review : null;

  return (
    <div className="flex min-h-[calc(100dvh-61px)] flex-col md:flex-row">
      <aside className="flex min-w-0 shrink-0 flex-col gap-4 border-line p-4 md:w-72 md:border-r">
        <div className="flex items-center gap-3 text-xs">
          <Link href="/dashboard" className="text-muted hover:text-accent">
            ← Dashboard
          </Link>
          <Link href={`/repos/${repoId}`} className="text-muted hover:text-accent">
            Ask questions
          </Link>
        </div>
        <div className="grid gap-0.5">
          <span className="text-xs text-muted">Repository</span>
          <span className="truncate font-mono text-sm font-medium" title={repo?.full_name}>
            {repo?.full_name ?? "…"}
          </span>
        </div>
        <ReviewForm repo={repo} disabled={!indexed} onStart={start} />
        <nav aria-label="Reviews" className="-mx-2 grid content-start gap-0.5">
          {reviews.map((r) => (
            <div
              key={r.id}
              className={`group flex items-center rounded-md ${r.id === selectedId ? "bg-surface" : "hover:bg-surface"}`}
            >
              <button
                type="button"
                onClick={() => setSelectedId(r.id)}
                className="grid min-w-0 flex-1 gap-0.5 px-2 py-1.5 text-left"
              >
                <span className="truncate text-sm">
                  <span className="font-mono text-muted">#{r.pr_number}</span> {r.title ?? "Loading…"}
                </span>
                <StatusLine review={r} />
              </button>
              <button
                type="button"
                onClick={() => remove(r.id)}
                aria-label={`Delete review of pull request #${r.pr_number}`}
                className="px-2 text-muted opacity-0 group-hover:opacity-100 hover:text-bad focus-visible:opacity-100"
              >
                ×
              </button>
            </div>
          ))}
        </nav>
      </aside>

      <section className="min-w-0 flex-1">
        <div className="mx-auto grid max-w-4xl gap-6 px-4 py-8">
          {error && (
            <p role="alert" className="text-sm text-bad">
              {error}
            </p>
          )}
          {current ? (
            <ReviewView review={current} />
          ) : (
            <div className="grid gap-2 py-10">
              <h1 className="text-2xl font-bold tracking-tight">Review a pull request</h1>
              <p className="text-muted">
                Enter an open pull request from{" "}
                <span className="font-mono">{repo?.full_name ?? "this repository"}</span>. Each changed file
                is reviewed with related code from the index as context, and problems are shown next to the
                lines they are about.
              </p>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

function ReviewForm({
  repo,
  disabled,
  onStart,
}: {
  repo: Repo | null;
  disabled: boolean;
  onStart: (prNumber: number) => Promise<void>;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!repo) return;
    const parsed = parsePullRequest(value, repo.full_name);
    if (typeof parsed === "string") {
      setError(parsed);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onStart(parsed);
      setValue("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="grid min-w-0 gap-2">
      <label htmlFor="pr" className="text-xs text-muted">
        Pull request number or link
      </label>
      <div className="flex min-w-0 gap-2">
        <input
          id="pr"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={disabled ? "Index the repo first" : "e.g. 5432"}
          disabled={disabled || busy}
          className="min-w-0 flex-1 rounded-md border border-line bg-surface px-3 py-2 font-mono text-sm outline-none focus:border-accent disabled:opacity-60"
        />
        <button
          type="submit"
          disabled={disabled || busy || value.trim() === ""}
          className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:text-background"
        >
          {busy ? "Starting…" : "Review"}
        </button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-bad">
          {error}
        </p>
      )}
    </form>
  );
}

function StatusLine({ review }: { review: ReviewSummary }) {
  if (review.status === "failed") return <span className="text-xs text-bad">Failed</span>;
  if (isActive(review)) {
    return (
      <span className="text-xs text-muted">
        {review.status === "queued" ? "Queued" : `Reviewing… ${review.progress}%`}
      </span>
    );
  }
  const n = review.finding_count;
  return (
    <span className={`text-xs ${n ? "text-foreground" : "text-ok"}`}>
      {n === 0 ? "No problems found" : `${n} finding${n === 1 ? "" : "s"}`}
    </span>
  );
}

function ReviewView({ review }: { review: Review }) {
  const byFile = useMemo(() => {
    const map = new Map<string, Finding[]>();
    for (const f of review.findings) map.set(f.path, [...(map.get(f.path) ?? []), f]);
    return map;
  }, [review.findings]);
  const reviewed = review.files.filter((f) => f.skipped === null);
  const skipped = review.files.filter((f) => f.skipped !== null);
  const counts = (["bug", "risk", "suggestion"] as const).map(
    (s) => [s, review.findings.filter((f) => f.severity === s).length] as const,
  );

  return (
    <article className="grid gap-6">
      <header className="grid gap-1">
        <h1 className="text-xl font-bold tracking-tight">
          <span className="font-mono text-muted">#{review.pr_number}</span> {review.title ?? "Loading…"}
        </h1>
        <p className="flex flex-wrap gap-x-3 text-sm text-muted">
          {review.author && <span>by {review.author}</span>}
          {review.head_sha && <span className="font-mono">{review.head_sha.slice(0, 7)}</span>}
          {review.html_url && (
            <a
              href={review.html_url}
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline"
            >
              Open on GitHub ↗
            </a>
          )}
        </p>
      </header>

      {isActive(review) && (
        <div className="grid gap-2" role="status">
          <div className="flex justify-between text-sm text-muted">
            <span>{review.status === "queued" ? "Waiting for the worker…" : "Reviewing changed files…"}</span>
            <span>{review.progress}%</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-line">
            <div className="h-full bg-accent transition-[width]" style={{ width: `${review.progress}%` }} />
          </div>
          <p className="text-xs text-muted">A local model takes 10–30 seconds per file.</p>
        </div>
      )}

      {review.status === "failed" && (
        <p role="alert" className="rounded-md border border-bad/40 bg-bad/10 px-4 py-3 text-sm text-bad">
          {review.error ?? "The review failed."}
        </p>
      )}

      {review.status === "done" && (
        <section className="grid gap-3 rounded-lg border border-line bg-surface p-4">
          <p className="leading-relaxed">{review.summary}</p>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {counts.map(([severity, n]) => (
              <span key={severity} className={`rounded border px-2 py-0.5 ${SEVERITY_STYLE[severity]}`}>
                {n} {n === 1 ? severity : `${severity}s`}
              </span>
            ))}
            <span className="text-muted">
              {reviewed.length} file{reviewed.length === 1 ? "" : "s"} reviewed
              {review.model && ` · ${review.model}`}
              {` · ${(review.input_tokens + review.output_tokens).toLocaleString()} tokens`}
            </span>
          </div>
        </section>
      )}

      {review.status === "done" &&
        reviewed.map((file) => (
          <FileDiff key={file.path} file={file} findings={byFile.get(file.path) ?? []} />
        ))}

      {review.status === "done" && skipped.length > 0 && (
        <details className="text-sm text-muted">
          <summary className="cursor-pointer">
            {skipped.length} file{skipped.length === 1 ? "" : "s"} not reviewed
          </summary>
          <ul className="mt-2 grid gap-1">
            {skipped.map((f) => (
              <li key={f.path}>
                <span className="font-mono">{f.path}</span> — {f.skipped}
              </li>
            ))}
          </ul>
        </details>
      )}
    </article>
  );
}

function FileDiff({ file, findings }: { file: ReviewedFile; findings: Finding[] }) {
  const [open, setOpen] = useState(true);
  const lines = useMemo(() => parsePatch(file.patch ?? ""), [file.patch]);
  const byLine = useMemo(() => {
    const map = new Map<number, Finding[]>();
    for (const f of findings) map.set(f.line, [...(map.get(f.line) ?? []), f]);
    return map;
  }, [findings]);

  return (
    <section className="overflow-hidden rounded-lg border border-line">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 bg-surface px-3 py-2 text-left text-sm"
      >
        <span className="text-muted">{open ? "▾" : "▸"}</span>
        <span className="min-w-0 flex-1 truncate font-mono">{file.path}</span>
        {findings.length > 0 && (
          <span className="rounded bg-bad/10 px-1.5 text-xs text-bad">{findings.length}</span>
        )}
        <span className="font-mono text-xs text-ok">+{file.additions}</span>
        <span className="font-mono text-xs text-bad">−{file.deletions}</span>
      </button>
      {open && (
        <div className="overflow-x-auto border-t border-line">
          <table className="w-full border-collapse font-mono text-xs leading-5">
            <tbody>
              {lines.map((line, i) => {
                const notes = line.newLine !== null ? byLine.get(line.newLine) : undefined;
                return <DiffRow key={i} line={line} notes={notes} />;
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function DiffRow({ line, notes }: { line: DiffLine; notes?: Finding[] }) {
  if (line.kind === "hunk") {
    return (
      <tr className="bg-accent/5 text-muted">
        <td colSpan={3} className="px-3 py-0.5">
          {line.text}
        </td>
      </tr>
    );
  }
  const tone = line.kind === "add" ? "bg-ok/10" : line.kind === "remove" ? "bg-bad/10" : "";
  const marker = line.kind === "add" ? "+" : line.kind === "remove" ? "−" : " ";
  return (
    <>
      <tr className={tone}>
        <td className="w-12 border-r border-line px-2 text-right text-muted select-none">
          {line.oldLine ?? ""}
        </td>
        <td className="w-12 border-r border-line px-2 text-right text-muted select-none">
          {line.newLine ?? ""}
        </td>
        <td className="px-3 whitespace-pre">
          <span className="text-muted select-none">{marker} </span>
          {line.text}
        </td>
      </tr>
      {notes?.map((note, i) => (
        <tr key={i}>
          <td colSpan={3} className="border-y border-line bg-background px-3 py-2">
            <div className="grid max-w-2xl gap-1 font-sans text-sm whitespace-normal">
              <span
                className={`w-fit rounded border px-1.5 text-[0.7rem] font-medium uppercase ${SEVERITY_STYLE[note.severity]}`}
              >
                {note.severity}
              </span>
              <p className="leading-relaxed">{note.comment}</p>
            </div>
          </td>
        </tr>
      ))}
    </>
  );
}
