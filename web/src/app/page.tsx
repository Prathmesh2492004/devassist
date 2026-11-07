import Link from "next/link";

const features = [
  {
    tag: "Ask",
    title: "Codebase Q&A with citations",
    body: "Ask how anything in your repo works and get an answer that links to the exact files and lines.",
  },
  {
    tag: "Review",
    title: "Automatic PR reviews",
    body: "Every new pull request gets inline comments on likely bugs within seconds.",
  },
  {
    tag: "Measure",
    title: "Quality and cost dashboard",
    body: "Track answer accuracy, latency and cost per question over time.",
  },
];

export default function Home() {
  return (
    <div className="mx-auto grid max-w-5xl gap-16 px-4 py-12">
      <section className="grid max-w-2xl gap-6">
        <h1 className="text-5xl font-bold tracking-tight text-balance">
          Understand any codebase in minutes.
        </h1>
        <p className="text-lg text-muted">
          Connect a GitHub repository. DevAssist indexes it, answers your questions with
          citations, and reviews every pull request automatically.
        </p>
        <div>
          <Link
            href="/dashboard"
            className="inline-block rounded-md bg-accent px-5 py-2.5 font-medium text-white hover:opacity-90"
          >
            Open dashboard
          </Link>
        </div>
      </section>

      <section className="grid gap-8 md:grid-cols-3">
        {features.map((f) => (
          <div key={f.tag} className="grid content-start gap-2 border-t-2 border-foreground pt-4">
            <span className="font-mono text-xs tracking-wider text-accent uppercase">{f.tag}</span>
            <h2 className="text-lg font-semibold">{f.title}</h2>
            <p className="text-sm text-muted">{f.body}</p>
          </div>
        ))}
      </section>
    </div>
  );
}
