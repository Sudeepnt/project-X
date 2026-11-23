import { CheckCircle2, PenLine, Plus, ShieldCheck, Sparkles, Trash2, Wand2 } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { ChecksList } from "@/components/decision";
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
  Switch,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Textarea,
} from "@/components/ui";
import { api } from "@/lib/api";
import { formatDate, sentence } from "@/lib/format";
import { gpath, useAction, useApi, useBid } from "@/lib/queries";
import type { Check } from "@/lib/types";
import { LoadError, Loading } from "./system";

interface Book {
  id: string;
  version: number;
  status: "draft" | "approved" | "archived";
  summary: string;
  voice: {
    tone?: string[];
    personality?: string;
    emoji_policy?: string;
    sample_lines?: string[];
    reading_level?: string;
    vocabulary_use?: string[];
    vocabulary_avoid?: string[];
  };
  offer: { summary?: string; value_proposition?: string; products?: { name: string; price?: string }[] };
  audience_summary: string;
  positioning: {
    statement?: string;
    differentiators?: string[];
    objections?: ({ objection: string; answer: string } | string)[];
  };
  do_list: string[];
  dont_list: string[];
  banned_words: string[];
  hashtags: string[];
  proof_points: { claim: string; source?: string }[];
  approved_at: string | null;
  created_at: string;
}
interface Audience {
  id: string;
  name: string;
  is_primary: boolean;
  description: string;
  segment: "b2c" | "b2b";
  pains: string[];
  desires: string[];
  objections: string[];
  buying_triggers: string[];
  platform_affinity: Record<string, number>;
}
interface Theme {
  id: string;
  name: string;
  pillar: string;
  description: string;
  weight: number;
  active: boolean;
}

const PILLARS = ["educate", "entertain", "inspire", "behind_the_scenes", "proof", "offer"];

function ListEditor({
  label,
  value,
  onChange,
  disabled,
  hint,
}: {
  label: string;
  value: string[];
  onChange: (v: string[]) => void;
  disabled?: boolean;
  hint?: string;
}) {
  return (
    <Field label={label} hint={hint ?? "One per line"}>
      {(id) => (
        <Textarea
          id={id}
          rows={Math.min(Math.max(value.length + 1, 3), 8)}
          disabled={disabled}
          value={value.join("\n")}
          onChange={(e) =>
            onChange(
              e.target.value
                .split("\n")
                .map((s) => s.trimStart())
                .filter((s, i, a) => s || i === a.length - 1),
            )
          }
        />
      )}
    </Field>
  );
}

function Section({ title, children, icon }: { title: string; children: ReactNode; icon?: ReactNode }) {
  return (
    <Card className="p-5">
      <h3 className="mb-4 flex items-center gap-2 text-[16px] font-semibold">
        {icon}
        {title}
      </h3>
      <div className="space-y-4">{children}</div>
    </Card>
  );
}

