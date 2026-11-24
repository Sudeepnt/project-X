import { useNavigate, useSearch } from "@tanstack/react-router";
import {
  CheckCircle2,
  Copy,
  Download,
  ExternalLink,
  Lock,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  UserPlus,
} from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Dialog,
  DialogContent,
  Field,
  Input,
  PageHeader,
  Select,
  StatusBadge,
  Switch,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Textarea,
  Tip,
} from "@/components/ui";
import { ask } from "@/components/ui/confirm";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDateTime, formatMoney, formatNumber, sentence, timeAgo } from "@/lib/format";
import { bpath, gpath, useAction, useApi, useBid, useMe } from "@/lib/queries";
import type { AutonomyRow, Business, Level, Usage } from "@/lib/types";
import { LoadError, Loading } from "./system";

const LEVELS: { value: Level; label: string; body: string }[] = [
  { value: "never", label: "Never", body: "Not allowed at all" },
  { value: "draft", label: "Draft only", body: "Prepares it; you do it" },
  { value: "ask_first", label: "Ask first", body: "Waits for your yes" },
  { value: "run_and_tell", label: "Do and tell", body: "Does it, tells you, you can undo" },
  { value: "run", label: "Just do it", body: "Runs quietly" },
];
const RANK: Record<Level, number> = { never: 0, draft: 1, ask_first: 2, run_and_tell: 3, run: 4 };

interface Policy {
  currency: string;
  per_action_limit_minor: number;
  per_day_limit_minor: number;
  approval_mode: string;
  promotion_threshold: number;
  spent_today_minor: number;
}
interface Member {
  user_id: string;
  name: string;
  email: string;
  role: string;
}
interface LedgerRow {
  seq: number;
  type: string;
  occurred_at: string;
  actor_type: string;
  actor_id: string;
  subject_type: string | null;
  data: Record<string, unknown>;
  hash: string;
}
interface Compliance {
  opted_in_subscribers: number;
  ai_label: string;
  double_opt_in: boolean;
  postal_address_set: boolean;
  legal_pages: Record<string, string>;
  data_deletion_callback: string;
}
interface DataRequest {
  id: string;
  kind: string;
  status: string;
  source: string;
  confirmation_code: string | null;
  requested_at: string;
  completed_at: string | null;
  result: Record<string, unknown> | null;
}
interface MediaKit {
  business: string;
  channels: { platform: string; name: string; followers: number }[];
  newsletter_subscribers: number;
  monthly_reach: number;
  engagement_rate: number;
  public: boolean;
  public_url: string | null;
}

