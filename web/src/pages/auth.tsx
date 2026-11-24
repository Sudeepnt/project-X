import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { CheckCircle2 } from "lucide-react";
import { type FormEvent, type ReactNode, useEffect, useState } from "react";
import { Button, Field, Input } from "@/components/ui";
import { api, errorMessage } from "@/lib/api";
import { useMe } from "@/lib/queries";

function AuthLayout({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center bg-bg px-4 py-12">
      <div className="mb-8 flex items-center gap-2.5">
        <img src="/favicon.svg" alt="" className="size-9" />
        <span className="text-[19px] font-semibold tracking-[-0.02em]">Project X</span>
      </div>
      <div className="w-full max-w-[400px] rounded-[22px] bg-surface p-7 shadow-[var(--shadow-md)] ring-1 ring-hairline sm:p-8 animate-in">
        <h1 className="text-[24px] font-bold tracking-[-0.03em]">{title}</h1>
        {subtitle ? <p className="mt-1.5 text-[14px] text-muted">{subtitle}</p> : null}
        <div className="mt-6">{children}</div>
      </div>
      {footer ? <div className="mt-6 text-[14px] text-muted">{footer}</div> : null}
    </main>
  );
}

function useNext(): string {
  const search = useSearch({ strict: false }) as { next?: string };
  const next = search.next ?? "/";
  return next.startsWith("/") && !next.startsWith("//") ? next : "/";
}

function FormError({ error }: { error: string | null }) {
  return error ? (
    <p role="alert" className="rounded-[10px] bg-[var(--critical-soft)] px-3 py-2 text-[13px] text-critical">
      {error}
    </p>
  ) : null;
}

export function Login() {
  const next = useNext();
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/v1/auth/login", { email, password });
      await qc.invalidateQueries();
      window.location.assign(next);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };
  return (
    <AuthLayout
      title="Sign in"
      subtitle="Welcome back."
      footer={
        <>
          New here?{" "}
          <Link to="/signup" search={{ next }} className="font-medium text-accent">
            Create an account
          </Link>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-4">
        <Field label="Email">
          {(id) => (
            <Input
              id={id}
              type="email"
              autoComplete="email"
              required
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          )}
        </Field>
        <Field label="Password">
          {(id) => (
            <Input
              id={id}
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          )}
        </Field>
        <FormError error={error} />
        <Button type="submit" variant="primary" size="lg" className="w-full justify-center" loading={busy}>
          Sign in
        </Button>
        <div className="text-center">
          <Link to="/forgot-password" className="text-[13px] text-accent">
            Forgot password?
          </Link>
        </div>
      </form>
    </AuthLayout>
  );
}

export function Signup() {
  const next = useNext();
  const [form, setForm] = useState({ name: "", email: "", password: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/v1/auth/signup", form);
      window.location.assign(next === "/" ? "/onboarding" : next);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [k]: e.target.value });
  return (
    <AuthLayout
      title="Create your account"
      subtitle="Run your growth on autopilot, with you in charge."
      footer={
        <>
          Already have an account?{" "}
          <Link to="/login" search={{ next }} className="font-medium text-accent">
            Sign in
          </Link>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-4">
        <Field label="Your name">
          {(id) => <Input id={id} autoComplete="name" required autoFocus value={form.name} onChange={set("name")} />}
        </Field>
        <Field label="Email">
          {(id) => (
            <Input id={id} type="email" autoComplete="email" required value={form.email} onChange={set("email")} />
          )}
        </Field>
        <Field label="Password" hint="At least 10 characters.">
          {(id) => (
            <Input
              id={id}
              type="password"
              autoComplete="new-password"
              minLength={10}
              required
              value={form.password}
              onChange={set("password")}
            />
          )}
        </Field>
        <FormError error={error} />
        <Button type="submit" variant="primary" size="lg" className="w-full justify-center" loading={busy}>
          Create account
        </Button>
        <p className="text-center text-[12px] text-muted">By continuing you agree to the terms and privacy policy.</p>
      </form>
    </AuthLayout>
  );
}

export function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api.post("/api/v1/auth/password/forgot", { email });
      setSent(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <AuthLayout
      title="Reset your password"
      footer={
        <Link to="/login" className="text-accent">
          Back to sign in
        </Link>
      }
    >
      {sent ? (
        <p className="flex gap-2 text-[14px] text-text-2">
          <CheckCircle2 className="size-5 shrink-0 text-good" />
          If an account exists for {email}, a reset link is on its way.
        </p>
      ) : (
        <form onSubmit={submit} className="space-y-4">
          <Field label="Email">
            {(id) => (
              <Input id={id} type="email" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} />
            )}
          </Field>
          <FormError error={error} />
          <Button type="submit" variant="primary" size="lg" className="w-full justify-center" loading={busy}>
            Send reset link
          </Button>
        </form>
      )}
    </AuthLayout>
  );
}

