import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { AlertTriangle, Archive, FlaskConical, Inbox as InboxIcon, Send, User } from "lucide-react";
import { useEffect, useState } from "react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  PageHeader,
  PlatformMark,
  platformLabel,
  Segmented,
  StatusBadge,
  Textarea,
} from "@/components/ui";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDateTime, sentence, timeAgo } from "@/lib/format";
import { gpath, useAction, useApi, useBid } from "@/lib/queries";
import type { InboxItem } from "@/lib/types";
import { LoadError, Loading } from "./system";

type Item = InboxItem & { priority: number; decision_id: string | null; replied_at: string | null };

const CATEGORY_TONE: Record<string, "critical" | "warning" | "accent" | "neutral" | "good"> = {
  negative: "critical",
  sensitive: "critical",
  spam: "neutral",
  sales_intent: "good",
  common_question: "accent",
  support: "warning",
  praise: "good",
};

function sentimentLabel(s: number | null): string | null {
  if (s == null) return null;
  return s <= -0.3 ? "Negative" : s >= 0.3 ? "Positive" : "Neutral";
}

function Detail({ bid, item }: { bid: string; item: Item }) {
  const [text, setText] = useState(item.reply_text ?? item.ai_draft ?? "");
  // biome-ignore lint/correctness/useExhaustiveDependencies: deliberately resets local state when the selection changes
  useEffect(() => setText(item.reply_text ?? item.ai_draft ?? ""), [item.id, item.ai_draft, item.reply_text]);
  const inv = [gpath(bid, "/inbox"), `/api/v1/b/${bid}/decisions`];
  const reply = useAction(() => api.post(gpath(bid, `/inbox/${item.id}/reply`), { text }), {
    invalidate: inv,
    success: "Reply sent",
  });
  const status = useAction((s: string) => api.post(gpath(bid, `/inbox/${item.id}/status`), { status: s }), {
    invalidate: inv,
    success: (r) => `Marked ${(r as Item)?.status ?? "updated"}`,
  });
  const done = ["replied", "closed"].includes(item.status);
  const mood = sentimentLabel(item.sentiment);
  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-2 px-6 py-4 hairline-b">
        <PlatformMark platform={item.platform} size={28} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[15px] font-semibold">
            {item.author_name ?? (item.author_handle ? `@${item.author_handle}` : "Someone")}
          </div>
          <div className="text-[12px] text-muted">
            {sentence(item.kind)} on <span>{platformLabel(item.platform)}</span> · {formatDateTime(item.received_at)}
          </div>
        </div>
        {item.contact_id ? (
          <Link to={`/b/${bid}/audience?contact=${item.contact_id}` as string}>
            <Button size="sm" variant="ghost" icon={<User className="size-3.5" />}>
              Profile
            </Button>
          </Link>
        ) : null}
        {item.status !== "escalated" && !done ? (
          <Button
            size="sm"
            variant="ghost"
            icon={<AlertTriangle className="size-3.5" />}
            onClick={() => status.mutate("escalated")}
          >
            Escalate
          </Button>
        ) : null}
        {item.status !== "closed" ? (
          <Button
            size="sm"
            variant="ghost"
            icon={<Archive className="size-3.5" />}
            onClick={() => status.mutate("closed")}
          >
            Close
          </Button>
        ) : (
          <Button size="sm" onClick={() => status.mutate("open")}>
            Reopen
          </Button>
        )}
      </div>
      <div className="flex-1 space-y-5 overflow-y-auto px-6 py-5">
        <div className="flex flex-wrap gap-1.5">
          {item.category ? (
            <Badge tone={CATEGORY_TONE[item.category] ?? "neutral"}>{sentence(item.category)}</Badge>
          ) : null}
          {mood ? (
            <Badge tone={mood === "Negative" ? "critical" : mood === "Positive" ? "good" : "neutral"}>{mood}</Badge>
          ) : null}
          <StatusBadge status={item.status} />
        </div>
        <div className="max-w-[85%] rounded-[18px] rounded-tl-[6px] bg-fill-2 px-4 py-3 text-[15px] leading-relaxed">
          {item.text}
        </div>
        {item.reply_text && done ? (
          <div className="ml-auto max-w-[85%] rounded-[18px] rounded-tr-[6px] bg-accent px-4 py-3 text-[15px] leading-relaxed text-white">
            {item.reply_text}
            <div className="mt-1 text-[11px] text-white/70">{item.replied_at ? timeAgo(item.replied_at) : "sent"}</div>
          </div>
        ) : null}
        {item.decision_id && item.status === "drafted" ? (
          <p className="rounded-[12px] bg-accent-soft px-3.5 py-2.5 text-[13px]">
            A reply is waiting for approval.{" "}
            <Link to={`/b/${bid}/decisions/${item.decision_id}` as string} className="font-medium text-accent">
              Review it
            </Link>
            , or write your own below.
          </p>
        ) : null}
        {item.category === "sensitive" || item.category === "negative" ? (
          <p className="rounded-[12px] bg-[var(--critical-soft)] px-3.5 py-2.5 text-[13px] text-critical">
            This one needs a person. Automatic replies are off for negative and sensitive messages.
          </p>
        ) : null}
      </div>
      {!done ? (
        <div className="px-6 py-4 shadow-[inset_0_1px_0_var(--hairline)]">
          <Textarea
            rows={3}
            aria-label="Reply"
            placeholder="Write a reply…"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim()) reply.mutate();
            }}
          />
          <div className="mt-2 flex items-center justify-between">
            <span className="text-[12px] text-faint">
              {item.ai_draft && text === item.ai_draft ? "Draft written in your brand voice" : "⌘↩ to send"}
            </span>
            <Button
              variant="primary"
              icon={<Send className="size-4" />}
              disabled={!text.trim()}
              loading={reply.isPending}
              onClick={() => reply.mutate()}
            >
              Send {item.kind === "dm" ? "message" : "reply"}
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function Inbox() {
  const bid = useBid();
  const search = useSearch({ strict: false }) as { item?: string; filter?: string };
  const navigate = useNavigate();
  const filter = search.filter ?? "open,drafted,escalated";
  const { data, isLoading, error } = useApi<{ items: Item[]; counts: Record<string, number> }>(
    gpath(bid, `/inbox?status_filter=${filter}`),
    { refetchInterval: 20_000 },
  );
  const simulate = useAction(() => api.post(gpath(bid, "/inbox/simulate")), {
    invalidate: [gpath(bid, "/inbox")],
    success: "Added sample messages",
  });
  const items = data?.items ?? [];
  const selected = items.find((i) => i.id === search.item) ?? items[0];
  const set = (patch: Record<string, string | undefined>) =>
    navigate({ to: ".", search: { ...search, ...patch } as never, replace: true });
  const counts = data?.counts ?? {};
  return (
    <div>
      <PageHeader
        title="Inbox"
        subtitle="Comments, DMs and mentions from every channel. Triaged and drafted for you; anything touchy comes to a person."
        actions={
          <>
            <Segmented
              size="sm"
              value={filter}
              onChange={(v) => set({ filter: v, item: undefined })}
              options={[
                {
                  value: "open,drafted,escalated",
                  label: `Needs reply${(counts.open ?? 0) + (counts.drafted ?? 0) + (counts.escalated ?? 0) ? ` · ${(counts.open ?? 0) + (counts.drafted ?? 0) + (counts.escalated ?? 0)}` : ""}`,
                },
                { value: "escalated", label: "Escalated" },
                { value: "replied", label: "Replied" },
                { value: "closed", label: "Closed" },
              ]}
            />
            <Button
              size="sm"
              icon={<FlaskConical className="size-3.5" />}
              onClick={() => simulate.mutate()}
              loading={simulate.isPending}
            >
              Simulate
            </Button>
          </>
        }
      />
      {isLoading ? (
        <Loading />
      ) : error ? (
        <LoadError error={error} />
      ) : !items.length ? (
        <Card>
          <EmptyState
            icon={<InboxIcon className="size-6" />}
            title="Inbox zero"
            body="New comments and messages show up here within a minute."
          />
        </Card>
      ) : (
        <Card className="grid h-[calc(100dvh-220px)] min-h-[480px] overflow-hidden md:grid-cols-[340px_1fr]">
          <ul
            className={cn(
              "overflow-y-auto md:shadow-[inset_-1px_0_0_var(--hairline)]",
              search.item && "hidden md:block",
            )}
          >
            {items.map((i) => (
              <li key={i.id}>
                <button
                  type="button"
                  onClick={() => set({ item: i.id })}
                  className={cn(
                    "flex w-full gap-3 px-4 py-3 text-left hairline-b",
                    selected?.id === i.id ? "bg-accent-soft" : "hover:bg-fill-2",
                  )}
                >
                  <PlatformMark platform={i.platform} size={22} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="truncate text-[13px] font-semibold">
                        {i.author_name ?? `@${i.author_handle ?? "someone"}`}
                      </span>
                      <span className="shrink-0 text-[11px] text-faint">{timeAgo(i.received_at)}</span>
                    </div>
                    <div className="line-clamp-2 text-[13px] text-text-2">{i.text}</div>
                    <div className="mt-1 flex gap-1">
                      {i.category ? (
                        <Badge tone={CATEGORY_TONE[i.category] ?? "neutral"}>{sentence(i.category)}</Badge>
                      ) : null}
                      {i.status === "drafted" ? <Badge tone="accent">Draft ready</Badge> : null}
                      {i.status === "escalated" ? <Badge tone="critical">Escalated</Badge> : null}
                    </div>
                  </div>
                </button>
              </li>
            ))}
          </ul>
          <div className={cn("min-h-0", !search.item && "hidden md:block")}>
            {selected ? (
              <>
                <button
                  type="button"
                  className="px-4 pt-3 text-[14px] text-accent md:hidden"
                  onClick={() => set({ item: undefined })}
                >
                  ‹ All messages
                </button>
                <Detail bid={bid} item={selected} />
              </>
            ) : null}
          </div>
        </Card>
      )}
    </div>
  );
}
