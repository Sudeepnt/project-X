import type { QueryClient } from "@tanstack/react-query";
import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  lazyRouteComponent,
  Outlet,
  redirect,
} from "@tanstack/react-router";
import { AppShell } from "@/components/layout/AppShell";
import { ApiError, api } from "@/lib/api";
import type { Me } from "@/lib/types";
import { NotFound, RouteError } from "@/pages/system";

interface Ctx {
  queryClient: QueryClient;
}

const fetchMe = (qc: QueryClient) =>
  qc
    .ensureQueryData<Me>({ queryKey: ["/api/v1/auth/me"], queryFn: () => api.get<Me>("/api/v1/auth/me") })
    .catch((e) => {
      if (e instanceof ApiError && e.status === 401) return { user: null, businesses: [], csrf_token: null } as Me;
      throw e;
    });

const root = createRootRouteWithContext<Ctx>()({
  component: Outlet,
  notFoundComponent: NotFound,
  errorComponent: RouteError,
});

const page = (loader: () => Promise<Record<string, unknown>>, name: string) =>
  lazyRouteComponent(loader as never, name);

/* ---------------------------------------------------------------- public (signed out) */
const guest = createRoute({
  getParentRoute: () => root,
  id: "guest",
  component: Outlet,
});
const auth = () => import("@/pages/auth");
const login = createRoute({ getParentRoute: () => guest, path: "/login", component: page(auth, "Login") });
const signup = createRoute({ getParentRoute: () => guest, path: "/signup", component: page(auth, "Signup") });
const forgot = createRoute({
  getParentRoute: () => guest,
  path: "/forgot-password",
  component: page(auth, "ForgotPassword"),
});
const reset = createRoute({
  getParentRoute: () => guest,
  path: "/reset-password",
  component: page(auth, "ResetPassword"),
});
const verify = createRoute({
  getParentRoute: () => guest,
  path: "/verify-email",
  component: page(auth, "VerifyEmail"),
});
const invite = createRoute({ getParentRoute: () => guest, path: "/invite", component: page(auth, "AcceptInvite") });

/* ---------------------------------------------------------------- signed in */
const authed = createRoute({
  getParentRoute: () => root,
  id: "authed",
  beforeLoad: async ({ context, location }) => {
    const me = await fetchMe(context.queryClient);
    if (!me.user) throw redirect({ to: "/login", search: { next: location.href } });
    return { me };
  },
  component: Outlet,
});

const index = createRoute({
  getParentRoute: () => authed,
  path: "/",
  beforeLoad: ({ context }) => {
    const first = context.me.businesses[0];
    if (!first) throw redirect({ to: "/onboarding" });
    const last = localStorage.getItem("px-business");
    const bid = context.me.businesses.some((b) => b.id === last) ? (last as string) : first.id;
    throw redirect({ to: "/b/$bid/today", params: { bid } });
  },
});

const onboarding = createRoute({
  getParentRoute: () => authed,
  path: "/onboarding",
  component: page(() => import("@/pages/onboarding"), "Onboarding"),
});

const business = createRoute({
  getParentRoute: () => authed,
  path: "/b/$bid",
  beforeLoad: ({ context, params }) => {
    if (!context.me.businesses.some((b) => b.id === params.bid)) throw redirect({ to: "/" });
    localStorage.setItem("px-business", params.bid);
  },
  component: AppShell,
});

const businessIndex = createRoute({
  getParentRoute: () => business,
  path: "/",
  beforeLoad: ({ params }) => {
    throw redirect({ to: "/b/$bid/today", params });
  },
});

const child = <P extends string>(path: P, loader: () => Promise<Record<string, unknown>>, name: string) =>
  createRoute({ getParentRoute: () => business, path, component: page(loader, name) });

const P = {
  today: () => import("@/pages/today"),
  decisions: () => import("@/pages/decisions"),
  goals: () => import("@/pages/goals"),
  campaigns: () => import("@/pages/campaigns"),
  content: () => import("@/pages/content"),
  channels: () => import("@/pages/channels"),
  audience: () => import("@/pages/audience"),
  inbox: () => import("@/pages/inbox"),
  email: () => import("@/pages/email"),
  pages: () => import("@/pages/growth"),
  analytics: () => import("@/pages/analytics"),
  brand: () => import("@/pages/brand"),
  settings: () => import("@/pages/settings"),
};

const businessRoutes = [
  businessIndex,
  child("/today", P.today, "Today"),
  child("/decisions", P.decisions, "Decisions"),
  child("/decisions/$id", P.decisions, "Decisions"),
  child("/goals", P.goals, "Goals"),
  child("/goals/new", P.goals, "NewGoal"),
  child("/goals/$id", P.goals, "GoalDetail"),
  child("/campaigns", P.campaigns, "Campaigns"),
  child("/content", P.content, "Content"),
  child("/channels", P.channels, "Channels"),
  child("/audience", P.audience, "Audience"),
  child("/inbox", P.inbox, "Inbox"),
  child("/email", P.email, "Email"),
  child("/pages", P.pages, "Growth"),
  child("/analytics", P.analytics, "Analytics"),
  child("/analytics/reports/$id", P.analytics, "Report"),
  child("/brand", P.brand, "Brand"),
  child("/settings", P.settings, "Settings"),
];

/**
 * Links from emails, notifications and OAuth callbacks are business-relative ("/channels?tab=queue").
 * Send them into the business the person last used.
 */
const relative = createRoute({
  getParentRoute: () => authed,
  path: "$",
  beforeLoad: ({ context, location }) => {
    const known = [
      "today",
      "decisions",
      "goals",
      "campaigns",
      "content",
      "channels",
      "audience",
      "inbox",
      "email",
      "pages",
      "analytics",
      "brand",
      "settings",
    ];
    const first = location.pathname.split("/")[1] ?? "";
    const last = localStorage.getItem("px-business");
    const bid = context.me.businesses.find((b) => b.id === last)?.id ?? context.me.businesses[0]?.id;
    if (bid && known.includes(first)) throw redirect({ href: `/b/${bid}${location.href}` });
  },
  component: NotFound,
});

const tree = root.addChildren([
  guest.addChildren([login, signup, forgot, reset, verify, invite]),
  authed.addChildren([index, onboarding, business.addChildren(businessRoutes), relative]),
]);

export const router = createRouter({
  routeTree: tree,
  context: { queryClient: undefined as unknown as QueryClient },
  defaultPreload: "intent",
  defaultPendingMs: 150,
  scrollRestoration: true,
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