const copy = (t: string) => navigator.clipboard.writeText(t).then(() => toast.success("Copied"));

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2 py-3.5 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="text-[14px] font-medium">{label}</div>
        {hint ? <div className="text-[13px] text-muted">{hint}</div> : null}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function BusinessTab({ bid }: { bid: string }) {
  const { data: b } = useApi<Business>(bpath(bid, ""));
  const [f, setF] = useState<Partial<Business>>({});
  useEffect(() => {
    if (b)
      setF({
        name: b.name,
        website: b.website,
        industry: b.industry,
        description: b.description,
        timezone: b.timezone,
        currency: b.currency,
        country: b.country,
      });
  }, [b]);
  const inv = [bpath(bid, ""), "/api/v1/auth/me"];
  const save = useAction((patch: Partial<Business>) => api.patch(bpath(bid, ""), patch), {
    invalidate: inv,
    success: "Saved",
  });
  const [secret, setSecret] = useState({ provider: "razorpay", value: "" });
  const hook = useAction(
    () =>
      api.put<{ webhook_url: string }>(gpath(bid, `/payment-webhooks/${secret.provider}`), { secret: secret.value }),
    {
      success: "Saved. Add the webhook URL in your provider's dashboard.",
      onSuccess: (r) => {
        setSecret({ ...secret, value: "" });
        copy(r.webhook_url);
      },
    },
  );
  if (!b) return <Loading />;
  return (
    <div className="space-y-6">
      <Card className="p-5">
        <h2 className="mb-4 text-[16px] font-semibold">Profile</h2>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name">
            {(id) => <Input id={id} value={f.name ?? ""} onChange={(e) => setF({ ...f, name: e.target.value })} />}
          </Field>
          <Field label="Website">
            {(id) => (
              <Input
                id={id}
                type="url"
                value={f.website ?? ""}
                onChange={(e) => setF({ ...f, website: e.target.value || null })}
              />
            )}
          </Field>
          <Field label="Industry">
            {(id) => (
              <Input id={id} value={f.industry ?? ""} onChange={(e) => setF({ ...f, industry: e.target.value })} />
            )}
          </Field>
          <Field label="Time zone">
            {(id) => (
              <Select id={id} value={f.timezone ?? ""} onChange={(e) => setF({ ...f, timezone: e.target.value })}>
                {Intl.supportedValuesOf("timeZone").map((tz) => (
                  <option key={tz}>{tz}</option>
                ))}
              </Select>
            )}
          </Field>
          <Field label="Currency" hint="Changing it doesn't convert past numbers.">
            {(id) => (
              <Input
                id={id}
                maxLength={3}
                value={f.currency ?? ""}
                onChange={(e) => setF({ ...f, currency: e.target.value.toUpperCase() })}
              />
            )}
          </Field>
          <Field label="Country">
            {(id) => (
              <Input
                id={id}
                maxLength={2}
                value={f.country ?? ""}
                onChange={(e) => setF({ ...f, country: e.target.value.toUpperCase() })}
              />
            )}
          </Field>
          <Field label="Description" className="sm:col-span-2">
            {(id) => (
              <Textarea
                id={id}
                rows={3}
                value={f.description ?? ""}
                onChange={(e) => setF({ ...f, description: e.target.value })}
              />
            )}
          </Field>
        </div>
        <div className="mt-4 flex justify-end">
          <Button variant="primary" loading={save.isPending} onClick={() => save.mutate(f)}>
            Save
          </Button>
        </div>
      </Card>
      <Card className="px-5 py-2">
        <Row
          label="Practice mode"
          hint={
            b.practice_mode
              ? "Platforms are simulated. Nothing is posted or sent for real."
              : "Live: approved posts publish to your real accounts."
          }
        >
          <Switch
            label="Practice mode"
            checked={b.practice_mode}
            onCheckedChange={async (v) =>
              (v ||
                (await ask({
                  title: "Go live?",
                  body: "Approved posts will publish to your real accounts.",
                  confirmLabel: "Go live",
                }))) &&
              save.mutate({ practice_mode: v })
            }
          />
        </Row>
      </Card>
      <Card className="p-5">
        <h2 className="text-[16px] font-semibold">Payments</h2>
        <p className="mt-1 mb-4 text-[13px] text-muted">
          Connect your payment provider so sales land in the CRM and credit the campaign that earned them. Paste the
          webhook signing secret; we'll give you the URL to add on their side.
        </p>
        <div className="grid gap-3 sm:grid-cols-[160px_1fr_auto] sm:items-end">
          <Field label="Provider">
            {(id) => (
              <Select
                id={id}
                value={secret.provider}
                onChange={(e) => setSecret({ ...secret, provider: e.target.value })}
              >
                <option value="razorpay">Razorpay</option>
                <option value="stripe">Stripe</option>
                <option value="dodo">Dodo Payments</option>
              </Select>
            )}
          </Field>
          <Field label="Webhook signing secret">
            {(id) => (
              <Input
                id={id}
                type="password"
                autoComplete="off"
                value={secret.value}
                onChange={(e) => setSecret({ ...secret, value: e.target.value })}
              />
            )}
          </Field>
          <Button
            variant="primary"
            icon={<Lock className="size-4" />}
            disabled={secret.value.length < 8}
            loading={hook.isPending}
            onClick={() => hook.mutate()}
          >
            Save secret
          </Button>
        </div>
      </Card>
    </div>
  );
}

