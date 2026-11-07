"use client";

import Link from "next/link";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ApiError,
  api,
  ask,
  githubUrl,
  type ConversationSummary,
  type Message,
  type Repo,
  type Source,
} from "@/lib/api";
import { findSource, linkCitations, parseCitationHref, type Citation } from "@/lib/citations";

type Turn = {
  key: string;
  question: string;
  answer: string;
  sources: Source[];
  commitSha: string | null;
  meta: Pick<Message, "model" | "input_tokens" | "output_tokens" | "latency_ms"> | null;
  error: string | null;
  streaming: boolean;
};

type Selection = { turnKey: string; source: Source; citation: Citation; commitSha: string | null };

const SUGGESTIONS = [
  "Where is the main entry point, and what happens at startup?",
  "How is configuration loaded?",
  "How are errors handled and reported?",
];

function turnsFromMessages(messages: Message[]): Turn[] {
  const turns: Turn[] = [];
  for (const message of messages) {
    if (message.role === "user") {
      turns.push({
        key: `m${message.id}`,
        question: message.content,
        answer: "",
        sources: [],
        commitSha: null,
        meta: null,
        error: null,
        streaming: false,
      });
    } else if (turns.length > 0) {
      Object.assign(turns[turns.length - 1], {
        answer: message.content,
        sources: message.sources ?? [],
        commitSha: message.commit_sha,
        meta: message,
        error: message.error,
      });
    }
  }
  return turns;
}

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : "Could not reach the API.";
}

