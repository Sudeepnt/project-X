import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as Popover from "@radix-ui/react-popover";
import { Link, Outlet, useNavigate, useParams, useRouterState } from "@tanstack/react-router";
import { Command } from "cmdk";
import {
  BarChart3,
  Bell,
  BookOpen,
  Calendar,
  CheckCircle2,
  ChevronsUpDown,
  FileText,
  Inbox,
  LayoutGrid,
  LogOut,
  Mail,
  Megaphone,
  Menu,
  Moon,
  Plug,
  Plus,
  Search,
  Settings,
  Sun,
  Target,
  Users,
  Wand2,
  X,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { Badge, Kbd } from "@/components/ui";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { timeAgo } from "@/lib/format";
import { bpath, useAction, useApi, useMe } from "@/lib/queries";
import type { Notification } from "@/lib/types";

interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  badgeKey?: string;
}

const NAV: { section: string; items: NavItem[] }[] = [
  {
    section: "",
    items: [
      { to: "today", label: "Today", icon: <LayoutGrid className="size-[18px]" /> },
      { to: "decisions", label: "Decisions", icon: <CheckCircle2 className="size-[18px]" />, badgeKey: "decisions" },
    ],
  },
  {
    section: "Grow",
    items: [
      { to: "goals", label: "Goals", icon: <Target className="size-[18px]" /> },
      { to: "campaigns", label: "Campaigns", icon: <Megaphone className="size-[18px]" /> },
      { to: "content", label: "Content", icon: <Calendar className="size-[18px]" /> },
      { to: "channels", label: "Channels", icon: <Plug className="size-[18px]" /> },
    ],
  },
  {
    section: "Customers",
    items: [
      { to: "audience", label: "Audience", icon: <Users className="size-[18px]" /> },
      { to: "inbox", label: "Inbox", icon: <Inbox className="size-[18px]" /> },
      { to: "email", label: "Email", icon: <Mail className="size-[18px]" /> },
      { to: "pages", label: "Pages & links", icon: <FileText className="size-[18px]" /> },
    ],
  },
  {
    section: "Learn",
    items: [
      { to: "analytics", label: "Analytics", icon: <BarChart3 className="size-[18px]" /> },
      { to: "brand", label: "Brand", icon: <BookOpen className="size-[18px]" /> },
    ],
  },
];