function AutonomyTab({ bid }: { bid: string }) {
  const { data: policy } = useApi<Policy>(bpath(bid, "/policy"));
  const { data: rows, isLoading } = useApi<AutonomyRow[]>(bpath(bid, "/autonomy"));
  const [limits, setLimits] = useState({ action: "", day: "", threshold: "" });
  useEffect(() => {
    if (policy)
      setLimits({
        action: String(policy.per_action_limit_minor / 100),
        day: String(policy.per_day_limit_minor / 100),
        threshold: String(policy.promotion_threshold),
      });
  }, [policy]);
  const savePolicy = useAction(
    () =>
      api.patch(bpath(bid, "/policy"), {
        per_action_limit_minor: Math.round(Number(limits.action) * 100),
        per_day_limit_minor: Math.round(Number(limits.day) * 100),
        promotion_threshold: Number(limits.threshold),
      }),
    { invalidate: [bpath(bid, "/policy")], success: "Limits saved" },
  );
  const setLevel = useAction(
    ({ action, level }: { action: string; level: Level }) => api.put(bpath(bid, `/autonomy/${action}`), { level }),
    { invalidate: [bpath(bid, "/autonomy")], success: "Updated" },
  );
  const groups: Record<string, AutonomyRow[]> = {};
  for (const r of rows ?? []) {
    groups[r.module] = [...(groups[r.module] ?? []), r];
  }
  return (
    <div className="space-y-6">
      <Card className="p-5">
        <h2 className="text-[16px] font-semibold">Money limits</h2>
        <p className="mt-1 mb-4 text-[13px] text-muted">
          Hard ceilings. Anything above them always asks you, whatever else is set.{" "}
          {policy ? `Spent today: ${formatMoney(policy.spent_today_minor, policy.currency)}.` : ""}
        </p>
        <div className="grid gap-3 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-end">
          <Field label={`Per action (${policy?.currency ?? ""})`}>
            {(id) => (
              <Input
                id={id}
                type="number"
                min={0}
                value={limits.action}
                onChange={(e) => setLimits({ ...limits, action: e.target.value })}
              />
            )}
          </Field>
          <Field label={`Per day (${policy?.currency ?? ""})`}>
            {(id) => (
              <Input
                id={id}
                type="number"
                min={0}
                value={limits.day}
                onChange={(e) => setLimits({ ...limits, day: e.target.value })}
              />
            )}
          </Field>
          <Field label="Clean runs before suggesting more freedom">
            {(id) => (
              <Input
                id={id}
                type="number"
                min={5}
                max={500}
                value={limits.threshold}
                onChange={(e) => setLimits({ ...limits, threshold: e.target.value })}
              />
            )}
          </Field>
          <Button variant="primary" loading={savePolicy.isPending} onClick={() => savePolicy.mutate()}>
            Save
          </Button>
        </div>
      </Card>
      {isLoading ? (
        <Loading />
      ) : (
        Object.entries(groups).map(([module, list]) => (
          <Card key={module}>
            <CardHeader
              title={module === "gtm" ? "Growth" : sentence(module)}
              subtitle="How much each action can do on its own. Freedom is earned: after a clean streak we suggest the next step, and any incident steps it back."
            />
            <ul className="divide-y divide-[var(--hairline)] pb-1">
              {list.map((r) => (
                <li key={r.action_type} className="flex flex-col gap-3 px-5 py-3.5 sm:flex-row sm:items-center">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2 text-[14px] font-medium">
                      {r.title}
                      {r.one_way_door ? (
                        <Tip content="Can't be undone. Never runs without asking.">
                          <span>
                            <Badge tone="critical">
                              <ShieldAlert className="size-3" />
                              One-way
                            </Badge>
                          </span>
                        </Tip>
                      ) : null}
                      {r.moves_money ? <Badge tone="warning">Moves money</Badge> : null}
                      {r.public ? <Badge>Public</Badge> : null}
                      {r.promotion_pending ? <Badge tone="accent">Promotion suggested</Badge> : null}
                    </div>
                    <div className="text-[13px] text-muted">{r.description}</div>
                    {r.threshold && RANK[r.level] < RANK[r.max_level] ? (
                      <div className="mt-1.5 flex items-center gap-2 text-[11px] text-faint">
                        <div className="h-1 w-28 overflow-hidden rounded-full bg-fill-2">
                          <div
                            className="h-full rounded-full bg-good"
                            style={{ width: `${Math.min(r.clean_streak / r.threshold, 1) * 100}%` }}
                          />
                        </div>
                        {r.clean_streak}/{r.threshold} clean runs
                      </div>
                    ) : null}
                  </div>
                  <Select
                    aria-label={`${r.title} level`}
                    className="w-full sm:w-48"
                    value={r.level}
                    onChange={(e) => setLevel.mutate({ action: r.action_type, level: e.target.value as Level })}
                  >
                    {LEVELS.filter((l) => RANK[l.value] <= RANK[r.max_level]).map((l) => (
                      <option key={l.value} value={l.value}>
                        {l.label}
                      </option>
                    ))}
                  </Select>
                </li>
              ))}
            </ul>
          </Card>
        ))
      )}
    </div>
  );
}

