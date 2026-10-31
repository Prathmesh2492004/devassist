"use client";

import { useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Status = "checking" | "ok" | "degraded" | "offline";

const labels: Record<Status, string> = {
  checking: "Checking API…",
  ok: "API and database online",
  degraded: "API up, database unreachable",
  offline: "API offline",
};

const dotColor: Record<Status, string> = {
  checking: "bg-muted",
  ok: "bg-ok",
  degraded: "bg-bad",
  offline: "bg-bad",
};

export function ApiStatus() {
  const [status, setStatus] = useState<Status>("checking");

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/health/ready`, { signal: controller.signal })
      .then((res) => setStatus(res.ok ? "ok" : "degraded"))
      .catch((err: unknown) => {
        if (!(err instanceof DOMException && err.name === "AbortError")) setStatus("offline");
      });
    return () => controller.abort();
  }, []);

  return (
    <span
      role="status"
      className="inline-flex items-center gap-2 rounded-md border border-line bg-surface px-3 py-1.5 font-mono text-xs"
    >
      <span className={`size-2 rounded-full ${dotColor[status]}`} aria-hidden />
      {labels[status]}
    </span>
  );
}
