/** Decision cards: the single place a founder says yes, no, or "yes, with this change". */
import { AlertTriangle, Check, ChevronRight, Clock, RotateCcw, ShieldCheck, X } from "lucide-react";
import { useEffect, useState } from "react";
import { aiLabelText, Badge, Button, LevelBadge, PlatformMark, platformLabel, Sheet, Textarea } from "@/components/ui";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDateTime, formatMoney, sentence, timeAgo } from "@/lib/format";
import { bpath, useAction, useApi } from "@/lib/queries";
import type { Check as CheckT, Decision } from "@/lib/types";

const INVALIDATE = (bid: string) => [
  bpath(bid, "/decisions"),
  bpath(bid, "/cockpit"),
  bpath(bid, "/gtm"),
  bpath(bid, "/notifications"),
];

interface PreviewVariant {
  id: string;
  platform: string;
  format: string;
  text: string;
  title?: string;
  thread?: string[];
  spec?: { brand?: CheckT; specs?: CheckT; ai_label?: string };
  ab?: "B";
}

/** Turn a forecast into readable rows: `*_minor` values are money, `currency` is a unit, not a row. */
export function forecastRows(forecast: Record<string, unknown>, fallbackCurrency: string | null): [string, string][] {
  const currency = typeof forecast.currency === "string" ? forecast.currency : (fallbackCurrency ?? "USD");
  return Object.entries(forecast)
    .filter(([k, v]) => k !== "currency" && v != null && typeof v !== "object")
    .map(([k, v]) => {
      if (k.endsWith("_minor") && typeof v === "number")
        return [sentence(k.replace(/_minor$/, "")), formatMoney(v, currency)];
      if (typeof v === "number") return [sentence(k), Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2)];
      return [sentence(k), String(v)];
    });
}

