import { type QueryKey, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { toast } from "sonner";
import { ApiError, api, errorMessage } from "./api";
import type { Me } from "./types";

/** Read any endpoint with caching, background refresh and retries handled by React Query. */
export function useApi<T>(path: string | null, options?: { refetchInterval?: number; enabled?: boolean }) {
  return useQuery<T>({
    queryKey: [path],
    queryFn: () => api.get<T>(path as string),
    enabled: !!path && options?.enabled !== false,
    refetchInterval: options?.refetchInterval,
  });
}

/**
 * A write that refreshes what it touched. `invalidate` lists path prefixes to refetch afterwards;
 * errors surface as a toast with the server's own plain-language message.
 */
export function useAction<TVars = void, TResult = unknown>(
  fn: (vars: TVars) => Promise<TResult>,
  options?: { invalidate?: string[]; success?: string | ((r: TResult) => string); onSuccess?: (r: TResult) => void },
) {
  const qc = useQueryClient();
  return useMutation<TResult, Error, TVars>({
    mutationFn: fn,
    onSuccess: (result) => {
      for (const prefix of options?.invalidate ?? []) {
        qc.invalidateQueries({ predicate: (q) => String((q.queryKey as QueryKey)[0] ?? "").startsWith(prefix) });
      }
      const message = typeof options?.success === "function" ? options.success(result) : options?.success;
      if (message) toast.success(message);
      options?.onSuccess?.(result);
    },
    onError: (error) => {
      // Plan limits (HTTP 402) come with a way forward, not just a "no".
      if (error instanceof ApiError && error.status === 402) {
        const bid = window.location.pathname.match(/^\/b\/([^/]+)/)?.[1];
        toast.error(errorMessage(error), {
          action: bid
            ? { label: "See plans", onClick: () => window.location.assign(`/b/${bid}/settings?tab=plan`) }
            : undefined,
        });
        return;
      }
      toast.error(errorMessage(error));
    },
  });
}

export const useMe = () =>
  useQuery<Me>({ queryKey: ["/api/v1/auth/me"], queryFn: () => api.get<Me>("/api/v1/auth/me"), staleTime: 60_000 });

export const bpath = (bid: string, path: string) => `/api/v1/b/${bid}${path}`;
export const gpath = (bid: string, path: string) => `/api/v1/b/${bid}/gtm${path}`;

/** The current business id from the URL (/b/$bid/...). */
export function useBid(): string {
  return (useParams({ strict: false }) as { bid: string }).bid;
}
