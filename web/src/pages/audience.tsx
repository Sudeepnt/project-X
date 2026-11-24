import { useNavigate, useSearch } from "@tanstack/react-router";
import { Download, Plus, Search, Upload, Users } from "lucide-react";
import { useEffect, useState } from "react";
import { FunnelChart } from "@/components/charts";
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
  Sheet,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui";
import { ask } from "@/components/ui/confirm";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDateTime, formatMoney, formatNumber, sentence, timeAgo } from "@/lib/format";
import { gpath, useAction, useApi, useBid, useMe } from "@/lib/queries";
import type { Contact, Funnel } from "@/lib/types";
import { LoadError, Loading } from "./system";

const STAGES = [
  "audience",
  "subscriber",
  "lead",
  "mql",
  "sql",
  "opportunity",
  "customer",
  "active",
  "expansion",
  "advocate",
];
const STATUSES = ["new", "contacted", "engaged", "no_response", "meeting_scheduled", "proposal_sent", "won", "lost"];
const SIDE_STATES = ["disqualified", "recycled", "at_risk", "churned", "do_not_contact"];
const stageLabel = (s: string) => (s === "mql" ? "MQL" : s === "sql" ? "SQL" : sentence(s));

interface Deal {
  id: string;
  pipeline_id: string;
  contact_id: string | null;
  contact_name?: string | null;
  title: string;
  stage_key: string;
  status: string;
  value_minor: number;
  currency: string;
  deal_type: string;
  expected_close: string | null;
  next_action: string | null;
  stage_entered_at: string;
}
interface Pipeline {
  id: string;
  key: string;
  name: string;
  stages: { key: string; kind: string; label: string; probability: number }[];
  is_default: boolean;
}
interface Payment {
  id: string;
  contact_id: string | null;
  provider: string;
  amount_minor: number;
  currency: string;
  kind: string;
  status: string;
  paid_at: string;
  promo_code: string | null;
  description: string | null;
}
interface TimelineEntry {
  type: "stage" | "touch" | "message";
  at: string;
  [k: string]: unknown;
}
interface ContactDetail extends Contact {
  timeline: TimelineEntry[];
  deals: Deal[];
  payments: Payment[];
  enrollments: { sequence_name?: string; status: string; step?: number }[];
}

function useCurrency(bid: string) {
  const { data } = useMe();
  return data?.businesses.find((b) => b.id === bid)?.currency ?? "USD";
}

function describe(t: TimelineEntry): string {
  if (t.type === "stage") {
    const parts = [];
    if (t.from_stage !== t.to_stage)
      parts.push(`${stageLabel(String(t.from_stage ?? "new"))} → ${stageLabel(String(t.to_stage))}`);
    if (t.from_status !== t.to_status) parts.push(`status ${sentence(String(t.to_status))}`);
    if (t.side_state) parts.push(sentence(String(t.side_state)));
    return `${parts.join(", ") || "Updated"}${t.cause_type ? ` · via ${sentence(String(t.cause_type))}` : ""}${t.reason ? ` · ${t.reason}` : ""}`;
  }
  if (t.type === "touch")
    return `${sentence(String(t.kind))}${t.channel ? ` on ${t.channel}` : ""}${t.points ? ` (+${t.points})` : ""}`;
  return `${sentence(String(t.direction ?? "message"))}: ${String(t.subject ?? t.kind ?? "")}`;
}