function BookEditor({ bid, book, editable }: { bid: string; book: Book; editable: boolean }) {
  const [b, setB] = useState(book);
  useEffect(() => setB(book), [book]);
  const dirty = JSON.stringify(b) !== JSON.stringify(book);
  const inv = [gpath(bid, "/brand-book")];
  const save = useAction(
    () =>
      api.patch(gpath(bid, `/brand-book/${book.id}`), {
        summary: b.summary,
        voice: b.voice,
        offer: b.offer,
        audience_summary: b.audience_summary,
        positioning: {
          ...b.positioning,
          differentiators: (b.positioning.differentiators ?? []).filter(Boolean),
          objections: (b.positioning.objections ?? []).filter((o) => (typeof o === "string" ? o : o.objection)),
        },
        do_list: b.do_list.filter(Boolean),
        dont_list: b.dont_list.filter(Boolean),
        banned_words: b.banned_words.filter(Boolean),
        hashtags: b.hashtags.filter(Boolean),
      }),
    { invalidate: inv, success: "Draft saved" },
  );
  const approve = useAction(
    async () => {
      if (dirty) await save.mutateAsync();
      return api.post(gpath(bid, `/brand-book/${book.id}/approve`));
    },
    {
      invalidate: [...inv, gpath(bid, "/content")],
      success: "Approved. Every post is now checked against this version.",
    },
  );
  const voice = b.voice;
  const setVoice = (patch: Partial<Book["voice"]>) => setB({ ...b, voice: { ...voice, ...patch } });
  const objections = (b.positioning.objections ?? []).map((o) =>
    typeof o === "string" ? o : `${o.objection} → ${o.answer}`,
  );
  return (
    <div className="space-y-5">
      {editable ? (
        <div className="glass sticky top-14 z-10 -mx-1 flex flex-wrap items-center gap-3 rounded-[14px] px-4 py-3 ring-1 ring-hairline">
          <Badge tone="warning">Draft v{book.version}</Badge>
          <span className="flex-1 text-[13px] text-muted">
            Nothing changes until you approve. The current version stays in use.
          </span>
          {dirty ? (
            <Button size="sm" onClick={() => save.mutate()} loading={save.isPending}>
              Save draft
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="primary"
            icon={<CheckCircle2 className="size-4" />}
            onClick={() => approve.mutate()}
            loading={approve.isPending}
          >
            Approve v{book.version}
          </Button>
        </div>
      ) : null}
      <Section title="Who you are">
        <Field label="Summary">
          {(id) => (
            <Textarea
              id={id}
              rows={3}
              disabled={!editable}
              value={b.summary}
              onChange={(e) => setB({ ...b, summary: e.target.value })}
            />
          )}
        </Field>
        <Field label="Value proposition">
          {(id) => (
            <Textarea
              id={id}
              rows={2}
              disabled={!editable}
              value={b.offer.value_proposition ?? ""}
              onChange={(e) => setB({ ...b, offer: { ...b.offer, value_proposition: e.target.value } })}
            />
          )}
        </Field>
        <Field label="Positioning statement">
          {(id) => (
            <Textarea
              id={id}
              rows={3}
              disabled={!editable}
              value={b.positioning.statement ?? ""}
              onChange={(e) => setB({ ...b, positioning: { ...b.positioning, statement: e.target.value } })}
            />
          )}
        </Field>
        <div className="grid gap-4 md:grid-cols-2">
          <ListEditor
            label="What makes you different"
            disabled={!editable}
            value={b.positioning.differentiators ?? []}
            onChange={(v) => setB({ ...b, positioning: { ...b.positioning, differentiators: v } })}
          />
          <ListEditor
            label="Objections and answers"
            hint="One per line: objection → answer"
            disabled={!editable}
            value={objections}
            onChange={(v) =>
              setB({
                ...b,
                positioning: {
                  ...b.positioning,
                  objections: v.map((line) => {
                    const [objection = "", answer = ""] = line.split(/\s*(?:→|->)\s*/);
                    return { objection, answer };
                  }),
                },
              })
            }
          />
        </div>
      </Section>
      <Section title="How you sound">
        <Field label="Personality">
          {(id) => (
            <Textarea
              id={id}
              rows={2}
              disabled={!editable}
              value={voice.personality ?? ""}
              onChange={(e) => setVoice({ personality: e.target.value })}
            />
          )}
        </Field>
        <div className="grid gap-4 md:grid-cols-3">
          <Field label="Tone (comma separated)">
            {(id) => (
              <Input
                id={id}
                disabled={!editable}
                value={(voice.tone ?? []).join(", ")}
                onChange={(e) =>
                  setVoice({
                    tone: e.target.value
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean),
                  })
                }
              />
            )}
          </Field>
          <Field label="Reading level">
            {(id) => (
              <Input
                id={id}
                disabled={!editable}
                value={voice.reading_level ?? ""}
                onChange={(e) => setVoice({ reading_level: e.target.value })}
              />
            )}
          </Field>
          <Field label="Emoji">
            {(id) => (
              <Select
                id={id}
                disabled={!editable}
                value={voice.emoji_policy ?? "sparing"}
                onChange={(e) => setVoice({ emoji_policy: e.target.value })}
              >
                {["none", "sparing", "liberal"].map((o) => (
                  <option key={o} value={o}>
                    {sentence(o)}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <ListEditor
            label="Words we use"
            disabled={!editable}
            value={voice.vocabulary_use ?? []}
            onChange={(v) => setVoice({ vocabulary_use: v })}
          />
          <ListEditor
            label="Words we avoid"
            disabled={!editable}
            value={voice.vocabulary_avoid ?? []}
            onChange={(v) => setVoice({ vocabulary_avoid: v })}
          />
        </div>
        <ListEditor
          label="Sample lines in our voice"
          disabled={!editable}
          value={voice.sample_lines ?? []}
          onChange={(v) => setVoice({ sample_lines: v })}
        />
      </Section>
      <Section title="Rules" icon={<ShieldCheck className="size-4 text-good" />}>
        <div className="grid gap-4 md:grid-cols-2">
          <ListEditor
            label="Always"
            disabled={!editable}
            value={b.do_list}
            onChange={(v) => setB({ ...b, do_list: v })}
          />
          <ListEditor
            label="Never"
            disabled={!editable}
            value={b.dont_list}
            onChange={(v) => setB({ ...b, dont_list: v })}
          />
          <ListEditor
            label="Banned words"
            hint="Posts containing these are blocked."
            disabled={!editable}
            value={b.banned_words}
            onChange={(v) => setB({ ...b, banned_words: v })}
          />
          <ListEditor
            label="Hashtags"
            disabled={!editable}
            value={b.hashtags}
            onChange={(v) => setB({ ...b, hashtags: v })}
          />
        </div>
      </Section>
    </div>
  );
}

function CheckTool({ bid }: { bid: string }) {
  const [text, setText] = useState("");
  const check = useAction(() => api.post<{ brand: Check; claims: Check }>(gpath(bid, "/brand-book/check"), { text }));
  return (
    <Card className="p-5">
      <h3 className="mb-1 flex items-center gap-2 text-[16px] font-semibold">
        <Wand2 className="size-4 text-accent" />
        Check any text
      </h3>
      <p className="mb-3 text-[13px] text-muted">
        Paste a caption, an email, an ad. We check voice, banned words and claims that need proof.
      </p>
      <Textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} />
      <div className="mt-3 flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">{check.data ? <ChecksList checks={check.data as never} /> : null}</div>
        <Button
          variant="primary"
          size="sm"
          disabled={!text.trim()}
          loading={check.isPending}
          onClick={() => check.mutate()}
        >
          Check
        </Button>
      </div>
    </Card>
  );
}

function Audiences({ bid }: { bid: string }) {
  const { data, isLoading } = useApi<Audience[]>(gpath(bid, "/audiences"));
  const [edit, setEdit] = useState<Audience | null>(null);
  const blank: Audience = {
    id: "",
    name: "",
    is_primary: false,
    description: "",
    segment: "b2c",
    pains: [],
    desires: [],
    objections: [],
    buying_triggers: [],
    platform_affinity: {},
  };
  const save = useAction(
    (a: Audience) => {
      const { id, ...body } = a;
      return id ? api.put(gpath(bid, `/audiences/${id}`), body) : api.post(gpath(bid, "/audiences"), body);
    },
    { invalidate: [gpath(bid, "/audiences")], success: "Audience saved", onSuccess: () => setEdit(null) },
  );
  const remove = useAction((id: string) => api.delete(gpath(bid, `/audiences/${id}`)), {
    invalidate: [gpath(bid, "/audiences")],
    success: "Removed",
  });
  if (isLoading) return <Loading />;
  return (
    <div>
      <div className="mb-4 flex justify-end">
        <Button icon={<Plus className="size-4" />} onClick={() => setEdit(blank)}>
          Add audience
        </Button>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        {data?.map((a) => (
          <Card key={a.id} className="p-5">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h3 className="text-[16px] font-semibold">{a.name}</h3>
                <div className="text-[12px] text-muted uppercase">{a.segment}</div>
              </div>
              {a.is_primary ? <Badge tone="accent">Primary</Badge> : null}
            </div>
            <p className="mt-2 text-[13px] text-text-2">{a.description}</p>
            <dl className="mt-3 grid grid-cols-2 gap-3 text-[12px]">
              {(["pains", "desires", "objections", "buying_triggers"] as const).map((k) => (
                <div key={k}>
                  <dt className="font-medium text-muted">{sentence(k)}</dt>
                  <dd className="mt-0.5 text-text-2">{a[k].slice(0, 3).join(" · ") || "—"}</dd>
                </div>
              ))}
            </dl>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {Object.entries(a.platform_affinity)
                .sort((x, y) => y[1] - x[1])
                .slice(0, 5)
                .map(([p, v]) => (
                  <span
                    key={p}
                    className="flex items-center gap-1 rounded-full bg-fill-2 py-0.5 pr-2 pl-0.5 text-[11px] tabular"
                  >
                    <PlatformMark platform={p} size={16} />
                    {Math.round(v * 100)}
                  </span>
                ))}
            </div>
            <div className="mt-4 flex gap-2">
              <Button size="sm" onClick={() => setEdit(a)}>
                Edit
              </Button>
              {!a.is_primary ? (
                <Button
                  size="sm"
                  variant="ghost"
                  icon={<Trash2 className="size-3.5" />}
                  onClick={() => remove.mutate(a.id)}
                >
                  Remove
                </Button>
              ) : null}
            </div>
          </Card>
        ))}
      </div>
      <Dialog open={!!edit} onOpenChange={(o) => !o && setEdit(null)}>
        {edit ? (
          <DialogContent title={edit.id ? `Edit ${edit.name}` : "New audience"} wide>
            <div className="space-y-4">
              <div className="grid grid-cols-[1fr_auto] gap-3">
                <Field label="Name">
                  {(id) => (
                    <Input id={id} value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
                  )}
                </Field>
                <Field label="Type">
                  {(id) => (
                    <Select
                      id={id}
                      value={edit.segment}
                      onChange={(e) => setEdit({ ...edit, segment: e.target.value as "b2c" | "b2b" })}
                    >
                      <option value="b2c">Consumers</option>
                      <option value="b2b">Businesses</option>
                    </Select>
                  )}
                </Field>
              </div>
              <Field label="Who they are">
                {(id) => (
                  <Textarea
                    id={id}
                    rows={2}
                    value={edit.description}
                    onChange={(e) => setEdit({ ...edit, description: e.target.value })}
                  />
                )}
              </Field>
              <div className="grid gap-4 sm:grid-cols-2">
                {(["pains", "desires", "objections", "buying_triggers"] as const).map((k) => (
                  <ListEditor
                    key={k}
                    label={sentence(k)}
                    value={edit[k]}
                    onChange={(v) => setEdit({ ...edit, [k]: v })}
                  />
                ))}
              </div>
              <div className="flex items-center gap-3 text-[14px]">
                <Switch
                  label="Primary audience"
                  checked={edit.is_primary}
                  onCheckedChange={(v) => setEdit({ ...edit, is_primary: v })}
                />
                Primary audience
              </div>
              <div className="flex justify-end">
                <Button
                  variant="primary"
                  disabled={!edit.name}
                  loading={save.isPending}
                  onClick={() =>
                    save.mutate({
                      ...edit,
                      pains: edit.pains.filter(Boolean),
                      desires: edit.desires.filter(Boolean),
                      objections: edit.objections.filter(Boolean),
                      buying_triggers: edit.buying_triggers.filter(Boolean),
                    })
                  }
                >
                  Save
                </Button>
              </div>
            </div>
          </DialogContent>
        ) : null}
      </Dialog>
    </div>
  );
}

function Themes({ bid }: { bid: string }) {
  const { data, isLoading } = useApi<Theme[]>(gpath(bid, "/themes"));
  const [edit, setEdit] = useState<Theme | null>(null);
  const save = useAction(
    (t: Theme) => {
      const { id, ...body } = t;
      return id ? api.put(gpath(bid, `/themes/${id}`), body) : api.post(gpath(bid, "/themes"), body);
    },
    { invalidate: [gpath(bid, "/themes")], success: "Theme saved", onSuccess: () => setEdit(null) },
  );
  if (isLoading) return <Loading />;
  return (
    <div>
      <div className="mb-4 flex items-center justify-between gap-4">
        <p className="text-[13px] text-muted">
          The recurring topics your content rotates through. Weight sets how often each comes up.
        </p>
        <Button
          icon={<Plus className="size-4" />}
          onClick={() => setEdit({ id: "", name: "", pillar: "educate", description: "", weight: 1, active: true })}
        >
          Add theme
        </Button>
      </div>
      <Card className="divide-y divide-[var(--hairline)] overflow-hidden">
        {data?.map((t) => (
          <div key={t.id} className="flex items-center gap-4 px-5 py-3">
            <div className="min-w-0 flex-1">
              <div className="text-[14px] font-medium">{t.name}</div>
              <div className="truncate text-[12px] text-muted">
                {sentence(t.pillar)} · {t.description}
              </div>
            </div>
            <span className="text-[12px] text-muted tabular">×{t.weight}</span>
            <Switch
              label={`${t.name} active`}
              checked={t.active}
              onCheckedChange={(v) => save.mutate({ ...t, active: v })}
            />
            <Button
              size="sm"
              variant="ghost"
              icon={<PenLine className="size-3.5" />}
              aria-label={`Edit ${t.name}`}
              onClick={() => setEdit(t)}
            />
          </div>
        ))}
      </Card>
      <Dialog open={!!edit} onOpenChange={(o) => !o && setEdit(null)}>
        {edit ? (
          <DialogContent title={edit.id ? "Edit theme" : "New theme"}>
            <div className="space-y-4">
              <Field label="Name">
                {(id) => (
                  <Input id={id} value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
                )}
              </Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Pillar">
                  {(id) => (
                    <Select id={id} value={edit.pillar} onChange={(e) => setEdit({ ...edit, pillar: e.target.value })}>
                      {PILLARS.map((p) => (
                        <option key={p} value={p}>
                          {sentence(p)}
                        </option>
                      ))}
                    </Select>
                  )}
                </Field>
                <Field label="Weight">
                  {(id) => (
                    <Input
                      id={id}
                      type="number"
                      min={0}
                      max={5}
                      step={0.1}
                      value={edit.weight}
                      onChange={(e) => setEdit({ ...edit, weight: Number(e.target.value) })}
                    />
                  )}
                </Field>
              </div>
              <Field label="What it covers">
                {(id) => (
                  <Textarea
                    id={id}
                    rows={3}
                    value={edit.description}
                    onChange={(e) => setEdit({ ...edit, description: e.target.value })}
                  />
                )}
              </Field>
              <div className="flex justify-end">
                <Button
                  variant="primary"
                  disabled={!edit.name}
                  loading={save.isPending}
                  onClick={() => save.mutate(edit)}
                >
                  Save
                </Button>
              </div>
            </div>
          </DialogContent>
        ) : null}
      </Dialog>
    </div>
  );
}

export function Brand() {
  const bid = useBid();
  const [tab, setTab] = useState("book");
  const [waiting, setWaiting] = useState(false);
  const { data, isLoading, error, refetch } = useApi<{ current: Book | null; draft: Book | null }>(
    gpath(bid, "/brand-book"),
    { refetchInterval: waiting ? 2500 : undefined },
  );
  const { data: versions } = useApi<
    { id: string; version: number; status: string; created_at: string; approved_at: string | null }[]
  >(gpath(bid, "/brand-book/versions"));
  const [redraft, setRedraft] = useState(false);
  const [src, setSrc] = useState({ website: "", notes: "" });
  const inv = [gpath(bid, "/brand-book")];
  const startEdit = useAction(() => api.post(gpath(bid, "/brand-book/edit")), { invalidate: inv });
  const draft = useAction(
    () => api.post(gpath(bid, "/brand-book/draft"), { website: src.website || null, notes: src.notes || null }),
    {
      success: "Reading and drafting. This takes about a minute.",
      onSuccess: () => {
        setRedraft(false);
        setWaiting(true);
      },
    },
  );
  const draftCount = data?.draft?.id;
  useEffect(() => {
    if (waiting && draftCount) setWaiting(false);
  }, [waiting, draftCount]);

  return (
    <div>
      <PageHeader
        title="Brand"
        subtitle="The source of truth for how you look, sound and what you promise. Every piece of content is checked against it."
        actions={
          <>
            <Button icon={<Sparkles className="size-4" />} onClick={() => setRedraft(true)}>
              Redraft with AI
            </Button>
            {data?.current && !data.draft ? (
              <Button
                variant="primary"
                icon={<PenLine className="size-4" />}
                onClick={() => startEdit.mutate()}
                loading={startEdit.isPending}
              >
                Edit
              </Button>
            ) : null}
          </>
        }
      />
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="book">Brand book</TabsTrigger>
          <TabsTrigger value="audiences">Audiences</TabsTrigger>
          <TabsTrigger value="themes">Themes</TabsTrigger>
          <TabsTrigger value="check">Check text</TabsTrigger>
        </TabsList>
        <TabsContent value="book">
          {isLoading ? (
            <Loading rows={4} />
          ) : error ? (
            <LoadError error={error} retry={refetch} />
          ) : data?.draft ? (
            <BookEditor bid={bid} book={data.draft} editable />
          ) : data?.current ? (
            <div className="grid gap-6 lg:grid-cols-[1fr_240px]">
              <BookEditor bid={bid} book={data.current} editable={false} />
              <Card className="h-fit p-5 text-[13px]">
                <div className="mb-3 flex items-center gap-2 font-semibold">
                  <CheckCircle2 className="size-4 text-good" />
                  Version {data.current.version} in use
                </div>
                <p className="mb-4 text-muted">
                  Approved {formatDate(data.current.approved_at, { day: "numeric", month: "long", year: "numeric" })}
                </p>
                <div className="mb-2 font-medium">History</div>
                <ul className="space-y-1.5">
                  {versions?.map((v) => (
                    <li key={v.id} className="flex justify-between text-muted">
                      <span>v{v.version}</span>
                      <span className="capitalize">
                        {v.status} · {formatDate(v.approved_at ?? v.created_at)}
                      </span>
                    </li>
                  ))}
                </ul>
              </Card>
            </div>
          ) : (
            <Card>
              <EmptyState
                icon={<Sparkles className="size-6" />}
                title={waiting ? "Drafting your brand book…" : "No brand book yet"}
                body={
                  waiting
                    ? "We're reading your site and writing a first version."
                    : "We draft it from your website and notes. You review and approve."
                }
                action={
                  !waiting ? (
                    <Button variant="primary" onClick={() => setRedraft(true)}>
                      Draft it
                    </Button>
                  ) : null
                }
              />
            </Card>
          )}
        </TabsContent>
        <TabsContent value="audiences">
          <Audiences bid={bid} />
        </TabsContent>
        <TabsContent value="themes">
          <Themes bid={bid} />
        </TabsContent>
        <TabsContent value="check">
          <CheckTool bid={bid} />
        </TabsContent>
      </Tabs>
      <Dialog open={redraft} onOpenChange={setRedraft}>
        <DialogContent
          title="Draft a brand book"
          description="Creates a new draft. Your approved version stays in use until you approve the new one."
        >
          <div className="space-y-4">
            <Field label="Website">
              {(id) => (
                <Input
                  id={id}
                  type="url"
                  placeholder="https://"
                  value={src.website}
                  onChange={(e) => setSrc({ ...src, website: e.target.value })}
                />
              )}
            </Field>
            <Field label="Notes">
              {(id) => (
                <Textarea
                  id={id}
                  rows={4}
                  value={src.notes}
                  onChange={(e) => setSrc({ ...src, notes: e.target.value })}
                />
              )}
            </Field>
            <div className="flex justify-end">
              <Button variant="primary" loading={draft.isPending} onClick={() => draft.mutate()}>
                Draft
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
