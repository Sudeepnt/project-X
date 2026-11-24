import { useNavigate } from "@tanstack/react-router";
import { FlaskConical, KeyRound, Megaphone, Pause, Play, Plus, RefreshCw, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Sparkline } from "@/components/charts";
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
  Select,
  StatusBadge,
} from "@/components/ui";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatMoney, formatNumber, sentence } from "@/lib/format";
import { gpath, useAction, useApi, useBid } from "@/lib/queries";
import { LoadError, Loading } from "./system";

interface AdAccount {
  id: string;
  platform: "meta" | "google";
  external_id: string;
  name: string;
  currency: string;
  status: string;
  monthly_cap_minor: number;
  platform_spend_limit_minor: number | null;
  spent_this_month_minor: number;
  cap_mirror_error: string | null;
}
interface AdCampaign {
  id: string;
  ad_account_id: string;
  platform: string;
  name: string;
  objective: string;
  audience_kind: string;
  daily_budget_minor: number;
  status: string;
  special_category: string | null;
  target_cpl_minor: number | null;
  paused_reason: string | null;
  totals: { spend_minor: number; leads: number; clicks: number; impressions: number; cpl_minor: number | null };
  series: { day: string; spend_minor: number; leads: number }[];
}
interface Overview {
  practice_mode: boolean;
  accounts: AdAccount[];
  campaigns: AdCampaign[];
}
interface Candidate {
  content_id: string;
  title: string;
  platform: string;
  score: number;
}

const toMinor = (v: string) => Math.round(Number(v || 0) * 100);

