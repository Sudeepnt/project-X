import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { ArrowLeft, ExternalLink, FileBarChart, RefreshCw } from "lucide-react";
import { useState } from "react";
import { Bars, LineChart, StatTile } from "@/components/charts";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  PageHeader,
  PlatformMark,
  platformLabel,
  Segmented,
  Switch,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatCompact, formatDate, formatMoney, formatNumber, formatPercent, sentence } from "@/lib/format";
import { gpath, useAction, useApi, useBid, useMe } from "@/lib/queries";
import { LoadError, Loading } from "./system";

interface Overview {
  numbers: {
    revenue?: { value_minor: number; previous_minor: number };
    customers?: number;
    new_leads_30d?: number;
    new_subscribers_30d?: number;
    new_customers_30d?: number;
    churn?: { rate: number };
  };
  series: { date: string; contacts: number; customers: number; revenue_minor: number; posts: number; reach: number }[];
  channels: {
    platform: string;
    posts: number;
    reach: number;
    engagements: number;
    engagement_rate: number;
    clicks: number;
    leads: number;
    customers: number;
  }[];
  links: { clicks: number };
  referrals: number;
  compute: { micro_usd: number; calls: number };
}
interface Attribution {
  campaigns: {
    campaign_id: string;
    name: string;
    first_touch_leads: number;
    last_touch_leads: number;
    revenue_minor: number;
  }[];
}
interface PatternRow {
  key: string;
  n: number;
  score: number;
  mean: number;
}
interface ContentReport {
  leaderboard: {
    post_id: string;
    content_id: string;
    title: string;
    platform: string;
    url: string | null;
    score: number;
    metrics: Record<string, number>;
    hook_type: string | null;
    pillar: string;
    published_at: string;
  }[];
  patterns: Record<string, PatternRow[]>;
}
interface ReportRow {
  id: string;
  kind: string;
  period_start: string;
  period_end: string;
  headline: string;
}

function useCurrency(bid: string) {
  const { data } = useMe();
  return data?.businesses.find((b) => b.id === bid)?.currency ?? "USD";
}

function Trends({ data, currency }: { data: Overview; currency: string }) {
  const labels = data.series.map((s) => s.date);
  const charts: { key: keyof Overview["series"][number]; label: string; format: (v: number) => string }[] = [
    { key: "contacts", label: "New contacts", format: formatNumber },
    { key: "customers", label: "New customers", format: formatNumber },
    { key: "revenue_minor", label: "Revenue", format: (v) => formatMoney(v, currency, { compact: true }) },
    { key: "reach", label: "Reach", format: formatCompact },
  ];
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {charts.map((c) => (
        <Card key={c.key}>
          <CardHeader
            title={c.label}
            subtitle={`Total ${c.format(data.series.reduce((s, r) => s + Number(r[c.key]), 0))}`}
          />
          <div className="px-4 pb-4">
            <LineChart
              height={170}
              labels={labels}
              formatLabel={(l) => formatDate(l)}
              format={c.format}
              series={[{ key: c.key, label: c.label, values: data.series.map((r) => Number(r[c.key])), area: true }]}
            />
          </div>
        </Card>
      ))}
    </div>
  );
}

