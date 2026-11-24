import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import {
  Archive,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  FlaskConical,
  ImagePlus,
  PenLine,
  Plus,
  RefreshCw,
  RotateCcw,
  Shuffle,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { ChecksList } from "@/components/decision";
import { AssetGrid, MediaPicker } from "@/components/media";
import {
  aiLabelText,
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
  Segmented,
  Select,
  Sheet,
  StatusBadge,
  Textarea,
} from "@/components/ui";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDateTime, formatNumber, sentence } from "@/lib/format";
import { gpath, useAction, useApi, useBid } from "@/lib/queries";
import type { Campaign, ContentItem, PlatformInfo, Post, Variant } from "@/lib/types";
import { LoadError, Loading } from "./system";

interface CalendarData {
  start: string;
  days: number;
  items: ContentItem[];
  posts: (Post & { content_id: string })[];
  moments: { date: string; name: string; kind: string }[];
}

const dayKey = (iso: string) => new Date(iso).toLocaleDateString("en-CA");
const addDays = (d: string, n: number) =>
  new Date(new Date(`${d}T00:00:00`).getTime() + n * 864e5).toLocaleDateString("en-CA");
const today = () => new Date().toLocaleDateString("en-CA");

function ContentChip({ item, onOpen }: { item: ContentItem; onOpen: () => void }) {
  const platforms = [...new Set((item.variants ?? []).map((v) => v.platform))];
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        "w-full rounded-[10px] bg-surface p-2 text-left shadow-[var(--shadow-sm)] ring-1 ring-hairline transition-all hover:-translate-y-px hover:shadow-[var(--shadow-md)]",
        item.status === "needs_review" && "ring-[color-mix(in_srgb,var(--warning)_45%,transparent)]",
      )}
    >
      <div className="flex items-center gap-1">
        {platforms.slice(0, 4).map((p) => (
          <PlatformMark key={p} platform={p} size={16} />
        ))}
        {item.intent === "ask" ? (
          <span className="ml-auto text-[10px] font-semibold text-warning uppercase">Ask</span>
        ) : null}
      </div>
      <div className="mt-1.5 line-clamp-2 text-[12px] font-medium leading-snug">{item.title}</div>
      <div className="mt-1 flex items-center justify-between text-[11px] text-muted">
        <span>
          {item.planned_for
            ? new Date(item.planned_for).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
            : ""}
        </span>
        <span className="capitalize">{item.status.replace(/_/g, " ")}</span>
      </div>
    </button>
  );
}

