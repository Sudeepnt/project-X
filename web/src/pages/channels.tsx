import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { Bot, ExternalLink, FlaskConical, KeyRound, Link2, MoreHorizontal, RefreshCw, RotateCcw } from "lucide-react";
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
  platformLabel,
  Segmented,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui";
import { ask } from "@/components/ui/confirm";
import { api, errorMessage } from "@/lib/api";
import { formatCompact, formatDateTime, formatNumber } from "@/lib/format";
import { gpath, useAction, useApi, useBid } from "@/lib/queries";
import type { Account, PlatformInfo, Post } from "@/lib/types";
import { LoadError, Loading } from "./system";

const OAUTH_ERRORS: Record<string, string> = {
  denied: "The connection was cancelled on the platform.",
  expired: "The connection took too long. Please try again.",
  mismatch: "That connection link wasn't started from this browser. Please try again.",
  connect_failed: "The platform didn't accept the connection. Check the account's permissions and try again.",
  unknown_business: "That business no longer exists.",
};

const TOKEN_FIELDS: Record<
  string,
  {
    credentials: { key: string; label: string; hint?: string }[];
    settings: { key: string; label: string; hint?: string }[];
  }
> = {
  telegram: {
    credentials: [
      { key: "bot_token", label: "Bot token", hint: "From @BotFather. Add the bot as an admin of your channel." },
    ],
    settings: [{ key: "chat_id", label: "Channel", hint: "@yourchannel or the numeric chat id." }],
  },
  whatsapp: {
    credentials: [
      {
        key: "access_token",
        label: "Access token",
        hint: "A permanent system-user token from Meta Business settings.",
      },
    ],
    settings: [
      { key: "phone_number_id", label: "Phone number ID", hint: "WhatsApp Manager → Phone numbers (digits only)." },
    ],
  },
  discord: {
    credentials: [{ key: "webhook_url", label: "Webhook URL", hint: "Channel settings → Integrations → Webhooks." }],
    settings: [],
  },
};

const POSTING: Record<string, { label: string; tone: "good" | "warning" | "neutral" }> = {
  instant: { label: "Auto-posting", tone: "good" },
  review_required: { label: "Needs app review", tone: "warning" },
  unaudited_private: { label: "Private until audited", tone: "warning" },
  manual_only: { label: "You post, we prepare", tone: "neutral" },
};