export function useTheme(): [string, (t: string) => void] {
  const [theme, setTheme] = useState(() => localStorage.getItem("px-theme") ?? "system");
  useEffect(() => {
    const apply = () => {
      const dark = theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
      document.documentElement.dataset.theme = dark ? "dark" : "light";
    };
    apply();
    localStorage.setItem("px-theme", theme);
    const mq = matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, [theme]);
  return [theme, setTheme];
}

function BusinessSwitcher({ bid }: { bid: string }) {
  const { data } = useMe();
  const navigate = useNavigate();
  const current = data?.businesses.find((b) => b.id === bid);
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger className="flex w-full items-center gap-2.5 rounded-[10px] px-2 py-1.5 text-left hover:bg-fill-2">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-[9px] bg-accent text-[14px] font-semibold text-white">
          {current?.name.slice(0, 1) ?? "·"}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[14px] font-semibold">{current?.name ?? "Business"}</span>
          <span className="block truncate text-[12px] text-muted first-letter:uppercase">
            {current?.plan} plan{current?.practice_mode ? " · practice" : ""}
          </span>
        </span>
        <ChevronsUpDown className="size-4 text-faint" />
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          sideOffset={6}
          className="z-50 min-w-60 rounded-[12px] bg-elevated p-1.5 shadow-[var(--shadow-lg)] ring-1 ring-hairline animate-in"
        >
          {data?.businesses.map((b) => (
            <DropdownMenu.Item
              key={b.id}
              onSelect={() => navigate({ to: "/b/$bid/today", params: { bid: b.id } })}
              className={cn(
                "flex cursor-default items-center gap-2 rounded-[8px] px-2.5 py-2 text-[14px] outline-none data-[highlighted]:bg-fill-2",
                b.id === bid && "font-semibold",
              )}
            >
              {b.name}
            </DropdownMenu.Item>
          ))}
          <DropdownMenu.Separator className="my-1 h-px bg-hairline" />
          <DropdownMenu.Item
            onSelect={() => navigate({ to: "/onboarding" })}
            className="flex cursor-default items-center gap-2 rounded-[8px] px-2.5 py-2 text-[14px] text-accent outline-none data-[highlighted]:bg-fill-2"
          >
            <Plus className="size-4" /> New business
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function Notifications({ bid }: { bid: string }) {
  const { data } = useApi<{ items: Notification[]; unread: number }>(bpath(bid, "/notifications"), {
    refetchInterval: 30_000,
  });
  const navigate = useNavigate();
  const markRead = useAction(() => api.post(bpath(bid, "/notifications/read"), {}), {
    invalidate: [bpath(bid, "/notifications")],
  });
  return (
    <Popover.Root
      onOpenChange={(open) => {
        if (!open && data?.unread) markRead.mutate();
      }}
    >
      <Popover.Trigger className="relative rounded-full p-2 text-text-2 hover:bg-fill-2" aria-label="Notifications">
        <Bell className="size-[18px]" />
        {data?.unread ? (
          <span className="absolute top-1.5 right-1.5 size-2 rounded-full bg-critical ring-2 ring-[var(--bg)]" />
        ) : null}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={8}
          className="z-50 max-h-[70vh] w-[360px] overflow-y-auto rounded-[16px] bg-elevated p-2 shadow-[var(--shadow-lg)] ring-1 ring-hairline animate-in"
        >
          <div className="px-2.5 pt-1.5 pb-2 text-[13px] font-semibold">Notifications</div>
          {!data?.items.length ? (
            <p className="px-2.5 py-6 text-center text-[13px] text-muted">You're all caught up.</p>
          ) : null}
          {data?.items.map((n) => (
            <button
              key={n.id}
              type="button"
              onClick={() => n.link && navigate({ to: `/b/${bid}${n.link}` as string })}
              className="block w-full rounded-[10px] px-2.5 py-2 text-left hover:bg-fill-2"
            >
              <div className="flex items-start gap-2">
                {!n.read ? (
                  <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-accent" />
                ) : (
                  <span className="w-1.5" />
                )}
                <div className="min-w-0">
                  <div className="text-[13px] font-medium leading-snug">{n.title}</div>
                  {n.body ? <div className="mt-0.5 line-clamp-2 text-[12px] text-muted">{n.body}</div> : null}
                  <div className="mt-0.5 text-[11px] text-faint">{timeAgo(n.created_at)}</div>
                </div>
              </div>
            </button>
          ))}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function CommandMenu({ bid, open, setOpen }: { bid: string; open: boolean; setOpen: (o: boolean) => void }) {
  const navigate = useNavigate();
  const go = (to: string) => {
    setOpen(false);
    navigate({ to: `/b/${bid}/${to}` });
  };
  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setOpen(!open);
      }
    };
    document.addEventListener("keydown", down);
    return () => document.removeEventListener("keydown", down);
  }, [open, setOpen]);
  return (
    <Command.Dialog
      open={open}
      onOpenChange={setOpen}
      label="Command menu"
      overlayClassName="fixed inset-0 z-50 bg-black/25 animate-in"
      contentClassName="fixed top-[15vh] left-1/2 z-50 w-[min(640px,calc(100vw-32px))] -translate-x-1/2 overflow-hidden rounded-[16px] bg-elevated shadow-[var(--shadow-lg)] ring-1 ring-hairline"
    >
      <div className="flex items-center gap-2 px-4 hairline-b">
        <Search className="size-4 text-faint" />
        <Command.Input
          autoFocus
          placeholder="Go to, or do…"
          className="h-12 flex-1 bg-transparent text-[15px] outline-none placeholder:text-faint"
        />
        <Kbd>esc</Kbd>
      </div>
      <Command.List className="max-h-[50vh] overflow-y-auto p-2">
        <Command.Empty className="py-8 text-center text-[13px] text-muted">No results</Command.Empty>
        <Command.Group
          heading="Go to"
          className="text-[11px] text-faint [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5"
        >
          {[
            ...NAV.flatMap((s) => s.items),
            { to: "settings", label: "Settings", icon: <Settings className="size-[18px]" /> },
          ].map((item) => (
            <Command.Item
              key={item.to}
              onSelect={() => go(item.to)}
              className="flex cursor-default items-center gap-3 rounded-[10px] px-2.5 py-2 text-[14px] text-text data-[selected=true]:bg-fill-2"
            >
              <span className="text-muted">{item.icon}</span>
              {item.label}
            </Command.Item>
          ))}
        </Command.Group>
        <Command.Group
          heading="Do"
          className="text-[11px] text-faint [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5"
        >
          {[
            ["goals/new", "Set a new goal"],
            ["campaigns?tab=library", "Start a campaign"],
            ["content?new=1", "Write a post"],
            ["audience?new=1", "Add a contact"],
            ["channels", "Connect a channel"],
          ].map(([to, label]) => (
            <Command.Item
              key={to}
              onSelect={() => go(to as string)}
              className="flex cursor-default items-center gap-3 rounded-[10px] px-2.5 py-2 text-[14px] text-text data-[selected=true]:bg-fill-2"
            >
              <Wand2 className="size-[18px] text-muted" />
              {label}
            </Command.Item>
          ))}
        </Command.Group>
      </Command.List>
    </Command.Dialog>
  );
}

