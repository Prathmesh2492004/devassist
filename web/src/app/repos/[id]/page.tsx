import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Chat } from "@/components/chat";

export const metadata: Metadata = { title: "Ask · DevAssist" };

export default async function RepoChatPage({ params }: { params: Promise<{ id: string }> }) {
  const repoId = Number((await params).id);
  if (!Number.isInteger(repoId) || repoId <= 0) notFound();
  return <Chat repoId={repoId} />;
}