export function ResetPassword() {
  const { token } = useSearch({ strict: false }) as { token?: string };
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api.post("/api/v1/auth/password/reset", { token, password });
      navigate({ to: "/login" });
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };
  return (
    <AuthLayout title="Choose a new password">
      <form onSubmit={submit} className="space-y-4">
        <Field label="New password" hint="At least 10 characters.">
          {(id) => (
            <Input
              id={id}
              type="password"
              autoComplete="new-password"
              minLength={10}
              required
              autoFocus
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          )}
        </Field>
        <FormError error={error ?? (!token ? "This link is missing its token. Request a new one." : null)} />
        <Button
          type="submit"
          variant="primary"
          size="lg"
          className="w-full justify-center"
          loading={busy}
          disabled={!token}
        >
          Save password
        </Button>
      </form>
    </AuthLayout>
  );
}

export function VerifyEmail() {
  const { token } = useSearch({ strict: false }) as { token?: string };
  const [state, setState] = useState<"working" | "done" | "error">("working");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!token) {
      setState("error");
      setError("This link is missing its token.");
      return;
    }
    api
      .post("/api/v1/auth/verify-email", { token })
      .then(() => setState("done"))
      .catch((e) => {
        setState("error");
        setError(errorMessage(e));
      });
  }, [token]);
  return (
    <AuthLayout title={state === "done" ? "Email confirmed" : state === "error" ? "Link not valid" : "Confirming…"}>
      {state === "done" ? (
        <Link to="/">
          <Button variant="primary" size="lg" className="w-full justify-center">
            Continue
          </Button>
        </Link>
      ) : null}
      {state === "error" ? <FormError error={error} /> : null}
    </AuthLayout>
  );
}

export function AcceptInvite() {
  const { token } = useSearch({ strict: false }) as { token?: string };
  const { data: me, isLoading } = useMe();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const here = `/invite?token=${encodeURIComponent(token ?? "")}`;
  const accept = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ business_id: string }>("/api/v1/auth/invites/accept", { token });
      window.location.assign(r?.business_id ? `/b/${r.business_id}/today` : "/");
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };
  return (
    <AuthLayout title="You're invited" subtitle="Join the team on Project X.">
      {isLoading ? null : me?.user ? (
        <div className="space-y-4">
          <p className="text-[14px] text-text-2">
            Signed in as <b>{me.user.email}</b>.
          </p>
          <FormError error={error} />
          <Button variant="primary" size="lg" className="w-full justify-center" loading={busy} onClick={accept}>
            Accept invitation
          </Button>
        </div>
      ) : (
        <div className="space-y-2">
          <Link to="/signup" search={{ next: here }}>
            <Button variant="primary" size="lg" className="w-full justify-center">
              Create an account
            </Button>
          </Link>
          <Link to="/login" search={{ next: here }}>
            <Button size="lg" className="w-full justify-center">
              I have an account
            </Button>
          </Link>
        </div>
      )}
    </AuthLayout>
  );
}
