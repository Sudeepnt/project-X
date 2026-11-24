import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { ArrowLeft, Pause, Play, Plus, Sparkles, Square, Target, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Bars, LineChart } from "@/components/charts";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Dialog,
  DialogContent,
  EmptyState,
  Field,
  Input,
  PageHeader,
  PlatformMark,
  platformLabel,
  Select,
  StatusBadge,
  Textarea,
} from "@/components/ui";
import { ask } from "@/components/ui/confirm";
import { api, errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDate, formatMoney, formatNumber, formatPercent, sentence } from "@/lib/format";
import { gpath, useAction, useApi, useBid, useMe } from "@/lib/queries";
import type { Forecast, Goal, Restriction } from "@/lib/types";
import { LoadError, Loading } from "./system";

interface MetricDef {
  key: string;
  label: string;
  goal_type: string;
  unit: string;
  higher_is_better: boolean;
}
interface MetricsMeta {
  metrics: MetricDef[];
  defaults: Record<string, string>;
  restriction_kinds: Record<string, string>;
}
interface Diagnosis {
  numbers: Record<string, unknown>;
  goal: { type: string; metric: string; why: string };
  growth_approach: { key: string; label: string; why: string };
  alternatives: string[];
}

const GOAL_TYPES = [
  { key: "awareness", label: "Get known", body: "Followers, reach, views" },
  { key: "demand", label: "Get leads", body: "Subscribers, signups, leads" },
  { key: "sales", label: "Get customers", body: "New customers, revenue" },
  { key: "partnerships", label: "Get partners", body: "Sponsors, affiliates, resellers" },
  { key: "retention", label: "Keep customers", body: "Churn, repeat purchase" },
  { key: "reputation", label: "Build trust", body: "Reviews, rating, mentions" },
];

const VERDICT_TONE = { likely: "good", achieved: "good", stretch: "warning", unrealistic: "critical" } as const;

const PART_LABEL: Record<string, string> = {
  paid_meta: "Meta ads",
  paid_google: "Google ads",
  paid_linkedin: "LinkedIn ads",
  paid: "Ads",
  existing_pipeline: "Leads already in your pipeline",
  outreach: "Outreach",
};
const PART_MARK: Record<string, string> = {
  paid_meta: "facebook",
  paid_google: "youtube",
  paid_linkedin: "linkedin",
  paid: "facebook",
};

