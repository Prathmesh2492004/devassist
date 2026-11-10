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

/** A chunk of code the model was shown. Citations in an answer point into these. */
export type Source = {
  path: string;
  start_line: number;
  end_line: number;
  symbol: string | null;
  language: string;
  content: string;
};

export type Message = {
  id: number;
  role: "user" | "assistant";
  content: string;
  sources: Source[] | null;
  commit_sha: string | null;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  error: string | null;
  created_at: string;
};

export type ConversationSummary = {
  id: number;
  repo_id: number;
  title: string;
  created_at: string;
  updated_at: string;
};

export type Conversation = ConversationSummary & { messages: Message[] };

export type ReviewStatus = "queued" | "running" | "done" | "failed";
export type Severity = "bug" | "risk" | "suggestion";

export type Finding = {
  path: string;
  line: number;
  severity: Severity;
  comment: string;
  /** "rule" for a linter rule (reliable), "model" for the AI's judgement (a hint to check). */
  source: "rule" | "model";
};

export type ReviewedFile = {
  path: string;
  status: string;
  additions: number;
  deletions: number;
  patch: string | null;
  /** Why the file wasn't reviewed, or null if it was. */
  skipped: string | null;
};

export type ReviewSummary = {
  id: number;
  repo_id: number;
  pr_number: number;
  title: string | null;
  author: string | null;
  html_url: string | null;
  head_sha: string | null;
  status: ReviewStatus;
  progress: number;
  error: string | null;
  finding_count: number;
  // true when the GitHub App started the review on a new commit; it is then posted on the PR
  automatic: boolean;
  github_review_url: string | null;
  post_error: string | null;
  created_at: string;
  finished_at: string | null;
};

export type Review = ReviewSummary & {
  summary: string | null;
  findings: Finding[];
  files: ReviewedFile[];
  model: string | null;
  input_tokens: number;
  output_tokens: number;
};

export const api = {
  listRepos: () => request<Repo[]>("/repos"),
  getRepo: (id: number) => request<Repo>(`/repos/${id}`),
  addRepo: (fullName: string) =>
    request<Repo>("/repos", { method: "POST", body: JSON.stringify({ full_name: fullName }) }),
  reindexRepo: (id: number) => request<Repo>(`/repos/${id}/reindex`, { method: "POST" }),
  deleteRepo: (id: number) => request<void>(`/repos/${id}`, { method: "DELETE" }),
  listConversations: (repoId: number) =>
    request<ConversationSummary[]>(`/repos/${repoId}/conversations`),
  getConversation: (id: number) => request<Conversation>(`/conversations/${id}`),
  deleteConversation: (id: number) => request<void>(`/conversations/${id}`, { method: "DELETE" }),
  listReviews: (repoId: number) => request<ReviewSummary[]>(`/repos/${repoId}/reviews`),
  getReview: (id: number) => request<Review>(`/reviews/${id}`),
  createReview: (repoId: number, prNumber: number) =>
    request<ReviewSummary>(`/repos/${repoId}/reviews`, {
      method: "POST",
      body: JSON.stringify({ pr_number: prNumber }),
    }),
  deleteReview: (id: number) => request<void>(`/reviews/${id}`, { method: "DELETE" }),
};

const PR_URL = /^(?:https?:\/\/)?github\.com\/([\w.-]+\/[\w.-]+)\/pull\/(\d+)(?:[/?#].*)?$/i;

/**
 * Read a pull request number from "123", "#123" or a GitHub pull request link. A link must
 * point at this repository. Returns an error message when the input can't be used.
 */
export function parsePullRequest(input: string, fullName: string): number | string {
  const text = input.trim();
  const plain = /^#?(\d+)$/.exec(text);
  const url = PR_URL.exec(text);
  let number: number;
  if (plain) {
    number = Number(plain[1]);
  } else if (url) {
    if (url[1].toLowerCase() !== fullName.toLowerCase()) {
      return `That pull request is in ${url[1]}, not ${fullName}.`;
    }
    number = Number(url[2]);
  } else {
    return "Enter a pull request number, like 5432, or its GitHub link.";
  }
  return number > 0 ? number : "Pull request numbers start at 1.";
}

export type AskEvent =
  | { type: "conversation"; data: { id: number; title: string } }
  | { type: "sources"; data: Source[] }
  | { type: "delta"; data: { text: string } }
  | { type: "done"; data: Message }
  | { type: "error"; data: { message: string } };

/**
 * Ask a question and yield the answer's Server-Sent Events as they arrive. EventSource only
 * supports GET, so this reads the POST response body as a stream instead.
 */
export async function* ask(
  repoId: number,
  question: string,
  conversationId: number | null,
  signal: AbortSignal,
): AsyncGenerator<AskEvent> {
  const res = await fetch(`${API_URL}/repos/${repoId}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, conversation_id: conversationId }),
    signal,
  });
  if (!res.ok || !res.body) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
    throw new ApiError(describeError(res.status, body?.detail));
  }

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let end;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let type = "";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) type = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (type && data) yield { type, data: JSON.parse(data) } as AskEvent;
    }
  }
}

export function githubUrl(
  fullName: string,
  sha: string,
  path: string,
  start: number,
  end: number,
): string {
  const lines = start === end ? `L${start}` : `L${start}-L${end}`;
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  return `https://github.com/${fullName}/blob/${sha}/${encodedPath}#${lines}`;
}
