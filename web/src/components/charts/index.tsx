/**
 * Hand-rolled SVG charts (tiny bundle, full control). Rules from the dataviz method:
 * one y-axis, 2px lines, recessive grid, crosshair + tooltip on hover, direct labels for few series,
 * colors only from the validated series tokens, text always in text tokens.
 */
import { type ReactNode, useLayoutEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry?.contentRect.width ?? 0)));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

/** Four evenly spaced ticks on round numbers (1, 2, 2.5 or 5 times a power of ten). */
export function niceTicks(dataMax: number, count = 4): number[] {
  const raw = Math.max(dataMax, 0) / count || 1;
  const exp = 10 ** Math.floor(Math.log10(raw));
  const f = raw / exp;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * exp;
  return Array.from({ length: count + 1 }, (_, i) => i * step);
}

export interface Series {
  key: string;
  label: string;
  values: (number | null)[];
  color?: string; // a CSS variable, e.g. "var(--series-1)"
  dashed?: boolean;
  area?: boolean;
}

export function LineChart({
  labels,
  series,
  band,
  height = 220,
  format = (v) => String(Math.round(v)),
  formatLabel = (l) => l,
  target,
}: {
  labels: string[];
  series: Series[];
  band?: { low: (number | null)[]; high: (number | null)[]; label: string };
  height?: number;
  format?: (v: number) => string;
  formatLabel?: (l: string) => string;
  target?: { value: number; label: string };
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const pad = { top: 12, right: 12, bottom: 26, left: 44 };
  const w = Math.max(width - pad.left - pad.right, 10);
  const h = height - pad.top - pad.bottom;
  const all = [...series.flatMap((s) => s.values), ...(band?.high ?? []), target?.value ?? 0].filter(
    (v): v is number => v != null,
  );
  const ticks = niceTicks(Math.max(...all, 0));
  const max = ticks[ticks.length - 1] ?? 1;
  const n = Math.max(labels.length - 1, 1);
  const x = (i: number) => (i / n) * w;
  const y = (v: number) => h - (v / max) * h;
  const path = (values: (number | null)[]) => {
    let d = "";
    let pen = false;
    values.forEach((v, i) => {
      if (v == null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const labelEvery = Math.max(1, Math.ceil(labels.length / Math.max(Math.floor(w / 70), 1)));
  const bandPath = (() => {
    if (!band) return "";
    const pts = band.high.map((v, i) => (v == null ? null : [x(i), y(v)])).filter(Boolean) as number[][];
    const low = band.low.map((v, i) => (v == null ? null : [x(i), y(v)])).filter(Boolean) as number[][];
    if (!pts.length) return "";
    return `M${pts.map((p) => p.join(",")).join("L")}L${low
      .reverse()
      .map((p) => p.join(","))
      .join("L")}Z`;
  })(); // eslint-disable-line

  return (
    <div ref={ref} className="relative w-full select-none" style={{ height }}>
      {width > 0 && (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={series.map((s) => s.label).join(", ")}
          onMouseLeave={() => setHover(null)}
          onMouseMove={(e) => {
            const rect = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
            const px = e.clientX - rect.left - pad.left;
            setHover(Math.min(labels.length - 1, Math.max(0, Math.round((px / w) * n))));
          }}
        >
          <g transform={`translate(${pad.left},${pad.top})`}>
            {ticks.map((t) => (
              <g key={t}>
                <line x1={0} x2={w} y1={y(t)} y2={y(t)} stroke="var(--grid)" />
                <text
                  x={-8}
                  y={y(t)}
                  dy="0.32em"
                  textAnchor="end"
                  fontSize={11}
                  fill="var(--faint)"
                  className="tabular"
                >
                  {format(t)}
                </text>
              </g>
            ))}
            {labels.map((l, i) =>
              i % labelEvery === 0 ? (
                <text key={l} x={x(i)} y={h + 18} textAnchor="middle" fontSize={11} fill="var(--faint)">
                  {formatLabel(l)}
                </text>
              ) : null,
            )}
            {band && bandPath ? <path d={bandPath} fill="var(--series-1)" opacity={0.1} /> : null}
            {target ? (
              <g>
                <line
                  x1={0}
                  x2={w}
                  y1={y(target.value)}
                  y2={y(target.value)}
                  stroke="var(--muted)"
                  strokeDasharray="4 4"
                />
                <text x={w} y={y(target.value) - 6} textAnchor="end" fontSize={11} fill="var(--muted)">
                  {target.label}
                </text>
              </g>
            ) : null}
            {series.map((s, si) => (
              <g key={s.key}>
                {s.area ? (
                  <path
                    d={`${path(s.values)}L${x(s.values.length - 1)},${h}L0,${h}Z`}
                    fill={s.color ?? `var(--series-${si + 1})`}
                    opacity={0.08}
                  />
                ) : null}
                <path
                  d={path(s.values)}
                  fill="none"
                  stroke={s.color ?? `var(--series-${si + 1})`}
                  strokeWidth={2}
                  strokeDasharray={s.dashed ? "5 4" : undefined}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                />
              </g>
            ))}
            {hover != null ? (
              <g>
                <line x1={x(hover)} x2={x(hover)} y1={0} y2={h} stroke="var(--hairline-strong)" />
                {series.map((s, si) => {
                  const v = s.values[hover];
                  return v == null ? null : (
                    <circle
                      key={s.key}
                      cx={x(hover)}
                      cy={y(v)}
                      r={4.5}
                      fill={s.color ?? `var(--series-${si + 1})`}
                      stroke="var(--surface)"
                      strokeWidth={2}
                    />
                  );
                })}
              </g>
            ) : null}
          </g>
        </svg>
      )}
      {hover != null && width > 0 ? (
        <div
          className="pointer-events-none absolute top-1 z-10 min-w-36 rounded-[10px] bg-elevated px-3 py-2 text-[12px] shadow-[var(--shadow-md)] ring-1 ring-hairline"
          style={{ left: Math.min(pad.left + x(hover) + 12, width - 170) }}
        >
          <div className="mb-1 font-medium text-text">{formatLabel(labels[hover] ?? "")}</div>
          {series.map((s, si) => (
            <div key={s.key} className="flex items-center justify-between gap-4 text-text-2">
              <span className="flex items-center gap-1.5">
                <span className="size-2 rounded-full" style={{ background: s.color ?? `var(--series-${si + 1})` }} />
                {s.label}
              </span>
              <span className="tabular font-medium text-text">
                {s.values[hover] == null ? "—" : format(s.values[hover] as number)}
              </span>
            </div>
          ))}
          {band && band.low[hover] != null ? (
            <div className="mt-1 text-muted">
              {band.label}: {format(band.low[hover] as number)}–{format(band.high[hover] as number)}
            </div>
          ) : null}
        </div>
      ) : null}
      {series.length > 1 ? (
        <div className="absolute right-0 -top-7 flex gap-3 text-[12px] text-muted">
          {series.map((s, si) => (
            <span key={s.key} className="flex items-center gap-1.5">
              <span
                className={cn("h-0.5 w-3 rounded", s.dashed && "opacity-60")}
                style={{ background: s.color ?? `var(--series-${si + 1})` }}
              />
              {s.label}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function Sparkline({
  values,
  color = "var(--series-1)",
  height = 36,
}: {
  values: number[];
  color?: string;
  height?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const max = Math.max(...values, 1);
  const n = Math.max(values.length - 1, 1);
  const d = values
    .map(
      (v, i) => `${i ? "L" : "M"}${((i / n) * width).toFixed(1)},${(height - 2 - (v / max) * (height - 4)).toFixed(1)}`,
    )
    .join("");
  return (
    <div ref={ref} style={{ height }} className="w-full" aria-hidden>
      {width > 0 && values.length > 1 ? (
        <svg width={width} height={height} aria-hidden="true">
          <path d={`${d}L${width},${height}L0,${height}Z`} fill={color} opacity={0.08} />
          <path d={d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        </svg>
      ) : null}
    </div>
  );
}

/** Horizontal bars with direct labels; hover shows the exact value. Magnitude = one hue. */
export function Bars({
  rows,
  format = (v) => String(Math.round(v)),
  color = "var(--series-1)",
}: {
  rows: { label: ReactNode; value: number; hint?: string }[];
  format?: (v: number) => string;
  color?: string;
}) {
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <ul className="space-y-2.5">
      {rows.map((r, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional
        <li key={i} className="group" title={r.hint}>
          <div className="mb-1 flex items-center justify-between gap-3 text-[13px]">
            <span className="flex min-w-0 items-center gap-2 text-text-2">{r.label}</span>
            <span className="tabular font-medium text-text">{format(r.value)}</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-fill-2">
            <div
              className="h-full rounded-full transition-[width] duration-500 group-hover:brightness-110"
              style={{ width: `${Math.max((r.value / max) * 100, r.value > 0 ? 2 : 0)}%`, background: color }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

/** The funnel: bar length = how many reached each stage; conversion printed between stages. */
export function FunnelChart({
  stages,
}: {
  stages: { stage: string; reached: number; count: number; conversion_to_next: number | null }[];
}) {
  const max = Math.max(...stages.map((s) => s.reached), 1);
  return (
    <ol className="space-y-1">
      {stages.map((s, i) => (
        <li key={s.stage}>
          <div className="flex items-center gap-3">
            <span className="w-24 shrink-0 text-[13px] capitalize text-text-2">
              {s.stage === "mql" ? "MQL" : s.stage === "sql" ? "SQL" : s.stage}
            </span>
            <div
              className="relative h-7 flex-1 overflow-hidden rounded-[7px] bg-fill-2"
              title={`${s.count} now in ${s.stage}; ${s.reached} reached it`}
            >
              <div
                className="h-full rounded-[7px] transition-[width] duration-500"
                style={{
                  width: `${Math.max((s.reached / max) * 100, s.reached ? 1.5 : 0)}%`,
                  background: "var(--series-1)",
                  opacity: 1 - i * 0.06,
                }}
              />
              <span className="tabular absolute inset-y-0 left-2.5 flex items-center text-[12px] font-medium text-text mix-blend-normal">
                {s.reached.toLocaleString()}
              </span>
            </div>
            <span className="w-14 shrink-0 text-right text-[12px] tabular text-muted">
              {s.count.toLocaleString()} now
            </span>
          </div>
          {s.conversion_to_next != null && i < stages.length - 1 ? (
            <div className="ml-[108px] py-0.5 text-[11px] text-faint tabular">
              ↓ {(s.conversion_to_next * 100).toFixed(0)}%
            </div>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

export function StatTile({
  label,
  value,
  delta,
  hint,
  spark,
}: {
  label: string;
  value: ReactNode;
  delta?: { value: string; good: boolean } | null;
  hint?: ReactNode;
  spark?: number[];
}) {
  return (
    <div className="flex flex-col rounded-[18px] bg-surface p-5 shadow-[var(--shadow-sm)] ring-1 ring-hairline">
      <span className="text-[13px] text-muted">{label}</span>
      <span className="mt-1.5 text-[30px] font-semibold leading-none tracking-[-0.03em] tabular">{value}</span>
      <span className="mt-2 flex min-h-5 items-center gap-2 text-[12px]">
        {delta ? (
          <span className={delta.good ? "text-good" : "text-critical"}>
            {delta.good ? "▲" : "▼"} {delta.value}
          </span>
        ) : null}
        {hint ? <span className="text-muted">{hint}</span> : null}
      </span>
      {spark && spark.length > 1 ? (
        <div className="mt-3">
          <Sparkline values={spark} />
        </div>
      ) : null}
    </div>
  );
}
