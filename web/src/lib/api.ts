export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type RepoStatus = "queued" | "cloning" | "parsing" | "embedding" | "ready" | "failed";

export const ACTIVE_STATUSES: ReadonlySet<RepoStatus> = new Set([
  "queued",
  "cloning",
  "parsing",
  "embedding",
]);

export type Repo = {
  id: number;
  full_name: string;
  default_branch: string | null;
  status: RepoStatus;
  progress: number;
  error: string | null;
  file_count: number;
  chunk_count: number;
  embedding_tokens: number;
  last_indexed_sha: string | null;
  indexing_started_at: string | null;
  indexed_at: string | null;
  created_at: string;
  index_seconds: number | null;
};

export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
    throw new ApiError(describeError(res.status, body?.detail));
  }
  return (res.status === 204 ? undefined : await res.json()) as T;
}

function describeError(status: number, detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (status === 422) return "Enter the repository as owner/name, for example tiangolo/typer.";
  return `Request failed (${status}). Is the API running?`;
}

export const api = {
  listRepos: () => request<Repo[]>("/repos"),
  addRepo: (fullName: string) =>
    request<Repo>("/repos", { method: "POST", body: JSON.stringify({ full_name: fullName }) }),
  reindexRepo: (id: number) => request<Repo>(`/repos/${id}/reindex`, { method: "POST" }),
  deleteRepo: (id: number) => request<void>(`/repos/${id}`, { method: "DELETE" }),
};