function ChannelTable({ rows }: { rows: Overview["channels"] }) {
  if (!rows.length)
    return <EmptyState title="No channel data yet" body="Numbers arrive a few hours after posts go out." />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-[13px]">
        <thead>
          <tr className="text-left text-[12px] text-muted hairline-b">
            {["Channel", "Posts", "Reach", "Engagement", "Clicks", "Leads", "Customers"].map((h, i) => (
              <th key={h} className={i ? "px-3 py-2.5 text-right font-medium" : "px-5 py-2.5 font-medium"}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--hairline)]">
          {rows.map((r) => (
            <tr key={r.platform}>
              <td className="px-5 py-2.5">
                <span className="flex items-center gap-2">
                  <PlatformMark platform={r.platform} size={20} />
                  <span>{platformLabel(r.platform)}</span>
                </span>
              </td>
              <td className="px-3 py-2.5 text-right tabular">{formatNumber(r.posts)}</td>
              <td className="px-3 py-2.5 text-right tabular">{formatCompact(r.reach)}</td>
              <td className="px-3 py-2.5 text-right tabular">{formatPercent(r.engagement_rate)}</td>
              <td className="px-3 py-2.5 text-right tabular">{formatNumber(r.clicks)}</td>
              <td className="px-3 py-2.5 text-right tabular font-medium">{formatNumber(r.leads)}</td>
              <td className="px-3 py-2.5 text-right tabular">{formatNumber(r.customers)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const PATTERN_LABELS: Record<string, string> = {
  hook_type: "Hooks",
  theme: "Themes",
  format: "Formats",
  platform: "Platforms",
  intent: "Value vs ask",
  hour: "Time of day",
  playbook: "Playbooks",
};

function Working({ bid, days }: { bid: string; days: number }) {
  const { data, isLoading, error } = useApi<ContentReport>(gpath(bid, `/analytics/content?days=${days}`));
  if (isLoading) return <Loading />;
  if (error) return <LoadError error={error} />;
  const groups = Object.entries(data!.patterns).filter(([, rows]) => rows.length);
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          title="Top posts"
          subtitle="Scored against your own average for that platform, so small and big channels compare fairly."
        />
        {data!.leaderboard.length ? (
          <ol className="divide-y divide-[var(--hairline)] pb-2">
            {data!.leaderboard.slice(0, 10).map((p, i) => (
              <li key={p.post_id} className="flex items-center gap-3 px-5 py-2.5 text-[13px]">
                <span className="w-5 text-right text-faint tabular">{i + 1}</span>
                <PlatformMark platform={p.platform} size={20} />
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium">{p.title}</div>
                  <div className="text-[12px] text-muted">
                    {p.hook_type ? `${sentence(p.hook_type)} hook · ` : ""}
                    {sentence(p.pillar)} · {formatDate(p.published_at)}
                  </div>
                </div>
                <span className="hidden text-right text-[12px] text-muted tabular sm:block">
                  {formatCompact(p.metrics.reach ?? 0)} reach · {formatNumber(p.metrics.clicks ?? 0)} clicks
                </span>
                <span className="w-12 text-right font-semibold tabular">{p.score.toFixed(2)}</span>
                {p.url ? (
                  <a
                    href={p.url}
                    target="_blank"
                    rel="noreferrer"
                    aria-label="Open post"
                    className="text-faint hover:text-accent"
                  >
                    <ExternalLink className="size-4" />
                  </a>
                ) : (
                  <span className="w-4" />
                )}
              </li>
            ))}
          </ol>
        ) : (
          <EmptyState title="No published posts in this period" />
        )}
      </Card>
      {groups.length ? (
        <div className="grid gap-4 md:grid-cols-2">
          {groups.map(([k, rows]) => (
            <Card key={k}>
              <CardHeader
                title={PATTERN_LABELS[k] ?? sentence(k)}
                subtitle="Average score. Used to plan what comes next."
              />
              <div className="px-5 pb-5">
                <Bars
                  format={(v) => v.toFixed(2)}
                  rows={rows.slice(0, 6).map((r) => ({
                    label: (
                      <>
                        {k === "platform" ? <PlatformMark platform={r.key} size={18} /> : null}
                        <span>{k === "hour" ? `${r.key}:00` : sentence(r.key)}</span>
                        <span className="text-faint">· {r.n}</span>
                      </>
                    ),
                    value: Math.max(r.mean, 0),
                    hint: `${r.n} posts, mean ${r.mean.toFixed(2)}`,
                  }))}
                />
              </div>
            </Card>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Reports({ bid }: { bid: string }) {
  const { data, isLoading } = useApi<{ items: ReportRow[] }>(gpath(bid, "/analytics/reports"));
  const run = useAction(() => api.post(gpath(bid, "/analytics/reports/run")), {
    invalidate: [gpath(bid, "/analytics/reports")],
    success: "Writing this week's review",
  });
  return (
    <div>
      <div className="mb-4 flex justify-end">
        <Button icon={<RefreshCw className="size-4" />} onClick={() => run.mutate()} loading={run.isPending}>
          Write one now
        </Button>
      </div>
      {isLoading ? (
        <Loading />
      ) : !data?.items.length ? (
        <Card>
          <EmptyState
            icon={<FileBarChart className="size-6" />}
            title="No reviews yet"
            body="Every Monday you get a plain-language review of the week: what worked, what didn't, what changes next."
          />
        </Card>
      ) : (
        <Card className="divide-y divide-[var(--hairline)] overflow-hidden">
          {data.items.map((r) => (
            <Link
              key={r.id}
              to={`/b/${bid}/analytics/reports/${r.id}` as string}
              className="block px-5 py-3.5 hover:bg-fill-2"
            >
              <div className="text-[12px] text-muted">
                {sentence(r.kind)} · {formatDate(r.period_start)} – {formatDate(r.period_end)}
              </div>
              <div className="mt-0.5 line-clamp-2 text-[14px] font-medium">{r.headline}</div>
            </Link>
          ))}
        </Card>
      )}
    </div>
  );
}

interface BenchmarkItem {
  metric: string;
  platform: string | null;
  you: number | null;
  median: number;
  p25: number;
  p75: number;
  businesses: number;
}

const BENCH_LABEL: Record<string, string> = {
  engagement_rate: "Engagement rate",
  reach_per_post: "Reach per post",
  posts_per_week: "Posts per week",
  page_conversion: "Landing page conversion",
  cost_per_lead_usd: "Cost per lead (USD)",
};
const isRate = (m: string) => m === "engagement_rate" || m === "page_conversion";
const fmtBench = (m: string, v: number) =>
  isRate(m)
    ? formatPercent(v, 1)
    : m === "cost_per_lead_usd"
      ? `$${v.toFixed(2)}`
      : m === "posts_per_week"
        ? v.toFixed(1)
        : formatCompact(v);

function Benchmarks({ bid }: { bid: string }) {
  const { data, isLoading } = useApi<{
    sharing: boolean;
    min_businesses: number;
    business_model?: string;
    items: BenchmarkItem[];
  }>(gpath(bid, "/analytics/benchmarks"));
  const toggle = useAction((share: boolean) => api.put(gpath(bid, "/analytics/benchmarks"), { share }), {
    invalidate: [gpath(bid, "/analytics/benchmarks")],
    success: (r) =>
      (r as { sharing: boolean }).sharing
        ? "Thanks for sharing. Your figures join next week's benchmarks."
        : "Sharing off. Everything you shared was deleted.",
  });
  if (isLoading || !data) return <Loading />;
  return (
    <div className="space-y-6">
      <Card className="flex flex-wrap items-center justify-between gap-4 p-5">
        <div className="max-w-xl">
          <div className="text-[15px] font-semibold">Compare with similar businesses</div>
          <p className="mt-1 text-[13px] text-muted">
            Share anonymous weekly rates (engagement, reach per post, conversion) and see how you compare. No names,
            content or contacts are shared, and a figure only appears when at least {data.min_businesses} businesses
            contribute. Turning it off deletes what you shared.
          </p>
        </div>
        <Switch label="Share anonymous benchmarks" checked={data.sharing} onCheckedChange={(v) => toggle.mutate(v)} />
      </Card>
      {data.sharing ? (
        data.items.length ? (
          <Card>
            <CardHeader
              title={`Businesses like yours (${sentence(data.business_model ?? "")})`}
              subtitle="The bar is the middle half of businesses; the line is the median; the dot is you (last 4 weeks)."
            />
            <ul className="divide-y divide-[var(--hairline)] pb-2">
              {data.items.map((b) => {
                const hi = Math.max(b.p75, b.you ?? 0, b.median) * 1.25 || 1;
                const pos = (v: number) => `${Math.min((v / hi) * 100, 100)}%`;
                return (
                  <li
                    key={`${b.metric}${b.platform}`}
                    className="grid grid-cols-1 gap-2 px-5 py-3 sm:grid-cols-[220px_1fr_120px] sm:items-center"
                  >
                    <div className="flex items-center gap-2 text-[13px]">
                      {b.platform ? <PlatformMark platform={b.platform} size={18} /> : null}
                      <span className="font-medium">{BENCH_LABEL[b.metric] ?? sentence(b.metric)}</span>
                    </div>
                    <div
                      className="relative h-6"
                      role="img"
                      aria-label={`Middle half ${fmtBench(b.metric, b.p25)} to ${fmtBench(b.metric, b.p75)}, median ${fmtBench(b.metric, b.median)}${b.you != null ? `, you ${fmtBench(b.metric, b.you)}` : ""}`}
                    >
                      <div className="absolute inset-x-0 top-2.5 h-1 rounded-full bg-fill-2" />
                      <div
                        className="absolute top-1.5 h-3 rounded-full bg-[var(--series-1)] opacity-25"
                        style={{ left: pos(b.p25), width: `calc(${pos(b.p75)} - ${pos(b.p25)})` }}
                      />
                      <div className="absolute top-0.5 h-5 w-0.5 bg-text-2" style={{ left: pos(b.median) }} />
                      {b.you != null ? (
                        <div
                          className="absolute top-1 size-4 -translate-x-1/2 rounded-full border-2 border-[var(--surface)] bg-[var(--series-1)] shadow"
                          style={{ left: pos(b.you) }}
                        />
                      ) : null}
                    </div>
                    <div className="text-right text-[12px] tabular">
                      <div className="font-medium text-text">
                        {b.you != null ? fmtBench(b.metric, b.you) : "—"}{" "}
                        <span className="font-normal text-muted">you</span>
                      </div>
                      <div className="text-muted">
                        {fmtBench(b.metric, b.median)} median · {b.businesses}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          </Card>
        ) : (
          <Card>
            <EmptyState
              title="Not enough businesses yet"
              body={`Benchmarks appear once ${data.min_businesses} similar businesses share each figure.`}
            />
          </Card>
        )
      ) : null}
    </div>
  );
}

interface ExperimentRow {
  id: string;
  name: string;
  hypothesis: string;
  status: string;
  winner: string | null;
  min_samples: number;
  result: { prob_best?: Record<string, number>; rates?: Record<string, number | null>; leader?: string | null };
  variants: { key: string; label: string; reach: number; engagements: number }[];
  started_at: string;
}

function Experiments({ bid }: { bid: string }) {
  const { data, isLoading } = useApi<{ items: ExperimentRow[]; min_reach: number; confidence: number }>(
    gpath(bid, "/experiments"),
  );
  if (isLoading || !data) return <Loading />;
  if (!data.items.length) {
    return (
      <Card>
        <EmptyState
          title="No tests yet"
          body="Open a post that's waiting for review and choose A/B test on any platform. Version B posts at the same hour two days later, and the winner shapes what's planned next."
        />
      </Card>
    );
  }
  return (
    <div className="space-y-4">
      {data.items.map((e) => (
        <Card key={e.id} className="p-5">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate text-[15px] font-semibold">{e.name}</div>
              {e.hypothesis ? <div className="text-[13px] text-muted">{e.hypothesis}</div> : null}
            </div>
            <Badge tone={e.winner ? "good" : e.status === "running" ? "accent" : "neutral"}>
              {e.winner ? `Version ${e.winner} won` : e.status === "running" ? "Collecting results" : "No clear winner"}
            </Badge>
          </div>
          <ul className="mt-4 space-y-3">
            {e.variants.map((v) => {
              const rate = e.result.rates?.[v.key];
              const chance = e.result.prob_best?.[v.key];
              return (
                <li key={v.key} className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_160px] sm:items-center">
                  <div className="min-w-0 text-[13px]">
                    <span className={cn("mr-2 font-semibold", e.winner === v.key && "text-good")}>{v.key}</span>
                    <span className="text-text-2">{v.label}</span>
                  </div>
                  <div className="text-right text-[12px] tabular">
                    <div className="font-medium">{rate != null ? formatPercent(rate, 1) : "—"} engagement</div>
                    <div className="text-muted">
                      {formatCompact(v.reach)} reached{chance != null ? ` · ${formatPercent(chance, 0)} best` : ""}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
          <p className="mt-3 text-[11px] text-faint">
            Indicative: versions post on different days. A winner needs {data.min_reach.toLocaleString()} people reached
            on each and a {formatPercent(data.confidence, 0)} chance of being better.
          </p>
        </Card>
      ))}
    </div>
  );
}

export function Analytics() {
  const bid = useBid();
  const currency = useCurrency(bid);
  const [days, setDays] = useState("30");
  const search = useSearch({ strict: false }) as { tab?: string };
  const navigate = useNavigate();
  const tab = search.tab ?? "overview";
  const setTab = (v: string) => navigate({ to: ".", search: { tab: v } as never, replace: true });
  const { data, isLoading, error, refetch } = useApi<Overview>(gpath(bid, `/analytics/overview?days=${days}`));
  const attribution = useApi<Attribution>(gpath(bid, "/analytics/attribution?days=90"));
  const d = data;
  const rev = d?.numbers.revenue;
  return (
    <div>
      <PageHeader
        title="Analytics"
        subtitle="What's growing, where it comes from, and what to do more of."
        actions={
          <Segmented
            value={days}
            onChange={setDays}
            options={[
              { value: "7", label: "7 days" },
              { value: "30", label: "30 days" },
              { value: "90", label: "90 days" },
            ]}
          />
        }
      />
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="working">What's working</TabsTrigger>
          <TabsTrigger value="reports">Weekly reviews</TabsTrigger>
          <TabsTrigger value="experiments">A/B tests</TabsTrigger>
          <TabsTrigger value="benchmarks">Benchmarks</TabsTrigger>
        </TabsList>
        <TabsContent value="overview">
          {isLoading ? (
            <Loading rows={4} />
          ) : error ? (
            <LoadError error={error} retry={refetch} />
          ) : (
            <div className="space-y-6">
              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                <StatTile
                  label="Revenue"
                  value={formatMoney(rev?.value_minor ?? 0, currency, { compact: true })}
                  hint="30 days"
                  delta={
                    rev?.previous_minor
                      ? {
                          value: formatPercent(Math.abs(rev.value_minor - rev.previous_minor) / rev.previous_minor, 0),
                          good: rev.value_minor >= rev.previous_minor,
                        }
                      : null
                  }
                />
                <StatTile
                  label="New contacts"
                  value={formatCompact(d!.series.reduce((s, r) => s + r.contacts, 0))}
                  hint={`${days} days`}
                />
                <StatTile
                  label="Link clicks"
                  value={formatCompact(d!.links.clicks)}
                  hint={`${formatNumber(d!.referrals)} referrals`}
                />
                <StatTile
                  label="AI cost"
                  value={formatMoney(Math.round(d!.compute.micro_usd / 10_000), "USD")}
                  hint={`${formatNumber(d!.compute.calls)} model calls`}
                />
              </div>
              <Trends data={d!} currency={currency} />
              <Card>
                <CardHeader
                  title="Channels"
                  subtitle="Leads and customers are credited to the channel of their first touch."
                />
                <div className="pb-2">
                  <ChannelTable rows={d!.channels} />
                </div>
              </Card>
              <Card>
                <CardHeader
                  title="Campaigns"
                  subtitle="Last 90 days. First touch credits who found them; last touch credits who converted them."
                />
                {attribution.data?.campaigns.length ? (
                  <div className="overflow-x-auto pb-2">
                    <table className="w-full min-w-[560px] text-[13px]">
                      <thead>
                        <tr className="text-left text-[12px] text-muted hairline-b">
                          <th className="px-5 py-2.5 font-medium">Campaign</th>
                          <th className="px-3 py-2.5 text-right font-medium">First-touch leads</th>
                          <th className="px-3 py-2.5 text-right font-medium">Last-touch leads</th>
                          <th className="px-5 py-2.5 text-right font-medium">Revenue</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-[var(--hairline)]">
                        {attribution.data.campaigns.map((c) => (
                          <tr key={c.campaign_id}>
                            <td className="px-5 py-2.5 font-medium">{c.name}</td>
                            <td className="px-3 py-2.5 text-right tabular">{formatNumber(c.first_touch_leads)}</td>
                            <td className="px-3 py-2.5 text-right tabular">{formatNumber(c.last_touch_leads)}</td>
                            <td className="px-5 py-2.5 text-right tabular font-medium">
                              {formatMoney(c.revenue_minor, currency)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="px-5 pb-5 text-[14px] text-muted">No attributed results yet.</p>
                )}
              </Card>
            </div>
          )}
        </TabsContent>
        <TabsContent value="working">
          <Working bid={bid} days={Number(days)} />
        </TabsContent>
        <TabsContent value="experiments">
          <Experiments bid={bid} />
        </TabsContent>
        <TabsContent value="benchmarks">
          <Benchmarks bid={bid} />
        </TabsContent>
        <TabsContent value="reports">
          <Reports bid={bid} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

interface ReportDetail {
  id: string;
  kind: string;
  period_start: string;
  period_end: string;
  narrative: string;
  data: Record<string, unknown> & {
    channels?: Overview["channels"];
    leads?: number;
    leads_prev?: number;
    customers?: number;
    customers_prev?: number;
    subscribers?: number;
    subscribers_prev?: number;
    revenue_minor?: number;
    revenue_prev_minor?: number;
  };
}

export function Report() {
  const bid = useBid();
  const currency = useCurrency(bid);
  const { id } = useParams({ strict: false }) as { id: string };
  const { data, isLoading, error } = useApi<ReportDetail>(gpath(bid, `/analytics/reports/${id}`));
  if (isLoading) return <Loading rows={3} />;
  if (error) return <LoadError error={error} />;
  const r = data!;
  const d = r.data;
  const change = (now?: number, prev?: number) =>
    prev ? { value: formatPercent(Math.abs((now ?? 0) - prev) / prev, 0), good: (now ?? 0) >= prev } : null;
  return (
    <div className="mx-auto max-w-3xl">
      <Link
        to={`/b/${bid}/analytics` as string}
        className="mb-4 inline-flex items-center gap-1 text-[14px] text-accent"
      >
        <ArrowLeft className="size-4" />
        Analytics
      </Link>
      <p className="text-[13px] text-muted">
        {sentence(r.kind)} review · {formatDate(r.period_start, { day: "numeric", month: "long" })} –{" "}
        {formatDate(r.period_end, { day: "numeric", month: "long" })}
      </p>
      <h1 className="mt-1 mb-6 text-[30px] font-bold tracking-[-0.03em]">This week</h1>
      <div className="mb-8 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatTile label="Leads" value={formatNumber(d.leads)} delta={change(d.leads, d.leads_prev)} />
        <StatTile
          label="Subscribers"
          value={formatNumber(d.subscribers)}
          delta={change(d.subscribers, d.subscribers_prev)}
        />
        <StatTile label="Customers" value={formatNumber(d.customers)} delta={change(d.customers, d.customers_prev)} />
        <StatTile
          label="Revenue"
          value={formatMoney(d.revenue_minor ?? 0, currency, { compact: true })}
          delta={change(d.revenue_minor, d.revenue_prev_minor)}
        />
      </div>
      <article className="space-y-4 text-[16px] leading-[1.7] text-text-2">
        {r.narrative.split(/\n{2,}/).map((para) => (
          <p key={para.slice(0, 40)}>{para}</p>
        ))}
      </article>
      {d.channels?.length ? (
        <Card className="mt-8">
          <CardHeader title="By channel" />
          <div className="pb-2">
            <ChannelTable rows={d.channels} />
          </div>
        </Card>
      ) : null}
    </div>
  );
}
