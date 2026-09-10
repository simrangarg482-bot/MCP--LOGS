import { Link } from "react-router-dom";
import {
  ArrowRight,
  Bot,
  BookOpenCheck,
  Boxes,
  MessageCircleQuestion,
  Plug,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import { FluidParticlesBackground } from "@/components/ui/fluid-particles-background";

interface Pillar {
  icon: LucideIcon;
  title: string;
  body: string;
  to: string;
  cta: string;
}

// Copy is drawn from the real page descriptions (PageHeader strings) and
// routes/nav.ts -- every pillar links to a capability that already ships.
const PILLARS: Pillar[] = [
  {
    icon: MessageCircleQuestion,
    title: "Ask EKIP",
    body: "Ask a question in plain language. EKIP searches incidents, code, runbooks, and conversations, then ties every answer back to the evidence that supports it and escalates when it isn't sure.",
    to: "/ask",
    cta: "Open Ask EKIP",
  },
  {
    icon: TriangleAlert,
    title: "Incidents & postmortems",
    body: "Run an incident from a single timeline: capture notes, track status and owning team, then generate a postmortem draft with root cause and follow-ups you can review and approve.",
    to: "/incidents",
    cta: "View incidents",
  },
  {
    icon: BookOpenCheck,
    title: "Knowledge & gaps",
    body: "A searchable library of operational knowledge, plus an agent that surfaces missing or outdated guidance so the gaps get closed before the next incident finds them.",
    to: "/knowledge",
    cta: "Browse knowledge",
  },
  {
    icon: Plug,
    title: "Connectors",
    body: "Connect the sources your team already uses -- GitHub repositories, documentation, and incident history -- and EKIP keeps an ingested, chunked, searchable copy in sync.",
    to: "/connectors",
    cta: "Manage connectors",
  },
  {
    icon: Bot,
    title: "AI investigation & agents",
    body: "Kick off an AI investigation on an incident to get verified evidence and unverified hypotheses side by side, and watch how each agent in the pipeline performed.",
    to: "/agents",
    cta: "See agent activity",
  },
  {
    icon: Wrench,
    title: "MCP tools",
    body: "The same retrieval and investigation capabilities are exposed as Model Context Protocol tools, so your own assistants can query EKIP directly.",
    to: "/mcp",
    cta: "Explore MCP tools",
  },
];

const OUTCOMES = [
  {
    title: "Triage faster",
    body: "The context that used to take an hour of scrolling Slack and dashboards is one grounded answer away.",
  },
  {
    title: "Less tribal knowledge",
    body: "What the team knows lives in a searchable system instead of a few people's heads.",
  },
  {
    title: "Answers you can trust",
    body: "Every claim cites the document, commit, or incident it came from -- so you can verify, not just believe.",
  },
  {
    title: "Better postmortems",
    body: "Evidence and timeline are already assembled, so the write-up starts from facts instead of a blank page.",
  },
];

const STEPS = [
  { n: "01", title: "Connect sources", body: "GitHub, docs, and incident history." },
  { n: "02", title: "Ingest & chunk", body: "Content is embedded and indexed for retrieval." },
  { n: "03", title: "Ask or investigate", body: "Route a question or launch an investigation." },
  { n: "04", title: "Evidence-cited answer", body: "Every response links back to its sources." },
];

export function AboutPage() {
  const { user } = useAuth();
  const primaryTo = user ? "/ask" : "/login";
  const primaryLabel = user ? "Go to workspace" : "Sign in";

  return (
    <div className="relative min-h-screen">
      <FluidParticlesBackground asBackground particleCount={1100} />

      <header className="mx-auto flex max-w-5xl items-center justify-between px-5 py-5 sm:px-8">
        <div className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-accent to-info text-white shadow-glow">
            <Boxes className="h-5 w-5" />
          </div>
          <span className="font-display text-sm font-semibold text-gradient">EKIP</span>
        </div>
        <Link
          to={primaryTo}
          className="inline-flex items-center gap-1.5 rounded-lg border border-white/15 px-3 py-1.5 text-sm font-medium text-ink transition-colors hover:bg-white/[0.05]"
        >
          {user ? "Back to EKIP" : "Sign in"}
          <ArrowRight className="h-3.5 w-3.5" />
        </Link>
      </header>

      <main className="mx-auto max-w-5xl px-5 pb-24 sm:px-8">
        {/* Hero */}
        <section className="pt-10 sm:pt-16">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1 text-xs font-medium text-ink-muted">
            <Sparkles className="h-3.5 w-3.5 text-accent" />
            Enterprise Knowledge &amp; Incident Intelligence
          </span>
          <h1 className="mt-5 font-display text-4xl font-semibold leading-[1.1] tracking-[-0.03em] text-gradient sm:text-5xl">
            Ask what happened.
            <br />
            See exactly why.
          </h1>
          <p className="mt-5 max-w-2xl text-base leading-7 text-ink-muted">
            EKIP is the connective layer between your incidents and the knowledge that explains
            them. It reads your code, runbooks, conversations, and incident history, and answers
            questions about your systems with the evidence attached &mdash; so responders spend
            their time deciding, not digging.
          </p>
          <div className="mt-7 flex flex-wrap items-center gap-3">
            <Link
              to={primaryTo}
              className="inline-flex items-center gap-2 rounded-xl border border-white/10 bg-accent px-4 py-2.5 text-sm font-medium text-white shadow-glow transition-colors hover:bg-accent-hover"
            >
              {primaryLabel}
              <ArrowRight className="h-4 w-4" />
            </Link>
            <a
              href="#how-it-works"
              className="inline-flex items-center gap-2 rounded-xl border border-white/15 px-4 py-2.5 text-sm font-medium text-ink transition-colors hover:bg-white/[0.05]"
            >
              How it works
            </a>
          </div>
        </section>

        {/* What EKIP does today */}
        <section className="mt-20">
          <h2 className="font-display text-2xl font-semibold tracking-[-0.02em] text-ink">
            What EKIP does today
          </h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-ink-muted">
            Six capabilities, one connected knowledge base. Each is live in the product now.
          </p>
          <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {PILLARS.map((pillar) => {
              const Icon = pillar.icon;
              return (
                <div key={pillar.title} className="glass flex flex-col rounded-2xl p-5">
                  <span className="flex h-10 w-10 items-center justify-center rounded-xl border border-accent-border bg-accent-subtle text-accent">
                    <Icon className="h-5 w-5" />
                  </span>
                  <h3 className="mt-4 text-sm font-semibold text-ink">{pillar.title}</h3>
                  <p className="mt-1.5 flex-1 text-xs leading-5 text-ink-muted">{pillar.body}</p>
                  <Link
                    to={pillar.to}
                    className="mt-4 inline-flex items-center gap-1.5 text-xs font-medium text-accent transition-colors hover:text-accent-hover"
                  >
                    {pillar.cta}
                    <ArrowRight className="h-3.5 w-3.5" />
                  </Link>
                </div>
              );
            })}
          </div>
        </section>

        {/* How it helps */}
        <section className="mt-20">
          <h2 className="font-display text-2xl font-semibold tracking-[-0.02em] text-ink">
            Why it&apos;s useful
          </h2>
          <div className="mt-8 grid gap-4 sm:grid-cols-2">
            {OUTCOMES.map((outcome) => (
              <div key={outcome.title} className="glass-hollow rounded-2xl p-5">
                <div className="flex items-start gap-3">
                  <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                  <div>
                    <h3 className="text-sm font-semibold text-ink">{outcome.title}</h3>
                    <p className="mt-1 text-xs leading-5 text-ink-muted">{outcome.body}</p>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* How it works */}
        <section id="how-it-works" className="mt-20 scroll-mt-8">
          <h2 className="font-display text-2xl font-semibold tracking-[-0.02em] text-ink">
            How it works
          </h2>
          <ol className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {STEPS.map((step, index) => (
              <li key={step.n} className="glass relative rounded-2xl p-5">
                <span className="font-display text-xs font-semibold text-accent">{step.n}</span>
                <h3 className="mt-2 text-sm font-semibold text-ink">{step.title}</h3>
                <p className="mt-1 text-xs leading-5 text-ink-muted">{step.body}</p>
                {index < STEPS.length - 1 && (
                  <ArrowRight className="absolute -right-3 top-1/2 hidden h-4 w-4 -translate-y-1/2 text-ink-subtle lg:block" />
                )}
              </li>
            ))}
          </ol>
        </section>

        {/* Closing CTA */}
        <section className="glass mt-20 flex flex-col items-center gap-4 rounded-3xl px-6 py-12 text-center">
          <h2 className="font-display text-2xl font-semibold tracking-[-0.02em] text-gradient">
            Start with a question
          </h2>
          <p className="max-w-md text-sm leading-6 text-ink-muted">
            The fastest way to understand EKIP is to ask it something about your own systems.
          </p>
          <Link
            to={primaryTo}
            className="inline-flex items-center gap-2 rounded-xl border border-white/10 bg-accent px-4 py-2.5 text-sm font-medium text-white shadow-glow transition-colors hover:bg-accent-hover"
          >
            {primaryLabel}
            <ArrowRight className="h-4 w-4" />
          </Link>
        </section>
      </main>
    </div>
  );
}