function TeamTab({ bid }: { bid: string }) {
  const { data: me } = useMe();
  const { data, isLoading } = useApi<Member[]>(bpath(bid, "/members"));
  const [invite, setInvite] = useState({ open: false, email: "", role: "editor" });
  const inv = [bpath(bid, "/members")];
  const send = useAction(() => api.post(bpath(bid, "/members/invite"), { email: invite.email, role: invite.role }), {
    invalidate: inv,
    success: "Invitation sent",
    onSuccess: () => setInvite({ open: false, email: "", role: "editor" }),
  });
  const role = useAction(
    ({ id, r }: { id: string; r: string }) => api.patch(bpath(bid, `/members/${id}`), { role: r }),
    { invalidate: inv, success: "Role updated" },
  );
  const remove = useAction((id: string) => api.delete(bpath(bid, `/members/${id}`)), {
    invalidate: inv,
    success: "Removed",
  });
  return (
    <Card>
      <CardHeader
        title="Team"
        subtitle="Owners and admins approve; editors prepare; viewers watch."
        action={
          <Button
            size="sm"
            variant="primary"
            icon={<UserPlus className="size-4" />}
            onClick={() => setInvite({ ...invite, open: true })}
          >
            Invite
          </Button>
        }
      />
      {isLoading ? (
        <div className="p-5">
          <Loading rows={2} />
        </div>
      ) : (
        <ul className="divide-y divide-[var(--hairline)] pb-2">
          {data?.map((m) => (
            <li key={m.user_id} className="flex items-center gap-3 px-5 py-3">
              <span className="flex size-8 items-center justify-center rounded-full bg-fill text-[13px] font-semibold">
                {m.name.slice(0, 1)}
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-[14px] font-medium">
                  {m.name}
                  {m.user_id === me?.user?.id ? <span className="text-muted"> (you)</span> : null}
                </div>
                <div className="text-[12px] text-muted">{m.email}</div>
              </div>
              {m.role === "owner" ? (
                <Badge tone="accent">Owner</Badge>
              ) : (
                <>
                  <Select
                    aria-label={`${m.name} role`}
                    className="h-8 w-28 text-[13px]"
                    value={m.role}
                    onChange={(e) => role.mutate({ id: m.user_id, r: e.target.value })}
                  >
                    {["admin", "editor", "viewer"].map((r) => (
                      <option key={r} value={r}>
                        {sentence(r)}
                      </option>
                    ))}
                  </Select>
                  <Button
                    size="icon"
                    variant="ghost"
                    aria-label={`Remove ${m.name}`}
                    onClick={async () =>
                      (await ask({
                        title: `Remove ${m.name}?`,
                        body: "They lose access to this business right away.",
                        confirmLabel: "Remove",
                        destructive: true,
                      })) && remove.mutate(m.user_id)
                    }
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      <Dialog open={invite.open} onOpenChange={(o) => setInvite({ ...invite, open: o })}>
        <DialogContent title="Invite someone">
          <div className="space-y-4">
            <Field label="Email">
              {(id) => (
                <Input
                  id={id}
                  type="email"
                  value={invite.email}
                  onChange={(e) => setInvite({ ...invite, email: e.target.value })}
                />
              )}
            </Field>
            <Field label="Role">
              {(id) => (
                <Select id={id} value={invite.role} onChange={(e) => setInvite({ ...invite, role: e.target.value })}>
                  <option value="admin">Admin: approves and changes settings</option>
                  <option value="editor">Editor: prepares and edits</option>
                  <option value="viewer">Viewer: read only</option>
                </Select>
              )}
            </Field>
            <div className="flex justify-end">
              <Button variant="primary" disabled={!invite.email} loading={send.isPending} onClick={() => send.mutate()}>
                Send invitation
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function Meter({ label, used, limit }: { label: string; used: number; limit: number | null }) {
  const pct = limit ? Math.min(used / limit, 1) : 0;
  return (
    <div>
      <div className="mb-1.5 flex justify-between text-[13px]">
        <span className="text-text-2">{label}</span>
        <span className="tabular text-muted">
          {formatNumber(used)}
          {limit ? ` of ${formatNumber(limit)}` : " · unlimited"}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-fill-2">
        <div
          className={cn(
            "h-full rounded-full",
            pct > 0.9 ? "bg-critical" : pct > 0.75 ? "bg-warning" : "bg-accent-fill",
          )}
          style={{ width: `${limit ? pct * 100 : 0}%` }}
        />
      </div>
    </div>
  );
}

interface Billing {
  plan: string;
  plan_name: string;
  status: string;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  configured: boolean;
  for_sale: string[];
  can_manage: boolean;
}

const PLAN_RANK: Record<string, number> = { free: 0, autopilot: 1, growth: 2, scale: 3 };

function PlanTab({ bid }: { bid: string }) {
  const search = useSearch({ strict: false }) as { checkout?: string };
  const { data, isLoading, error } = useApi<Usage>(bpath(bid, "/usage"));
  const { data: billing } = useApi<Billing>(bpath(bid, "/billing"), {
    refetchInterval: search.checkout === "done" ? 4000 : undefined,
  });
  const { data: plans } =
    useApi<
      {
        key: string;
        name: string;
        tagline: string;
        price_usd_month: number;
        ad_spend_share_bps: number;
        highlights: string[];
      }[]
    >("/api/v1/plans");
  const inv = [bpath(bid, "/billing"), bpath(bid, "/usage"), "/api/v1/auth/me"];
  const choose = useAction(
    (plan: string) => api.post<{ mode: string; url: string | null }>(bpath(bid, "/billing/checkout"), { plan }),
    {
      invalidate: inv,
      onSuccess: (r) => {
        if (r.url) window.location.assign(r.url);
        else toast.success("Changing your plan. It updates as soon as the payment provider confirms.");
      },
    },
  );
  const portal = useAction(() => api.post<{ url: string }>(bpath(bid, "/billing/portal")), {
    onSuccess: (r) => window.location.assign(r.url),
  });
  const cancel = useAction((undo: boolean) => api.post(bpath(bid, "/billing/cancel"), { undo }), {
    invalidate: inv,
    success: (r) =>
      (r as Billing).cancel_at_period_end ? "Your plan ends at the end of this period" : "Your plan continues",
  });
  useEffect(() => {
    if (search.checkout === "done") toast.success("Thanks! Your plan updates as soon as the payment is confirmed.");
  }, [search.checkout]);
  if (isLoading) return <Loading />;
  if (error) return <LoadError error={error} />;
  const u = data!;
  const planList = plans ?? [];
  const cap = u.compute.cap_micro_usd;
  return (
    <div className="space-y-6">
      <Card className="p-5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-[16px] font-semibold">{u.plan.name} plan</h2>
          <span className="text-[14px] text-muted tabular">${u.plan.price_usd_month}/month</span>
        </div>
        {billing?.status === "past_due" ? (
          <p className="mt-3 rounded-[10px] bg-[var(--critical-soft)] px-3 py-2 text-[13px] text-critical">
            Your last payment didn't go through. Update your card to keep this plan.
          </p>
        ) : null}
        {billing?.current_period_end ? (
          <p className="mt-2 text-[13px] text-muted">
            {billing.cancel_at_period_end ? "Ends" : "Renews"} on {formatDateTime(billing.current_period_end)}
          </p>
        ) : null}
        {billing?.can_manage ? (
          <div className="mt-4 flex flex-wrap gap-2">
            <Button size="sm" onClick={() => portal.mutate()} loading={portal.isPending}>
              Manage billing
            </Button>
            {billing.cancel_at_period_end ? (
              <Button size="sm" variant="ghost" onClick={() => cancel.mutate(true)} loading={cancel.isPending}>
                Keep my plan
              </Button>
            ) : (
              <Button
                size="sm"
                variant="ghost"
                onClick={async () =>
                  (await ask({
                    title: "Cancel your plan?",
                    body: "You keep everything until the end of this billing period, then move to Free.",
                    confirmLabel: "Cancel plan",
                    destructive: true,
                  })) && cancel.mutate(false)
                }
              >
                Cancel plan
              </Button>
            )}
          </div>
        ) : billing && !billing.configured ? (
          <p className="mt-3 text-[13px] text-muted">Paid plans open soon on this server.</p>
        ) : null}
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          {Object.entries(u.metrics).map(([k, m]) => (
            <Meter key={k} label={sentence(k)} used={m.used} limit={m.limit} />
          ))}
        </div>
      </Card>
      {planList.length ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {planList.map((p) => (
            <div
              key={p.key}
              className={cn(
                "rounded-[16px] p-4 ring-1",
                p.key === u.plan.key ? "bg-accent-soft ring-accent" : "bg-surface ring-hairline",
              )}
            >
              <div className="text-[14px] font-semibold">{p.name}</div>
              <div className="text-[12px] text-muted">{p.tagline}</div>
              <div className="mt-2 text-[22px] font-semibold tabular">
                ${p.price_usd_month}
                <span className="text-[13px] font-normal text-muted">/mo</span>
              </div>
              {p.ad_spend_share_bps ? (
                <div className="text-[12px] text-muted">+ {p.ad_spend_share_bps / 100}% of ad spend</div>
              ) : null}
              <ul className="mt-2 space-y-0.5 text-[12px] text-text-2">
                {p.highlights.map((h) => (
                  <li key={h}>· {h}</li>
                ))}
              </ul>
              {p.key === u.plan.key ? (
                <div className="mt-3 text-[12px] font-medium text-accent">Current plan</div>
              ) : p.key === "free" ? null : billing?.for_sale.includes(p.key) ? (
                <Button
                  size="sm"
                  variant={(PLAN_RANK[p.key] ?? 0) > (PLAN_RANK[u.plan.key] ?? 0) ? "primary" : "secondary"}
                  className="mt-3 w-full justify-center"
                  loading={choose.isPending && choose.variables === p.key}
                  onClick={() => choose.mutate(p.key)}
                >
                  {(PLAN_RANK[p.key] ?? 0) > (PLAN_RANK[u.plan.key] ?? 0) ? "Upgrade" : "Switch"}
                </Button>
              ) : (
                <div className="mt-3 text-[12px] text-muted">Not for sale yet</div>
              )}
            </div>
          ))}
        </div>
      ) : null}
      <Card className="p-5">
        <h2 className="text-[16px] font-semibold">AI usage today</h2>
        <div className="mt-4">
          <Meter
            label="Spend (US cents)"
            used={Math.round(u.compute.spent_today_micro_usd / 10_000)}
            limit={cap ? Math.round(cap / 10_000) : null}
          />
        </div>
        <div className="mt-5 grid gap-6 md:grid-cols-2">
          <div>
            <div className="mb-2 text-[13px] font-medium">Costliest tasks</div>
            <ul className="space-y-1 text-[13px]">
              {u.compute.costliest_tasks.slice(0, 6).map((t) => (
                <li key={t.task} className="flex justify-between">
                  <span className="truncate text-text-2">{t.task}</span>
                  <span className="tabular text-muted">
                    {t.calls} calls · ${(t.cost_micro_usd / 1e6).toFixed(3)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <div className="mb-2 text-[13px] font-medium">Models</div>
            <ul className="space-y-1 text-[13px]">
              {Object.entries(u.models).map(([rung, m]) => (
                <li key={rung} className="flex justify-between">
                  <span className="capitalize text-text-2">
                    {rung} <span className="text-faint">· {u.compute.steps_by_rung[rung] ?? 0} steps</span>
                  </span>
                  <span className="flex items-center gap-1.5 font-mono text-[12px] text-muted">
                    {m.model}
                    {m.available ? (
                      <CheckCircle2 className="size-3.5 text-good" />
                    ) : (
                      <ShieldAlert className="size-3.5 text-critical" />
                    )}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </Card>
    </div>
  );
}

function ComplianceTab({ bid }: { bid: string }) {
  const { data: c } = useApi<Compliance>(gpath(bid, "/compliance/overview"));
  const { data: requests } = useApi<DataRequest[]>(gpath(bid, "/compliance/requests"));
  const { data: kit } = useApi<MediaKit>(gpath(bid, "/sales/media-kit"));
  const { data: business } = useApi<Business>(bpath(bid, ""));
  const dash = (business?.settings.gtm as { public_dashboard?: { enabled: boolean; metrics: string[] } } | undefined)
    ?.public_dashboard;
  const label = useAction((v: string) => api.put(gpath(bid, "/compliance/ai-label"), { ai_label: v }), {
    invalidate: [gpath(bid, "/compliance")],
    success: "Saved",
  });
  const kitPublic = useAction((v: boolean) => api.put(gpath(bid, "/sales/media-kit"), { public: v }), {
    invalidate: [gpath(bid, "/sales")],
    success: "Saved",
  });
  const dashPublic = useAction(
    (v: boolean) =>
      api.put(gpath(bid, "/analytics/public-dashboard"), {
        enabled: v,
        metrics: dash?.metrics ?? ["subscribers", "posts"],
      }),
    { invalidate: [bpath(bid, "")], success: "Saved" },
  );
  const [req, setReq] = useState({ kind: "export", email: "" });
  const submit = useAction(() => api.post(gpath(bid, "/compliance/requests"), { kind: req.kind, email: req.email }), {
    invalidate: [gpath(bid, "/compliance")],
    success: "Request recorded",
    onSuccess: () => setReq({ ...req, email: "" }),
  });
  if (!c) return <Loading />;
  return (
    <div className="space-y-6">
      <Card className="px-5 py-2">
        <Row
          label="Opted-in email subscribers"
          hint={c.double_opt_in ? "Double opt-in is on." : "Double opt-in is off."}
        >
          <span className="text-[15px] font-semibold tabular">{formatNumber(c.opted_in_subscribers)}</span>
        </Row>
        <Row label="Postal address in emails" hint="Required by CAN-SPAM and similar laws.">
          {c.postal_address_set ? (
            <Badge tone="good">
              <ShieldCheck className="size-3" />
              Set
            </Badge>
          ) : (
            <Badge tone="critical">Missing</Badge>
          )}
        </Row>
        <Row
          label="Label AI-made content"
          hint="Platform: use each platform's own AI label. Text: add a short note. Off: only where the law requires it."
        >
          <Select
            className="w-40"
            aria-label="AI label"
            value={c.ai_label}
            onChange={(e) => label.mutate(e.target.value)}
          >
            <option value="platform">Platform label</option>
            <option value="text">Text note</option>
            <option value="off">Off</option>
          </Select>
        </Row>
      </Card>
      <Card className="p-5">
        <h2 className="mb-3 text-[16px] font-semibold">Legal pages</h2>
        <ul className="space-y-2 text-[13px]">
          {[
            ...Object.entries(c.legal_pages),
            ["data-deletion callback", c.data_deletion_callback] as [string, string],
          ].map(([k, url]) => (
            <li key={k} className="flex items-center gap-2">
              <span className="w-44 shrink-0 capitalize text-text-2">{k.replace(/-/g, " ")}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-muted">{url}</span>
              <Button
                size="sm"
                variant="ghost"
                aria-label="Copy"
                icon={<Copy className="size-3.5" />}
                onClick={() => copy(url)}
              />
              {!k.includes("callback") ? (
                <a
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                  aria-label="Open"
                  className="text-faint hover:text-accent"
                >
                  <ExternalLink className="size-4" />
                </a>
              ) : (
                <span className="w-4" />
              )}
            </li>
          ))}
        </ul>
        <p className="mt-3 text-[12px] text-muted">
          Use these URLs in platform app reviews. Generated from your business details; have a lawyer review them before
          you rely on them.
        </p>
      </Card>
      <Card>
        <CardHeader
          title="Privacy requests"
          subtitle="Exports and erasures, including ones that arrive from platforms."
        />
        <div className="grid gap-3 px-5 pb-4 sm:grid-cols-[140px_1fr_auto] sm:items-end">
          <Field label="Kind">
            {(id) => (
              <Select id={id} value={req.kind} onChange={(e) => setReq({ ...req, kind: e.target.value })}>
                <option value="export">Export</option>
                <option value="erase">Erase</option>
              </Select>
            )}
          </Field>
          <Field label="Person's email">
            {(id) => (
              <Input
                id={id}
                type="email"
                value={req.email}
                onChange={(e) => setReq({ ...req, email: e.target.value })}
              />
            )}
          </Field>
          <Button
            variant="primary"
            disabled={!req.email}
            loading={submit.isPending}
            onClick={async () =>
              (req.kind !== "erase" ||
                (await ask({
                  title: "Erase this person?",
                  body: "All their personal data is removed everywhere. This can't be undone.",
                  confirmLabel: "Erase",
                  destructive: true,
                }))) &&
              submit.mutate()
            }
          >
            Submit
          </Button>
        </div>
        {requests?.length ? (
          <ul className="divide-y divide-[var(--hairline)] pb-2">
            {requests.map((r) => (
              <li key={r.id} className="flex items-center gap-3 px-5 py-2.5 text-[13px]">
                <span className="w-16 capitalize font-medium">{r.kind}</span>
                <span className="flex-1 text-muted">
                  {sentence(r.source)} · {formatDateTime(r.requested_at)}
                  {r.confirmation_code ? ` · code ${r.confirmation_code}` : ""}
                </span>
                <StatusBadge status={r.status} />
                {typeof r.result?.download_url === "string" ? (
                  <a href={r.result.download_url} className="text-accent" aria-label="Download export">
                    <Download className="size-4" />
                  </a>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
      </Card>
      <Card className="px-5 py-2">
        <Row
          label="Public media kit"
          hint={
            kit
              ? `${formatNumber(kit.monthly_reach)} monthly reach · ${formatNumber(kit.newsletter_subscribers)} subscribers. Live numbers for sponsors.`
              : undefined
          }
        >
          <div className="flex items-center gap-3">
            {kit?.public_url ? (
              <a href={kit.public_url} target="_blank" rel="noreferrer" className="text-[13px] text-accent">
                Open
              </a>
            ) : null}
            <Switch label="Public media kit" checked={!!kit?.public} onCheckedChange={(v) => kitPublic.mutate(v)} />
          </div>
        </Row>
        <Row
          label="Public growth dashboard"
          hint="Share your numbers openly (build in public). Only the metrics you pick."
        >
          <Switch label="Public dashboard" checked={!!dash?.enabled} onCheckedChange={(v) => dashPublic.mutate(v)} />
        </Row>
      </Card>
    </div>
  );
}

function ActivityTab({ bid }: { bid: string }) {
  const [before, setBefore] = useState<number | null>(null);
  const [pages, setPages] = useState<LedgerRow[][]>([]);
  const { data, isFetching } = useApi<{ items: LedgerRow[]; next_before_seq: number | null }>(
    bpath(bid, `/ledger?limit=50${before ? `&before_seq=${before}` : ""}`),
  );
  const verify = useAction(
    () =>
      api.get<{ ok: boolean; checked: number; problem: string | null; first_broken_seq: number | null }>(
        bpath(bid, "/ledger/verify"),
      ),
    {
      success: (r) =>
        r.ok
          ? `All ${formatNumber(r.checked)} entries verified. Nothing has been altered.`
          : `Problem at entry ${r.first_broken_seq}: ${r.problem}`,
    },
  );
  const rows = [...pages.flat(), ...(data?.items ?? [])];
  return (
    <Card>
      <CardHeader
        title="Activity log"
        subtitle="Every action by people and agents, in order. Append-only and hash-chained, so it can't be quietly changed."
        action={
          <Button
            size="sm"
            icon={<ShieldCheck className="size-4" />}
            loading={verify.isPending}
            onClick={() => verify.mutate()}
          >
            Verify
          </Button>
        }
      />
      <ol className="divide-y divide-[var(--hairline)]">
        {rows.map((r) => (
          <li key={r.seq} className="flex items-start gap-3 px-5 py-2.5 text-[13px]">
            <span className="w-10 shrink-0 pt-px text-right font-mono text-[11px] text-faint">#{r.seq}</span>
            <div className="min-w-0 flex-1">
              <div className="font-medium">{sentence(r.type.split(".").slice(1).join(" "))}</div>
              <div className="text-[12px] text-muted">
                {r.actor_type === "user"
                  ? "You or a teammate"
                  : r.actor_type === "agent"
                    ? `Agent ${r.actor_id.replace("pack:", "")}`
                    : sentence(r.actor_type)}
                {r.subject_type ? ` · ${sentence(r.subject_type)}` : ""}
              </div>
            </div>
            <Tip content={formatDateTime(r.occurred_at)}>
              <span className="shrink-0 text-[12px] text-faint">{timeAgo(r.occurred_at)}</span>
            </Tip>
          </li>
        ))}
      </ol>
      {data?.next_before_seq ? (
        <div className="flex justify-center p-3">
          <Button
            size="sm"
            loading={isFetching}
            onClick={() => {
              setPages([...pages, data.items]);
              setBefore(data.next_before_seq);
            }}
          >
            Older
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

export function Settings() {
  const bid = useBid();
  const search = useSearch({ strict: false }) as { tab?: string };
  const navigate = useNavigate();
  const tab = search.tab ?? "business";
  return (
    <div>
      <PageHeader title="Settings" />
      <Tabs value={tab} onValueChange={(v) => navigate({ to: ".", search: { tab: v } as never, replace: true })}>
        <TabsList>
          {[
            ["business", "Business"],
            ["autonomy", "Autonomy & limits"],
            ["team", "Team"],
            ["plan", "Plan & usage"],
            ["compliance", "Privacy & compliance"],
            ["activity", "Activity log"],
          ].map(([k, l]) => (
            <TabsTrigger key={k} value={k as string}>
              {l}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="business">
          <BusinessTab bid={bid} />
        </TabsContent>
        <TabsContent value="autonomy">
          <AutonomyTab bid={bid} />
        </TabsContent>
        <TabsContent value="team">
          <TeamTab bid={bid} />
        </TabsContent>
        <TabsContent value="plan">
          <PlanTab bid={bid} />
        </TabsContent>
        <TabsContent value="compliance">
          <ComplianceTab bid={bid} />
        </TabsContent>
        <TabsContent value="activity">
          <ActivityTab bid={bid} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
