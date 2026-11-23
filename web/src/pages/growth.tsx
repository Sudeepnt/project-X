import { Copy, ExternalLink, FileText, FlaskConical, Link2, Plus, Tag, Trophy } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
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
  Select,
  Sheet,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Textarea,
} from "@/components/ui";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatNumber, formatPercent, sentence } from "@/lib/format";
import { gpath, useAction, useApi, useBid } from "@/lib/queries";
import type { Campaign } from "@/lib/types";
import { LoadError, Loading } from "./system";

interface VariantContent {
  headline?: string;
  subheadline?: string;
  bullets?: string[];
  cta_label?: string;
  thank_you?: string;
}
interface PageVariant {
  id: string;
  key: string;
  weight: number;
  content: VariantContent;
  views: number;
  conversions: number;
  conversion_rate: number | null;
}
interface Experiment {
  prob_best: Record<string, number>;
  leader: string;
  status: "collecting" | "winner" | "no_clear_winner";
  min_views_per_variant: number;
  confidence: number;
}
interface LandingPage {
  id: string;
  slug: string;
  title: string;
  kind: string;
  status: string;
  settings: Record<string, unknown>;
  campaign_id: string | null;
  public_url: string;
  views: number;
  submissions: number;
  conversion_rate: number | null;
  published_at: string | null;
  variants: PageVariant[];
  experiment: Experiment | null;
}
interface TrackedLink {
  id: string;
  slug: string;
  short_url: string;
  destination: string;
  label: string | null;
  platform: string | null;
  campaign_id: string | null;
  clicks: number;
  created_at: string;
}
interface Promo {
  id: string;
  code: string;
  description: string | null;
  campaign_id: string | null;
  uses?: number;
  revenue_minor?: number;
}

const copy = (text: string) => navigator.clipboard.writeText(text).then(() => toast.success("Copied"));

function VariantEditor({
  bid,
  page,
  variant,
  onDone,
}: {
  bid: string;
  page: LandingPage;
  variant: PageVariant | { key: string; weight: number; content: VariantContent };
  onDone: () => void;
}) {
  const [c, setC] = useState<VariantContent>(variant.content);
  const [weight, setWeight] = useState(variant.weight);
  useEffect(() => {
    setC(variant.content);
    setWeight(variant.weight);
  }, [variant]);
  const save = useAction(
    () => api.put(gpath(bid, `/landing-pages/${page.id}/variants/${variant.key}`), { content: c, weight }),
    { invalidate: [gpath(bid, "/landing-pages")], success: `Variant ${variant.key} saved`, onSuccess: onDone },
  );
  return (
    <div className="space-y-3">
      <Field label="Headline">
        {(id) => <Input id={id} value={c.headline ?? ""} onChange={(e) => setC({ ...c, headline: e.target.value })} />}
      </Field>
      <Field label="Subheadline">
        {(id) => (
          <Textarea
            id={id}
            rows={2}
            value={c.subheadline ?? ""}
            onChange={(e) => setC({ ...c, subheadline: e.target.value })}
          />
        )}
      </Field>
      <Field label="Bullets (one per line)">
        {(id) => (
          <Textarea
            id={id}
            rows={3}
            value={(c.bullets ?? []).join("\n")}
            onChange={(e) => setC({ ...c, bullets: e.target.value.split("\n").filter(Boolean) })}
          />
        )}
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Button">
          {(id) => (
            <Input id={id} value={c.cta_label ?? ""} onChange={(e) => setC({ ...c, cta_label: e.target.value })} />
          )}
        </Field>
        <Field label="Traffic share">
          {(id) => (
            <Input
              id={id}
              type="number"
              min={0}
              max={10}
              step={0.1}
              value={weight}
              onChange={(e) => setWeight(Number(e.target.value))}
            />
          )}
        </Field>
      </div>
      <Field label="Thank-you message">
        {(id) => (
          <Input id={id} value={c.thank_you ?? ""} onChange={(e) => setC({ ...c, thank_you: e.target.value })} />
        )}
      </Field>
      <div className="flex justify-end">
        <Button variant="primary" size="sm" loading={save.isPending} onClick={() => save.mutate()}>
          Save variant {variant.key}
        </Button>
      </div>
    </div>
  );
}

