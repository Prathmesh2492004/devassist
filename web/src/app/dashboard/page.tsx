import type { Metadata } from "next";
import { ApiStatus } from "@/components/api-status";
import { RepoManager } from "@/components/repo-manager";

export const metadata: Metadata = { title: "Dashboard · DevAssist" };

export default function Dashboard() {
  return (
    <div className="grid gap-10">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="grid gap-1">
          <h1 className="text-3xl font-bold tracking-tight">Dashboard</h1>
          <p className="text-muted">Connect repositories and watch them get indexed.</p>
        </div>
        <ApiStatus />
      </div>
      <RepoManager />
    </div>
  );
}