function TokenDialog({ bid, platform, onClose }: { bid: string; platform: PlatformInfo | null; onClose: () => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  // biome-ignore lint/correctness/useExhaustiveDependencies: deliberately resets local state when the selection changes
  useEffect(() => setValues({}), [platform]);
  const fields = platform ? TOKEN_FIELDS[platform.key] : undefined;
  const connect = useAction(
    () =>
      api.post(gpath(bid, "/channels/token"), {
        platform: platform?.key,
        credentials: Object.fromEntries((fields?.credentials ?? []).map((f) => [f.key, values[f.key] ?? ""])),
        settings: Object.fromEntries((fields?.settings ?? []).map((f) => [f.key, values[f.key] ?? ""])),
      }),
    { invalidate: [gpath(bid, "/channels")], success: `${platform?.label} connected`, onSuccess: onClose },
  );
  return (
    <Dialog open={!!platform} onOpenChange={(o) => !o && onClose()}>
      {platform && fields ? (
        <DialogContent
          title={`Connect ${platform.label}`}
          description="Credentials are encrypted in your vault and never shown again."
        >
          <div className="space-y-4">
            {[...fields.credentials, ...fields.settings].map((f) => (
              <Field key={f.key} label={f.label} hint={f.hint}>
                {(id) => (
                  <Input
                    id={id}
                    autoComplete="off"
                    type={f.key.includes("token") ? "password" : "text"}
                    value={values[f.key] ?? ""}
                    onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
                  />
                )}
              </Field>
            ))}
            <div className="flex justify-end">
              <Button variant="primary" loading={connect.isPending} onClick={() => connect.mutate()}>
                Connect
              </Button>
            </div>
          </div>
        </DialogContent>
      ) : null}
    </Dialog>
  );
}

function AccountRow({ bid, a }: { bid: string; a: Account }) {
  const remove = useAction(() => api.delete(gpath(bid, `/channels/accounts/${a.id}`)), {
    invalidate: [gpath(bid, "/channels")],
    success: "Disconnected",
  });
  return (
    <li className="flex items-center gap-3 py-2">
      <div className="flex size-8 items-center justify-center rounded-full bg-fill text-[12px] font-semibold">
        {a.name.slice(0, 1)}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13px] font-medium">
          {a.name}
          {a.handle ? <span className="font-normal text-muted"> @{a.handle}</span> : null}
        </div>
        <div className="text-[12px] text-muted">
          {a.followers != null ? `${formatCompact(a.followers)} followers` : ""}
          {a.last_error ? <span className="text-critical"> · {a.last_error}</span> : null}
        </div>
      </div>
      <StatusBadge status={a.status} />
      <DropdownMenu.Root>
        <DropdownMenu.Trigger className="rounded-[8px] p-1.5 text-muted hover:bg-fill-2" aria-label="Account options">
          <MoreHorizontal className="size-4" />
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="end"
            className="z-50 min-w-44 rounded-[12px] bg-elevated p-1.5 shadow-[var(--shadow-lg)] ring-1 ring-hairline"
          >
            <DropdownMenu.Item
              onSelect={async () =>
                (await ask({
                  title: `Disconnect ${a.name}?`,
                  body: "Scheduled posts for it will not go out.",
                  confirmLabel: "Disconnect",
                  destructive: true,
                })) && remove.mutate()
              }
              className="cursor-default rounded-[8px] px-2.5 py-2 text-[14px] text-critical outline-none data-[highlighted]:bg-fill-2"
            >
              Disconnect
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
    </li>
  );
}