function PageSheet({ bid, page, onClose }: { bid: string; page: LandingPage | null; onClose: () => void }) {
  const [editing, setEditing] = useState<string | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: deliberately resets local state when the selection changes
  useEffect(() => setEditing(null), [page?.id]);
  const inv = [gpath(bid, "/landing-pages")];
  const publish = useAction(() => api.post(gpath(bid, `/landing-pages/${page?.id}/publish`)), {
    invalidate: inv,
    success: "Live",
  });
  const unpublish = useAction(() => api.post(gpath(bid, `/landing-pages/${page?.id}/unpublish`)), {
    invalidate: inv,
    success: "Unpublished",
  });
  if (!page)
    return (
      <Sheet open={false} onOpenChange={() => {}} title="">
        {null}
      </Sheet>
    );
  const exp = page.experiment;
  const nextKey = String.fromCharCode(65 + page.variants.length);
  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={page.title}
      subtitle={
        <span className="flex items-center gap-2">
          <StatusBadge status={page.status} />
          <span>{sentence(page.kind)}</span>
        </span>
      }
      footer={
        <>
          <Button
            variant="ghost"
            icon={<ExternalLink className="size-4" />}
            onClick={() => window.open(page.public_url, "_blank", "noopener")}
          >
            Open
          </Button>
          {page.status === "published" ? (
            <Button onClick={() => unpublish.mutate()} loading={unpublish.isPending}>
              Unpublish
            </Button>
          ) : (
            <Button variant="primary" onClick={() => publish.mutate()} loading={publish.isPending}>
              Publish
            </Button>
          )}
        </>
      }
    >
      <div className="space-y-6">
        <div className="flex items-center gap-2 rounded-[12px] bg-fill-2 px-3 py-2 text-[13px]">
          <span className="min-w-0 flex-1 truncate font-mono text-[12px]">{page.public_url}</span>
          <Button size="sm" variant="ghost" icon={<Copy className="size-3.5" />} onClick={() => copy(page.public_url)}>
            Copy
          </Button>
        </div>
        <div className="grid grid-cols-3 gap-3 text-center">
          {[
            ["Views", formatNumber(page.views)],
            ["Signups", formatNumber(page.submissions)],
            ["Conversion", formatPercent(page.conversion_rate)],
          ].map(([k, v]) => (
            <div key={k} className="rounded-[14px] bg-fill-2 py-3">
              <div className="text-[20px] font-semibold tabular">{v}</div>
              <div className="text-[12px] text-muted">{k}</div>
            </div>
          ))}
        </div>
        {exp ? (
          <div
            className={cn(
              "rounded-[14px] p-4 text-[13px]",
              exp.status === "winner" ? "bg-[var(--good-soft)]" : "bg-accent-soft",
            )}
          >
            <div className="mb-2 flex items-center gap-2 font-semibold">
              {exp.status === "winner" ? (
                <Trophy className="size-4 text-good" />
              ) : (
                <FlaskConical className="size-4 text-accent" />
              )}
              {exp.status === "winner"
                ? `Variant ${exp.leader} wins`
                : exp.status === "no_clear_winner"
                  ? "No clear winner yet"
                  : "Collecting evidence"}
            </div>
            <p className="text-text-2">
              {exp.status === "collecting"
                ? `Each variant needs ${exp.min_views_per_variant} views before we call it.`
                : exp.status === "winner"
                  ? `${formatPercent(exp.prob_best[exp.leader] ?? 0, 0)} chance it's truly best. Give it all the traffic by setting the others' share to 0.`
                  : "The variants perform about the same. Try a bolder change."}
            </p>
          </div>
        ) : null}
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <h4 className="text-[14px] font-semibold">Variants</h4>
            {page.variants.length < 4 ? (
              <Button size="sm" icon={<Plus className="size-3.5" />} onClick={() => setEditing(nextKey)}>
                Add variant {nextKey}
              </Button>
            ) : null}
          </div>
          {page.variants.map((v) => (
            <div key={v.id} className="rounded-[14px] ring-1 ring-hairline">
              <button
                type="button"
                className="flex w-full items-center gap-3 px-4 py-3 text-left"
                onClick={() => setEditing(editing === v.key ? null : v.key)}
              >
                <Badge tone={exp?.leader === v.key ? "good" : "neutral"}>{v.key}</Badge>
                <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{v.content.headline}</span>
                <span className="text-[12px] text-muted tabular">
                  {formatNumber(v.views)} views · {formatPercent(v.conversion_rate)}
                  {exp ? ` · ${formatPercent(exp.prob_best[v.key] ?? 0, 0)} best` : ""}
                </span>
              </button>
              {editing === v.key ? (
                <div className="px-4 pb-4">
                  <VariantEditor bid={bid} page={page} variant={v} onDone={() => setEditing(null)} />
                </div>
              ) : null}
            </div>
          ))}
          {editing === nextKey ? (
            <div className="rounded-[14px] p-4 ring-1 ring-accent">
              <div className="mb-3 text-[13px] font-semibold">New variant {nextKey}</div>
              <VariantEditor
                bid={bid}
                page={page}
                variant={{ key: nextKey, weight: 1, content: { ...(page.variants[0]?.content ?? {}) } }}
                onDone={() => setEditing(null)}
              />
            </div>
          ) : null}
        </div>
      </div>
    </Sheet>
  );
}