export function AppShell() {
  const { bid } = useParams({ strict: false }) as { bid: string };
  const { data: me } = useMe();
  const { data: pending } = useApi<{ items: unknown[] }>(bpath(bid, "/decisions?status=pending&limit=200"), {
    refetchInterval: 30_000,
  });
  const path = useRouterState({ select: (s) => s.location.pathname });
  const navigate = useNavigate();
  const [theme, setTheme] = useTheme();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);
  const business = me?.businesses.find((b) => b.id === bid);
  // biome-ignore lint/correctness/useExhaustiveDependencies: deliberately resets local state when the selection changes
  useEffect(() => {
    setMobileOpen(false);
  }, [path]);

  const logout = async () => {
    await api.post("/api/v1/auth/logout");
    window.location.href = "/login";
  };

  const sidebar = (
    <nav className="flex h-full flex-col gap-1 px-3 py-3" aria-label="Main">
      <BusinessSwitcher bid={bid} />
      <button
        type="button"
        onClick={() => setCommandOpen(true)}
        className="mx-0.5 mt-2 mb-2 flex items-center gap-2 rounded-[10px] bg-fill-2 px-3 py-2 text-[13px] text-muted hover:text-text"
      >
        <Search className="size-4" /> Search{" "}
        <span className="ml-auto">
          <Kbd>⌘K</Kbd>
        </span>
      </button>
      {NAV.map((section) => (
        <div key={section.section} className="mt-1">
          {section.section ? (
            <div className="px-3 pt-3 pb-1 text-[11px] font-semibold tracking-wide text-faint uppercase">
              {section.section}
            </div>
          ) : null}
          {section.items.map((item) => {
            const active = path.startsWith(`/b/${bid}/${item.to}`);
            const count = item.badgeKey === "decisions" ? (pending?.items.length ?? 0) : 0;
            return (
              <Link
                key={item.to}
                to={`/b/${bid}/${item.to}` as string}
                className={cn(
                  "flex items-center gap-3 rounded-[10px] px-3 py-[7px] text-[14px] font-medium transition-colors",
                  active ? "bg-accent-soft text-accent" : "text-text-2 hover:bg-fill-2 hover:text-text",
                )}
              >
                {item.icon}
                <span className="flex-1">{item.label}</span>
                {count ? <Badge tone={active ? "accent" : "warning"}>{count}</Badge> : null}
              </Link>
            );
          })}
        </div>
      ))}
      <div className="mt-auto space-y-1 pt-4">
        <Link
          to={`/b/${bid}/settings` as string}
          className={cn(
            "flex items-center gap-3 rounded-[10px] px-3 py-[7px] text-[14px] font-medium",
            path.startsWith(`/b/${bid}/settings`) ? "bg-accent-soft text-accent" : "text-text-2 hover:bg-fill-2",
          )}
        >
          <Settings className="size-[18px]" /> Settings
        </Link>
        <DropdownMenu.Root>
          <DropdownMenu.Trigger className="flex w-full items-center gap-2.5 rounded-[10px] px-3 py-2 text-left hover:bg-fill-2">
            <span className="flex size-7 items-center justify-center rounded-full bg-fill text-[12px] font-semibold">
              {me?.user?.name.slice(0, 1)}
            </span>
            <span className="min-w-0 flex-1 truncate text-[13px]">{me?.user?.name}</span>
          </DropdownMenu.Trigger>
          <DropdownMenu.Portal>
            <DropdownMenu.Content
              side="top"
              align="start"
              className="z-50 min-w-56 rounded-[12px] bg-elevated p-1.5 shadow-[var(--shadow-lg)] ring-1 ring-hairline animate-in"
            >
              <div className="px-2.5 py-2 text-[12px] text-muted">{me?.user?.email}</div>
              {(["light", "dark", "system"] as const).map((t) => (
                <DropdownMenu.Item
                  key={t}
                  onSelect={() => setTheme(t)}
                  className="flex cursor-default items-center gap-2 rounded-[8px] px-2.5 py-2 text-[14px] capitalize outline-none data-[highlighted]:bg-fill-2"
                >
                  {t === "dark" ? <Moon className="size-4" /> : <Sun className="size-4" />} {t}
                  {theme === t ? " ✓" : ""}
                </DropdownMenu.Item>
              ))}
              <DropdownMenu.Separator className="my-1 h-px bg-hairline" />
              <DropdownMenu.Item
                onSelect={logout}
                className="flex cursor-default items-center gap-2 rounded-[8px] px-2.5 py-2 text-[14px] text-critical outline-none data-[highlighted]:bg-fill-2"
              >
                <LogOut className="size-4" /> Sign out
              </DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
      </div>
    </nav>
  );

  return (
    <div className="min-h-dvh">
      <aside className="glass fixed inset-y-0 left-0 z-30 hidden w-[248px] shadow-[inset_-1px_0_0_var(--hairline)] lg:block">
        {sidebar}
      </aside>
      {mobileOpen ? (
        <div className="fixed inset-0 z-40 lg:hidden">
          <button
            type="button"
            aria-label="Close menu"
            className="absolute inset-0 bg-black/30"
            onClick={() => setMobileOpen(false)}
          />
          <aside className="absolute inset-y-0 left-0 w-[280px] bg-surface shadow-[var(--shadow-lg)] animate-in">
            {sidebar}
          </aside>
        </div>
      ) : null}
      <div className="lg:pl-[248px]">
        <header className="glass sticky top-0 z-20 flex h-14 items-center gap-2 px-4 hairline-b sm:px-8">
          <button
            type="button"
            className="rounded-[8px] p-2 lg:hidden"
            onClick={() => setMobileOpen(true)}
            aria-label="Open menu"
          >
            {mobileOpen ? <X className="size-5" /> : <Menu className="size-5" />}
          </button>
          {business?.practice_mode ? <Badge tone="accent">Practice mode: platforms are simulated</Badge> : null}
          <div className="ml-auto flex items-center gap-1">
            <button
              type="button"
              onClick={() => navigate({ to: `/b/${bid}/decisions` })}
              className="hidden rounded-full px-3 py-1.5 text-[13px] font-medium text-text-2 hover:bg-fill-2 sm:block"
            >
              {pending?.items.length ? `${pending.items.length} waiting for you` : "Queue clear"}
            </button>
            <Notifications bid={bid} />
          </div>
        </header>
        <main className="mx-auto w-full max-w-[1200px] px-4 py-6 sm:px-8 sm:py-8 animate-in" key={path}>
          <Outlet />
        </main>
      </div>
      <CommandMenu bid={bid} open={commandOpen} setOpen={setCommandOpen} />
    </div>
  );
}
