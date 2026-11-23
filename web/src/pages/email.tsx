import { Link } from "@tanstack/react-router";
import { ChevronDown, Mail, Newspaper, Plus, Send, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
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
} from "@/components/ui";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDateTime, formatNumber, formatPercent, sentence } from "@/lib/format";
import { bpath, gpath, useAction, useApi, useBid } from "@/lib/queries";
import type { Business } from "@/lib/types";
import { LoadError, Loading } from "./system";

interface Step {
  delay_hours: number;
  subject: string;
  body: string;
}
interface Sequence {
  id: string;
  key: string;
  name: string;
  channel: "email" | "whatsapp";
  trigger: { type: string; stage?: string };
  steps: Step[];
  stop_on: { stages?: string[] };
  active: boolean;
  stats: { active: number; completed: number; stopped: number; sent: number };
}
interface Issue {
  id: string;
  title: string;
  subject: string;
  preheader: string | null;
  sections: { title: string; body: string; url?: string }[];
  status: string;
  scheduled_at: string | null;
  sent_at: string | null;
  stats: Record<string, number>;
  decision_id: string | null;
  created_at: string;
}

const HOURS = Array.from({ length: 24 }, (_, h) => h);

function delayLabel(h: number): string {
  if (h === 0) return "Right away";
  if (h < 24) return `After ${h} hour${h === 1 ? "" : "s"}`;
  const d = Math.round(h / 24);
  return `After ${d} day${d === 1 ? "" : "s"}`;
}

function SequenceEditor({ bid, seq }: { bid: string; seq: Sequence }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(seq);
  useEffect(() => setDraft(seq), [seq]);
  const dirty = JSON.stringify(draft) !== JSON.stringify(seq);
  const save = useAction(
    (next: Sequence) =>
      api.put(gpath(bid, `/sequences/${seq.id}`), {
        name: next.name,
        steps: next.steps,
        active: next.active,
        channel: next.channel,
        stop_on: next.stop_on,
      }),
    { invalidate: [gpath(bid, "/sequences")], success: "Sequence saved" },
  );
  const setStep = (i: number, patch: Partial<Step>) =>
    setDraft({ ...draft, steps: draft.steps.map((s, j) => (j === i ? { ...s, ...patch } : s)) });
  return (
    <Card>
      <div className="flex items-center gap-4 px-5 py-4">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="flex min-w-0 flex-1 items-center gap-3 text-left"
          aria-expanded={open}
        >
          <ChevronDown className={cn("size-4 shrink-0 text-faint transition-transform", !open && "-rotate-90")} />
          <div className="min-w-0">
            <div className="truncate text-[15px] font-semibold">{seq.name}</div>
            <div className="text-[12px] text-muted">
              {seq.trigger.type === "stage_entered"
                ? `When someone becomes ${seq.trigger.stage?.toUpperCase() === "MQL" ? "an MQL" : `a ${seq.trigger.stage}`}`
                : sentence(seq.trigger.type)}{" "}
              · {seq.steps.length} emails
            </div>
          </div>
        </button>
        <div className="hidden gap-5 text-right text-[12px] sm:flex">
          {(["active", "completed", "sent"] as const).map((k) => (
            <div key={k}>
              <div className="text-[15px] font-semibold tabular">{formatNumber(seq.stats[k])}</div>
              <div className="text-muted">{sentence(k)}</div>
            </div>
          ))}
        </div>
        <Switch
          label={`${seq.name} active`}
          checked={seq.active}
          onCheckedChange={(v) => save.mutate({ ...seq, active: v })}
        />
      </div>
      {open ? (
        <div className="space-y-4 px-5 pb-5">
          <Field label="Name">
            {(id) => (
              <Input id={id} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
            )}
          </Field>
          <ol className="space-y-3">
            {draft.steps.map((s, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: steps are positional
              <li key={i} className="rounded-[14px] bg-fill-2 p-4">
                <div className="mb-3 flex items-center gap-2">
                  <Badge tone="accent">Email {i + 1}</Badge>
                  <Select
                    aria-label="Delay"
                    className="h-8 w-auto text-[13px]"
                    value={String(s.delay_hours)}
                    onChange={(e) => setStep(i, { delay_hours: Number(e.target.value) })}
                  >
                    {[0, 1, 4, 24, 48, 72, 96, 120, 168, 240, 336].map((h) => (
                      <option key={h} value={h}>
                        {delayLabel(h)}
                      </option>
                    ))}
                    {![0, 1, 4, 24, 48, 72, 96, 120, 168, 240, 336].includes(s.delay_hours) ? (
                      <option value={s.delay_hours}>{delayLabel(s.delay_hours)}</option>
                    ) : null}
                  </Select>
                  <button
                    type="button"
                    aria-label="Remove email"
                    className="ml-auto rounded p-1 text-faint hover:text-critical"
                    onClick={() => setDraft({ ...draft, steps: draft.steps.filter((_, j) => j !== i) })}
                  >
                    <Trash2 className="size-4" />
                  </button>
                </div>
                <Input
                  aria-label="Subject"
                  placeholder="Subject"
                  value={s.subject}
                  onChange={(e) => setStep(i, { subject: e.target.value })}
                  className="mb-2"
                />
                <Textarea
                  aria-label="Body"
                  rows={5}
                  value={s.body}
                  onChange={(e) => setStep(i, { body: e.target.value })}
                />
              </li>
            ))}
          </ol>
          <div className="flex items-center justify-between">
            <Button
              size="sm"
              icon={<Plus className="size-4" />}
              onClick={() =>
                setDraft({
                  ...draft,
                  steps: [...draft.steps, { delay_hours: 48, subject: "", body: "Hi {{ first_name }},\n\n" }],
                })
              }
            >
              Add email
            </Button>
            <div className="flex gap-2">
              {dirty ? (
                <Button size="sm" variant="ghost" onClick={() => setDraft(seq)}>
                  Discard
                </Button>
              ) : null}
              <Button
                size="sm"
                variant="primary"
                disabled={!dirty}
                loading={save.isPending}
                onClick={() => save.mutate(draft)}
              >
                Save
              </Button>
            </div>
          </div>
          <p className="text-[12px] text-muted">
            Use {"{{ first_name }}"}, {"{{ business_name }}"}. Every email has a one-click unsubscribe and your postal
            address. People leave the sequence when they reach {draft.stop_on.stages?.join(", ") || "a later stage"}.
          </p>
        </div>
      ) : null}
    </Card>
  );
}