function ContactSheet({ bid, id, onClose }: { bid: string; id: string | null; onClose: () => void }) {
  const currency = useCurrency(bid);
  const { data: c } = useApi<ContactDetail>(id ? gpath(bid, `/contacts/${id}`) : null);
  const [note, setNote] = useState("");
  // biome-ignore lint/correctness/useExhaustiveDependencies: deliberately resets local state when the selection changes
  useEffect(() => setNote(""), [id]);
  const inv = [gpath(bid, "/contacts"), gpath(bid, "/funnel")];
  const update = useAction(
    (patch: Record<string, unknown>) =>
      api.patch(gpath(bid, `/contacts/${id}`), { ...patch, reason: note || "Changed by founder" }),
    { invalidate: inv, success: "Updated" },
  );
  const exportData = useAction(() => api.post(gpath(bid, "/compliance/requests"), { kind: "export", contact_id: id }), {
    success: "Export started. You'll get a download link.",
  });
  const erase = useAction(() => api.post(gpath(bid, "/compliance/requests"), { kind: "erase", contact_id: id }), {
    invalidate: inv,
    success: "Erased",
    onSuccess: onClose,
  });
  return (
    <Sheet
      open={!!id}
      onOpenChange={(o) => !o && onClose()}
      title={c ? (c.name ?? c.email ?? Object.values(c.handles)[0] ?? "Contact") : "Loading…"}
      subtitle={
        c ? (
          <span className="flex flex-wrap items-center gap-2">
            <Badge tone="accent">{stageLabel(c.stage)}</Badge>
            <StatusBadge status={c.status} />
            {c.side_state ? <Badge tone="warning">{sentence(c.side_state)}</Badge> : null}
            <span>Score {c.lead_score}</span>
          </span>
        ) : null
      }
      footer={
        c ? (
          <>
            <Button
              variant="ghost"
              size="sm"
              icon={<Download className="size-4" />}
              onClick={() => exportData.mutate()}
            >
              Export data
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="text-critical"
              onClick={async () =>
                (await ask({
                  title: "Erase this person?",
                  body: "Their personal data is removed everywhere. This can't be undone.",
                  confirmLabel: "Erase",
                  destructive: true,
                })) && erase.mutate()
              }
            >
              Erase
            </Button>
          </>
        ) : null
      }
    >
      {c ? (
        <div className="space-y-6">
          <dl className="grid grid-cols-2 gap-3 text-[13px]">
            {[
              ["Email", c.email],
              ["Company", c.company],
              ["Source", c.source && sentence(c.source)],
              ["First touch", c.first_touch?.platform],
              ["Lifetime value", formatMoney(c.lifetime_value_minor, currency)],
              ["Last active", timeAgo(c.last_activity_at)],
              ...Object.entries(c.handles).map(([p, h]) => [sentence(p), `@${h}`]),
            ]
              .filter(([, v]) => v)
              .map(([k, v]) => (
                <div key={k as string}>
                  <dt className="text-muted">{k}</dt>
                  <dd className="mt-0.5 truncate font-medium">{v}</dd>
                </div>
              ))}
          </dl>
          {c.unsubscribed ? (
            <p className="rounded-[10px] bg-fill-2 px-3 py-2 text-[13px] text-muted">Unsubscribed from email.</p>
          ) : null}
          <div className="space-y-3 rounded-[14px] bg-fill-2 p-4">
            <div className="text-[13px] font-semibold">Move or mark</div>
            <div className="grid grid-cols-3 gap-2">
              <Select aria-label="Stage" value={c.stage} onChange={(e) => update.mutate({ stage: e.target.value })}>
                {STAGES.map((s) => (
                  <option key={s} value={s}>
                    {stageLabel(s)}
                  </option>
                ))}
              </Select>
              <Select aria-label="Status" value={c.status} onChange={(e) => update.mutate({ status: e.target.value })}>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {sentence(s)}
                  </option>
                ))}
              </Select>
              <Select
                aria-label="Side state"
                value={c.side_state ?? ""}
                onChange={(e) => update.mutate({ side_state: e.target.value || "none" })}
              >
                <option value="">No flag</option>
                {SIDE_STATES.map((s) => (
                  <option key={s} value={s}>
                    {sentence(s)}
                  </option>
                ))}
              </Select>
            </div>
            <Input placeholder="Why? (logged with the change)" value={note} onChange={(e) => setNote(e.target.value)} />
          </div>
          {c.deals.length ? (
            <div>
              <h3 className="mb-2 text-[13px] font-semibold">Deals</h3>
              <ul className="space-y-1.5">
                {c.deals.map((d) => (
                  <li
                    key={d.id}
                    className="flex items-center justify-between rounded-[10px] bg-fill-2 px-3 py-2 text-[13px]"
                  >
                    <span>{d.title}</span>
                    <span className="flex items-center gap-2 tabular">
                      {formatMoney(d.value_minor, d.currency)}
                      <StatusBadge status={d.status} />
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {c.payments.length ? (
            <div>
              <h3 className="mb-2 text-[13px] font-semibold">Payments</h3>
              <ul className="space-y-1">
                {c.payments.map((p) => (
                  <li key={p.id} className="flex justify-between text-[13px]">
                    <span className="text-muted">
                      {formatDateTime(p.paid_at)} · {sentence(p.kind)}
                    </span>
                    <span className="tabular font-medium">{formatMoney(p.amount_minor, p.currency)}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          <div>
            <h3 className="mb-3 text-[13px] font-semibold">Timeline</h3>
            <ol className="relative space-y-3 border-l border-hairline pl-4">
              {c.timeline.map((t, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: timeline is append-only
                <li key={i} className="relative text-[13px]">
                  <span
                    className={cn(
                      "absolute top-1.5 -left-[21px] size-2.5 rounded-full ring-2 ring-[var(--elevated)]",
                      t.type === "stage"
                        ? "bg-accent-fill"
                        : t.type === "message"
                          ? "bg-[var(--series-3)]"
                          : "bg-faint",
                    )}
                  />
                  <div>{describe(t)}</div>
                  <div className="text-[11px] text-faint">{formatDateTime(t.at)}</div>
                </li>
              ))}
            </ol>
          </div>
        </div>
      ) : null}
    </Sheet>
  );
}

function AddContact({ bid, open, setOpen }: { bid: string; open: boolean; setOpen: (o: boolean) => void }) {
  const [f, setF] = useState({ name: "", email: "", company: "", stage: "lead", consent: false });
  const add = useAction(
    () =>
      api.post(gpath(bid, "/contacts"), {
        name: f.name || null,
        email: f.email || null,
        company: f.company || null,
        stage: f.stage,
        consent:
          f.consent && f.email
            ? { channel: "email", purpose: "marketing", source: "manual", evidence: { note: "Confirmed by founder" } }
            : null,
      }),
    {
      invalidate: [gpath(bid, "/contacts"), gpath(bid, "/funnel")],
      success: "Contact added",
      onSuccess: () => {
        setOpen(false);
        setF({ name: "", email: "", company: "", stage: "lead", consent: false });
      },
    },
  );
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent title="Add a contact">
        <div className="space-y-4">
          <Field label="Name">
            {(id) => <Input id={id} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />}
          </Field>
          <Field label="Email">
            {(id) => (
              <Input id={id} type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} />
            )}
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Company">
              {(id) => <Input id={id} value={f.company} onChange={(e) => setF({ ...f, company: e.target.value })} />}
            </Field>
            <Field label="Stage">
              {(id) => (
                <Select id={id} value={f.stage} onChange={(e) => setF({ ...f, stage: e.target.value })}>
                  {STAGES.map((s) => (
                    <option key={s} value={s}>
                      {stageLabel(s)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>
          <label className="flex items-start gap-2 text-[13px]">
            <input
              type="checkbox"
              className="mt-0.5 accent-[var(--accent)]"
              checked={f.consent}
              onChange={(e) => setF({ ...f, consent: e.target.checked })}
            />
            <span>
              They agreed to receive marketing email.{" "}
              <span className="text-muted">Only tick this if they actually did. It's recorded.</span>
            </span>
          </label>
          <div className="flex justify-end">
            <Button
              variant="primary"
              loading={add.isPending}
              disabled={!f.name && !f.email}
              onClick={() => add.mutate()}
            >
              Add
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ImportContacts({ bid }: { bid: string }) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [stage, setStage] = useState("subscriber");
  const [consent, setConsent] = useState(false);
  const run = useAction(
    () => {
      const form = new FormData();
      form.append("file", file as File);
      form.append("stage", stage);
      form.append("consent_confirmed", String(consent));
      return api.post<{ created: number; updated: number; skipped: number }>(gpath(bid, "/contacts/import"), form);
    },
    {
      invalidate: [gpath(bid, "/contacts"), gpath(bid, "/funnel")],
      success: (r) => `${r.created} added, ${r.updated} updated, ${r.skipped} skipped`,
      onSuccess: () => {
        setOpen(false);
        setFile(null);
        setConsent(false);
      },
    },
  );
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button icon={<Upload className="size-4" />} onClick={() => setOpen(true)}>
        Import
      </Button>
      <DialogContent
        title="Import contacts"
        description="A CSV with a header row. We read email, name, phone, company and tags (separated by ;). Rows without an email are skipped."
      >
        <div className="space-y-4">
          <label className="flex cursor-pointer flex-col items-center justify-center gap-1 rounded-[14px] border border-dashed border-[var(--hairline-strong)] px-4 py-8 text-center hover:bg-fill-2">
            <Upload className="size-5 text-muted" />
            <span className="text-[14px] font-medium">{file ? file.name : "Choose a CSV file"}</span>
            <span className="text-[12px] text-muted">Up to 5 MB, 20,000 rows</span>
            <input
              type="file"
              accept=".csv,text/csv"
              className="sr-only"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </label>
          <Field label="Put them in stage">
            {(id) => (
              <Select id={id} value={stage} onChange={(e) => setStage(e.target.value)}>
                {STAGES.map((s) => (
                  <option key={s} value={s}>
                    {stageLabel(s)}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <label className="flex items-start gap-2 text-[13px]">
            <input
              type="checkbox"
              className="mt-0.5 accent-[var(--accent)]"
              checked={consent}
              onChange={(e) => setConsent(e.target.checked)}
            />
            <span>
              Everyone in this file agreed to receive marketing email.{" "}
              <span className="text-muted">
                Recorded as consent evidence. Leave unticked if unsure; they won't be emailed.
              </span>
            </span>
          </label>
          <div className="flex justify-end">
            <Button variant="primary" disabled={!file} loading={run.isPending} onClick={() => run.mutate()}>
              Import
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function People({ bid, onOpen }: { bid: string; onOpen: (id: string) => void }) {
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [stage, setStage] = useState("");
  const [sort, setSort] = useState("recent");
  const [cursor, setCursor] = useState<string | null>(null);
  const [pages, setPages] = useState<Contact[][]>([]);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q), 250);
    return () => clearTimeout(t);
  }, [q]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: deliberately resets local state when the selection changes
  useEffect(() => {
    setCursor(null);
    setPages([]);
  }, [debounced, stage, sort]);
  const params = new URLSearchParams({
    limit: "50",
    sort,
    ...(debounced ? { q: debounced } : {}),
    ...(stage ? { stage } : {}),
    ...(cursor ? { cursor } : {}),
  });
  const { data, isLoading, error, isFetching } = useApi<{
    items: Contact[];
    next_cursor: string | null;
    total: number;
  }>(gpath(bid, `/contacts?${params}`));
  const all = cursor ? [...pages.flat(), ...(data?.items ?? [])] : (data?.items ?? []);
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="relative min-w-60 flex-1">
          <Search className="absolute top-3 left-3 size-4 text-faint" />
          <Input
            className="pl-9"
            placeholder="Search name, email, company, handle"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <Select className="w-auto" aria-label="Stage" value={stage} onChange={(e) => setStage(e.target.value)}>
          <option value="">All stages</option>
          {STAGES.map((s) => (
            <option key={s} value={s}>
              {stageLabel(s)}
            </option>
          ))}
        </Select>
        <Select className="w-auto" aria-label="Sort" value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="recent">Newest</option>
          <option value="score">Lead score</option>
          <option value="activity">Last active</option>
        </Select>
      </div>
      {isLoading && !all.length ? (
        <Loading />
      ) : error ? (
        <LoadError error={error} />
      ) : !all.length ? (
        <Card>
          <EmptyState
            icon={<Users className="size-6" />}
            title="No one yet"
            body="Signups, buyers, commenters and imports all land here, with where they came from."
          />
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-[13px]">
              <thead>
                <tr className="text-left text-[12px] text-muted hairline-b">
                  <th className="px-5 py-2.5 font-medium">Person</th>
                  <th className="px-3 py-2.5 font-medium">Stage</th>
                  <th className="px-3 py-2.5 font-medium">Status</th>
                  <th className="px-3 py-2.5 font-medium">From</th>
                  <th className="px-3 py-2.5 text-right font-medium">Score</th>
                  <th className="px-5 py-2.5 text-right font-medium">Last active</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--hairline)]">
                {all.map((c) => (
                  <tr key={c.id} onClick={() => onOpen(c.id)} className="cursor-pointer hover:bg-fill-2">
                    <td className="px-5 py-2.5">
                      <div className="font-medium">
                        {c.name ?? c.email ?? `@${Object.values(c.handles)[0] ?? "unknown"}`}
                      </div>
                      {c.name && c.email ? <div className="text-[12px] text-muted">{c.email}</div> : null}
                    </td>
                    <td className="px-3 py-2.5">
                      <Badge tone="accent">{stageLabel(c.stage)}</Badge>
                      {c.side_state ? (
                        <Badge tone="warning" className="ml-1">
                          {sentence(c.side_state)}
                        </Badge>
                      ) : null}
                    </td>
                    <td className="px-3 py-2.5 text-text-2">{sentence(c.status)}</td>
                    <td className="px-3 py-2.5">
                      {c.first_touch?.platform ? (
                        <span className="flex items-center gap-1.5">
                          <PlatformMark platform={c.first_touch.platform} size={18} />
                          <span>{platformLabel(c.first_touch.platform)}</span>
                        </span>
                      ) : (
                        <span className="text-muted">{c.source ? sentence(c.source) : "—"}</span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular">{c.lead_score}</td>
                    <td className="px-5 py-2.5 text-right text-muted">{timeAgo(c.last_activity_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between px-5 py-3 text-[13px] text-muted shadow-[inset_0_1px_0_var(--hairline)]">
            <span>{formatNumber(data?.total ?? all.length)} people</span>
            {data?.next_cursor ? (
              <Button
                size="sm"
                loading={isFetching}
                onClick={() => {
                  setPages([...pages, data.items]);
                  setCursor(data.next_cursor);
                }}
              >
                Load more
              </Button>
            ) : null}
          </div>
        </Card>
      )}
    </div>
  );
}

function FunnelTab({ bid }: { bid: string }) {
  const { data, isLoading, error } = useApi<Funnel>(gpath(bid, "/funnel"));
  if (isLoading) return <Loading />;
  if (error) return <LoadError error={error} />;
  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_300px]">
      <Card>
        <CardHeader
          title="Funnel"
          subtitle="How many people reached each stage, and how many move on. Bars show reached; the right column shows who's there now."
        />
        <div className="px-5 pb-5">
          <FunnelChart stages={data!.stages} />
        </div>
      </Card>
      <Card className="h-fit">
        <CardHeader title="Flags" subtitle="People outside the main path." />
        <ul className="px-5 pb-5 text-[14px]">
          {SIDE_STATES.map((s) => (
            <li key={s} className="flex justify-between py-1.5">
              <span className="text-text-2">{sentence(s)}</span>
              <span className="tabular font-medium">{formatNumber(data!.side_states[s] ?? 0)}</span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

const lostReason = () =>
  ask({
    title: "Why was it lost?",
    body: "Logged with the deal, and used to spot patterns.",
    confirmLabel: "Mark lost",
    input: { label: "Reason", placeholder: "Budget, timing, went with someone else…", required: true },
  });

function Deals({ bid }: { bid: string }) {
  const currency = useCurrency(bid);
  const { data: pipes } = useApi<{ items: Pipeline[]; templates: string[] }>(gpath(bid, "/pipelines"));
  const [pid, setPid] = useState<string | null>(null);
  const pipeline = pipes?.items.find((p) => p.id === pid) ?? pipes?.items.find((p) => p.is_default) ?? pipes?.items[0];
  const { data, isLoading } = useApi<{ items: Deal[] }>(
    pipeline ? gpath(bid, `/deals?pipeline_id=${pipeline.id}`) : null,
  );
  const inv = [gpath(bid, "/deals"), gpath(bid, "/contacts"), gpath(bid, "/funnel")];
  const move = useAction(
    ({ id, stage_key, lost_reason }: { id: string; stage_key: string; lost_reason?: string }) =>
      api.patch(gpath(bid, `/deals/${id}`), { stage_key, lost_reason: lost_reason ?? null }),
    { invalidate: inv },
  );
  const addPipeline = useAction((key: string) => api.post(gpath(bid, "/pipelines"), { key }), {
    invalidate: [gpath(bid, "/pipelines")],
    success: "Pipeline added",
  });
  const [adding, setAdding] = useState(false);
  const [nd, setNd] = useState({ title: "", value: "", contact: "" });
  const create = useAction(
    () =>
      api.post(gpath(bid, "/deals"), {
        pipeline_id: pipeline?.id,
        title: nd.title,
        value_minor: Math.round(Number(nd.value || 0) * 100),
        currency,
      }),
    {
      invalidate: inv,
      success: "Deal added",
      onSuccess: () => {
        setAdding(false);
        setNd({ title: "", value: "", contact: "" });
      },
    },
  );
  const [dragging, setDragging] = useState<string | null>(null);

  if (!pipes) return <Loading />;
  if (!pipeline)
    return (
      <Card>
        <EmptyState
          title="No pipeline yet"
          body="Pick one to start tracking deals."
          action={
            <div className="flex gap-2">
              {pipes.templates.map((t) => (
                <Button key={t} onClick={() => addPipeline.mutate(t)}>
                  {sentence(t)}
                </Button>
              ))}
            </div>
          }
        />
      </Card>
    );
  const deals = data?.items ?? [];
  const missing = pipes.templates.filter((t) => !pipes.items.some((p) => p.key === t));
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {pipes.items.length > 1 ? (
          <Select className="w-auto" aria-label="Pipeline" value={pipeline.id} onChange={(e) => setPid(e.target.value)}>
            {pipes.items.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        ) : (
          <h2 className="text-[15px] font-semibold">{pipeline.name}</h2>
        )}
        <span className="text-[13px] text-muted tabular">
          {formatMoney(
            deals.filter((d) => d.status === "open").reduce((s, d) => s + d.value_minor, 0),
            currency,
          )}{" "}
          open
        </span>
        <div className="ml-auto flex gap-2">
          {missing.map((t) => (
            <Button key={t} size="sm" variant="ghost" onClick={() => addPipeline.mutate(t)}>
              + {sentence(t)} pipeline
            </Button>
          ))}
          <Button size="sm" variant="primary" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>
            Deal
          </Button>
        </div>
      </div>
      {isLoading ? (
        <Loading />
      ) : (
        <div className="-mx-4 overflow-x-auto px-4 pb-2">
          <div className="flex min-w-max gap-3">
            {pipeline.stages.map((s) => {
              const list = deals.filter((d) => d.stage_key === s.key);
              return (
                <section
                  key={s.key}
                  aria-label={s.label}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={async () => {
                    const id = dragging;
                    setDragging(null);
                    if (!id) return;
                    const lost = s.kind === "lost" ? await lostReason() : undefined;
                    if (lost === null) return;
                    move.mutate({ id, stage_key: s.key, lost_reason: lost });
                  }}
                  className={cn(
                    "w-[260px] shrink-0 rounded-[14px] p-2",
                    s.kind === "won"
                      ? "bg-[var(--good-soft)]"
                      : s.kind === "lost"
                        ? "bg-[var(--critical-soft)]"
                        : "bg-fill-2",
                  )}
                >
                  <div className="mb-2 flex items-baseline justify-between px-1.5 pt-1">
                    <span className="text-[13px] font-semibold">{s.label}</span>
                    <span className="text-[11px] text-muted tabular">
                      {list.length} ·{" "}
                      {formatMoney(
                        list.reduce((a, d) => a + d.value_minor, 0),
                        currency,
                        { compact: true },
                      )}
                    </span>
                  </div>
                  <div className="space-y-1.5">
                    {list.map((d) => (
                      <article
                        key={d.id}
                        draggable
                        onDragStart={() => setDragging(d.id)}
                        onDragEnd={() => setDragging(null)}
                        className={cn(
                          "cursor-grab rounded-[10px] bg-surface p-3 shadow-[var(--shadow-sm)] ring-1 ring-hairline active:cursor-grabbing",
                          dragging === d.id && "opacity-50",
                        )}
                      >
                        <div className="text-[13px] font-medium leading-snug">{d.title}</div>
                        <div className="mt-1 flex items-center justify-between text-[12px] text-muted">
                          <span>{d.contact_name ?? sentence(d.deal_type)}</span>
                          <span className="tabular font-medium text-text">
                            {formatMoney(d.value_minor, d.currency, { compact: true })}
                          </span>
                        </div>
                        <Select
                          aria-label="Move to stage"
                          className="mt-2 h-8 text-[12px]"
                          value={d.stage_key}
                          onChange={async (e) => {
                            const stageKey = e.target.value;
                            const target = pipeline.stages.find((x) => x.key === stageKey);
                            const lost = target?.kind === "lost" ? await lostReason() : undefined;
                            if (lost === null) return;
                            move.mutate({ id: d.id, stage_key: stageKey, lost_reason: lost });
                          }}
                        >
                          {pipeline.stages.map((x) => (
                            <option key={x.key} value={x.key}>
                              {x.label}
                            </option>
                          ))}
                        </Select>
                      </article>
                    ))}
                  </div>
                </section>
              );
            })}
          </div>
        </div>
      )}
      <Dialog open={adding} onOpenChange={setAdding}>
        <DialogContent title={`New deal · ${pipeline.name}`}>
          <div className="space-y-4">
            <Field label="Title">
              {(id) => <Input id={id} value={nd.title} onChange={(e) => setNd({ ...nd, title: e.target.value })} />}
            </Field>
            <Field label={`Value (${currency})`}>
              {(id) => (
                <Input
                  id={id}
                  type="number"
                  min={0}
                  value={nd.value}
                  onChange={(e) => setNd({ ...nd, value: e.target.value })}
                />
              )}
            </Field>
            <div className="flex justify-end">
              <Button variant="primary" disabled={!nd.title} loading={create.isPending} onClick={() => create.mutate()}>
                Add deal
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Payments({ bid }: { bid: string }) {
  const currency = useCurrency(bid);
  const { data, isLoading } = useApi<{ items: Payment[] }>(gpath(bid, "/payments"));
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ amount: "", email: "", kind: "one_time", description: "" });
  const add = useAction(
    () =>
      api.post(gpath(bid, "/payments"), {
        amount_minor: Math.round(Number(f.amount) * 100),
        currency,
        email: f.email || null,
        kind: f.kind,
        description: f.description || null,
      }),
    {
      invalidate: [gpath(bid, "/payments"), gpath(bid, "/contacts"), gpath(bid, "/funnel"), gpath(bid, "/analytics")],
      success: "Payment recorded",
      onSuccess: () => setOpen(false),
    },
  );
  return (
    <div>
      <div className="mb-4 flex justify-end">
        <Button icon={<Plus className="size-4" />} onClick={() => setOpen(true)}>
          Record payment
        </Button>
      </div>
      {isLoading ? (
        <Loading />
      ) : !data?.items.length ? (
        <Card>
          <EmptyState title="No payments yet" body="Connect a payment provider in Settings, or record one by hand." />
        </Card>
      ) : (
        <Card className="divide-y divide-[var(--hairline)] overflow-hidden">
          {data.items.map((p) => (
            <div key={p.id} className="flex items-center gap-4 px-5 py-3 text-[13px]">
              <div className="min-w-0 flex-1">
                <div className="font-medium">{p.description ?? sentence(p.kind)}</div>
                <div className="text-[12px] text-muted">
                  {formatDateTime(p.paid_at)} · {sentence(p.provider)}
                  {p.promo_code ? ` · ${p.promo_code}` : ""}
                </div>
              </div>
              <StatusBadge status={p.status} />
              <span className={cn("w-28 text-right tabular font-medium", p.kind === "refund" && "text-critical")}>
                {p.kind === "refund" ? "−" : ""}
                {formatMoney(p.amount_minor, p.currency)}
              </span>
            </div>
          ))}
        </Card>
      )}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          title="Record a payment"
          description="Links to the person by email, moves them to Customer, and credits the campaign that brought them."
        >
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <Field label={`Amount (${currency})`}>
                {(id) => (
                  <Input
                    id={id}
                    type="number"
                    min={0}
                    step="any"
                    value={f.amount}
                    onChange={(e) => setF({ ...f, amount: e.target.value })}
                  />
                )}
              </Field>
              <Field label="Kind">
                {(id) => (
                  <Select id={id} value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>
                    <option value="one_time">One-time</option>
                    <option value="subscription">Subscription</option>
                    <option value="refund">Refund</option>
                  </Select>
                )}
              </Field>
            </div>
            <Field label="Customer email">
              {(id) => (
                <Input id={id} type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} />
              )}
            </Field>
            <Field label="Description">
              {(id) => (
                <Input id={id} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} />
              )}
            </Field>
            <div className="flex justify-end">
              <Button variant="primary" disabled={!f.amount} loading={add.isPending} onClick={() => add.mutate()}>
                Record
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export function Audience() {
  const bid = useBid();
  const search = useSearch({ strict: false }) as { tab?: string; contact?: string; new?: string };
  const navigate = useNavigate();
  const set = (patch: Record<string, string | undefined>) =>
    navigate({ to: ".", search: { ...search, ...patch } as never, replace: true });
  const tab = search.tab ?? "people";
  return (
    <div>
      <PageHeader
        title="Audience"
        subtitle="Everyone who has touched your business, where they came from, and where they are now."
        actions={
          <>
            <ImportContacts bid={bid} />
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => set({ new: "1" })}>
              Add
            </Button>
          </>
        }
      />
      <Tabs value={tab} onValueChange={(v) => set({ tab: v })}>
        <TabsList>
          <TabsTrigger value="people">People</TabsTrigger>
          <TabsTrigger value="funnel">Funnel</TabsTrigger>
          <TabsTrigger value="deals">Deals</TabsTrigger>
          <TabsTrigger value="payments">Payments</TabsTrigger>
        </TabsList>
        <TabsContent value="people">
          <People bid={bid} onOpen={(id) => set({ contact: id })} />
        </TabsContent>
        <TabsContent value="funnel">
          <FunnelTab bid={bid} />
        </TabsContent>
        <TabsContent value="deals">
          <Deals bid={bid} />
        </TabsContent>
        <TabsContent value="payments">
          <Payments bid={bid} />
        </TabsContent>
      </Tabs>
      <ContactSheet bid={bid} id={search.contact ?? null} onClose={() => set({ contact: undefined })} />
      <AddContact bid={bid} open={!!search.new} setOpen={(o) => set({ new: o ? "1" : undefined })} />
    </div>
  );
}
