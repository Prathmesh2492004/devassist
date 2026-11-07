"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ACTIVE_STATUSES, ApiError, api, type Repo, type RepoStatus } from "@/lib/api";

const POLL_MS = 2000;

/** Accepts "owner/name" or a full GitHub URL and returns "owner/name". */
function normalizeRepoInput(value: string): string {
  return value
    .trim()
    .replace(/^https?:\/\/(www\.)?github\.com\//i, "")
    .replace(/\.git$/, "")
    .replace(/\/+$/, "");
}

export function RepoManager() {
  const [repos, setRepos] = useState<Repo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setRepos(await api.listRepos());
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "Could not reach the API.");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll only while something is being indexed.
  const anyActive = repos?.some((r) => ACTIVE_STATUSES.has(r.status)) ?? false;
  useEffect(() => {
    if (!anyActive) return;
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [anyActive, refresh]);

  return (
    <div className="grid gap-6">
      <AddRepoForm onAdded={refresh} />
      {loadError && (
        <p role="alert" className="text-sm text-bad">
          {loadError}
        </p>
      )}
      {repos === null && !loadError && <p className="text-sm text-muted">Loading repositories…</p>}
      {repos?.length === 0 && (
        <section className="grid place-items-center gap-2 rounded-lg border border-dashed border-line bg-surface px-6 py-14 text-center">
          <h2 className="text-lg font-semibold">No repositories yet</h2>
          <p className="max-w-md text-sm text-muted">
            Add a public GitHub repository above. DevAssist will clone it, split the code into
            functions and classes, and build a searchable index.
          </p>
        </section>
      )}
      {repos && repos.length > 0 && (
        <ul className="grid gap-3">
          {repos.map((repo) => (
            <RepoRow key={repo.id} repo={repo} onChange={refresh} />
          ))}
        </ul>
      )}
    </div>
  );
}

function AddRepoForm({ onAdded }: { onAdded: () => Promise<void> }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const fullName = normalizeRepoInput(value);
    if (!fullName) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.addRepo(fullName);
      setValue("");
      await onAdded();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the API.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={submit} className="grid gap-2">
      <label htmlFor="repo-input" className="text-sm font-medium">
        Add a public GitHub repository
      </label>
      <div className="flex flex-wrap gap-2">
        <input
          id="repo-input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="owner/name or https://github.com/owner/name"
          autoComplete="off"
          spellCheck={false}
          className="min-w-0 flex-1 rounded-md border border-line bg-surface px-3 py-2 font-mono text-sm outline-none focus:border-accent"
        />
        <button
          type="submit"
          disabled={submitting || !value.trim()}
          className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? "Adding…" : "Add and index"}
        </button>
      </div>
      {error && (
        <p role="alert" className="text-sm text-bad">
          {error}
        </p>
      )}
    </form>
  );
}

const statusLabel: Record<RepoStatus, string> = {
  queued: "Queued",
  cloning: "Cloning",
  parsing: "Parsing code",
  embedding: "Embedding",
  ready: "Ready",
  failed: "Failed",
};

const statusStyle: Record<RepoStatus, string> = {
  queued: "border-line text-muted",
  cloning: "border-accent text-accent",
  parsing: "border-accent text-accent",
  embedding: "border-accent text-accent",
  ready: "border-ok text-ok",
  failed: "border-bad text-bad",
};

function RepoRow({ repo, onChange }: { repo: Repo; onChange: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = ACTIVE_STATUSES.has(repo.status);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await onChange();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the API.");
    } finally {
      setBusy(false);
      setConfirmingDelete(false);
    }
  }

  return (
    <li className="grid gap-3 rounded-lg border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <a
            href={`https://github.com/${repo.full_name}`}
            target="_blank"
            rel="noreferrer"
            className="truncate font-mono text-sm font-medium hover:text-accent"
          >
            {repo.full_name}
          </a>
          <span
            className={`rounded-full border px-2 py-0.5 font-mono text-xs ${statusStyle[repo.status]}`}
          >
            {statusLabel[repo.status]}
            {active && repo.status !== "queued" ? ` · ${repo.progress}%` : ""}
          </span>
        </div>
        <div className="flex gap-2">
          {repo.last_indexed_sha && repo.chunk_count > 0 && (
            <Link
              href={`/repos/${repo.id}`}
              className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white dark:text-background"
            >
              Ask questions
            </Link>
          )}
          <button
            type="button"
            onClick={() => run(() => api.reindexRepo(repo.id))}
            disabled={busy || active}
            className="rounded-md border border-line px-3 py-1.5 text-xs hover:border-accent hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
          >
            Re-index
          </button>
          {confirmingDelete ? (
            <>
              <button
                type="button"
                onClick={() => run(() => api.deleteRepo(repo.id))}
                disabled={busy}
                className="rounded-md border border-bad px-3 py-1.5 text-xs text-bad"
              >
                Confirm remove
              </button>
              <button
                type="button"
                onClick={() => setConfirmingDelete(false)}
                className="rounded-md border border-line px-3 py-1.5 text-xs"
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmingDelete(true)}
              disabled={busy}
              className="rounded-md border border-line px-3 py-1.5 text-xs hover:border-bad hover:text-bad"
            >
              Remove
            </button>
          )}
        </div>
      </div>

      {active && (
        <div
          className="h-1.5 overflow-hidden rounded-full bg-line"
          role="progressbar"
          aria-valuenow={repo.progress}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`Indexing ${repo.full_name}`}
        >
          <div
            className="h-full bg-accent transition-[width] duration-500 motion-reduce:transition-none"
            style={{ width: `${Math.max(repo.progress, 3)}%` }}
          />
        </div>
      )}

      {repo.status === "failed" && repo.error && (
        <p className="text-sm text-bad">{repo.error}</p>
      )}

      {(repo.file_count > 0 || repo.status === "ready") && (
        <dl className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-xs text-muted tabular-nums">
          <Stat label="files" value={repo.file_count.toLocaleString()} />
          <Stat label="chunks" value={repo.chunk_count.toLocaleString()} />
          {repo.embedding_tokens > 0 && (
            <Stat label="tokens" value={repo.embedding_tokens.toLocaleString()} />
          )}
          {repo.index_seconds !== null && repo.status === "ready" && (
            <Stat label="indexed in" value={`${repo.index_seconds}s`} />
          )}
          {repo.last_indexed_sha && (
            <Stat
              label={repo.default_branch ?? "commit"}
              value={repo.last_indexed_sha.slice(0, 7)}
            />
          )}
        </dl>
      )}

      {error && (
        <p role="alert" className="text-sm text-bad">
          {error}
        </p>
      )}
    </li>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-1.5">
      <dt>{label}</dt>
      <dd className="text-foreground">{value}</dd>
    </div>
  );
}
