import { useNavigate, useSearch } from "@tanstack/react-router";
import { Bot, Megaphone, Pause, Play, Square, Star, User, Users } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  Card,
  Dialog,
  DialogContent,
  EmptyState,
  Field,
  Input,
  PageHeader,
  PlatformMark,
  platformLabel,
  Segmented,
  Select,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui";
import { ask } from "@/components/ui/confirm";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDate, formatMoney } from "@/lib/format";
import { gpath, useAction, useApi, useBid, useMe } from "@/lib/queries";
import type { Campaign, Goal, Playbook, Ranked } from "@/lib/types";
import { LoadError, Loading } from "./system";

const RUNS: Record<string, { label: string; icon: React.ReactNode }> = {
  ai: { label: "Runs on its own", icon: <Bot className="size-3.5" /> },
  ai_human: { label: "AI + you", icon: <Users className="size-3.5" /> },
  human: { label: "You lead, AI helps", icon: <User className="size-3.5" /> },
};

function PlaybookCard({
  p,
  score,
  reasons,
  blocked,
  recommended,
  onStart,
}: {
  p: Playbook;
  score?: number;
  reasons?: string[];
  blocked?: string | null;
  recommended?: string;
  onStart: () => void;
}) {
  return (
    <div
      className={cn(
        "flex flex-col rounded-[18px] bg-surface p-5 shadow-[var(--shadow-sm)] ring-1 ring-hairline",
        blocked && "opacity-60",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          {recommended ? (
            <Badge tone="accent" className="mb-2">
              <Star className="size-3" />
              {recommended}
            </Badge>
          ) : null}
          <h2 className="text-[16px] font-semibold tracking-[-0.01em]">{p.name}</h2>
          <div className="mt-0.5 text-[12px] text-muted capitalize">
            {p.category} · {p.tier} plan
          </div>
        </div>
        <Badge>
          {RUNS[p.runs]?.icon}
          {RUNS[p.runs]?.label}
        </Badge>
      </div>
      <p className="mt-3 line-clamp-3 flex-1 text-[13px] leading-relaxed text-text-2">{p.choose_when}</p>
      <div className="mt-3 flex flex-wrap gap-1">
        {p.channels.slice(0, 7).map((c) => (
          <PlatformMark key={c} platform={c} size={20} />
        ))}
      </div>
      {reasons?.length ? <div className="mt-3 text-[12px] text-good">✓ {reasons.join(" · ")}</div> : null}
      {blocked ? <div className="mt-3 text-[12px] text-warning">{blocked}</div> : null}
      <div className="mt-4 flex items-center justify-between">
        <span className="text-[12px] text-muted">
          {p.value_ratio}:1 value to ask{p.paid ? " · paid" : ""}
          {score != null ? ` · fit ${score.toFixed(1)}` : ""}
        </span>
        <Button size="sm" variant={recommended ? "primary" : "secondary"} onClick={onStart} disabled={!!blocked}>
          Start
        </Button>
      </div>
    </div>
  );
}

function StartDialog({
  bid,
  playbook,
  onClose,
  goals,
}: {
  bid: string;
  playbook: Playbook | null;
  onClose: () => void;
  goals: Goal[];
}) {
  const { data: me } = useMe();
  const currency = me?.businesses.find((b) => b.id === bid)?.currency ?? "USD";
  const active = goals.find((g) => g.status === "active");
  const [goalId, setGoalId] = useState<string>(active?.id ?? "");
  const [role, setRole] = useState<"main" | "supporting">("supporting");
  const [channels, setChannels] = useState<string[] | null>(null);
  const [budget, setBudget] = useState("0");
  const chosen = channels ?? playbook?.channels ?? [];
  const start = useAction(
    () =>
      api.post(gpath(bid, "/campaigns"), {
        playbook_key: playbook?.key,
        goal_id: goalId || null,
        role,
        channels: chosen,
        budget_minor: Math.round(Number(budget) * 100),
        start: true,
      }),
    {
      invalidate: [gpath(bid, "/campaigns"), gpath(bid, "/goals"), gpath(bid, "/content")],
      success: "Campaign started. The first posts are being planned.",
      onSuccess: onClose,
    },
  );
  return (
    <Dialog
      open={!!playbook}
      onOpenChange={(o) => {
        if (!o) {
          onClose();
          setChannels(null);
        }
      }}
    >
      {playbook ? (
        <DialogContent
          title={`Start “${playbook.name}”`}
          description={playbook.approval_note ?? "Posts will be planned and sent to you for approval."}
          wide
        >
          <div className="space-y-5">
            <div>
              <div className="mb-2 text-[13px] font-medium text-text-2">What it does</div>
              <ol className="list-decimal space-y-1 pl-5 text-[13px] text-text-2">
                {playbook.steps.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ol>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="For goal">
                {(id) => (
                  <Select id={id} value={goalId} onChange={(e) => setGoalId(e.target.value)}>
                    <option value="">No goal</option>
                    {goals
                      .filter((g) => ["active", "paused", "draft"].includes(g.status))
                      .map((g) => (
                        <option key={g.id} value={g.id}>
                          {g.title}
                        </option>
                      ))}
                  </Select>
                )}
              </Field>
              <Field label="Role">
                {() => (
                  <Segmented
                    value={role}
                    onChange={setRole}
                    options={[
                      { value: "main", label: "Main" },
                      { value: "supporting", label: "Supporting" },
                    ]}
                  />
                )}
              </Field>
            </div>
            <div>
              <div className="mb-2 text-[13px] font-medium text-text-2">Channels</div>
              <div className="flex flex-wrap gap-2">
                {playbook.channels.map((c) => {
                  const on = chosen.includes(c);
                  return (
                    <button
                      key={c}
                      type="button"
                      aria-pressed={on}
                      onClick={() => setChannels(on ? chosen.filter((x) => x !== c) : [...chosen, c])}
                      className={cn(
                        "flex items-center gap-2 rounded-full py-1 pr-3 pl-1 text-[13px] ring-1",
                        on ? "bg-accent-soft ring-accent" : "ring-hairline text-muted",
                      )}
                    >
                      <PlatformMark platform={c} size={20} />
                      {platformLabel(c)}
                    </button>
                  );
                })}
              </div>
            </div>
            {playbook.paid ? (
              <Field
                label={`Ad budget (${currency})`}
                hint="Capped by your money limits. Every increase asks you first."
              >
                {(id) => (
                  <Input id={id} type="number" min={0} value={budget} onChange={(e) => setBudget(e.target.value)} />
                )}
              </Field>
            ) : null}
            <div className="flex justify-end gap-2">
              <Button onClick={onClose}>Cancel</Button>
              <Button
                variant="primary"
                loading={start.isPending}
                disabled={!chosen.length}
                onClick={() => start.mutate()}
              >
                Start campaign
              </Button>
            </div>
          </div>
        </DialogContent>
      ) : null}
    </Dialog>
  );
}

function ActiveCampaigns({ bid, items }: { bid: string; items: Campaign[] }) {
  const { data: me } = useMe();
  const currency = me?.businesses.find((b) => b.id === bid)?.currency ?? "USD";
  const inv = [gpath(bid, "/campaigns"), gpath(bid, "/goals")];
  const setStatus = useAction(
    ({ id, status }: { id: string; status: string }) =>
      api.patch(gpath(bid, `/campaigns/${id}`), { status, reason: `Set to ${status} by founder` }),
    { invalidate: inv, success: (r) => `Campaign ${(r as Campaign).status}` },
  );
  const plan = useAction((id: string) => api.post(gpath(bid, "/content/plan"), { campaign_id: id, days: 7 }), {
    invalidate: [gpath(bid, "/content")],
    success: "Planning the next 7 days",
  });
  if (!items.length)
    return (
      <Card>
        <EmptyState
          icon={<Megaphone className="size-6" />}
          title="No campaigns yet"
          body="Pick a playbook from the library. We recommend the best fit for your goal."
        />
      </Card>
    );
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {items.map((c) => (
        <Card key={c.id} className="flex flex-col p-5">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="truncate text-[16px] font-semibold">{c.name}</h2>
              <div className="text-[12px] text-muted capitalize">
                {c.role} · {c.category} · {formatDate(c.starts_on)} – {formatDate(c.ends_on)}
              </div>
            </div>
            <StatusBadge status={c.status} />
          </div>
          <p className="mt-3 line-clamp-2 text-[13px] text-text-2">{c.brief}</p>
          <div className="mt-4 grid grid-cols-2 gap-2 text-[12px]">
            {Object.entries(c.frequency).map(([p, n]) => (
              <div key={p} className="flex items-center gap-2 rounded-[10px] bg-fill-2 px-2.5 py-1.5">
                <PlatformMark platform={p} size={18} />
                <span>{platformLabel(p)}</span>
                <span className="ml-auto tabular text-muted">{n}/wk</span>
              </div>
            ))}
          </div>
          <div className="mt-4 flex items-center gap-2">
            <span className="mr-auto text-[12px] text-muted">
              {c.value_ratio}:1 value to ask{c.budget_minor ? ` · ${formatMoney(c.budget_minor, currency)}` : ""}
            </span>
            {c.status === "active" ? (
              <>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => plan.mutate(c.id)}
                  loading={plan.isPending && plan.variables === c.id}
                >
                  Plan week
                </Button>
                <Button
                  size="sm"
                  icon={<Pause className="size-3.5" />}
                  onClick={() => setStatus.mutate({ id: c.id, status: "paused" })}
                >
                  Pause
                </Button>
              </>
            ) : c.status === "paused" || c.status === "draft" ? (
              <Button
                size="sm"
                variant="primary"
                icon={<Play className="size-3.5" />}
                onClick={() => setStatus.mutate({ id: c.id, status: "active" })}
              >
                {c.status === "draft" ? "Start" : "Resume"}
              </Button>
            ) : null}
            {["active", "paused"].includes(c.status) ? (
              <Button
                size="sm"
                variant="ghost"
                aria-label="Stop"
                icon={<Square className="size-3.5" />}
                onClick={async () =>
                  (await ask({
                    title: `Stop “${c.name}”?`,
                    body: "Scheduled posts from it won't go out.",
                    confirmLabel: "Stop",
                    destructive: true,
                  })) && setStatus.mutate({ id: c.id, status: "stopped" })
                }
              />
            ) : null}
          </div>
        </Card>
      ))}
    </div>
  );
}

export function Campaigns() {
  const bid = useBid();
  const search = useSearch({ strict: false }) as { tab?: string };
  const navigate = useNavigate();
  const tab = search.tab ?? "active";
  const campaigns = useApi<{ items: Campaign[] }>(gpath(bid, "/campaigns"));
  const goals = useApi<{ items: Goal[]; active: Goal | null }>(gpath(bid, "/goals"));
  const recs = useApi<{ main: Ranked | null; supporting: Ranked[]; others: Ranked[] }>(
    gpath(bid, "/campaigns/recommendations"),
  );
  const [category, setCategory] = useState("all");
  const [starting, setStarting] = useState<Playbook | null>(null);
  const runningKeys = new Set(
    campaigns.data?.items.filter((c) => ["active", "paused"].includes(c.status)).map((c) => c.playbook_key),
  );
  const all = useMemo(() => {
    if (!recs.data) return [];
    return [
      ...(recs.data.main ? [{ ...recs.data.main, tag: "Best fit" }] : []),
      ...recs.data.supporting.map((r) => ({ ...r, tag: "Pairs well" })),
      ...recs.data.others.map((r) => ({ ...r, tag: "" })),
    ];
  }, [recs.data]);
  const categories = ["all", ...new Set(all.map((r) => r.playbook.category))];
  const shown = all.filter((r) => category === "all" || r.playbook.category === category);

  return (
    <div>
      <PageHeader
        title="Campaigns"
        subtitle="Proven playbooks, run for you. Each one plans content, publishes, follows up and reports."
      />
      <Tabs value={tab} onValueChange={(v) => navigate({ to: ".", search: { tab: v } as never })}>
        <TabsList>
          <TabsTrigger value="active">Running</TabsTrigger>
          <TabsTrigger value="library">Library</TabsTrigger>
        </TabsList>
        <TabsContent value="active">
          {campaigns.isLoading ? (
            <Loading />
          ) : campaigns.error ? (
            <LoadError error={campaigns.error} />
          ) : (
            <ActiveCampaigns bid={bid} items={campaigns.data!.items} />
          )}
        </TabsContent>
        <TabsContent value="library">
          <div className="mb-5 flex flex-wrap gap-1.5">
            {categories.map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => setCategory(c)}
                className={cn(
                  "rounded-full px-3 py-1 text-[13px] font-medium capitalize",
                  category === c ? "bg-text text-[var(--bg)]" : "bg-fill-2 text-text-2 hover:bg-fill",
                )}
              >
                {c}
              </button>
            ))}
          </div>
          {recs.isLoading ? (
            <Loading rows={2} />
          ) : (
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {shown.map((r) => (
                <PlaybookCard
                  key={r.playbook.key}
                  p={r.playbook}
                  score={r.score}
                  reasons={r.reasons}
                  recommended={r.tag || undefined}
                  blocked={runningKeys.has(r.playbook.key) ? "Already running" : r.blocked}
                  onStart={() => setStarting(r.playbook)}
                />
              ))}
            </div>
          )}
        </TabsContent>
      </Tabs>
      <StartDialog bid={bid} playbook={starting} onClose={() => setStarting(null)} goals={goals.data?.items ?? []} />
    </div>
  );
}