function Newsletter({ bid }: { bid: string }) {
  const { data, isLoading, error } = useApi<{
    items: Issue[];
    subscribers: number;
    settings: { enabled: boolean; send_hour: number; double_opt_in: boolean };
  }>(gpath(bid, "/newsletter/issues"));
  const { data: business } = useApi<Business>(bpath(bid, ""));
  const [form, setForm] = useState({ reply_to_email: "", postal_address: "" });
  useEffect(() => {
    if (business)
      setForm({
        reply_to_email: String(business.settings.reply_to_email ?? ""),
        postal_address: String(business.settings.postal_address ?? ""),
      });
  }, [business]);
  const inv = [gpath(bid, "/newsletter"), bpath(bid, "")];
  const save = useAction((patch: Record<string, unknown>) => api.put(gpath(bid, "/email-settings"), patch), {
    invalidate: inv,
    success: "Saved",
  });
  const build = useAction(() => api.post(gpath(bid, "/newsletter/build")), {
    invalidate: inv,
    success: "Building the next issue. It comes to you for approval.",
  });
  if (isLoading) return <Loading />;
  if (error) return <LoadError error={error} />;
  const s = data!.settings;
  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_340px]">
      <Card>
        <CardHeader
          title="Issues"
          subtitle={`${formatNumber(data!.subscribers)} opted-in subscribers`}
          action={
            <Button
              size="sm"
              icon={<Newspaper className="size-4" />}
              onClick={() => build.mutate()}
              loading={build.isPending}
            >
              Build next issue
            </Button>
          }
        />
        {data!.items.length ? (
          <ul className="divide-y divide-[var(--hairline)] pb-2">
            {data!.items.map((i) => {
              const recipients = i.stats.recipients ?? 0;
              const clicks = i.stats.clicks;
              return (
                <li key={i.id} className="flex items-center gap-4 px-5 py-3">
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[14px] font-medium">{i.subject}</div>
                    <div className="text-[12px] text-muted">
                      {i.sent_at
                        ? `Sent ${formatDateTime(i.sent_at)}`
                        : i.scheduled_at
                          ? `Scheduled ${formatDateTime(i.scheduled_at)}`
                          : `Created ${formatDateTime(i.created_at)}`}{" "}
                      · {i.sections.length} stories
                    </div>
                  </div>
                  {recipients ? (
                    <div className="hidden text-right text-[12px] sm:block">
                      <div className="tabular font-medium">{formatNumber(recipients)} recipients</div>
                      {clicks != null ? (
                        <div className="text-muted tabular">{formatPercent(clicks / recipients, 1)} clicked</div>
                      ) : null}
                    </div>
                  ) : null}
                  {i.decision_id && i.status === "draft" ? (
                    <Link to={`/b/${bid}/decisions/${i.decision_id}` as string}>
                      <Button size="sm" variant="primary">
                        Review
                      </Button>
                    </Link>
                  ) : (
                    <StatusBadge status={i.status} />
                  )}
                </li>
              );
            })}
          </ul>
        ) : (
          <EmptyState
            icon={<Mail className="size-6" />}
            title="No issues yet"
            body="Each issue gathers your best recent posts into one email. Build one now, or wait for the daily run."
          />
        )}
      </Card>
      <div className="space-y-6">
        <Card className="space-y-4 p-5">
          <h3 className="text-[17px] font-semibold">Newsletter</h3>
          <div className="flex items-center justify-between">
            <span className="text-[14px]">Send automatically</span>
            <Switch
              label="Newsletter enabled"
              checked={s.enabled}
              onCheckedChange={(v) => save.mutate({ newsletter_enabled: v })}
            />
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="text-[14px]">Send at</span>
            <Select
              className="w-32"
              aria-label="Send hour"
              value={String(s.send_hour)}
              onChange={(e) => save.mutate({ newsletter_send_hour: Number(e.target.value) })}
            >
              {HOURS.map((h) => (
                <option key={h} value={h}>
                  {new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: "numeric" })}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-[14px]">Double opt-in</div>
              <div className="text-[12px] text-muted">New signups confirm by email first.</div>
            </div>
            <Switch
              label="Double opt-in"
              checked={s.double_opt_in}
              onCheckedChange={(v) => save.mutate({ double_opt_in: v })}
            />
          </div>
        </Card>
        <Card className="space-y-4 p-5">
          <h3 className="text-[17px] font-semibold">Sender details</h3>
          <Field label="Reply-to email">
            {(id) => (
              <Input
                id={id}
                type="email"
                value={form.reply_to_email}
                onChange={(e) => setForm({ ...form, reply_to_email: e.target.value })}
              />
            )}
          </Field>
          <Field
            label="Postal address"
            hint="Required by anti-spam law in most countries. Shown in every email footer."
          >
            {(id) => (
              <Textarea
                id={id}
                rows={2}
                value={form.postal_address}
                onChange={(e) => setForm({ ...form, postal_address: e.target.value })}
              />
            )}
          </Field>
          <Button
            size="sm"
            variant="primary"
            icon={<Send className="size-3.5" />}
            loading={save.isPending}
            onClick={() =>
              save.mutate({ reply_to_email: form.reply_to_email || null, postal_address: form.postal_address })
            }
          >
            Save
          </Button>
        </Card>
      </div>
    </div>
  );
}

export function Email() {
  const bid = useBid();
  const [tab, setTab] = useState("sequences");
  const { data, isLoading, error } = useApi<Sequence[]>(gpath(bid, "/sequences"));
  return (
    <div>
      <PageHeader
        title="Email"
        subtitle="Follow-ups that send themselves, and a newsletter built from your best posts. Only to people who said yes."
      />
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="sequences">Sequences</TabsTrigger>
          <TabsTrigger value="newsletter">Newsletter</TabsTrigger>
        </TabsList>
        <TabsContent value="sequences">
          {isLoading ? (
            <Loading />
          ) : error ? (
            <LoadError error={error} />
          ) : (
            <div className="space-y-3">
              {data!.map((s) => (
                <SequenceEditor key={s.id} bid={bid} seq={s} />
              ))}
            </div>
          )}
        </TabsContent>
        <TabsContent value="newsletter">
          <Newsletter bid={bid} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
