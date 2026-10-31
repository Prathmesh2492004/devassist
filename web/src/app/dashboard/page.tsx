import type { Metadata } from "next";
import { ApiStatus } from "@/components/api-status";

export const metadata: Metadata = { title: "Dashboard · DevAssist" };

export default function Dashboard() {
  return (
    <div className="grid gap-10">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="grid gap-1">
          <h1 className="text-3xl font-bold tracking-tight">Dashboard</h1>
          <p className="text-muted">Your connected repositories will appear here.</p>
        </div>
        <ApiStatus />
      </div>

      <section className="grid place-items-center gap-3 rounded-lg border border-dashed border-line bg-surface px-6 py-16 text-center">
        <h2 className="text-lg font-semibold">No repositories yet</h2>
        <p className="max-w-md text-sm text-muted">
          Sign in with GitHub and connect a repository to start indexing. Connecting repos
          arrives in Week 5; indexing arrives in Week 2.
        </p>
        <button
          type="button"
          disabled
          className="mt-2 cursor-not-allowed rounded-md border border-line px-4 py-2 text-sm text-muted"
        >
          Connect repository (coming soon)
        </button>
      </section>
    </div>
  );
}
