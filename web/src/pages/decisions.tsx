import { useNavigate, useParams } from "@tanstack/react-router";
import { CheckCircle2 } from "lucide-react";
import { useState } from "react";
import { DecisionRow, DecisionSheet } from "@/components/decision";
import { Card, EmptyState, PageHeader, Segmented, StatusBadge } from "@/components/ui";
import { bpath, useApi, useBid } from "@/lib/queries";
import type { Decision, Page } from "@/lib/types";
import { LoadError, Loading } from "./system";

type Filter = "pending" | "approval" | "manual_task" | "history";

export function Decisions() {
  const bid = useBid();
  const { id } = useParams({ strict: false }) as { id?: string };
  const navigate = useNavigate();
  const [filter, setFilter] = useState<Filter>("pending");
  const query =
    filter === "history"
      ? "?limit=100"
      : filter === "pending"
        ? "?status=pending&limit=200"
        : `?status=pending&kind=${filter}&limit=200`;
  const { data, isLoading, error, refetch } = useApi<Page<Decision>>(bpath(bid, `/decisions${query}`), {
    refetchInterval: 30_000,
  });
  const items = filter === "history" ? data?.items.filter((d) => d.status !== "pending") : data?.items;
  const open = (did: string | null) => navigate({ to: did ? `/b/${bid}/decisions/${did}` : `/b/${bid}/decisions` });

  return (
    <div>
      <PageHeader
        title="Decisions"
        subtitle="Everything the system wants to do that needs your yes. Nothing public, costly or irreversible happens without it."
        actions={
          <Segmented
            value={filter}
            onChange={setFilter}
            options={[
              { value: "pending", label: "Waiting" },
              { value: "approval", label: "Approvals" },
              { value: "manual_task", label: "Tasks" },
              { value: "history", label: "History" },
            ]}
          />
        }
      />
      {isLoading ? (
        <Loading />
      ) : error ? (
        <LoadError error={error} retry={refetch} />
      ) : (
        <Card className="overflow-hidden">
          {items?.length ? (
            <div className="divide-y divide-[var(--hairline)]">
              {items.map((d) => (
                <div key={d.id} className="relative">
                  <DecisionRow d={d} onOpen={() => open(d.id)} />
                  {filter === "history" ? (
                    <span className="pointer-events-none absolute top-4 right-12">
                      <StatusBadge status={d.status} />
                    </span>
                  ) : null}
                </div>
              ))}
            </div>
          ) : (
            <EmptyState
              icon={<CheckCircle2 className="size-6" />}
              title={filter === "history" ? "No history yet" : "You're all caught up"}
              body="New decisions appear here and on Today."
            />
          )}
        </Card>
      )}
      <DecisionSheet bid={bid} id={id ?? null} onClose={() => open(null)} />
    </div>
  );
}
