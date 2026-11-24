import { Link, useNavigate } from "@tanstack/react-router";
import { AlertTriangle, ArrowRight, BookOpen, CheckCircle2, Circle, Info, Plug, Sparkles, Target } from "lucide-react";
import { useState } from "react";
import { StatTile } from "@/components/charts";
import { DecisionRow, DecisionSheet } from "@/components/decision";
import { Badge, Button, Card, CardHeader, EmptyState } from "@/components/ui";
import { cn } from "@/lib/cn";
import { formatCompact, formatMoney, formatNumber, formatPercent } from "@/lib/format";
import { bpath, gpath, useApi, useBid } from "@/lib/queries";
import type { BriefingItem, Cockpit, Goal, PlatformInfo } from "@/lib/types";
import { LoadError, Loading } from "./system";

interface Overview {
  series: { date: string; contacts: number; customers: number; revenue_minor: number; posts: number; reach: number }[];
}

function greeting(): string {
  const h = new Date().getHours();
  return h < 5 ? "Working late" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}

function delta(now: number, before: number): { value: string; good: boolean } | null {
  if (!before) return null;
  const change = (now - before) / before;
  return { value: formatPercent(Math.abs(change), 0), good: change >= 0 };
}

const BRIEF_ICON: Record<string, React.ReactNode> = {
  critical: <AlertTriangle className="size-4 text-critical" />,
  warning: <AlertTriangle className="size-4 text-warning" />,
  success: <CheckCircle2 className="size-4 text-good" />,
  info: <Info className="size-4 text-accent" />,
};