function CapMeter({ a }: { a: AdAccount }) {
  const pct = a.monthly_cap_minor ? Math.min(a.spent_this_month_minor / a.monthly_cap_minor, 1) : 0;
  return (
    <div>
      <div className="mb-1.5 flex justify-between text-[13px]">
        <span className="text-text-2">Spent this month</span>
        <span className="tabular font-medium">
          {formatMoney(a.spent_this_month_minor, a.currency)}{" "}
          <span className="text-muted">of {formatMoney(a.monthly_cap_minor, a.currency)}</span>
        </span>
      </div>
      <div
        className="h-2 overflow-hidden rounded-full bg-fill-2"
        role="progressbar"
        aria-valuenow={Math.round(pct * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Monthly cap used"
      >
        <div
          className={cn("h-full rounded-full", pct >= 1 ? "bg-critical" : pct > 0.8 ? "bg-warning" : "bg-accent-fill")}
          style={{ width: `${pct * 100}%` }}
        />
      </div>
    </div>
  );
}

function AccountCard({ bid, a }: { bid: string; a: AdAccount }) {
  const [editing, setEditing] = useState(false);
  const [cap, setCap] = useState(String(a.monthly_cap_minor / 100));
  const save = useAction(() => api.put(gpath(bid, `/ads/accounts/${a.id}/cap`), { monthly_cap_minor: toMinor(cap) }), {
    invalidate: [gpath(bid, "/ads")],
    success: "Cap saved",
    onSuccess: () => setEditing(false),
  });
  return (
    <Card className="p-5">
      <div className="mb-4 flex items-start gap-3">
        <PlatformMark platform={a.platform === "meta" ? "facebook" : "youtube"} size={32} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[15px] font-semibold">{a.name}</div>
          <div className="text-[12px] text-muted">
            {a.platform === "meta" ? "Meta" : "Google"} · {a.external_id}
          </div>
        </div>
        <StatusBadge status={a.status} />
      </div>
      <CapMeter a={a} />
      <div className="mt-3 flex items-center gap-2 text-[12px] text-muted">
        <ShieldCheck className="size-3.5 text-good" />
        {a.platform_spend_limit_minor != null
          ? "Cap mirrored to the platform's own spending limit"
          : "Enforced by daily checks (platform has no account limit)"}
      </div>
      {a.cap_mirror_error ? (
        <p className="mt-1 text-[12px] text-critical">Couldn't mirror the cap: {a.cap_mirror_error}</p>
      ) : null}
      {editing ? (
        <div className="mt-4 flex items-end gap-2">
          <Field label={`Monthly cap (${a.currency})`} className="flex-1">
            {(id) => <Input id={id} type="number" min={0} value={cap} onChange={(e) => setCap(e.target.value)} />}
          </Field>
          <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>
            Save
          </Button>
        </div>
      ) : (
        <Button size="sm" className="mt-4" onClick={() => setEditing(true)}>
          Change cap
        </Button>
      )}
    </Card>
  );
}

function ConnectDialog({
  bid,
  open,
  setOpen,
  practice,
}: {
  bid: string;
  open: boolean;
  setOpen: (o: boolean) => void;
  practice: boolean;
}) {
  const [f, setF] = useState({
    platform: "meta",
    cap: "",
    external_id: "",
    name: "",
    access_token: "",
    developer_token: "",
    refresh_token: "",
    page_id: "",
  });
  const connect = useAction(
    () =>
      practice
        ? api.post(gpath(bid, "/ads/accounts/practice"), { platform: f.platform, monthly_cap_minor: toMinor(f.cap) })
        : api.post(gpath(bid, "/ads/accounts"), {
            platform: f.platform,
            external_id: f.external_id,
            name: f.name || `${f.platform === "meta" ? "Meta" : "Google"} ads`,
            monthly_cap_minor: toMinor(f.cap),
            credentials:
              f.platform === "meta"
                ? { access_token: f.access_token }
                : {
                    access_token: f.access_token,
                    developer_token: f.developer_token,
                    ...(f.refresh_token ? { refresh_token: f.refresh_token } : {}),
                  },
            settings: f.platform === "meta" && f.page_id ? { page_id: f.page_id } : {},
          }),
    { invalidate: [gpath(bid, "/ads")], success: "Ad account connected", onSuccess: () => setOpen(false) },
  );
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setF({ ...f, [k]: e.target.value });
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        title={practice ? "Add a practice ad account" : "Connect an ad account"}
        description="The platform bills you directly. Set a monthly cap: nothing ever spends past it."
      >
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Platform">
              {(id) => (
                <Select id={id} value={f.platform} onChange={set("platform")}>
                  <option value="meta">Meta (Facebook, Instagram)</option>
                  <option value="google">Google Ads</option>
                </Select>
              )}
            </Field>
            <Field label="Monthly cap">
              {(id) => <Input id={id} type="number" min={0} required value={f.cap} onChange={set("cap")} />}
            </Field>
          </div>
          {!practice ? (
            <>
              <Field
                label={f.platform === "meta" ? "Ad account ID" : "Customer ID"}
                hint={f.platform === "meta" ? "act_… in Ads Manager" : "123-456-7890"}
              >
                {(id) => <Input id={id} value={f.external_id} onChange={set("external_id")} />}
              </Field>
              <Field label="Name">{(id) => <Input id={id} value={f.name} onChange={set("name")} />}</Field>
              <Field label="Access token">
                {(id) => (
                  <Input
                    id={id}
                    type="password"
                    autoComplete="off"
                    value={f.access_token}
                    onChange={set("access_token")}
                  />
                )}
              </Field>
              {f.platform === "meta" ? (
                <Field label="Facebook Page ID" hint="The Page ads run from.">
                  {(id) => <Input id={id} value={f.page_id} onChange={set("page_id")} />}
                </Field>
              ) : (
                <>
                  <Field label="Developer token">
                    {(id) => (
                      <Input
                        id={id}
                        type="password"
                        autoComplete="off"
                        value={f.developer_token}
                        onChange={set("developer_token")}
                      />
                    )}
                  </Field>
                  <Field label="Refresh token" hint="Lets us renew access without asking you again.">
                    {(id) => (
                      <Input
                        id={id}
                        type="password"
                        autoComplete="off"
                        value={f.refresh_token}
                        onChange={set("refresh_token")}
                      />
                    )}
                  </Field>
                </>
              )}
            </>
          ) : null}
          <div className="flex justify-end">
            <Button variant="primary" disabled={!f.cap} loading={connect.isPending} onClick={() => connect.mutate()}>
              Connect
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function NewCampaign({
  bid,
  accounts,
  open,
  setOpen,
}: {
  bid: string;
  accounts: AdAccount[];
  open: boolean;
  setOpen: (o: boolean) => void;
}) {
  const navigate = useNavigate();
  const { data: candidates } = useApi<{ items: Candidate[] }>(open ? gpath(bid, "/ads/candidates") : null);
  const [f, setF] = useState({
    ad_account_id: "",
    name: "",
    objective: "leads",
    daily: "",
    audience_kind: "retargeting",
    special_category: "",
    landing_url: "",
    target_cpl: "",
    external_campaign_id: "",
    content_ids: [] as string[],
  });
  const account = accounts.find((a) => a.id === (f.ad_account_id || accounts[0]?.id));
  const create = useAction(
    () =>
      api.post<{ status: string; decision_id: string | null; reasons: string[] }>(gpath(bid, "/ads/campaigns"), {
        ad_account_id: account?.id,
        name: f.name,
        objective: f.objective,
        daily_budget_minor: toMinor(f.daily),
        audience_kind: f.audience_kind,
        special_category: f.special_category || null,
        landing_url: f.landing_url || null,
        target_cpl_minor: f.target_cpl ? toMinor(f.target_cpl) : null,
        external_campaign_id: f.external_campaign_id || null,
        content_ids: f.content_ids,
      }),
    {
      invalidate: [gpath(bid, "/ads"), `/api/v1/b/${bid}/decisions`],
      success: (r) =>
        r.status === "blocked" ? `Blocked: ${r.reasons[0] ?? "over a limit"}` : "Sent for your approval",
      onSuccess: (r) => {
        setOpen(false);
        if (r.decision_id && r.status !== "blocked") navigate({ to: `/b/${bid}/decisions/${r.decision_id}` });
      },
    },
  );
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setF({ ...f, [k]: e.target.value });
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        title="New ad campaign"
        description="Back a post that already worked. You'll see the cost and the cap before anything spends."
        wide
      >
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Ad account">
              {(id) => (
                <Select id={id} value={account?.id ?? ""} onChange={set("ad_account_id")}>
                  {accounts.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label="Name">{(id) => <Input id={id} value={f.name} onChange={set("name")} />}</Field>
            <Field label={`Daily budget (${account?.currency ?? ""})`}>
              {(id) => <Input id={id} type="number" min={1} value={f.daily} onChange={set("daily")} />}
            </Field>
            <Field label="Goal">
              {(id) => (
                <Select id={id} value={f.objective} onChange={set("objective")}>
                  <option value="leads">Leads</option>
                  <option value="traffic">Traffic</option>
                  <option value="sales">Sales</option>
                </Select>
              )}
            </Field>
            <Field label="Audience">
              {(id) => (
                <Select id={id} value={f.audience_kind} onChange={set("audience_kind")}>
                  <option value="retargeting">People who know you</option>
                  <option value="lookalike">Lookalikes</option>
                  <option value="cold">New people</option>
                </Select>
              )}
            </Field>
            <Field label="Special category" hint="Required by law for these ad types.">
              {(id) => (
                <Select id={id} value={f.special_category} onChange={set("special_category")}>
                  <option value="">None</option>
                  <option value="housing">Housing</option>
                  <option value="employment">Employment</option>
                  <option value="credit">Credit or finance</option>
                  <option value="politics">Politics or social issues</option>
                </Select>
              )}
            </Field>
            <Field label="Landing page">
              {(id) => (
                <Input id={id} type="url" placeholder="https://" value={f.landing_url} onChange={set("landing_url")} />
              )}
            </Field>
            <Field label="Target cost per lead (optional)" hint="Pauses at 2x this for 3 days.">
              {(id) => <Input id={id} type="number" min={0} value={f.target_cpl} onChange={set("target_cpl")} />}
            </Field>
            {account?.platform === "google" ? (
              <Field label="Google campaign ID" hint="Create the campaign in Google Ads; we manage budget and results.">
                {(id) => <Input id={id} value={f.external_campaign_id} onChange={set("external_campaign_id")} />}
              </Field>
            ) : null}
          </div>
          {account?.platform === "meta" ? (
            <div>
              <div className="mb-2 text-[13px] font-medium text-text-2">Posts to back</div>
              {candidates?.items.length ? (
                <ul className="max-h-48 space-y-1 overflow-y-auto">
                  {candidates.items.map((c) => {
                    const on = f.content_ids.includes(c.content_id);
                    return (
                      <li key={c.content_id}>
                        <button
                          type="button"
                          aria-pressed={on}
                          onClick={() =>
                            setF({
                              ...f,
                              content_ids: on
                                ? f.content_ids.filter((x) => x !== c.content_id)
                                : [...f.content_ids, c.content_id].slice(0, 5),
                            })
                          }
                          className={cn(
                            "flex w-full items-center gap-3 rounded-[10px] px-3 py-2 text-left text-[13px] ring-1",
                            on ? "bg-accent-soft ring-accent" : "ring-hairline hover:bg-fill-2",
                          )}
                        >
                          <PlatformMark platform={c.platform} size={20} />
                          <span className="min-w-0 flex-1 truncate">{c.title}</span>
                          <span className="tabular text-muted">score {c.score.toFixed(2)}</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p className="text-[13px] text-muted">
                  No published posts with results yet. Ads work best on posts that already did well.
                </p>
              )}
            </div>
          ) : null}
          {f.special_category === "politics" ? (
            <p className="rounded-[10px] bg-[var(--critical-soft)] px-3 py-2 text-[13px] text-critical">
              Political and issue ads need platform authorisation and disclaimers. They're never automated here.
            </p>
          ) : null}
          <div className="flex justify-end">
            <Button
              variant="primary"
              disabled={!f.name || !f.daily || !account || f.special_category === "politics"}
              loading={create.isPending}
              onClick={() => create.mutate()}
            >
              Review and approve
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function CampaignRow({ bid, c, currency }: { bid: string; c: AdCampaign; currency: string }) {
  const navigate = useNavigate();
  const [budget, setBudget] = useState<string | null>(null);
  const inv = [gpath(bid, "/ads"), `/api/v1/b/${bid}/decisions`];
  const pause = useAction(() => api.post(gpath(bid, `/ads/campaigns/${c.id}/pause`)), {
    invalidate: inv,
    success: "Paused",
  });
  const resume = useAction(() => api.post(gpath(bid, `/ads/campaigns/${c.id}/resume`)), {
    invalidate: inv,
    success: "Running again",
  });
  const change = useAction(
    () =>
      api.post<{ status: string; decision_id?: string }>(gpath(bid, `/ads/campaigns/${c.id}/budget`), {
        daily_budget_minor: toMinor(budget ?? "0"),
      }),
    {
      invalidate: inv,
      success: (r) =>
        r.status === "applied"
          ? "Budget lowered"
          : r.status === "blocked"
            ? "Blocked by a cap"
            : "Increase sent for approval",
      onSuccess: (r) => {
        setBudget(null);
        if (r.decision_id && r.status === "waiting_for_approval")
          navigate({ to: `/b/${bid}/decisions/${r.decision_id}` });
      },
    },
  );
  return (
    <li className="grid grid-cols-1 gap-3 px-5 py-4 md:grid-cols-[1.6fr_1fr_1fr_1fr_120px_auto] md:items-center">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate text-[14px] font-medium">{c.name}</span>
          <StatusBadge status={c.status} />
        </div>
        <div className="text-[12px] text-muted">
          {sentence(c.objective)} · {sentence(c.audience_kind)}
          {c.special_category ? ` · ${sentence(c.special_category)}` : ""}
        </div>
        {c.paused_reason ? <div className="mt-0.5 text-[12px] text-warning">{c.paused_reason}</div> : null}
      </div>
      <div className="text-[13px]">
        <div className="text-muted">Daily</div>
        {budget == null ? (
          <button
            type="button"
            className="tabular font-medium hover:text-accent"
            onClick={() => setBudget(String(c.daily_budget_minor / 100))}
          >
            {formatMoney(c.daily_budget_minor, currency)}
          </button>
        ) : (
          <span className="flex gap-1">
            <Input
              aria-label="Daily budget"
              className="h-8 w-20 text-[13px]"
              type="number"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
            />
            <Button size="sm" variant="primary" loading={change.isPending} onClick={() => change.mutate()}>
              Set
            </Button>
          </span>
        )}
      </div>
      <div className="text-[13px]">
        <div className="text-muted">Spent</div>
        <div className="tabular font-medium">{formatMoney(c.totals.spend_minor, currency)}</div>
      </div>
      <div className="text-[13px]">
        <div className="text-muted">Leads · CPL</div>
        <div className="tabular font-medium">
          {formatNumber(c.totals.leads)} ·{" "}
          {c.totals.cpl_minor != null ? formatMoney(c.totals.cpl_minor, currency) : "—"}
        </div>
      </div>
      <div role="img" aria-label="Daily spend, last 30 days">
        {c.series.length > 1 ? (
          <Sparkline values={c.series.map((s) => s.spend_minor)} height={30} />
        ) : (
          <span className="text-[12px] text-faint">No data yet</span>
        )}
      </div>
      <div className="flex justify-end gap-1">
        {c.status === "active" ? (
          <Button
            size="sm"
            icon={<Pause className="size-3.5" />}
            onClick={() => pause.mutate()}
            loading={pause.isPending}
          >
            Pause
          </Button>
        ) : null}
        {c.status === "paused" ? (
          <Button
            size="sm"
            icon={<Play className="size-3.5" />}
            onClick={() => resume.mutate()}
            loading={resume.isPending}
          >
            Resume
          </Button>
        ) : null}
      </div>
    </li>
  );
}

export function Ads() {
  const bid = useBid();
  const { data, isLoading, error, refetch } = useApi<Overview>(gpath(bid, "/ads"), { refetchInterval: 60_000 });
  const [connect, setConnect] = useState<null | "practice" | "real">(null);
  const [creating, setCreating] = useState(false);
  const sync = useAction(() => api.post(gpath(bid, "/ads/sync")), {
    invalidate: [gpath(bid, "/ads")],
    success: "Fetching the latest numbers",
  });
  if (isLoading) return <Loading rows={3} />;
  if (error) return <LoadError error={error} retry={refetch} />;
  const d = data!;
  const currencyOf = (accountId: string) => d.accounts.find((a) => a.id === accountId)?.currency ?? "USD";
  return (
    <div>
      <PageHeader
        title="Ads"
        subtitle="Money only behind what already works, inside a cap you set. Starting ads and raising budgets always ask you; losers pause themselves."
        actions={
          <>
            {d.accounts.length ? (
              <Button icon={<RefreshCw className="size-4" />} onClick={() => sync.mutate()} loading={sync.isPending}>
                Sync
              </Button>
            ) : null}
            {d.accounts.length ? (
              <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>
                New campaign
              </Button>
            ) : null}
          </>
        }
      />
      {!d.accounts.length ? (
        <Card>
          <EmptyState
            icon={<Megaphone className="size-6" />}
            title="No ad account yet"
            body="Connect Meta or Google Ads. The platform bills you directly; we keep every campaign inside the monthly cap you choose."
            action={
              <div className="flex gap-2">
                {d.practice_mode ? (
                  <Button icon={<FlaskConical className="size-4" />} onClick={() => setConnect("practice")}>
                    Practice account
                  </Button>
                ) : null}
                <Button variant="primary" icon={<KeyRound className="size-4" />} onClick={() => setConnect("real")}>
                  Connect
                </Button>
              </div>
            }
          />
        </Card>
      ) : (
        <div className="space-y-6">
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {d.accounts.map((a) => (
              <AccountCard key={a.id} bid={bid} a={a} />
            ))}
            <button
              type="button"
              onClick={() => setConnect(d.practice_mode ? "practice" : "real")}
              className="flex min-h-40 items-center justify-center gap-2 rounded-[18px] border border-dashed border-[var(--hairline-strong)] text-[14px] text-muted hover:bg-fill-2"
            >
              <Plus className="size-4" /> Add account
            </button>
          </div>
          <Card>
            <CardHeader
              title="Campaigns"
              subtitle="Click a daily budget to change it. Lowering applies now; raising asks you first."
            />
            {d.campaigns.length ? (
              <ul className="divide-y divide-[var(--hairline)] pb-1">
                {d.campaigns.map((c) => (
                  <CampaignRow key={c.id} bid={bid} c={c} currency={currencyOf(c.ad_account_id)} />
                ))}
              </ul>
            ) : (
              <p className="px-5 pb-5 text-[14px] text-muted">
                No campaigns yet. Start with a small daily budget behind your best post.
              </p>
            )}
          </Card>
          <p className="flex items-center gap-2 text-[12px] text-muted">
            <Badge tone="good">
              <ShieldCheck className="size-3" />
              Stop rules
            </Badge>
            Pause at 2x the target cost per lead for 3 days, after 3 days of budget with no results, and the moment the
            monthly cap is reached.
          </p>
        </div>
      )}
      <ConnectDialog
        bid={bid}
        open={!!connect}
        setOpen={(o) => !o && setConnect(null)}
        practice={connect === "practice"}
      />
      <NewCampaign bid={bid} accounts={d.accounts} open={creating} setOpen={setCreating} />
    </div>
  );
}