function PlatformCard({
  bid,
  p,
  practice,
  onToken,
}: {
  bid: string;
  p: PlatformInfo;
  practice: boolean;
  onToken: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const addPractice = useAction(
    () => api.post(gpath(bid, "/channels/practice"), { platform: p.key, followers: 1000 }),
    { invalidate: [gpath(bid, "/channels")], success: `Practice ${p.label} added` },
  );
  const oauth = async () => {
    setBusy(true);
    try {
      const { url } = await api.get<{ url: string }>(gpath(bid, `/channels/oauth/${p.key}/start`));
      window.location.assign(url);
    } catch (e) {
      toast.error(errorMessage(e));
      setBusy(false);
    }
  };
  const [botBusy, setBotBusy] = useState(false);
  const addBot = async () => {
    setBotBusy(true);
    try {
      const { url } = await api.get<{ url: string }>(gpath(bid, "/discord/install"));
      window.location.assign(url);
    } catch (e) {
      toast.error(errorMessage(e));
      setBotBusy(false);
    }
  };
  const posting = POSTING[p.api_posting] ?? { label: p.api_posting, tone: "neutral" as const };
  return (
    <Card className="flex flex-col p-5">
      <div className="flex items-start gap-3">
        <PlatformMark platform={p.key} size={36} />
        <div className="min-w-0 flex-1">
          <h3 className="text-[16px] font-semibold">{p.label}</h3>
          <p className="line-clamp-2 text-[12px] text-muted">{p.best_for}</p>
        </div>
        <Badge tone={posting.tone}>{posting.label}</Badge>
      </div>
      {p.accounts.length ? (
        <ul className="mt-3 divide-y divide-[var(--hairline)]">
          {p.accounts.map((a) => (
            <AccountRow key={a.id} bid={bid} a={a} />
          ))}
        </ul>
      ) : null}
      <div className="mt-auto flex flex-wrap gap-2 pt-4">
        {practice && !p.accounts.some((a) => a.status === "practice") ? (
          <Button
            size="sm"
            icon={<FlaskConical className="size-3.5" />}
            onClick={() => addPractice.mutate()}
            loading={addPractice.isPending}
          >
            Practice account
          </Button>
        ) : null}
        {p.oauth ? (
          <Button
            size="sm"
            variant={practice ? "secondary" : "primary"}
            icon={<Link2 className="size-3.5" />}
            onClick={oauth}
            loading={busy}
            disabled={!p.app_configured}
            title={p.app_configured ? undefined : "This platform's app keys aren't configured on the server yet."}
          >
            Connect
          </Button>
        ) : TOKEN_FIELDS[p.key] ? (
          <Button
            size="sm"
            variant={practice ? "secondary" : "primary"}
            icon={<KeyRound className="size-3.5" />}
            onClick={onToken}
          >
            {p.key === "discord" ? "Announcements webhook" : "Connect"}
          </Button>
        ) : null}
        {p.key === "discord" ? (
          <Button size="sm" icon={<Bot className="size-3.5" />} onClick={addBot} loading={botBusy}>
            Add community bot
          </Button>
        ) : null}
      </div>
      {p.key === "discord" ? (
        <p className="mt-3 text-[12px] text-muted">
          The bot answers <span className="font-mono">/ask</span> in your brand voice (anything tricky comes to your
          inbox), signs people up with <span className="font-mono">/subscribe</span>, and shares{" "}
          <span className="font-mono">/latest</span> posts.
        </p>
      ) : null}
      {p.approval_notes ? <p className="mt-3 text-[11px] leading-relaxed text-faint">{p.approval_notes}</p> : null}
    </Card>
  );
}

function Queue({ bid }: { bid: string }) {
  const [status, setStatus] = useState("all");
  const { data, isLoading, error } = useApi<{ items: Post[] }>(
    gpath(bid, `/posts?limit=100${status === "all" ? "" : `&status_filter=${status}`}`),
    { refetchInterval: 20_000 },
  );
  const retry = useAction((id: string) => api.post(gpath(bid, `/posts/${id}/retry`)), {
    invalidate: [gpath(bid, "/posts")],
    success: "Retrying now",
  });
  return (
    <div>
      <div className="mb-4">
        <Segmented
          size="sm"
          value={status}
          onChange={setStatus}
          options={[
            { value: "all", label: "All" },
            { value: "scheduled", label: "Scheduled" },
            { value: "published", label: "Published" },
            { value: "failed", label: "Failed" },
            { value: "manual", label: "Manual" },
          ]}
        />
      </div>
      {isLoading ? (
        <Loading />
      ) : error ? (
        <LoadError error={error} />
      ) : !data?.items.length ? (
        <Card>
          <EmptyState title="Queue is empty" body="Approved posts wait here until their time." />
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="text-left text-[12px] text-muted hairline-b">
                <th className="px-5 py-2.5 font-medium">Platform</th>
                <th className="px-3 py-2.5 font-medium">Status</th>
                <th className="px-3 py-2.5 font-medium">When</th>
                <th className="hidden px-3 py-2.5 text-right font-medium sm:table-cell">Reach</th>
                <th className="hidden px-3 py-2.5 text-right font-medium sm:table-cell">Clicks</th>
                <th className="px-5 py-2.5" />
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--hairline)]">
              {data.items.map((p) => (
                <tr key={p.id}>
                  <td className="px-5 py-2.5">
                    <span className="flex items-center gap-2">
                      <PlatformMark platform={p.platform} size={20} />
                      <span>{platformLabel(p.platform)}</span>
                    </span>
                  </td>
                  <td className="px-3 py-2.5">
                    <StatusBadge status={p.status} />
                    {p.last_error ? (
                      <div className="mt-0.5 max-w-60 truncate text-[11px] text-critical" title={p.last_error}>
                        {p.last_error}
                      </div>
                    ) : null}
                  </td>
                  <td className="px-3 py-2.5 text-muted">{formatDateTime(p.published_at ?? p.scheduled_at)}</td>
                  <td className="hidden px-3 py-2.5 text-right tabular sm:table-cell">
                    {formatNumber(p.metrics.reach ?? null)}
                  </td>
                  <td className="hidden px-3 py-2.5 text-right tabular sm:table-cell">
                    {formatNumber(p.metrics.clicks ?? null)}
                  </td>
                  <td className="px-5 py-2.5 text-right">
                    {p.status === "failed" ? (
                      <Button size="sm" icon={<RotateCcw className="size-3.5" />} onClick={() => retry.mutate(p.id)}>
                        Retry
                      </Button>
                    ) : p.url ? (
                      <a
                        href={p.url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex text-muted hover:text-accent"
                        aria-label="Open post"
                      >
                        <ExternalLink className="size-4" />
                      </a>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}

export function Channels() {
  const bid = useBid();
  const search = useSearch({ strict: false }) as { tab?: string; error?: string; connected?: string };
  const navigate = useNavigate();
  const { data, isLoading, error, refetch } = useApi<{ platforms: PlatformInfo[]; practice_mode: boolean }>(
    gpath(bid, "/channels"),
  );
  const [token, setToken] = useState<PlatformInfo | null>(null);
  const sync = useAction(() => api.post(gpath(bid, "/channels/sync-metrics")), {
    invalidate: [gpath(bid, "/posts"), gpath(bid, "/analytics")],
    success: "Fetching the latest numbers",
  });
  useEffect(() => {
    if (search.error) toast.error(OAUTH_ERRORS[search.error] ?? "The connection didn't work. Please try again.");
    if (search.connected) toast.success("Connected");
    if (search.error || search.connected) navigate({ to: ".", search: { tab: search.tab } as never, replace: true });
  }, [search.error, search.connected, search.tab, navigate]);

  const tab = search.tab ?? "accounts";
  const connected = data?.platforms.filter((p) => p.accounts.length) ?? [];
  const others = data?.platforms.filter((p) => !p.accounts.length) ?? [];
  return (
    <div>
      <PageHeader
        title="Channels"
        subtitle="Where your brand shows up. Posting respects each platform's limits and rules."
        actions={
          <Button icon={<RefreshCw className="size-4" />} onClick={() => sync.mutate()} loading={sync.isPending}>
            Sync numbers
          </Button>
        }
      />
      <Tabs value={tab} onValueChange={(v) => navigate({ to: ".", search: { tab: v } as never })}>
        <TabsList>
          <TabsTrigger value="accounts">Accounts</TabsTrigger>
          <TabsTrigger value="queue">Posting queue</TabsTrigger>
        </TabsList>
        <TabsContent value="accounts">
          {isLoading ? (
            <Loading />
          ) : error ? (
            <LoadError error={error} retry={refetch} />
          ) : (
            <div className="space-y-8">
              {data!.practice_mode ? (
                <div className="flex gap-3 rounded-[16px] bg-accent-soft p-4 text-[14px]">
                  <FlaskConical className="mt-0.5 size-5 shrink-0 text-accent" />
                  <div>
                    <span className="font-medium">Practice mode is on.</span> Practice accounts behave like the real
                    thing: posts publish, numbers come in, some posts fail. Nothing leaves this app. Turn it off in
                    Settings when you're ready.
                  </div>
                </div>
              ) : null}
              {connected.length ? (
                <section>
                  <h2 className="mb-3 text-[15px] font-semibold">Connected</h2>
                  <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                    {connected.map((p) => (
                      <PlatformCard
                        key={p.key}
                        bid={bid}
                        p={p}
                        practice={data!.practice_mode}
                        onToken={() => setToken(p)}
                      />
                    ))}
                  </div>
                </section>
              ) : null}
              <section>
                <h2 className="mb-3 text-[15px] font-semibold">
                  {connected.length ? "Add more" : "Connect your first channel"}
                </h2>
                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                  {others.map((p) => (
                    <PlatformCard
                      key={p.key}
                      bid={bid}
                      p={p}
                      practice={data!.practice_mode}
                      onToken={() => setToken(p)}
                    />
                  ))}
                </div>
              </section>
            </div>
          )}
        </TabsContent>
        <TabsContent value="queue">
          <Queue bid={bid} />
        </TabsContent>
      </Tabs>
      <TokenDialog bid={bid} platform={token} onClose={() => setToken(null)} />
    </div>
  );
}
