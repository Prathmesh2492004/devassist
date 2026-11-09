import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Reviews } from "@/components/reviews";

export const metadata: Metadata = { title: "Reviews · DevAssist" };

export default async function RepoReviewsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const repoId = Number((await params).id);
  if (!Number.isInteger(repoId) || repoId <= 0) notFound();
  return <Reviews repoId={repoId} />;
}