export function ForecastPanel({ f, target, currency }: { f: Forecast; target: number; currency: string }) {
  const max = Math.max(f.p90, target) * 1.1 || 1;
  const pct = (v: number) => `${Math.min((v / max) * 100, 100)}%`;
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-[13px] text-muted">Chance of hitting it</div>
          <div className="text-[34px] font-semibold leading-none tracking-[-0.03em] tabular">
            {formatPercent(f.probability, 0)}
          </div>
        </div>
        <Badge tone={VERDICT_TONE[f.verdict]}>{sentence(f.verdict)}</Badge>
      </div>
      <div>
        <div
          className="relative h-10"
          role="img"
          aria-label={`Likely range ${formatNumber(f.p10)} to ${formatNumber(f.p90)}, target ${formatNumber(target)}`}
        >
          <div className="absolute inset-x-0 top-4 h-2 rounded-full bg-fill-2" />
          <div
            className="absolute top-4 h-2 rounded-full bg-[var(--series-1)] opacity-30"
            style={{ left: pct(f.p10), width: `calc(${pct(f.p90)} - ${pct(f.p10)})` }}
          />
          <div
            className="absolute top-2.5 size-5 -translate-x-1/2 rounded-full border-[3px] border-[var(--surface)] bg-[var(--series-1)] shadow"
            style={{ left: pct(f.p50) }}
            title={`Most likely ${formatNumber(f.p50)}`}
          />
          <div className="absolute top-0 h-10 w-0.5 -translate-x-1/2 bg-text" style={{ left: pct(target) }} />
        </div>
        <div className="mt-1 flex justify-between text-[12px] text-muted tabular">
          <span>Low {formatNumber(f.p10)}</span>
          <span className="font-medium text-text">Likely {formatNumber(f.p50)}</span>
          <span>High {formatNumber(f.p90)}</span>
        </div>
        <div className="mt-0.5 text-[12px] text-muted">
          Black line: your target ({formatNumber(target)}). Shaded: 80% likely range.
        </div>
      </div>
      {Object.keys(f.breakdown).length ? (
        <div>
          <div className="mb-2 text-[13px] font-medium">Expected contribution by channel</div>
          <Bars
            rows={Object.entries(f.breakdown)
              .sort((a, b) => b[1] - a[1])
              .map(([k, v]) => ({
                label: (
                  <>
                    <PlatformMark platform={PART_MARK[k] ?? k} size={18} />
                    <span>{PART_LABEL[k] ?? platformLabel(k)}</span>
                  </>
                ),
                value: v,
              }))}
          />
        </div>
      ) : null}
      {f.suggestions.length ? (
        <div className="rounded-[14px] bg-[var(--warning-soft)] p-3.5 text-[13px]">
          <div className="mb-1 font-medium">To make it more likely</div>
          <ul className="space-y-0.5 text-text-2">
            {f.suggestions.map((s) => (
              <li key={s.label}>
                · {s.label}
                {s.budget_minor ? ` (${formatMoney(s.budget_minor, currency)})` : ""}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <details className="text-[12px] text-muted">
        <summary className="cursor-pointer select-none">How we worked this out</summary>
        <ul className="mt-2 space-y-1">
          {f.assumptions.map((a) => (
            <li key={a}>· {a}</li>
          ))}
        </ul>
      </details>
    </div>
  );
}

export function Goals() {
  const bid = useBid();
  const { data, isLoading, error, refetch } = useApi<{ items: Goal[]; active: Goal | null }>(gpath(bid, "/goals"));
  if (isLoading) return <Loading />;
  if (error) return <LoadError error={error} retry={refetch} />;
  const past = data!.items.filter((g) => g.id !== data!.active?.id);
  return (
    <div>
      <PageHeader
        title="Goals"
        subtitle="One main goal at a time. Everything else is planned, paced and stopped around it."
        actions={
          <Link to={`/b/${bid}/goals/new` as string}>
            <Button variant="primary" icon={<Plus className="size-4" />}>
              New goal
            </Button>
          </Link>
        }
      />
      {data!.active ? (
        <GoalHero goal={data!.active} bid={bid} />
      ) : (
        <Card>
          <EmptyState
            icon={<Target className="size-6" />}
            title="No active goal"
            body="Set one and we'll forecast it, choose campaigns, and pace it daily."
            action={
              <Link to={`/b/${bid}/goals/new` as string}>
                <Button variant="primary">Set a goal</Button>
              </Link>
            }
          />
        </Card>
      )}
      {past.length ? (
        <Card className="mt-6">
          <CardHeader title="Other goals" />
          <ul className="divide-y divide-[var(--hairline)] pb-2">
            {past.map((g) => (
              <li key={g.id}>
                <Link
                  to={`/b/${bid}/goals/${g.id}` as string}
                  className="flex items-center gap-4 px-5 py-3 hover:bg-fill-2"
                >
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[14px] font-medium">{g.title}</div>
                    <div className="text-[12px] text-muted">
                      {formatDate(g.start_date)} – {formatDate(g.deadline)} · {formatNumber(g.current_value)} /{" "}
                      {formatNumber(g.target_value)}
                    </div>
                  </div>
                  <StatusBadge status={g.status} />
                </Link>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}
    </div>
  );
}

function GoalHero({ goal, bid }: { goal: Goal; bid: string }) {
  return (
    <Link to={`/b/${bid}/goals/${goal.id}` as string} className="block">
      <Card className="p-6 transition-shadow hover:shadow-[var(--shadow-md)]">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-[13px] text-muted">
              Active · ends {formatDate(goal.deadline, { day: "numeric", month: "long" })}
            </div>
            <div className="mt-1 text-[22px] font-semibold tracking-[-0.02em]">{goal.title}</div>
          </div>
          {goal.pace ? (
            <StatusBadge status={goal.pace.status} />
          ) : goal.forecast ? (
            <Badge tone={VERDICT_TONE[goal.forecast.verdict]}>{goal.forecast.verdict}</Badge>
          ) : null}
        </div>
        <div className="mt-5 grid gap-6 sm:grid-cols-3">
          <div>
            <div className="text-[13px] text-muted">So far</div>
            <div className="text-[28px] font-semibold tabular">{formatNumber(goal.current_value)}</div>
          </div>
          <div>
            <div className="text-[13px] text-muted">Target</div>
            <div className="text-[28px] font-semibold tabular">{formatNumber(goal.target_value)}</div>
          </div>
          <div>
            <div className="text-[13px] text-muted">Forecast</div>
            <div className="text-[28px] font-semibold tabular">
              {goal.forecast ? formatNumber(goal.forecast.p50) : "—"}
            </div>
          </div>
        </div>
      </Card>
    </Link>
  );
}

interface BudgetLevel {
  budget_minor: number;
  p10: number;
  p50: number;
  p90: number;
  probability: number;
  cost_per_extra_minor: number | null;
}
interface BudgetPlan {
  levels: BudgetLevel[];
  channels: { channel: string; p50: number; cost_per_result_minor: number | null }[];
  recommended_budget_minor: number;
  organic_p50: number;
}

function BudgetPlanner({
  bid,
  currency,
  body,
  onUse,
}: {
  bid: string;
  currency: string;
  body: Record<string, unknown>;
  onUse: (minor: number) => void;
}) {
  const [plan, setPlan] = useState<BudgetPlan | null>(null);
  const run = useAction(
    () => api.post<BudgetPlan>(gpath(bid, "/goals/budget-plan"), { ...body, paid_channels: ["meta", "google"] }),
    {
      onSuccess: setPlan,
    },
  );
  return (
    <div className="mt-6 border-t border-hairline pt-5">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-[14px] font-semibold">Budget planner</h3>
        <Button size="sm" onClick={() => run.mutate()} loading={run.isPending}>
          {plan ? "Recalculate" : "Compare budgets"}
        </Button>
      </div>
      {plan ? (
        <div className="mt-3 space-y-3">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-left text-muted">
                <th className="py-1 font-medium">Budget</th>
                <th className="py-1 text-right font-medium">Likely</th>
                <th className="py-1 text-right font-medium">Each extra</th>
              </tr>
            </thead>
            <tbody className="tabular">
              {plan.levels.map((lv) => (
                <tr
                  key={lv.budget_minor}
                  className={cn(lv.budget_minor === plan.recommended_budget_minor && "font-semibold text-accent")}
                >
                  <td className="py-1">
                    <button
                      type="button"
                      className="hover:underline"
                      onClick={() => onUse(lv.budget_minor)}
                      title="Use this budget"
                    >
                      {lv.budget_minor ? formatMoney(lv.budget_minor, currency, { compact: true }) : "Organic only"}
                    </button>
                  </td>
                  <td className="py-1 text-right">
                    {formatNumber(lv.p50)}{" "}
                    <span className="font-normal text-faint">
                      ({formatNumber(lv.p10)}–{formatNumber(lv.p90)})
                    </span>
                  </td>
                  <td className="py-1 text-right">
                    {lv.cost_per_extra_minor
                      ? formatMoney(Math.round(lv.cost_per_extra_minor / 100) * 100, currency)
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-[12px] text-muted">
            Highlighted: the most we'd spend before each extra result costs more than twice the first ones. Click a row
            to use it.
            {plan.channels[0]?.cost_per_result_minor
              ? ` Best value: ${plan.channels[0].channel === "meta" ? "Meta" : plan.channels[0].channel === "google" ? "Google" : "LinkedIn"} at about ${formatMoney(Math.round(plan.channels[0].cost_per_result_minor / 100) * 100, currency)} per result.`
              : ""}
          </p>
        </div>
      ) : (
        <p className="mt-2 text-[12px] text-muted">
          See what more or less budget is likely to buy, from the same model as the forecast.
        </p>
      )}
    </div>
  );
}

export function NewGoal() {
  const bid = useBid();
  const navigate = useNavigate();
  const { data: me } = useMe();
  const currency = me?.businesses.find((b) => b.id === bid)?.currency ?? "USD";
  const { data: meta } = useApi<MetricsMeta>(gpath(bid, "/goals/metrics"));
  const { data: diagnosis } = useApi<Diagnosis>(gpath(bid, "/diagnosis"));
  const { data: goals } = useApi<{ active: Goal | null }>(gpath(bid, "/goals"));
  const inDays = (d: number) => new Date(Date.now() + d * 864e5).toISOString().slice(0, 10);
  const [form, setForm] = useState({
    type: "demand",
    metric: "",
    target: "",
    deadline: inDays(60),
    budget: "0",
    approval_mode: "approve_all",
    title: "",
  });
  const [forecast, setForecast] = useState<Forecast | null>(null);
  const [forecastError, setForecastError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (diagnosis && !form.metric) setForm((f) => ({ ...f, type: diagnosis.goal.type, metric: diagnosis.goal.metric }));
  }, [diagnosis, form.metric]);

  const metrics = useMemo(() => meta?.metrics.filter((m) => m.goal_type === form.type) ?? [], [meta, form.type]);
  const metric = metrics.find((m) => m.key === form.metric) ?? metrics[0];
  const budgetMinor = Math.round(Number(form.budget || 0) * (currency === "JPY" ? 1 : 100));

  useEffect(() => {
    const target = Number(form.target);
    if (!metric || !target || !form.deadline) {
      setForecast(null);
      return;
    }
    const t = setTimeout(() => {
      api
        .post<Forecast>(gpath(bid, "/goals/forecast"), {
          type: form.type,
          metric: metric.key,
          target,
          deadline: form.deadline,
          budget_minor: budgetMinor,
        })
        .then((f) => {
          setForecast(f);
          setForecastError(null);
        })
        .catch((e) => setForecastError(errorMessage(e)));
    }, 350);
    return () => clearTimeout(t);
  }, [bid, form.type, metric, form.target, form.deadline, budgetMinor]);

  const save = async () => {
    if (!metric) return;
    setSaving(true);
    try {
      const body = {
        type: form.type,
        metric: metric.key,
        target: Number(form.target),
        deadline: form.deadline,
        budget_minor: budgetMinor,
        approval_mode: form.approval_mode,
        title: form.title || null,
        growth_approach: diagnosis?.growth_approach.key ?? "content_community",
        activate: true,
        replace_active: !!goals?.active,
      };
      const goal = await api.post<Goal>(gpath(bid, "/goals"), body);
      navigate({ to: `/b/${bid}/goals/${goal.id}` });
    } catch (e) {
      setForecastError(errorMessage(e));
      setSaving(false);
    }
  };

  return (
    <div>
      <Link to={`/b/${bid}/goals` as string} className="mb-4 inline-flex items-center gap-1 text-[14px] text-accent">
        <ArrowLeft className="size-4" />
        Goals
      </Link>
      <PageHeader
        title="Set your main goal"
        subtitle="Pick what matters most for the next few weeks. You'll see the odds before you commit."
      />
      {diagnosis ? (
        <div className="mb-6 flex gap-3 rounded-[16px] bg-accent-soft p-4 text-[14px]">
          <Sparkles className="mt-0.5 size-5 shrink-0 text-accent" />
          <div>
            <span className="font-medium">Our suggestion: </span>
            {diagnosis.goal.why}{" "}
            <span className="text-muted">
              Approach: {diagnosis.growth_approach.label}. {diagnosis.growth_approach.why}
            </span>
          </div>
        </div>
      ) : null}
      <div className="grid gap-6 lg:grid-cols-[1fr_380px]">
        <Card className="space-y-6 p-6">
          <div>
            <div className="mb-2 text-[13px] font-medium text-text-2">What do you want more of?</div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              {GOAL_TYPES.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => setForm({ ...form, type: t.key, metric: meta?.defaults[t.key] ?? "" })}
                  className={cn(
                    "rounded-[12px] px-3.5 py-3 text-left ring-1 transition-all",
                    form.type === t.key ? "bg-accent-soft ring-accent" : "ring-hairline hover:bg-fill-2",
                  )}
                >
                  <span className="block text-[14px] font-medium">{t.label}</span>
                  <span className="block text-[12px] text-muted">{t.body}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Measure">
              {(id) => (
                <Select
                  id={id}
                  value={metric?.key ?? ""}
                  onChange={(e) => setForm({ ...form, metric: e.target.value })}
                >
                  {metrics.map((m) => (
                    <option key={m.key} value={m.key}>
                      {m.label}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field
              label={
                metric?.unit === "rate"
                  ? "Target (e.g. 0.03 for 3%)"
                  : metric?.unit === "money"
                    ? `Target (${currency})`
                    : "Target"
              }
            >
              {(id) => (
                <Input
                  id={id}
                  type="number"
                  min={0}
                  step="any"
                  inputMode="decimal"
                  required
                  value={form.target}
                  onChange={(e) => setForm({ ...form, target: e.target.value })}
                />
              )}
            </Field>
            <Field label="By">
              {(id) => (
                <Input
                  id={id}
                  type="date"
                  min={inDays(7)}
                  value={form.deadline}
                  onChange={(e) => setForm({ ...form, deadline: e.target.value })}
                />
              )}
            </Field>
            <Field label={`Budget for ads (${currency})`} hint="0 means organic only.">
              {(id) => (
                <Input
                  id={id}
                  type="number"
                  min={0}
                  value={form.budget}
                  onChange={(e) => setForm({ ...form, budget: e.target.value })}
                />
              )}
            </Field>
          </div>
          <Field label="How much should we ask you?">
            {(id) => (
              <Select
                id={id}
                value={form.approval_mode}
                onChange={(e) => setForm({ ...form, approval_mode: e.target.value })}
              >
                <option value="approve_all">Approve everything public (recommended to start)</option>
                <option value="approve_new">Approve new kinds of things; repeat ones run</option>
                <option value="auto">Run on its own within limits</option>
              </Select>
            )}
          </Field>
          <Field label="Name (optional)">
            {(id) => (
              <Input
                id={id}
                placeholder={
                  metric && form.target ? `${metric.label}: ${form.target} by ${formatDate(form.deadline)}` : ""
                }
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
              />
            )}
          </Field>
          {goals?.active ? (
            <p className="text-[13px] text-warning">
              This replaces your current main goal, “{goals.active.title}”. It will be stopped and reported.
            </p>
          ) : null}
          <Button variant="primary" size="lg" loading={saving} disabled={!forecast} onClick={save}>
            Commit to this goal
          </Button>
        </Card>
        <Card className="h-fit p-6 lg:sticky lg:top-20">
          <h2 className="mb-4 text-[17px] font-semibold">Forecast</h2>
          {forecastError ? (
            <p className="text-[13px] text-critical">{forecastError}</p>
          ) : forecast ? (
            <ForecastPanel f={forecast} target={Number(form.target)} currency={currency} />
          ) : (
            <p className="text-[14px] text-muted">
              Enter a target to see how likely it is, given your channels and history.
            </p>
          )}
          {forecast && budgetMinor > 0 ? (
            <BudgetPlanner
              bid={bid}
              currency={currency}
              body={{
                type: form.type,
                metric: metric?.key,
                target: Number(form.target),
                deadline: form.deadline,
                budget_minor: budgetMinor,
              }}
              onUse={(minor) => setForm({ ...form, budget: String(minor / (currency === "JPY" ? 1 : 100)) })}
            />
          ) : null}
        </Card>
      </div>
    </div>
  );
}

const PARAM_FIELDS: Record<
  string,
  { key: string; label: string; kind: "list" | "number" | "money" | "rate" | "bool" }
> = {
  allowed_platforms: { key: "platforms", label: "Platforms (comma separated)", kind: "list" },
  blackout_dates: { key: "dates", label: "Dates (YYYY-MM-DD, comma separated)", kind: "list" },
  max_posts_per_day: { key: "total", label: "Posts per day", kind: "number" },
  max_outreach_per_day: { key: "max", label: "Messages per day", kind: "number" },
  daily_budget: { key: "amount_minor", label: "Amount per day", kind: "money" },
  total_budget: { key: "amount_minor", label: "Total amount", kind: "money" },
  cpl_cap: { key: "amount_minor", label: "Max cost per lead", kind: "money" },
  ai_credits: { key: "cents", label: "Cents per day", kind: "number" },
  do_not_list: { key: "phrases", label: "Words or topics (comma separated)", kind: "list" },
  no_unproven_claims: { key: "enabled", label: "Enabled", kind: "bool" },
  unsubscribe_rate_cap: { key: "rate", label: "Rate (0.005 = 0.5%)", kind: "rate" },
  complaint_rate_cap: { key: "rate", label: "Rate (0.001 = 0.1%)", kind: "rate" },
  negative_sentiment_spike: { key: "rate", label: "Share (0.2 = 20%)", kind: "rate" },
};

function AddRestriction({ bid, goalId, kinds }: { bid: string; goalId: string; kinds: Record<string, string> }) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState("max_posts_per_day");
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const field = PARAM_FIELDS[kind];
  const add = useAction(
    () => {
      let v: unknown = value;
      if (field?.kind === "list")
        v = value
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
      else if (field?.kind === "money") v = Math.round(Number(value) * 100);
      else if (field?.kind === "number" || field?.kind === "rate") v = Number(value);
      else if (field?.kind === "bool") v = true;
      return api.post(gpath(bid, "/restrictions"), {
        kind,
        params: field ? { [field.key]: v } : {},
        goal_id: goalId,
        note: note || null,
      });
    },
    {
      invalidate: [gpath(bid, "/goals"), gpath(bid, "/restrictions")],
      success: "Guardrail added",
      onSuccess: () => {
        setOpen(false);
        setValue("");
      },
    },
  );
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button size="sm" icon={<Plus className="size-4" />} onClick={() => setOpen(true)}>
        Add
      </Button>
      <DialogContent title="Add a guardrail" description="Hard limits the system will never cross for this goal.">
        <div className="space-y-4">
          <Field label="Rule">
            {(id) => (
              <Select
                id={id}
                value={kind}
                onChange={(e) => {
                  setKind(e.target.value);
                  setValue("");
                }}
              >
                {Object.entries(kinds).map(([k, l]) => (
                  <option key={k} value={k}>
                    {l}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {field && field.kind !== "bool" ? (
            <Field label={field.label}>
              {(id) => (
                <Input
                  id={id}
                  type={field.kind === "list" ? "text" : "number"}
                  step="any"
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                />
              )}
            </Field>
          ) : null}
          <Field label="Note (optional)">
            {(id) => <Textarea id={id} rows={2} value={note} onChange={(e) => setNote(e.target.value)} />}
          </Field>
          <div className="flex justify-end">
            <Button
              variant="primary"
              onClick={() => add.mutate()}
              loading={add.isPending}
              disabled={field?.kind !== "bool" && !value}
            >
              Add guardrail
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function describeRestriction(r: Restriction): string {
  const v = Object.values(r.params)[0];
  return Array.isArray(v) ? v.join(", ") : typeof v === "boolean" ? "" : v == null ? "" : String(v);
}

export function GoalDetail() {
  const bid = useBid();
  const { id } = useParams({ strict: false }) as { id: string };
  const { data: me } = useMe();
  const currency = me?.businesses.find((b) => b.id === bid)?.currency ?? "USD";
  const { data: goal, isLoading, error, refetch } = useApi<Goal>(gpath(bid, `/goals/${id}`));
  const { data: meta } = useApi<MetricsMeta>(gpath(bid, "/goals/metrics"));
  const inv = [gpath(bid, "/goals"), gpath(bid, "/campaigns")];
  const pace = useAction(() => api.post(gpath(bid, `/goals/${id}/pace`)), { invalidate: inv, success: "Pace updated" });
  const pause = useAction(() => api.post(gpath(bid, `/goals/${id}/pause`)), {
    invalidate: inv,
    success: "Paused. Campaigns are on hold.",
  });
  const activate = useAction(() => api.post(gpath(bid, `/goals/${id}/activate`), { replace_active: false }), {
    invalidate: inv,
    success: "Resumed",
  });
  const stop = useAction(() => api.post(gpath(bid, `/goals/${id}/stop`), { reason: "Stopped by founder" }), {
    invalidate: inv,
    success: "Stopped. A final report is on its way.",
  });
  const remove = useAction((rid: string) => api.delete(gpath(bid, `/restrictions/${rid}`)), {
    invalidate: inv,
    success: "Guardrail removed",
  });

  if (isLoading) return <Loading rows={4} />;
  if (error) return <LoadError error={error} retry={refetch} />;
  const g = goal!;
  const hist = g.pace_history ?? [];
  const labels = [g.start_date, ...hist.map((h) => h.at.slice(0, 10))];
  const expectedLine = [g.baseline_value, ...hist.map((h) => h.expected_to_date)];
  const actualLine = [g.baseline_value, ...hist.map((h) => h.actual)];
  const projectedLine = [null, ...hist.map((h) => h.projected)];

  return (
    <div>
      <Link to={`/b/${bid}/goals` as string} className="mb-4 inline-flex items-center gap-1 text-[14px] text-accent">
        <ArrowLeft className="size-4" />
        Goals
      </Link>
      <PageHeader
        title={g.title}
        subtitle={`${g.metric_label} · ${formatDate(g.start_date)} to ${formatDate(g.deadline, { day: "numeric", month: "short", year: "numeric" })}`}
        actions={
          <>
            <StatusBadge status={g.status} />
            {g.status === "active" ? (
              <>
                <Button size="sm" onClick={() => pace.mutate()} loading={pace.isPending}>
                  Check pace now
                </Button>
                <Button
                  size="sm"
                  icon={<Pause className="size-4" />}
                  onClick={() => pause.mutate()}
                  loading={pause.isPending}
                >
                  Pause
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  icon={<Square className="size-4" />}
                  onClick={async () =>
                    (await ask({
                      title: "Stop this goal?",
                      body: "Its campaigns stop too, and you get a final report.",
                      confirmLabel: "Stop goal",
                      destructive: true,
                    })) && stop.mutate()
                  }
                >
                  Stop
                </Button>
              </>
            ) : g.status === "paused" || g.status === "draft" ? (
              <Button
                size="sm"
                variant="primary"
                icon={<Play className="size-4" />}
                onClick={() => activate.mutate()}
                loading={activate.isPending}
              >
                {g.status === "draft" ? "Activate" : "Resume"}
              </Button>
            ) : null}
          </>
        }
      />
      <div className="grid gap-6 lg:grid-cols-[1fr_360px]">
        <div className="space-y-6">
          <Card>
            <CardHeader
              title="Pace"
              subtitle={
                g.pace
                  ? `${formatNumber(g.pace.actual)} so far · ${formatNumber(g.pace.expected_to_date)} expected by now · projected ${formatNumber(g.pace.projected)}`
                  : "Checked every day. The first check runs tonight."
              }
            />
            <div className="px-5 pt-6 pb-5">
              {hist.length ? (
                <LineChart
                  labels={labels}
                  formatLabel={(l) => formatDate(l)}
                  format={(v) => formatNumber(v)}
                  target={{ value: g.target_value, label: `Target ${formatNumber(g.target_value)}` }}
                  series={[
                    { key: "actual", label: "Actual", values: actualLine, color: "var(--series-1)", area: true },
                    { key: "expected", label: "Expected", values: expectedLine, color: "var(--muted)", dashed: true },
                    { key: "projected", label: "Projected", values: projectedLine, color: "var(--series-2)" },
                  ]}
                />
              ) : (
                <p className="py-10 text-center text-[14px] text-muted">The pace chart fills in as daily checks run.</p>
              )}
            </div>
          </Card>
          <Card>
            <CardHeader
              title="Campaigns working on this"
              action={
                <Link to={`/b/${bid}/campaigns` as string}>
                  <Button size="sm" variant="ghost">
                    Manage
                  </Button>
                </Link>
              }
            />
            <ul className="divide-y divide-[var(--hairline)] pb-2">
              {g.campaigns?.length ? (
                g.campaigns.map((c) => (
                  <li key={c.id} className="flex items-center gap-3 px-5 py-3">
                    <div className="min-w-0 flex-1">
                      <div className="text-[14px] font-medium">
                        {c.name} <span className="text-[12px] font-normal text-muted capitalize">· {c.role}</span>
                      </div>
                      <div className="flex gap-1 pt-1">
                        {c.channels.map((p) => (
                          <PlatformMark key={p} platform={p} size={18} />
                        ))}
                      </div>
                    </div>
                    <StatusBadge status={c.status} />
                  </li>
                ))
              ) : (
                <li className="px-5 py-4 text-[14px] text-muted">No campaigns yet.</li>
              )}
            </ul>
          </Card>
        </div>
        <div className="space-y-6">
          {g.forecast ? (
            <Card className="p-5">
              <h2 className="mb-4 text-[17px] font-semibold">Forecast</h2>
              <ForecastPanel f={g.forecast} target={g.target_value} currency={currency} />
            </Card>
          ) : null}
          <Card>
            <CardHeader
              title="Guardrails"
              subtitle="Limits the system never crosses."
              action={meta ? <AddRestriction bid={bid} goalId={g.id} kinds={meta.restriction_kinds} /> : null}
            />
            <ul className="px-2 pb-3">
              {g.restrictions?.length ? (
                g.restrictions.map((r) => (
                  <li key={r.id} className="group flex items-center gap-2 rounded-[10px] px-3 py-2 hover:bg-fill-2">
                    <div className="min-w-0 flex-1 text-[13px]">
                      <div className="font-medium">{r.label}</div>
                      <div className="truncate text-muted">{describeRestriction(r)}</div>
                    </div>
                    <button
                      type="button"
                      aria-label="Remove"
                      onClick={() => remove.mutate(r.id)}
                      className="rounded p-1 text-faint opacity-0 hover:text-critical group-hover:opacity-100 focus:opacity-100"
                    >
                      <Trash2 className="size-4" />
                    </button>
                  </li>
                ))
              ) : (
                <li className="px-3 py-2 text-[13px] text-muted">None yet. Budgets, blackout dates, banned words…</li>
              )}
            </ul>
          </Card>
          <Card className="p-5 text-[13px]">
            <div className="mb-2 font-semibold">Stop rules</div>
            <ul className="space-y-1 text-text-2">
              {((g as unknown as { stop_rules: { kind: string; action: string }[] }).stop_rules ?? []).map((r) => (
                <li key={r.kind}>
                  · {sentence(r.kind)} → {sentence(r.action)}
                </li>
              ))}
            </ul>
          </Card>
        </div>
      </div>
    </div>
  );
}
