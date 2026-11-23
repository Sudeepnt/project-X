import { type ErrorComponentProps, Link } from "@tanstack/react-router";
import { Button } from "@/components/ui";
import { errorMessage } from "@/lib/api";

export function NotFound() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center px-6 text-center">
      <p className="text-[13px] font-medium text-muted">404</p>
      <h1 className="mt-2 text-[28px] font-bold tracking-[-0.03em]">This page doesn't exist.</h1>
      <p className="mt-2 text-muted">The link may be old, or the page was moved.</p>
      <Link to="/" className="mt-6">
        <Button variant="primary">Go to Today</Button>
      </Link>
    </div>
  );
}

export function RouteError({ error, reset }: ErrorComponentProps) {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center px-6 text-center">
      <h1 className="text-[24px] font-bold tracking-[-0.03em]">Something went wrong.</h1>
      <p className="mt-2 max-w-md text-muted">{errorMessage(error)}</p>
      <div className="mt-6 flex gap-2">
        <Button onClick={reset}>Try again</Button>
        <Button variant="primary" onClick={() => window.location.assign("/")}>
          Go home
        </Button>
      </div>
    </div>
  );
}

const SKELETON_KEYS = ["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"];

/** Shared loading/error wrapper for a query result. */
export function Loading({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-3" aria-busy>
      {SKELETON_KEYS.slice(0, rows).map((k) => (
        <div
          key={k}
          className="h-20 rounded-[18px] bg-fill-2 [animation:px-shimmer_1.6s_linear_infinite] bg-[linear-gradient(90deg,var(--fill-2)_0%,var(--fill)_50%,var(--fill-2)_100%)] bg-[length:200%_100%]"
        />
      ))}
    </div>
  );
}

export function LoadError({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <div className="rounded-[18px] bg-[var(--critical-soft)] px-5 py-4 text-[14px] text-critical">
      {errorMessage(error)}
      {retry ? (
        <button type="button" onClick={retry} className="ml-3 font-medium underline">
          Retry
        </button>
      ) : null}
    </div>
  );
}