export function GoalSummary({ goal, bid }: { goal: Goal; bid: string }) {
  const progress = goal.target_value ? Math.min(goal.current_value / goal.target_value, 1) : 0;
  const pace = goal.pace?.status;
  const forecast = goal.forecast;
  return (
    <Link
      to={`/b/${bid}/goals/${goal.id}` as string}
      className="block rounded-[18px] bg-surface p-5 shadow-[var(--shadow-sm)] ring-1 ring-hairline transition-shadow hover:shadow-[var(--shadow-md)]"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[13px] text-muted">Main goal</div>
          <div className="mt-0.5 truncate text-[17px] font-semibold tracking-[-0.02em]">{goal.title}</div>
        </div>
        {pace ? (
          <Badge tone={pace === "behind" ? "warning" : pace === "at_risk" ? "critical" : "good"}>
            {pace.replace("_", " ")}
          </Badge>
        ) : forecast ? (
          <Badge
            tone={forecast.verdict === "likely" ? "good" : forecast.verdict === "stretch" ? "warning" : "critical"}
          >
            {forecast.verdict}
          </Badge>
        ) : null}
      </div>
      <div className="mt-4 flex items-end gap-2">
        <span className="text-[34px] font-semibold leading-none tracking-[-0.03em] tabular">
          {formatNumber(goal.current_value)}
        </span>
        <span className="pb-1 text-[14px] text-muted">
          of {formatNumber(goal.target_value)} {goal.metric_label.toLowerCase()}
        </span>
      </div>
      <div
        className="mt-3 h-2 overflow-hidden rounded-full bg-fill-2"
        role="progressbar"
        aria-label={`${goal.metric_label} progress`}
        aria-valuenow={Math.round(progress * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className="h-full rounded-full bg-accent-fill transition-[width] duration-700"
          style={{ width: `${progress * 100}%` }}
        />
      </div>
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[12px] text-muted">
        <span>
          {goal.pace?.days_left ?? Math.max(0, Math.ceil((new Date(goal.deadline).getTime() - Date.now()) / 864e5))}{" "}
          days left
        </span>
        {forecast ? (
          <span>
            Forecast {formatNumber(forecast.p10)}–{formatNumber(forecast.p90)} ·{" "}
            {formatPercent(forecast.probability, 0)} chance
          </span>
        ) : null}
        {goal.pace ? <span>Expected by now {formatNumber(goal.pace.expected_to_date)}</span> : null}
      </div>
    </Link>
  );
}

function Setup({ bid }: { bid: string }) {
  const { data: brand } = useApi<{ current: { status: string } | null; draft: unknown }>(gpath(bid, "/brand-book"));
  const { data: channels } = useApi<{ platforms: PlatformInfo[] }>(gpath(bid, "/channels"));
  const { data: goals } = useApi<{ active: Goal | null }>(gpath(bid, "/goals"));
  if (!brand || !channels || !goals) return null;
  const steps = [
    {
      done: brand.current?.status === "approved",
      label: "Approve your brand book",
      body: "Voice, audience and positioning. Every post is checked against it.",
      to: "brand",
      icon: <BookOpen className="size-4" />,
    },
    {
      done: channels.platforms.some((p) => p.accounts.length > 0),
      label: "Connect a channel",
      body: "Instagram, X, LinkedIn, Telegram and more. Or practise with simulated ones.",
      to: "channels",
      icon: <Plug className="size-4" />,
    },
    {
      done: !!goals.active,
      label: "Set one main goal",
      body: "We'll forecast it, pick campaigns, and pace it every day.",
      to: "goals/new",
      icon: <Target className="size-4" />,
    },
  ];
  if (steps.every((s) => s.done)) return null;
  const doneCount = steps.filter((s) => s.done).length;
  return (
    <Card className="mb-6">
      <CardHeader title="Get set up" subtitle={`${doneCount} of ${steps.length} done`} />
      <ol className="px-2 pb-2">
        {steps.map((s) => (
          <li key={s.label}>
            <Link
              to={`/b/${bid}/${s.to}` as string}
              className={cn("flex items-center gap-3 rounded-[12px] px-3 py-3 hover:bg-fill-2", s.done && "opacity-60")}
            >
              {s.done ? (
                <CheckCircle2 className="size-5 shrink-0 text-good" />
              ) : (
                <Circle className="size-5 shrink-0 text-faint" />
              )}
              <div className="min-w-0 flex-1">
                <div className={cn("text-[14px] font-medium", s.done && "line-through")}>{s.label}</div>
                <div className="text-[13px] text-muted">{s.body}</div>
              </div>
              {!s.done ? <ArrowRight className="size-4 text-faint" /> : null}
            </Link>
          </li>
        ))}
      </ol>
    </Card>
  );
}

export function Today() {
  const bid = useBid();
  const navigate = useNavigate();
  const cockpit = useApi<Cockpit>(bpath(bid, "/cockpit"), { refetchInterval: 60_000 });
  const overview = useApi<Overview>(gpath(bid, "/analytics/overview?days=30"));
  const goals = useApi<{ active: Goal | null }>(gpath(bid, "/goals"));
  const [open, setOpen] = useState<string | null>(null);

  if (cockpit.isLoading) return <Loading rows={4} />;
  if (cockpit.error) return <LoadError error={cockpit.error} retry={cockpit.refetch} />;
  const c = cockpit.data!;
  const n = c.numbers;
  const series = overview.data?.series ?? [];
  const currency = c.business.currency;

  return (
    <div>
      <header className="mb-7">
        <p className="text-[14px] text-muted">
          {new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long" }).format(new Date())}
        </p>
        <h1 className="mt-0.5 text-[32px] font-bold tracking-[-0.03em]">
          {greeting()}, {c.user.name.split(" ")[0]}.
        </h1>
        <p className="mt-1 text-[15px] text-muted">
          {c.decisions.count
            ? `${c.decisions.count} ${c.decisions.count === 1 ? "thing needs" : "things need"} your call. Everything else is running.`
            : "Nothing needs you right now. Everything is running."}
        </p>
      </header>

      <Setup bid={bid} />

      <section aria-label="Key numbers" className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          label="Revenue · 30 days"
          value={formatMoney(n.revenue?.value_minor ?? 0, currency, { compact: (n.revenue?.value_minor ?? 0) > 1e8 })}
          delta={n.revenue ? delta(n.revenue.value_minor, n.revenue.previous_minor) : null}
          spark={series.map((s) => s.revenue_minor)}
        />
        <StatTile
          label="New subscribers"
          value={formatCompact(n.new_subscribers_30d ?? 0)}
          hint="30 days"
          spark={series.map((s) => s.contacts)}
        />
        <StatTile label="New leads" value={formatCompact(n.new_leads_30d ?? 0)} hint="30 days" />
        <StatTile
          label="Customers"
          value={formatCompact(n.customers ?? 0)}
          hint={`${n.new_customers_30d ?? 0} new · churn ${formatPercent(n.churn?.rate ?? 0, 1)}`}
          spark={series.map((s) => s.customers)}
        />
      </section>

      <div className="grid gap-6 lg:grid-cols-[1fr_360px]">
        <div className="space-y-6">
          <Card>
            <CardHeader
              title="Waiting for you"
              subtitle="Approve, edit or decline. Each answer teaches the system."
              action={
                c.decisions.count > 5 ? (
                  <Link to={`/b/${bid}/decisions` as string}>
                    <Button size="sm" variant="ghost">
                      See all {c.decisions.count}
                    </Button>
                  </Link>
                ) : null
              }
            />
            {c.decisions.items.length ? (
              <div className="divide-y divide-[var(--hairline)] pb-2">
                {c.decisions.items.slice(0, 6).map((d) => (
                  <DecisionRow key={d.id} d={d} onOpen={() => setOpen(d.id)} />
                ))}
              </div>
            ) : (
              <EmptyState
                icon={<CheckCircle2 className="size-6" />}
                title="All clear"
                body="When something needs your judgement, it lands here first."
              />
            )}
          </Card>
        </div>
        <div className="space-y-6">
          {goals.data?.active ? (
            <GoalSummary goal={goals.data.active} bid={bid} />
          ) : (
            <Card className="p-5">
              <div className="mb-3 flex size-10 items-center justify-center rounded-[12px] bg-accent-soft text-accent">
                <Sparkles className="size-5" />
              </div>
              <div className="text-[17px] font-semibold">Set your main goal</div>
              <p className="mt-1 text-[14px] text-muted">One goal at a time. We forecast it before you commit.</p>
              <Button variant="primary" className="mt-4" onClick={() => navigate({ to: `/b/${bid}/goals/new` })}>
                Set a goal
              </Button>
            </Card>
          )}
          <Card>
            <CardHeader title="Briefing" />
            {c.briefing.length ? (
              <ul className="space-y-1 px-2 pb-3">
                {c.briefing.map((b: BriefingItem) => (
                  <li key={`${b.kind}${b.title}`}>
                    <button
                      type="button"
                      disabled={!b.link}
                      onClick={() => b.link && navigate({ to: `/b/${bid}${b.link}` as string })}
                      className="flex w-full gap-3 rounded-[12px] px-3 py-2.5 text-left enabled:hover:bg-fill-2"
                    >
                      <span className="mt-0.5">{BRIEF_ICON[b.level]}</span>
                      <span className="min-w-0">
                        <span className="block text-[14px] font-medium">{b.title}</span>
                        {b.body ? <span className="block text-[13px] text-muted">{b.body}</span> : null}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="px-5 pb-5 text-[14px] text-muted">Nothing unusual today.</p>
            )}
          </Card>
        </div>
      </div>
      <DecisionSheet bid={bid} id={open} onClose={() => setOpen(null)} />
    </div>
  );
}