export function ChecksList({ checks }: { checks: Record<string, CheckT | string | undefined> | undefined }) {
  if (!checks) return null;
  const rows = Object.entries(checks).filter((e): e is [string, CheckT] => typeof e[1] === "object" && e[1] != null);
  return (
    <ul className="space-y-1.5">
      {rows.map(([key, c]) => {
        const tone = !c.passed || c.severity === "block" ? "critical" : c.severity === "review" ? "warning" : "good";
        return (
          <li key={key} className="flex items-start gap-2 text-[13px]">
            {tone === "good" ? (
              <ShieldCheck className="mt-px size-4 shrink-0 text-good" />
            ) : (
              <AlertTriangle
                className={cn("mt-px size-4 shrink-0", tone === "critical" ? "text-critical" : "text-warning")}
              />
            )}
            <span>
              <span className="font-medium capitalize">{key}</span>
              <span className="text-muted">{c.issues.length ? `: ${c.issues.join("; ")}` : ": passed"}</span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function DecisionRow({ d, onOpen }: { d: Decision; onOpen: () => void }) {
  const platform =
    (d.preview?.variants as PreviewVariant[] | undefined)?.[0]?.platform ??
    (d.action_type.includes("reply") ? undefined : undefined);
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group flex w-full items-center gap-4 px-5 py-3.5 text-left transition-colors hover:bg-fill-2"
    >
      <div className="flex -space-x-1.5">
        {((d.preview?.variants as PreviewVariant[] | undefined) ?? []).slice(0, 3).map((v) => (
          <PlatformMark key={v.id} platform={v.platform} size={24} />
        ))}
        {!platform ? (
          <span className="flex size-6 items-center justify-center rounded-[6px] bg-fill text-muted">
            <Check className="size-3.5" />
          </span>
        ) : null}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[14px] font-medium">{d.title}</div>
        <div className="truncate text-[13px] text-muted">{d.why_now ?? d.summary}</div>
      </div>
      {d.cost_minor ? (
        <span className="tabular text-[13px] text-text-2">{formatMoney(d.cost_minor, d.currency ?? "USD")}</span>
      ) : null}
      <span className="hidden text-[12px] text-faint sm:block">{timeAgo(d.created_at)}</span>
      <ChevronRight className="size-4 text-faint transition-transform group-hover:translate-x-0.5" />
    </button>
  );
}

export function DecisionSheet({ bid, id, onClose }: { bid: string; id: string | null; onClose: () => void }) {
  const { data: d } = useApi<Decision>(id ? bpath(bid, `/decisions/${id}`) : null);
  const { data: reasons } = useApi<{ code: string; label: string }[]>(bpath(bid, "/decisions/reasons"));
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [reply, setReply] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  // biome-ignore lint/correctness/useExhaustiveDependencies: deliberately resets local state when the selection changes
  useEffect(() => {
    setEdits({});
    setReply(null);
    setRejecting(false);
    setNote("");
  }, [id]);

  const approve = useAction(
    (option?: string) => {
      const patch: Record<string, unknown> = {};
      if (Object.keys(edits).length)
        patch.edits = Object.fromEntries(Object.entries(edits).map(([k, text]) => [k, { text }]));
      if (reply != null) patch.text = reply;
      return api.post(bpath(bid, `/decisions/${id}/approve`), {
        payload_patch: Object.keys(patch).length ? patch : null,
        option: option ?? null,
        note: note || null,
      });
    },
    {
      invalidate: INVALIDATE(bid),
      success: d?.kind === "manual_task" ? "Marked done" : "Approved",
      onSuccess: onClose,
    },
  );
  const reject = useAction(
    (code: string) => api.post(bpath(bid, `/decisions/${id}/reject`), { reason_code: code, note: note || null }),
    {
      invalidate: INVALIDATE(bid),
      success: "Declined. We'll learn from it.",
      onSuccess: onClose,
    },
  );
  const undo = useAction(() => api.post(bpath(bid, `/decisions/${id}/undo`), {}), {
    invalidate: INVALIDATE(bid),
    success: "Undone",
  });

  const variants = (d?.preview?.variants as PreviewVariant[] | undefined) ?? [];
  const incoming = d?.preview?.incoming as string | undefined;
  const pending = d?.status === "pending";

  return (
    <Sheet
      open={!!id}
      onOpenChange={(o) => !o && onClose()}
      title={d?.title ?? "Loading…"}
      subtitle={
        d ? (
          <span className="flex flex-wrap items-center gap-2">
            <LevelBadge level={d.level} label={d.level_label} />{" "}
            <span>{sentence(d.action_type.split(".").pop() ?? "")}</span> · <span>{timeAgo(d.created_at)}</span>
          </span>
        ) : null
      }
      footer={
        d && pending ? (
          rejecting ? (
            <div className="w-full space-y-3">
              <Textarea
                rows={2}
                placeholder="Optional note: what should change next time?"
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
              <div className="flex flex-wrap gap-2">
                {reasons?.map((r) => (
                  <Button
                    key={r.code}
                    size="sm"
                    onClick={() => reject.mutate(r.code)}
                    loading={reject.isPending && reject.variables === r.code}
                  >
                    {r.label}
                  </Button>
                ))}
                <Button size="sm" variant="ghost" onClick={() => setRejecting(false)}>
                  Cancel
                </Button>
              </div>
            </div>
          ) : d.kind === "choice" && d.options ? (
            d.options.map((o) => (
              <Button
                key={o.key}
                variant="secondary"
                onClick={() => approve.mutate(o.key)}
                loading={approve.isPending && approve.variables === o.key}
              >
                {o.label}
              </Button>
            ))
          ) : (
            <>
              <Button variant="ghost" icon={<X className="size-4" />} onClick={() => setRejecting(true)}>
                {d.kind === "manual_task" ? "Skip" : "Decline"}
              </Button>
              <Button
                variant="primary"
                icon={<Check className="size-4" />}
                onClick={() => approve.mutate(undefined)}
                loading={approve.isPending}
              >
                {d.kind === "manual_task"
                  ? "Mark done"
                  : Object.keys(edits).length || reply != null
                    ? "Approve with edits"
                    : d.kind === "promotion"
                      ? "Promote"
                      : "Approve"}
              </Button>
            </>
          )
        ) : d?.can_undo ? (
          <Button icon={<RotateCcw className="size-4" />} onClick={() => undo.mutate()} loading={undo.isPending}>
            Undo
          </Button>
        ) : null
      }
    >
      {!d ? null : (
        <div className="space-y-6">
          {d.summary ? <p className="text-[15px] leading-relaxed text-text-2">{d.summary}</p> : null}
          <dl className="grid grid-cols-1 gap-3 rounded-[14px] bg-fill-2 p-4 text-[13px] sm:grid-cols-2">
            {d.why_now ? (
              <div>
                <dt className="text-muted">Why now</dt>
                <dd className="mt-0.5 font-medium">{d.why_now}</dd>
              </div>
            ) : null}
            {d.cost_minor != null ? (
              <div>
                <dt className="text-muted">Cost</dt>
                <dd className="mt-0.5 font-medium tabular">{formatMoney(d.cost_minor, d.currency ?? "USD")}</dd>
              </div>
            ) : null}
            {d.if_no ? (
              <div>
                <dt className="text-muted">If you say no</dt>
                <dd className="mt-0.5">{d.if_no}</dd>
              </div>
            ) : null}
            {d.undo ? (
              <div>
                <dt className="text-muted">Undo</dt>
                <dd className="mt-0.5">{d.undo}</dd>
              </div>
            ) : null}
            {d.expires_at ? (
              <div>
                <dt className="text-muted">Expires</dt>
                <dd className="mt-0.5 flex items-center gap-1">
                  <Clock className="size-3.5" />
                  {formatDateTime(d.expires_at)}
                </dd>
              </div>
            ) : null}
            {d.forecast
              ? forecastRows(d.forecast, d.currency).map(([label, value]) => (
                  <div key={label}>
                    <dt className="text-muted">{label}</dt>
                    <dd className="mt-0.5 font-medium tabular">{value}</dd>
                  </div>
                ))
              : null}
          </dl>
          {d.reasons.length ? (
            <div>
              <h3 className="mb-2 text-[13px] font-semibold">Why this needs you</h3>
              <ul className="space-y-1 text-[13px] text-text-2">
                {d.reasons.map((r) => (
                  <li key={r.code}>· {r.message}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {incoming ? (
            <div className="space-y-3">
              <div className="rounded-[14px] rounded-tl-[4px] bg-fill-2 px-4 py-3 text-[14px]">{incoming}</div>
              <div>
                <label htmlFor="reply" className="mb-1.5 block text-[13px] font-medium text-text-2">
                  Your reply
                </label>
                <Textarea
                  id="reply"
                  rows={4}
                  disabled={!pending}
                  value={reply ?? String(d.payload.text ?? "")}
                  onChange={(e) => setReply(e.target.value)}
                />
              </div>
            </div>
          ) : null}

          {variants.length ? (
            <div className="space-y-4">
              <ChecksList checks={(d.preview?.checks as Record<string, CheckT>) ?? undefined} />
              {variants.map((v) => (
                <div key={v.id} className="overflow-hidden rounded-[16px] ring-1 ring-hairline">
                  <div className="flex items-center gap-2 px-4 py-2.5 hairline-b">
                    <PlatformMark platform={v.platform} />
                    <span className="text-[13px] font-medium">{platformLabel(v.platform)}</span>
                    {v.ab ? <Badge tone="accent">Version B · posts 2 days later</Badge> : null}
                    <span className="text-[12px] text-muted">{sentence(v.format)}</span>
                    {aiLabelText(v.spec?.ai_label) ? (
                      <Badge className="ml-auto">{aiLabelText(v.spec?.ai_label)}</Badge>
                    ) : null}
                  </div>
                  <Textarea
                    aria-label={`${v.platform} text`}
                    rows={6}
                    disabled={!pending}
                    className="rounded-none border-0 focus:shadow-[inset_0_0_0_0_transparent]"
                    value={edits[v.id] ?? v.text}
                    onChange={(e) => setEdits({ ...edits, [v.id]: e.target.value })}
                  />
                  {v.spec?.specs?.issues.length ? (
                    <div className="px-4 pb-3">
                      <ChecksList checks={{ specs: v.spec.specs }} />
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          ) : null}

          {!pending ? (
            <div className="rounded-[14px] bg-fill-2 p-4 text-[13px]">
              <div className="font-medium capitalize">
                {d.status.replace(/_/g, " ")}
                {d.decided_at ? ` · ${formatDateTime(d.decided_at)}` : ""}
              </div>
              {d.reason_code ? <div className="text-muted">Reason: {sentence(d.reason_code)}</div> : null}
              {d.note ? <div className="text-muted">Note: {d.note}</div> : null}
              {d.result ? (
                <pre className="mt-2 overflow-x-auto font-mono text-[11px] text-muted">
                  {JSON.stringify(d.result, null, 2)}
                </pre>
              ) : null}
            </div>
          ) : null}
        </div>
      )}
    </Sheet>
  );
}