export function Chat({ repoId }: { repoId: number }) {
  const [repo, setRepo] = useState<Repo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const refreshConversations = useCallback(async () => {
    setConversations(await api.listConversations(repoId));
  }, [repoId]);

  useEffect(() => {
    Promise.all([api.getRepo(repoId), refreshConversations()])
      .then(([r]) => setRepo(r))
      .catch((err) => setLoadError(errorMessage(err)));
    return () => abortRef.current?.abort();
  }, [repoId, refreshConversations]);

  function reset(id: number | null, nextTurns: Turn[]) {
    abortRef.current?.abort();
    setConversationId(id);
    setTurns(nextTurns);
    setSelection(null);
  }

  async function openConversation(id: number) {
    try {
      const conversation = await api.getConversation(id);
      reset(id, turnsFromMessages(conversation.messages));
    } catch (err) {
      setLoadError(errorMessage(err));
    }
  }

  async function removeConversation(id: number) {
    try {
      await api.deleteConversation(id);
      if (id === conversationId) reset(null, []);
      await refreshConversations();
    } catch (err) {
      setLoadError(errorMessage(err));
    }
  }

  async function submit(question: string) {
    const key = crypto.randomUUID();
    const update = (patch: Partial<Turn> | ((turn: Turn) => Partial<Turn>)) =>
      setTurns((all) =>
        all.map((t) => (t.key === key ? { ...t, ...(typeof patch === "function" ? patch(t) : patch) } : t)),
      );
    setTurns((all) => [
      ...all,
      {
        key,
        question,
        answer: "",
        sources: [],
        commitSha: repo?.last_indexed_sha ?? null,
        meta: null,
        error: null,
        streaming: true,
      },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;
    setStreaming(true);
    try {
      for await (const event of ask(repoId, question, conversationId, controller.signal)) {
        switch (event.type) {
          case "conversation":
            setConversationId(event.data.id);
            break;
          case "sources":
            update({ sources: event.data });
            break;
          case "delta":
            update((t) => ({ answer: t.answer + event.data.text }));
            break;
          case "done":
            update({ answer: event.data.content, meta: event.data, commitSha: event.data.commit_sha });
            break;
          case "error":
            update({ error: event.data.message });
            break;
        }
      }
    } catch (err) {
      update({ error: controller.signal.aborted ? "Stopped." : errorMessage(err) });
    } finally {
      update({ streaming: false });
      setStreaming(false);
      if (abortRef.current === controller) abortRef.current = null;
      refreshConversations().catch(() => undefined);
    }
  }

  if (loadError && !repo) {
    return (
      <div className="mx-auto grid max-w-5xl gap-3 px-4 py-12">
        <p role="alert" className="text-bad">
          {loadError}
        </p>
        <Link href="/dashboard" className="text-sm text-accent hover:underline">
          ← Back to the dashboard
        </Link>
      </div>
    );
  }

  const indexed = repo !== null && repo.last_indexed_sha !== null && repo.chunk_count > 0;

  return (
    <div className="flex h-[calc(100dvh-61px)] min-h-[32rem]">
      <aside className="hidden w-64 shrink-0 flex-col gap-3 border-r border-line p-4 md:flex">
        <Link href="/dashboard" className="text-xs text-muted hover:text-accent">
          ← Dashboard
        </Link>
        <div className="grid gap-0.5">
          <span className="text-xs text-muted">Repository</span>
          <span className="truncate font-mono text-sm font-medium" title={repo?.full_name}>
            {repo?.full_name ?? "…"}
          </span>
        </div>
        <button
          type="button"
          onClick={() => reset(null, [])}
          className="rounded-md border border-line px-3 py-2 text-left text-sm hover:border-accent hover:text-accent"
        >
          + New conversation
        </button>
        <nav
          aria-label="Conversations"
          className="-mx-2 grid min-h-0 content-start gap-0.5 overflow-x-hidden overflow-y-auto"
        >
          {conversations.map((c) => (
            <div
              key={c.id}
              className={`group flex items-center rounded-md ${c.id === conversationId ? "bg-surface" : "hover:bg-surface"}`}
            >
              <button
                type="button"
                onClick={() => openConversation(c.id)}
                className="min-w-0 flex-1 truncate px-2 py-1.5 text-left text-sm"
                title={c.title}
              >
                {c.title}
              </button>
              <button
                type="button"
                onClick={() => removeConversation(c.id)}
                aria-label={`Delete conversation: ${c.title}`}
                className="px-2 text-muted opacity-0 group-hover:opacity-100 hover:text-bad focus-visible:opacity-100"
              >
                ×
              </button>
            </div>
          ))}
        </nav>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col">
        <Transcript
          repo={repo}
          turns={turns}
          selection={selection}
          onSelect={setSelection}
          onSuggest={indexed && !streaming ? submit : undefined}
        />
        {loadError && (
          <p role="alert" className="px-4 text-sm text-bad">
            {loadError}
          </p>
        )}
        <Composer
          disabled={!indexed}
          streaming={streaming}
          onSubmit={submit}
          onStop={() => abortRef.current?.abort()}
          placeholder={
            repo === null
              ? "Loading…"
              : indexed
                ? `Ask about ${repo.full_name}…`
                : "This repository isn't indexed yet. Index it from the dashboard first."
          }
        />
      </section>

      {selection && repo && (
        <CodePanel selection={selection} repoFullName={repo.full_name} onClose={() => setSelection(null)} />
      )}
    </div>
  );
}

function Transcript({
  repo,
  turns,
  selection,
  onSelect,
  onSuggest,
}: {
  repo: Repo | null;
  turns: Turn[];
  selection: Selection | null;
  onSelect: (selection: Selection) => void;
  onSuggest?: (question: string) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const lastAnswer = turns.at(-1)?.answer;

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [turns.length, lastAnswer]);

  return (
    <div
      ref={scrollRef}
      onScroll={(e) => {
        const el = e.currentTarget;
        stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}
      className="min-h-0 flex-1 overflow-y-auto"
    >
      <div className="mx-auto grid max-w-3xl gap-8 px-4 py-8">
        {turns.length === 0 && (
          <div className="grid gap-5 py-10">
            <div className="grid gap-2">
              <h1 className="text-2xl font-bold tracking-tight">
                Ask about <span className="font-mono">{repo?.full_name ?? "…"}</span>
              </h1>
              <p className="text-muted">
                Answers are written from the repository&apos;s code, with a citation for every claim.
                Click a citation to see the exact lines.
              </p>
            </div>
            {onSuggest && (
              <div className="grid gap-2">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => onSuggest(s)}
                    className="rounded-lg border border-line bg-surface px-4 py-3 text-left text-sm hover:border-accent hover:text-accent"
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {turns.map((turn) => (
          <article key={turn.key} className="grid gap-4">
            <p className="justify-self-end rounded-2xl bg-accent px-4 py-2.5 whitespace-pre-wrap text-white dark:text-background">
              {turn.question}
            </p>
            <AnswerView turn={turn} selection={selection} onSelect={onSelect} />
          </article>
        ))}
      </div>
    </div>
  );
}

function AnswerView({
  turn,
  selection,
  onSelect,
}: {
  turn: Turn;
  selection: Selection | null;
  onSelect: (selection: Selection) => void;
}) {
  const markdown = useMemo(() => linkCitations(turn.answer), [turn.answer]);

  const select = useCallback(
    (source: Source, citation: Citation) =>
      onSelect({ turnKey: turn.key, source, citation, commitSha: turn.commitSha }),
    [onSelect, turn.key, turn.commitSha],
  );

  const components = useMemo<Components>(
    () => ({
      a({ href, children }) {
        const citation = parseCitationHref(href);
        if (!citation) {
          return (
            <a href={href} target="_blank" rel="noreferrer">
              {children}
            </a>
          );
        }
        const source = findSource(turn.sources, citation);
        if (!source) {
          return (
            <span className="citation citation-missing" title="Not in the code this answer was based on">
              {children}
            </span>
          );
        }
        const active =
          selection?.turnKey === turn.key &&
          selection.citation.path === citation.path &&
          selection.citation.start === citation.start &&
          selection.citation.end === citation.end;
        return (
          <button
            type="button"
            onClick={() => select(source, citation)}
            className={`citation ${active ? "citation-active" : ""}`}
          >
            {children}
          </button>
        );
      },
    }),
    [turn.sources, turn.key, selection, select],
  );

  const thinking = turn.streaming && turn.answer === "";

  return (
    <div className="grid gap-3">
      {thinking && (
        <p className="text-sm text-muted" aria-live="polite">
          {turn.sources.length > 0
            ? `Reading ${turn.sources.length} pieces of code…`
            : "Searching the code…"}
        </p>
      )}
      {turn.answer && (
        <div className="answer">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
            {markdown}
          </ReactMarkdown>
        </div>
      )}
      {turn.error && (
        <p role="alert" className="text-sm text-bad">
          {turn.error}
        </p>
      )}
      {!turn.streaming && turn.sources.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-muted hover:text-foreground">
            {turn.sources.length} sources searched
          </summary>
          <ul className="mt-2 flex flex-wrap gap-1.5">
            {turn.sources.map((s) => (
              <li key={`${s.path}:${s.start_line}`}>
                <button
                  type="button"
                  onClick={() => select(s, { path: s.path, start: s.start_line, end: s.end_line })}
                  className="citation"
                  title={s.symbol ?? undefined}
                >
                  {s.path}:{s.start_line}-{s.end_line}
                </button>
              </li>
            ))}
          </ul>
        </details>
      )}
      {turn.meta?.input_tokens != null && (
        <p className="font-mono text-xs text-muted tabular-nums">
          {turn.meta.model} · {turn.meta.input_tokens.toLocaleString()} in /{" "}
          {(turn.meta.output_tokens ?? 0).toLocaleString()} out tokens ·{" "}
          {((turn.meta.latency_ms ?? 0) / 1000).toFixed(1)} s
        </p>
      )}
    </div>
  );
}

function Composer({
  disabled,
  streaming,
  placeholder,
  onSubmit,
  onStop,
}: {
  disabled: boolean;
  streaming: boolean;
  placeholder: string;
  onSubmit: (question: string) => void;
  onStop: () => void;
}) {
  const [value, setValue] = useState("");

  function send() {
    const question = value.trim();
    if (!question || disabled || streaming) return;
    setValue("");
    onSubmit(question);
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
      className="mx-auto w-full max-w-3xl px-4 pb-5"
    >
      <div className="flex items-end gap-2 rounded-xl border border-line bg-surface p-2 focus-within:border-accent">
        <textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
          rows={Math.min(6, Math.max(1, value.split("\n").length))}
          maxLength={2000}
          disabled={disabled}
          placeholder={placeholder}
          aria-label="Your question"
          className="min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none placeholder:text-muted disabled:cursor-not-allowed"
        />
        {streaming ? (
          <button
            type="button"
            onClick={onStop}
            className="rounded-lg border border-line px-4 py-2 text-sm hover:border-bad hover:text-bad"
          >
            Stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={disabled || !value.trim()}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-40 dark:text-background"
          >
            Ask
          </button>
        )}
      </div>
      <p className="mt-1.5 px-1 text-xs text-muted">
        Enter to send, Shift+Enter for a new line. Answers can be wrong; check the cited code.
      </p>
    </form>
  );
}

function CodePanel({
  selection,
  repoFullName,
  onClose,
}: {
  selection: Selection;
  repoFullName: string;
  onClose: () => void;
}) {
  const { source, citation, commitSha } = selection;
  const from = Math.max(citation.start, source.start_line);
  const to = Math.min(citation.end, source.end_line);
  const firstHighlighted = useRef<HTMLTableRowElement>(null);

  useEffect(() => {
    firstHighlighted.current?.scrollIntoView({ block: "center" });
  }, [selection]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const lines = source.content.split("\n");

  return (
    <aside
      aria-label="Cited code"
      className="fixed inset-x-0 bottom-0 z-10 flex h-[60dvh] flex-col border-t border-line bg-background lg:static lg:h-auto lg:w-[46%] lg:max-w-3xl lg:border-t-0 lg:border-l"
    >
      <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
        <div className="grid min-w-0 gap-0.5">
          <span className="truncate font-mono text-sm font-medium" title={source.path}>
            {source.path}
          </span>
          <span className="font-mono text-xs text-muted">
            {from === to ? `line ${from}` : `lines ${from}-${to}`}
            {source.symbol ? ` · ${source.symbol}` : ""}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          {commitSha && (
            <a
              href={githubUrl(repoFullName, commitSha, source.path, from, to)}
              target="_blank"
              rel="noreferrer"
              className="text-xs text-accent hover:underline"
            >
              Open on GitHub ↗
            </a>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label="Close code panel"
            className="text-lg leading-none text-muted hover:text-foreground"
          >
            ×
          </button>
        </div>
      </header>
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-collapse font-mono text-xs leading-5">
          <tbody>
            {lines.map((line, i) => {
              const number = source.start_line + i;
              const highlighted = number >= from && number <= to;
              return (
                <tr
                  key={number}
                  ref={number === from ? firstHighlighted : undefined}
                  className={highlighted ? "bg-accent/12" : undefined}
                >
                  <td
                    className={`w-px border-l-2 px-3 text-right align-top text-muted select-none ${highlighted ? "border-accent" : "border-transparent"}`}
                  >
                    {number}
                  </td>
                  <td className="pr-4 whitespace-pre">{line || " "}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </aside>
  );
}