function CalendarView({ bid, onOpen }: { bid: string; onOpen: (id: string) => void }) {
  const [start, setStart] = useState(today());
  const { data, isLoading, error } = useApi<CalendarData>(gpath(bid, `/content/calendar?start=${start}&days=7`));
  const days = Array.from({ length: 7 }, (_, i) => addDays(start, i));
  const byDay = useMemo(() => {
    const m: Record<string, ContentItem[]> = {};
    for (const it of data?.items ?? []) {
      if (!it.planned_for) continue;
      const key = dayKey(it.planned_for);
      m[key] = [...(m[key] ?? []), it];
    }
    for (const list of Object.values(m)) list.sort((a, b) => (a.planned_for ?? "").localeCompare(b.planned_for ?? ""));
    return m;
  }, [data]);
  if (error) return <LoadError error={error} />;
  return (
    <div>
      <div className="mb-4 flex items-center gap-2">
        <Button size="icon" variant="ghost" aria-label="Previous week" onClick={() => setStart(addDays(start, -7))}>
          <ChevronLeft className="size-4" />
        </Button>
        <Button size="sm" onClick={() => setStart(today())}>
          Today
        </Button>
        <Button size="icon" variant="ghost" aria-label="Next week" onClick={() => setStart(addDays(start, 7))}>
          <ChevronRight className="size-4" />
        </Button>
        <span className="ml-2 text-[14px] font-medium">
          {new Date(`${start}T00:00`).toLocaleDateString(undefined, { month: "long", day: "numeric" })} –{" "}
          {new Date(`${addDays(start, 6)}T00:00`).toLocaleDateString(undefined, { month: "long", day: "numeric" })}
        </span>
      </div>
      {isLoading ? (
        <Loading rows={2} />
      ) : (
        <div className="-mx-4 overflow-x-auto px-4 pb-2">
          <div className="grid min-w-[840px] grid-cols-7 gap-2">
            {days.map((d) => {
              const moments = data?.moments.filter((m) => m.date === d) ?? [];
              const isToday = d === today();
              return (
                <div
                  key={d}
                  className={cn("min-h-[360px] rounded-[14px] p-2", isToday ? "bg-accent-soft" : "bg-fill-2")}
                >
                  <div className="mb-2 px-1">
                    <div className={cn("text-[11px] font-semibold uppercase", isToday ? "text-accent" : "text-muted")}>
                      {new Date(`${d}T00:00`).toLocaleDateString(undefined, { weekday: "short" })}
                    </div>
                    <div className="text-[18px] font-semibold tabular">{new Date(`${d}T00:00`).getDate()}</div>
                    {moments.map((m) => (
                      <div key={m.name} className="mt-0.5 truncate text-[10px] text-warning" title={m.name}>
                        ★ {m.name}
                      </div>
                    ))}
                  </div>
                  <div className="space-y-1.5">
                    {(byDay[d] ?? []).map((it) => (
                      <ContentChip key={it.id} item={it} onOpen={() => onOpen(it.id)} />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function ListView({ bid, status, onOpen }: { bid: string; status: string; onOpen: (id: string) => void }) {
  const { data, isLoading, error } = useApi<{ items: ContentItem[] }>(
    gpath(bid, `/content?limit=100${status === "all" ? "" : `&status_filter=${status}`}`),
  );
  if (isLoading) return <Loading />;
  if (error) return <LoadError error={error} />;
  if (!data?.items.length)
    return (
      <Card>
        <EmptyState
          icon={<PenLine className="size-6" />}
          title="Nothing here"
          body="Start a campaign, or write a post yourself."
        />
      </Card>
    );
  return (
    <Card className="divide-y divide-[var(--hairline)] overflow-hidden">
      {data.items.map((it) => (
        <button
          key={it.id}
          type="button"
          onClick={() => onOpen(it.id)}
          className="flex w-full items-center gap-4 px-5 py-3.5 text-left hover:bg-fill-2"
        >
          <div className="flex -space-x-1">
            {[...new Set((it.variants ?? []).map((v) => v.platform))].slice(0, 4).map((p) => (
              <PlatformMark key={p} platform={p} size={22} />
            ))}
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[14px] font-medium">{it.title}</div>
            <div className="truncate text-[12px] text-muted">
              {sentence(it.pillar)} · {it.hook_type ? sentence(it.hook_type) : "custom"} ·{" "}
              {it.planned_for ? formatDateTime(it.planned_for) : "unscheduled"}
            </div>
          </div>
          {it.intent === "ask" ? <Badge tone="warning">Ask</Badge> : null}
          <StatusBadge status={it.status} />
        </button>
      ))}
    </Card>
  );
}

function ContentSheet({ bid, id, onClose }: { bid: string; id: string | null; onClose: () => void }) {
  const { data: item } = useApi<ContentItem>(id ? gpath(bid, `/content/${id}`) : null);
  const [texts, setTexts] = useState<Record<string, string>>({});
  // biome-ignore lint/correctness/useExhaustiveDependencies: deliberately resets local state when the selection changes
  useEffect(() => setTexts({}), [id]);
  const inv = [gpath(bid, "/content"), gpath(bid, "/posts"), `/api/v1/b/${bid}/decisions`];
  const saveVariants = useAction(
    () => Promise.all(Object.entries(texts).map(([vid, text]) => api.patch(gpath(bid, `/variants/${vid}`), { text }))),
    { invalidate: inv, success: "Saved", onSuccess: () => setTexts({}) },
  );
  const [pickingFor, setPickingFor] = useState<string | null>(null);
  const [testing, setTesting] = useState<Variant | null>(null);
  const [textB, setTextB] = useState("");
  const startTest = useAction(
    () => api.post(gpath(bid, "/experiments"), { content_id: id, platform: testing?.platform, text_b: textB }),
    {
      invalidate: [...inv, gpath(bid, "/experiments")],
      success: "Version B added. Approve the post and both go out, two days apart.",
      onSuccess: () => {
        setTesting(null);
        setTextB("");
      },
    },
  );
  const setMedia = useAction(
    ({
      vid,
      media,
    }: {
      vid: string;
      media: { key?: string; url?: string; kind?: string; alt_text?: string | null }[];
    }) => api.patch(gpath(bid, `/variants/${vid}`), { media }),
    { invalidate: inv, success: "Media updated" },
  );
  const regenerate = useAction(() => api.post(gpath(bid, `/content/${id}/regenerate`)), {
    invalidate: inv,
    success: "Rewriting. It will come back for review.",
  });
  const archive = useAction(() => api.post(gpath(bid, `/content/${id}/archive`)), {
    invalidate: inv,
    success: "Archived",
    onSuccess: onClose,
  });
  const retry = useAction((pid: string) => api.post(gpath(bid, `/posts/${pid}/retry`)), {
    invalidate: inv,
    success: "Retrying",
  });
  const editable = item && !["published", "archived"].includes(item.status);
  return (
    <Sheet
      open={!!id}
      onOpenChange={(o) => !o && onClose()}
      title={item?.title ?? "Loading…"}
      subtitle={
        item ? (
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={item.status} />
            <span>{sentence(item.pillar)}</span>
            {item.hook_type ? <span>· {sentence(item.hook_type)} hook</span> : null}
            {item.planned_for ? <span>· {formatDateTime(item.planned_for)}</span> : null}
          </span>
        ) : null
      }
      footer={
        item ? (
          <>
            {editable ? (
              <Button variant="ghost" icon={<Archive className="size-4" />} onClick={() => archive.mutate()}>
                Archive
              </Button>
            ) : null}
            {editable ? (
              <Button
                icon={<RefreshCw className="size-4" />}
                onClick={() => regenerate.mutate()}
                loading={regenerate.isPending}
              >
                Rewrite
              </Button>
            ) : null}
            {Object.keys(texts).length ? (
              <Button variant="primary" onClick={() => saveVariants.mutate()} loading={saveVariants.isPending}>
                Save changes
              </Button>
            ) : null}
            {item.decision_id && item.status === "needs_review" ? (
              <Link to={`/b/${bid}/decisions/${item.decision_id}` as string}>
                <Button variant="primary">Review & approve</Button>
              </Link>
            ) : null}
          </>
        ) : null
      }
    >
      {item ? (
        <div className="space-y-6">
          <div>
            <div className="mb-1 text-[13px] font-medium text-muted">Core idea</div>
            <p className="text-[14px] leading-relaxed">{item.core_idea}</p>
          </div>
          <ChecksList checks={item.checks as never} />
          {(item.variants ?? []).map((v) => (
            <div key={v.id} className="overflow-hidden rounded-[16px] ring-1 ring-hairline">
              <div className="flex items-center gap-2 px-4 py-2.5 hairline-b">
                <PlatformMark platform={v.platform} />
                <span className="text-[13px] font-medium">{v.platform_label}</span>
                <span className="text-[12px] text-muted">{v.format_label}</span>
                <span className="ml-auto flex items-center gap-2">
                  {item.status === "needs_review" && item.source_type !== "ab_test" ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={<FlaskConical className="size-3.5" />}
                      onClick={() => setTesting(v)}
                    >
                      A/B test
                    </Button>
                  ) : null}
                  <StatusBadge status={v.status} />
                </span>
              </div>
              <Textarea
                aria-label={`${v.platform_label} text`}
                rows={6}
                disabled={!editable}
                className="rounded-none border-0 focus:shadow-[inset_0_0_0_0_transparent]"
                value={texts[v.id] ?? v.text}
                onChange={(e) => setTexts({ ...texts, [v.id]: e.target.value })}
              />
              <div className="flex flex-wrap items-center gap-2 px-4 pb-3">
                {v.media.map((m, i) => (
                  <span
                    key={m.key ?? m.url ?? i}
                    className="group relative size-14 overflow-hidden rounded-[10px] bg-fill-2 ring-1 ring-hairline"
                  >
                    {m.kind === "image" && m.url ? (
                      <img src={m.url} alt="" className="size-full object-cover" />
                    ) : (
                      <span className="flex size-full items-center justify-center text-[10px] text-muted uppercase">
                        {m.kind ?? "file"}
                      </span>
                    )}
                    {editable ? (
                      <button
                        type="button"
                        aria-label="Remove media"
                        onClick={() => setMedia.mutate({ vid: v.id, media: v.media.filter((_, j) => j !== i) })}
                        className="absolute inset-0 hidden items-center justify-center bg-black/50 text-[11px] font-medium text-white group-hover:flex"
                      >
                        Remove
                      </button>
                    ) : null}
                  </span>
                ))}
                {editable && v.media.length < 10 ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    icon={<ImagePlus className="size-4" />}
                    onClick={() => setPickingFor(v.id)}
                  >
                    Add media
                  </Button>
                ) : null}
              </div>
              <div className="flex justify-between px-4 pb-2 text-[11px] text-faint tabular">
                <span>{aiLabelText(v.spec.ai_label) ?? ""}</span>
                <span>{(texts[v.id] ?? v.text).length} characters</span>
              </div>
              {v.spec.specs?.issues.length ? (
                <div className="px-4 pb-3">
                  <ChecksList checks={{ specs: v.spec.specs }} />
                </div>
              ) : null}
            </div>
          ))}
          <Dialog open={!!testing} onOpenChange={(o) => !o && setTesting(null)}>
            {testing ? (
              <DialogContent
                title={`A/B test on ${testing.platform_label}`}
                description="Write a different version. It posts at the same hour two days later, and we compare engagement per person reached. A winner is called only with clear evidence."
                wide
              >
                <div className="space-y-3">
                  <div className="rounded-[12px] bg-fill-2 p-3 text-[13px] text-text-2">
                    <div className="mb-1 font-medium text-text">Version A</div>
                    {testing.text}
                  </div>
                  <Textarea
                    aria-label="Version B"
                    rows={6}
                    placeholder="Version B: try a different opening, angle or call to action"
                    value={textB}
                    onChange={(e) => setTextB(e.target.value)}
                  />
                  <div className="flex justify-end">
                    <Button
                      variant="primary"
                      disabled={!textB.trim() || textB.trim() === testing.text.trim()}
                      loading={startTest.isPending}
                      onClick={() => startTest.mutate()}
                    >
                      Start test
                    </Button>
                  </div>
                </div>
              </DialogContent>
            ) : null}
          </Dialog>
          <MediaPicker
            bid={bid}
            open={!!pickingFor}
            setOpen={(o) => !o && setPickingFor(null)}
            onPick={(a) => {
              const v = item.variants?.find((x) => x.id === pickingFor);
              if (v)
                setMedia.mutate({
                  vid: v.id,
                  media: [
                    ...v.media.filter((m) => m.key !== a.storage_key),
                    { key: a.storage_key, kind: a.kind, alt_text: a.alt_text },
                  ],
                });
            }}
          />
          {item.posts?.length ? (
            <div>
              <h4 className="mb-2 text-[13px] font-semibold">Posts</h4>
              <ul className="divide-y divide-[var(--hairline)] rounded-[14px] ring-1 ring-hairline">
                {item.posts.map((p) => (
                  <li key={p.id} className="flex items-center gap-3 px-4 py-2.5 text-[13px]">
                    <PlatformMark platform={p.platform} size={20} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <StatusBadge status={p.status} />
                        {p.published_at ? <span className="text-muted">{formatDateTime(p.published_at)}</span> : null}
                      </div>
                      {p.last_error ? <div className="truncate text-[12px] text-critical">{p.last_error}</div> : null}
                      {Object.keys(p.metrics).length ? (
                        <div className="text-[12px] text-muted tabular">
                          {formatNumber(p.metrics.reach ?? 0)} reach · {formatNumber(p.metrics.engagements ?? 0)}{" "}
                          engagements · {formatNumber(p.metrics.clicks ?? 0)} clicks
                        </div>
                      ) : null}
                    </div>
                    {p.url ? (
                      <a
                        href={p.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-muted hover:text-accent"
                        aria-label="Open post"
                      >
                        <ExternalLink className="size-4" />
                      </a>
                    ) : null}
                    {p.status === "failed" ? (
                      <Button size="sm" icon={<RotateCcw className="size-3.5" />} onClick={() => retry.mutate(p.id)}>
                        Retry
                      </Button>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </Sheet>
  );
}

function NewPost({ bid, open, setOpen }: { bid: string; open: boolean; setOpen: (o: boolean) => void }) {
  const { data: channels } = useApi<{ platforms: PlatformInfo[] }>(gpath(bid, "/channels"));
  const { data: campaigns } = useApi<{ items: Campaign[] }>(gpath(bid, "/campaigns"));
  const connected =
    channels?.platforms.filter((p) => p.accounts.some((a) => ["connected", "practice"].includes(a.status))) ?? [];
  const [form, setForm] = useState({
    title: "",
    idea: "",
    intent: "value" as "value" | "ask",
    platforms: [] as string[],
    planned_for: "",
    campaign_id: "",
  });
  const create = useAction(
    () =>
      api.post(gpath(bid, "/content"), {
        ...form,
        campaign_id: form.campaign_id || null,
        planned_for: form.planned_for ? new Date(form.planned_for).toISOString() : null,
      }),
    {
      invalidate: [gpath(bid, "/content")],
      success: "Drafting for each platform. It will appear for review in a moment.",
      onSuccess: () => {
        setOpen(false);
        setForm({ ...form, title: "", idea: "" });
      },
    },
  );
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        title="Write a post"
        description="Give the idea. We adapt it for each platform, run the checks, and send it to you for approval."
        wide
      >
        <div className="space-y-4">
          <Field label="Title">
            {(id) => (
              <Input
                id={id}
                required
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
              />
            )}
          </Field>
          <Field label="The idea, in your words">
            {(id) => (
              <Textarea
                id={id}
                rows={4}
                value={form.idea}
                onChange={(e) => setForm({ ...form, idea: e.target.value })}
              />
            )}
          </Field>
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Type">
              {() => (
                <Segmented
                  value={form.intent}
                  onChange={(v) => setForm({ ...form, intent: v })}
                  options={[
                    { value: "value", label: "Give value" },
                    { value: "ask", label: "Ask" },
                  ]}
                />
              )}
            </Field>
            <Field label="When">
              {(id) => (
                <Input
                  id={id}
                  type="datetime-local"
                  value={form.planned_for}
                  onChange={(e) => setForm({ ...form, planned_for: e.target.value })}
                />
              )}
            </Field>
            <Field label="Campaign">
              {(id) => (
                <Select
                  id={id}
                  value={form.campaign_id}
                  onChange={(e) => setForm({ ...form, campaign_id: e.target.value })}
                >
                  <option value="">None</option>
                  {campaigns?.items
                    .filter((c) => c.status === "active")
                    .map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                </Select>
              )}
            </Field>
          </div>
          <div>
            <div className="mb-2 text-[13px] font-medium text-text-2">Platforms</div>
            {connected.length ? (
              <div className="flex flex-wrap gap-2">
                {connected.map((p) => {
                  const on = form.platforms.includes(p.key);
                  return (
                    <button
                      key={p.key}
                      type="button"
                      aria-pressed={on}
                      onClick={() =>
                        setForm({
                          ...form,
                          platforms: on ? form.platforms.filter((x) => x !== p.key) : [...form.platforms, p.key],
                        })
                      }
                      className={cn(
                        "flex items-center gap-2 rounded-full py-1 pr-3 pl-1 text-[13px] ring-1",
                        on ? "bg-accent-soft ring-accent" : "ring-hairline text-muted",
                      )}
                    >
                      <PlatformMark platform={p.key} size={20} />
                      {p.label}
                    </button>
                  );
                })}
              </div>
            ) : (
              <p className="text-[13px] text-muted">Connect a channel first.</p>
            )}
          </div>
          <div className="flex justify-end">
            <Button
              variant="primary"
              loading={create.isPending}
              disabled={!form.title || !form.platforms.length}
              onClick={() => create.mutate()}
            >
              Create drafts
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function Repurpose({ bid, open, setOpen }: { bid: string; open: boolean; setOpen: (o: boolean) => void }) {
  const [f, setF] = useState({ title: "", text: "", source_url: "", count: 5 });
  const run = useAction(
    () =>
      api.post<{ job_id: string | null; count: number }>(gpath(bid, "/content/repurpose"), {
        title: f.title,
        text: f.text,
        count: f.count,
        source_url: f.source_url || null,
      }),
    {
      invalidate: [gpath(bid, "/content")],
      success: (r) =>
        r.job_id
          ? `Splitting into ${r.count} posts. They'll appear in To review shortly.`
          : "Already split. Check To review.",
      onSuccess: () => {
        setOpen(false);
        setF({ title: "", text: "", source_url: "", count: 5 });
      },
    },
  );
  const chars = f.text.trim().length;
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        title="Repurpose a long piece"
        description="Paste an article, transcript or newsletter. We split it into distinct posts, each in your voice, using only what the piece says. You review every one."
        wide
      >
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Title">
              {(id) => <Input id={id} value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} />}
            </Field>
            <Field label="Link to the original (optional)">
              {(id) => (
                <Input
                  id={id}
                  type="url"
                  placeholder="https://"
                  value={f.source_url}
                  onChange={(e) => setF({ ...f, source_url: e.target.value })}
                />
              )}
            </Field>
          </div>
          <Field label="The piece" hint={`${chars.toLocaleString()} characters · at least 400`}>
            {(id) => (
              <Textarea id={id} rows={10} value={f.text} onChange={(e) => setF({ ...f, text: e.target.value })} />
            )}
          </Field>
          <div className="flex items-center justify-between gap-4">
            <label className="flex items-center gap-3 text-[14px]" htmlFor="count">
              Make
              <input
                id="count"
                type="range"
                min={2}
                max={10}
                value={f.count}
                onChange={(e) => setF({ ...f, count: Number(e.target.value) })}
                className="accent-[var(--accent)]"
              />
              <span className="w-16 tabular font-medium">{f.count} posts</span>
            </label>
            <Button variant="primary" disabled={chars < 400} loading={run.isPending} onClick={() => run.mutate()}>
              Split into posts
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function Content() {
  const bid = useBid();
  const search = useSearch({ strict: false }) as { view?: string; item?: string; new?: string; repurpose?: string };
  const navigate = useNavigate();
  const view = search.view ?? "calendar";
  const setSearch = (patch: Record<string, string | undefined>) =>
    navigate({ to: ".", search: { ...search, ...patch } as never, replace: true });
  return (
    <div>
      <PageHeader
        title="Content"
        subtitle="Planned from your campaigns, written in your voice, checked before you ever see it."
        actions={
          <>
            <Segmented
              value={view}
              onChange={(v) => setSearch({ view: v })}
              options={[
                { value: "calendar", label: "Calendar" },
                { value: "needs_review", label: "To review" },
                { value: "published", label: "Published" },
                { value: "all", label: "All" },
                { value: "library", label: "Library" },
              ]}
            />
            <Button icon={<Shuffle className="size-4" />} onClick={() => setSearch({ repurpose: "1" })}>
              Repurpose
            </Button>
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setSearch({ new: "1" })}>
              Write
            </Button>
          </>
        }
      />
      {view === "calendar" ? (
        <CalendarView bid={bid} onOpen={(id) => setSearch({ item: id })} />
      ) : view === "library" ? (
        <AssetGrid bid={bid} />
      ) : (
        <ListView bid={bid} status={view} onOpen={(id) => setSearch({ item: id })} />
      )}
      <ContentSheet bid={bid} id={search.item ?? null} onClose={() => setSearch({ item: undefined })} />
      <NewPost bid={bid} open={!!search.new} setOpen={(o) => setSearch({ new: o ? "1" : undefined })} />
      <Repurpose bid={bid} open={!!search.repurpose} setOpen={(o) => setSearch({ repurpose: o ? "1" : undefined })} />
    </div>
  );
}