function Pages({ bid }: { bid: string }) {
  const { data, isLoading, error } = useApi<{ items: LandingPage[]; kinds: string[] }>(gpath(bid, "/landing-pages"));
  const { data: campaigns } = useApi<{ items: Campaign[] }>(gpath(bid, "/campaigns"));
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [f, setF] = useState({ title: "", kind: "newsletter", slug: "", campaign_id: "" });
  const create = useAction(
    () =>
      api.post<LandingPage>(gpath(bid, "/landing-pages"), {
        title: f.title,
        kind: f.kind,
        slug: f.slug || null,
        campaign_id: f.campaign_id || null,
      }),
    {
      invalidate: [gpath(bid, "/landing-pages")],
      success: "Page drafted in your brand voice",
      onSuccess: (p) => {
        setCreating(false);
        setOpenId(p.id);
      },
    },
  );
  if (isLoading) return <Loading />;
  if (error) return <LoadError error={error} />;
  const page = data!.items.find((p) => p.id === openId) ?? null;
  return (
    <div>
      <div className="mb-4 flex justify-end">
        <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>
          New page
        </Button>
      </div>
      {!data!.items.length ? (
        <Card>
          <EmptyState
            icon={<FileText className="size-6" />}
            title="No pages yet"
            body="Signup, waitlist, lead magnet, consult. Fast, accessible, privacy-friendly, with A/B testing built in."
          />
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {data!.items.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => setOpenId(p.id)}
              className="rounded-[18px] bg-surface p-5 text-left shadow-[var(--shadow-sm)] ring-1 ring-hairline transition-shadow hover:shadow-[var(--shadow-md)]"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-[16px] font-semibold">{p.title}</div>
                  <div className="text-[12px] text-muted">
                    /{p.slug} · {sentence(p.kind)}
                  </div>
                </div>
                <StatusBadge status={p.status} />
              </div>
              <div className="mt-4 grid grid-cols-3 text-[12px] text-muted">
                <div>
                  <div className="text-[18px] font-semibold text-text tabular">{formatNumber(p.views)}</div>views
                </div>
                <div>
                  <div className="text-[18px] font-semibold text-text tabular">{formatNumber(p.submissions)}</div>
                  signups
                </div>
                <div>
                  <div className="text-[18px] font-semibold text-text tabular">{formatPercent(p.conversion_rate)}</div>
                  conversion
                </div>
              </div>
              {p.variants.length > 1 ? (
                <div className="mt-3">
                  <Badge tone={p.experiment?.status === "winner" ? "good" : "accent"}>
                    <FlaskConical className="size-3" />
                    A/B · {p.variants.length} variants
                  </Badge>
                </div>
              ) : null}
            </button>
          ))}
        </div>
      )}
      <PageSheet bid={bid} page={page} onClose={() => setOpenId(null)} />
      <Dialog open={creating} onOpenChange={setCreating}>
        <DialogContent
          title="New landing page"
          description="We write the first version from your brand book. You can edit and test variants."
        >
          <div className="space-y-4">
            <Field label="Title">
              {(id) => <Input id={id} value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} />}
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Kind">
                {(id) => (
                  <Select id={id} value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>
                    {data!.kinds.map((k) => (
                      <option key={k} value={k}>
                        {sentence(k)}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
              <Field label="Address" hint="Letters, numbers, dashes">
                {(id) => (
                  <Input
                    id={id}
                    placeholder="auto"
                    pattern="[a-z0-9-]*"
                    value={f.slug}
                    onChange={(e) => setF({ ...f, slug: e.target.value.toLowerCase() })}
                  />
                )}
              </Field>
            </div>
            <Field label="Campaign">
              {(id) => (
                <Select id={id} value={f.campaign_id} onChange={(e) => setF({ ...f, campaign_id: e.target.value })}>
                  <option value="">None</option>
                  {campaigns?.items.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <div className="flex justify-end">
              <Button variant="primary" disabled={!f.title} loading={create.isPending} onClick={() => create.mutate()}>
                Create
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Links({ bid }: { bid: string }) {
  const { data, isLoading } = useApi<{ items: TrackedLink[] }>(gpath(bid, "/links"));
  const [f, setF] = useState({ destination: "", label: "", platform: "" });
  const create = useAction(
    () =>
      api.post<TrackedLink>(gpath(bid, "/links"), {
        destination: f.destination,
        label: f.label || null,
        platform: f.platform || null,
      }),
    {
      invalidate: [gpath(bid, "/links")],
      success: "Link ready",
      onSuccess: (l) => {
        copy(l.short_url);
        setF({ destination: "", label: "", platform: "" });
      },
    },
  );
  return (
    <div className="space-y-6">
      <Card className="p-5">
        <div className="grid gap-3 sm:grid-cols-[2fr_1fr_1fr_auto] sm:items-end">
          <Field label="Destination">
            {(id) => (
              <Input
                id={id}
                type="url"
                placeholder="https://"
                value={f.destination}
                onChange={(e) => setF({ ...f, destination: e.target.value })}
              />
            )}
          </Field>
          <Field label="Label">
            {(id) => <Input id={id} value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} />}
          </Field>
          <Field label="Where it's shared">
            {(id) => (
              <Select id={id} value={f.platform} onChange={(e) => setF({ ...f, platform: e.target.value })}>
                <option value="">Anywhere</option>
                {["instagram", "x", "linkedin", "tiktok", "youtube", "threads", "telegram", "whatsapp", "email"].map(
                  (p) => (
                    <option key={p} value={p}>
                      {sentence(p)}
                    </option>
                  ),
                )}
              </Select>
            )}
          </Field>
          <Button
            variant="primary"
            icon={<Link2 className="size-4" />}
            disabled={!f.destination}
            loading={create.isPending}
            onClick={() => create.mutate()}
          >
            Create
          </Button>
        </div>
        <p className="mt-3 text-[12px] text-muted">
          Every post already gets its own tracked link. Use these for bios, sponsors and anywhere else. No cookies;
          clicks are tied to signups by a one-time code.
        </p>
      </Card>
      {isLoading ? (
        <Loading />
      ) : data?.items.length ? (
        <Card className="divide-y divide-[var(--hairline)] overflow-hidden">
          {data.items.map((l) => (
            <div key={l.id} className="flex items-center gap-3 px-5 py-3 text-[13px]">
              {l.platform ? <PlatformMark platform={l.platform} size={20} /> : <Link2 className="size-5 text-faint" />}
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">{l.label ?? l.destination}</div>
                <div className="truncate font-mono text-[12px] text-muted">
                  {l.short_url} → {l.destination}
                </div>
              </div>
              <span className="tabular text-text-2">{formatNumber(l.clicks)} clicks</span>
              <Button
                size="sm"
                variant="ghost"
                aria-label="Copy link"
                icon={<Copy className="size-3.5" />}
                onClick={() => copy(l.short_url)}
              />
            </div>
          ))}
        </Card>
      ) : null}
    </div>
  );
}

function Promos({ bid }: { bid: string }) {
  const { data, isLoading } = useApi<{ items: Promo[] }>(gpath(bid, "/promo-codes"));
  const [f, setF] = useState({ code: "", description: "" });
  const create = useAction(
    () => api.post(gpath(bid, "/promo-codes"), { code: f.code.toUpperCase(), description: f.description || null }),
    {
      invalidate: [gpath(bid, "/promo-codes")],
      success: "Code added",
      onSuccess: () => setF({ code: "", description: "" }),
    },
  );
  return (
    <div className="space-y-6">
      <Card className="p-5">
        <div className="grid gap-3 sm:grid-cols-[1fr_2fr_auto] sm:items-end">
          <Field label="Code">
            {(id) => (
              <Input
                id={id}
                className="font-mono uppercase"
                value={f.code}
                onChange={(e) => setF({ ...f, code: e.target.value })}
              />
            )}
          </Field>
          <Field label="What it's for">
            {(id) => (
              <Input id={id} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} />
            )}
          </Field>
          <Button
            variant="primary"
            icon={<Tag className="size-4" />}
            disabled={!f.code}
            loading={create.isPending}
            onClick={() => create.mutate()}
          >
            Add
          </Button>
        </div>
        <p className="mt-3 text-[12px] text-muted">
          Codes credit sales to a campaign when payments report them. Discounts themselves are set in your payment
          provider; offering one always asks you first.
        </p>
      </Card>
      {isLoading ? (
        <Loading />
      ) : data?.items.length ? (
        <Card className="divide-y divide-[var(--hairline)]">
          {data.items.map((p) => (
            <div key={p.id} className="flex items-center gap-3 px-5 py-3 text-[13px]">
              <span className="font-mono font-semibold">{p.code}</span>
              <span className="flex-1 text-muted">{p.description}</span>
              {p.uses != null ? <span className="tabular">{p.uses} uses</span> : null}
            </div>
          ))}
        </Card>
      ) : null}
    </div>
  );
}

export function Growth() {
  const bid = useBid();
  const [tab, setTab] = useState("pages");
  return (
    <div>
      <PageHeader
        title="Pages & links"
        subtitle="Where interest turns into signups, and how every click is traced back to the post that earned it."
      />
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="pages">Landing pages</TabsTrigger>
          <TabsTrigger value="links">Tracked links</TabsTrigger>
          <TabsTrigger value="promo">Promo codes</TabsTrigger>
        </TabsList>
        <TabsContent value="pages">
          <Pages bid={bid} />
        </TabsContent>
        <TabsContent value="links">
          <Links bid={bid} />
        </TabsContent>
        <TabsContent value="promo">
          <Promos bid={bid} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
