/** The design system: small, accessible primitives built on Radix where behaviour is non-trivial. */
import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import * as TabsPrimitive from "@radix-ui/react-tabs";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { Loader2, X } from "lucide-react";
import {
  type ButtonHTMLAttributes,
  forwardRef,
  type HTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  useId,
} from "react";
import { cn } from "@/lib/cn";

/* ------------------------------------------------------------------ Button */

type Variant = "primary" | "secondary" | "ghost" | "danger" | "plain";
type Size = "sm" | "md" | "lg" | "icon";

const variants: Record<Variant, string> = {
  primary: "bg-accent-fill text-white hover:brightness-110 active:brightness-95 shadow-[0_1px_2px_rgb(0_0_0/0.1)]",
  secondary: "bg-fill text-text hover:bg-[color-mix(in_srgb,var(--fill)_160%,transparent)] active:scale-[0.98]",
  ghost: "text-text-2 hover:bg-fill-2 hover:text-text",
  danger: "bg-critical text-white hover:brightness-110",
  plain: "text-accent hover:underline underline-offset-4 px-0 h-auto",
};
const sizes: Record<Size, string> = {
  sm: "h-8 px-3 text-[13px] gap-1.5 rounded-[8px]",
  md: "h-9 px-4 text-[14px] gap-2 rounded-[10px]",
  lg: "h-11 px-5 text-[15px] gap-2 rounded-[12px]",
  icon: "h-9 w-9 rounded-[10px] justify-center",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading, icon, className, children, disabled, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      type={props.type ?? "button"}
      disabled={disabled || loading}
      className={cn(
        "inline-flex select-none items-center font-medium whitespace-nowrap transition-[background,filter,transform,color] duration-150 disabled:pointer-events-none disabled:opacity-45",
        variants[variant],
        sizes[size],
        className,
      )}
      {...props}
    >
      {loading ? <Loader2 className="size-4 animate-spin" aria-hidden /> : icon}
      {children}
    </button>
  );
});

/* ------------------------------------------------------------------ Form fields */

const fieldBase =
  "w-full rounded-[10px] border border-hairline bg-surface px-3 text-[15px] text-text placeholder:text-faint outline-none transition-[border,box-shadow] duration-150 focus:border-accent focus:shadow-[0_0_0_4px_var(--accent-soft)] disabled:opacity-60";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input(
  { className, ...props },
  ref,
) {
  return <input ref={ref} className={cn(fieldBase, "h-10", className)} {...props} />;
});

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea(
  { className, ...props },
  ref,
) {
  return <textarea ref={ref} className={cn(fieldBase, "min-h-24 py-2.5 leading-relaxed", className)} {...props} />;
});

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(function Select(
  { className, children, ...props },
  ref,
) {
  return (
    <select
      ref={ref}
      className={cn(fieldBase, "h-10 appearance-none bg-no-repeat pr-9", className)}
      {...props}
      style={{
        backgroundImage:
          "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12' fill='none' stroke='%2386868b' stroke-width='1.6'%3E%3Cpath d='M3 4.5l3 3 3-3'/%3E%3C/svg%3E\")",
        backgroundPosition: "right 12px center",
      }}
    >
      {children}
    </select>
  );
});

export function Field({
  label,
  hint,
  error,
  children,
  className,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  children: (id: string) => ReactNode;
  className?: string;
}) {
  const id = useId();
  return (
    <div className={cn("space-y-1.5", className)}>
      <label htmlFor={id} className="block text-[13px] font-medium text-text-2">
        {label}
      </label>
      {children(id)}
      {error ? (
        <p className="text-[13px] text-critical" role="alert">
          {error}
        </p>
      ) : hint ? (
        <p className="text-[13px] text-muted">{hint}</p>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ Surfaces */

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("rounded-[18px] bg-surface shadow-[var(--shadow-sm)] ring-1 ring-hairline", className)}
      {...props}
    />
  );
}

export function CardHeader({
  title,
  subtitle,
  action,
  className,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex items-start justify-between gap-4 px-5 pt-5 pb-3", className)}>
      <div className="min-w-0">
        <h2 className="text-[17px] font-semibold tracking-[-0.02em]">{title}</h2>
        {subtitle ? <p className="mt-0.5 text-[13px] text-muted">{subtitle}</p> : null}
      </div>
      {action}
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-[28px] font-bold leading-tight tracking-[-0.03em] sm:text-[32px]">{title}</h1>
        {subtitle ? <p className="mt-1 max-w-2xl text-[15px] text-muted">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

/* ------------------------------------------------------------------ Badges & status */

type Tone = "neutral" | "accent" | "good" | "warning" | "critical";
const tones: Record<Tone, string> = {
  neutral: "bg-fill-2 text-text-2",
  accent: "bg-accent-soft text-accent",
  good: "bg-[var(--good-soft)] text-good",
  warning: "bg-[var(--warning-soft)] text-warning",
  critical: "bg-[var(--critical-soft)] text-critical",
};

export function Badge({
  tone = "neutral",
  className,
  children,
}: {
  tone?: Tone;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[12px] font-medium",
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

const STATUS_TONE: Record<string, Tone> = {
  active: "good",
  published: "good",
  executed: "good",
  connected: "good",
  replied: "good",
  done: "good",
  achieved: "good",
  ready: "good",
  approved: "accent",
  scheduled: "accent",
  sent: "good",
  practice: "accent",
  pending: "warning",
  needs_review: "warning",
  drafted: "warning",
  draft: "neutral",
  idea: "neutral",
  paused: "neutral",
  queued: "neutral",
  open: "neutral",
  manual: "warning",
  escalated: "critical",
  failed: "critical",
  rejected: "critical",
  blocked: "critical",
  expired: "critical",
  missed: "critical",
  stopped: "neutral",
  revoked: "neutral",
  undone: "neutral",
  cancelled: "neutral",
  closed: "neutral",
  ahead: "good",
  on_track: "good",
  behind: "warning",
  at_risk: "critical",
};

export function StatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? "neutral"}>{status.replace(/_/g, " ")}</Badge>;
}

const PLATFORM_COLORS: Record<string, string> = {
  instagram: "#b81e88",
  facebook: "#1460c8",
  threads: "#101010",
  tiktok: "#000000",
  youtube: "#c40000",
  linkedin: "#0a5cad",
  x: "#000000",
  pinterest: "#c2001d",
  reddit: "#c43500",
  discord: "#4752c4",
  telegram: "#16729f",
  whatsapp: "#0f7a3d",
  newsletter: "#0062c4",
  blog: "#5c5c61",
  email: "#0062c4",
};
const PLATFORM_SHORT: Record<string, string> = {
  instagram: "IG",
  facebook: "FB",
  threads: "Th",
  tiktok: "TT",
  youtube: "YT",
  linkedin: "in",
  x: "X",
  pinterest: "P",
  reddit: "R",
  discord: "D",
  telegram: "TG",
  whatsapp: "WA",
  newsletter: "NL",
  blog: "B",
  email: "@",
};

const PLATFORM_LABELS: Record<string, string> = {
  instagram: "Instagram",
  facebook: "Facebook",
  threads: "Threads",
  tiktok: "TikTok",
  youtube: "YouTube",
  linkedin: "LinkedIn",
  x: "X",
  pinterest: "Pinterest",
  reddit: "Reddit",
  discord: "Discord",
  telegram: "Telegram",
  whatsapp: "WhatsApp",
  newsletter: "Newsletter",
  blog: "Blog",
  email: "Email",
};

/** The platform's own spelling ("TikTok", "LinkedIn"), never a naive capitalisation. */
export function platformLabel(key: string): string {
  return PLATFORM_LABELS[key] ?? key.charAt(0).toUpperCase() + key.slice(1);
}

const AI_LABELS: Record<string, string> = {
  api_flag: "Marked as AI on the platform",
  caption_note: "AI note in the caption",
  text: "AI note in the caption",
  manual_toggle: "Turn on the platform's AI label when posting",
};
export function aiLabelText(value: string | undefined): string | null {
  if (!value || value === "none") return null;
  return AI_LABELS[value] ?? value.replace(/_/g, " ");
}

/** A neutral letter mark per platform (no third-party logos). */
export function PlatformMark({ platform, size = 22 }: { platform: string; size?: number }) {
  return (
    <span
      title={platformLabel(platform)}
      role="img"
      className="inline-flex shrink-0 items-center justify-center rounded-[6px] font-semibold text-white"
      style={{ width: size, height: size, fontSize: size * 0.42, background: PLATFORM_COLORS[platform] ?? "#86868b" }}
      aria-label={platformLabel(platform)}
    >
      {PLATFORM_SHORT[platform] ?? platform.slice(0, 2).toUpperCase()}
    </span>
  );
}

const LEVEL_TONE: Record<string, Tone> = {
  never: "critical",
  draft: "neutral",
  ask_first: "warning",
  run_and_tell: "accent",
  run: "good",
};
export function LevelBadge({ level, label }: { level: string; label?: string }) {
  return <Badge tone={LEVEL_TONE[level] ?? "neutral"}>{label ?? level.replace(/_/g, " ")}</Badge>;
}

/* ------------------------------------------------------------------ Feedback */

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "rounded-[10px] bg-[linear-gradient(90deg,var(--fill-2)_0%,var(--fill)_50%,var(--fill-2)_100%)] bg-[length:200%_100%] [animation:px-shimmer_1.6s_linear_infinite]",
        className,
      )}
      aria-hidden
    />
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("size-5 animate-spin text-muted", className)} aria-label="Loading" />;
}

export function EmptyState({
  icon,
  title,
  body,
  action,
}: {
  icon?: ReactNode;
  title: string;
  body?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center px-6 py-14 text-center">
      {icon ? (
        <div className="mb-4 flex size-12 items-center justify-center rounded-[14px] bg-fill-2 text-muted">{icon}</div>
      ) : null}
      <h3 className="text-[17px] font-semibold">{title}</h3>
      {body ? <p className="mt-1 max-w-md text-[14px] text-muted">{body}</p> : null}
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded-[5px] border border-hairline bg-surface-2 px-1.5 py-px font-mono text-[11px] text-muted">
      {children}
    </kbd>
  );
}

/* ------------------------------------------------------------------ Segmented control & tabs */

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  className,
  size = "md",
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: ReactNode }[];
  className?: string;
  size?: "sm" | "md";
}) {
  return (
    <div role="tablist" className={cn("inline-flex rounded-[10px] bg-fill-2 p-0.5", className)}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          aria-selected={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            "rounded-[8px] font-medium transition-all duration-150",
            size === "sm" ? "px-2.5 py-1 text-[12px]" : "px-3.5 py-1.5 text-[13px]",
            value === o.value ? "bg-surface text-text shadow-[var(--shadow-sm)]" : "text-muted hover:text-text",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export const Tabs = TabsPrimitive.Root;
export function TabsList({ className, ...props }: TabsPrimitive.TabsListProps) {
  return <TabsPrimitive.List className={cn("mb-5 flex gap-1 overflow-x-auto hairline-b", className)} {...props} />;
}
export function TabsTrigger({ className, ...props }: TabsPrimitive.TabsTriggerProps) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        "-mb-px whitespace-nowrap border-b-2 border-transparent px-3 pb-2.5 text-[14px] font-medium text-muted transition-colors hover:text-text data-[state=active]:border-accent data-[state=active]:text-text",
        className,
      )}
      {...props}
    />
  );
}
export const TabsContent = TabsPrimitive.Content;

/* ------------------------------------------------------------------ Switch */

export function Switch({
  checked,
  onCheckedChange,
  label,
  disabled,
}: {
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <SwitchPrimitive.Root
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      aria-label={label}
      className="relative h-[26px] w-[44px] shrink-0 rounded-full bg-fill transition-colors data-[state=checked]:bg-good disabled:opacity-50"
    >
      <SwitchPrimitive.Thumb className="block size-[22px] translate-x-[2px] rounded-full bg-white shadow-[0_2px_4px_rgb(0_0_0/0.2)] transition-transform duration-200 data-[state=checked]:translate-x-[20px]" />
    </SwitchPrimitive.Root>
  );
}

/* ------------------------------------------------------------------ Dialog & sheet */

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export function DialogContent({
  title,
  description,
  children,
  className,
  wide,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
  wide?: boolean;
}) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/30 backdrop-blur-[2px] animate-in" />
      <DialogPrimitive.Content
        className={cn(
          "fixed top-1/2 left-1/2 z-50 max-h-[90vh] w-[calc(100vw-32px)] overflow-y-auto rounded-[20px] bg-elevated p-6 shadow-[var(--shadow-lg)] ring-1 ring-hairline [animation:px-scale-in_0.2s_cubic-bezier(0.2,0.8,0.2,1)]",
          "-translate-x-1/2 -translate-y-1/2",
          wide ? "max-w-2xl" : "max-w-md",
          className,
        )}
      >
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <DialogPrimitive.Title className="text-[19px] font-semibold tracking-[-0.02em]">
              {title}
            </DialogPrimitive.Title>
            {description ? (
              <DialogPrimitive.Description className="mt-1 text-[14px] text-muted">
                {description}
              </DialogPrimitive.Description>
            ) : null}
          </div>
          <DialogPrimitive.Close className="rounded-full p-1 text-muted hover:bg-fill-2" aria-label="Close">
            <X className="size-4" />
          </DialogPrimitive.Close>
        </div>
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

export function Sheet({
  open,
  onOpenChange,
  title,
  subtitle,
  children,
  footer,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/25 animate-in" />
        <DialogPrimitive.Content className="fixed inset-y-0 right-0 z-50 flex w-full max-w-xl flex-col bg-elevated shadow-[var(--shadow-lg)] [animation:px-slide-in_0.28s_cubic-bezier(0.2,0.8,0.2,1)]">
          <div className="flex items-start justify-between gap-4 px-6 pt-6 pb-4 hairline-b">
            <div className="min-w-0">
              <DialogPrimitive.Title className="text-[19px] font-semibold tracking-[-0.02em]">
                {title}
              </DialogPrimitive.Title>
              {subtitle ? (
                <DialogPrimitive.Description className="mt-1 text-[13px] text-muted">
                  {subtitle}
                </DialogPrimitive.Description>
              ) : null}
            </div>
            <DialogPrimitive.Close className="rounded-full p-1.5 text-muted hover:bg-fill-2" aria-label="Close">
              <X className="size-4" />
            </DialogPrimitive.Close>
          </div>
          <div className="flex-1 overflow-y-auto px-6 py-5">{children}</div>
          {footer ? (
            <div className="flex flex-wrap justify-end gap-2 px-6 py-4 shadow-[inset_0_1px_0_var(--hairline)]">
              {footer}
            </div>
          ) : null}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/* ------------------------------------------------------------------ Tooltip */

export function Tip({ content, children }: { content: ReactNode; children: ReactNode }) {
  return (
    <TooltipPrimitive.Root delayDuration={250}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          sideOffset={6}
          className="z-50 max-w-xs rounded-[8px] bg-[#1d1d1f] px-2.5 py-1.5 text-[12px] text-white shadow-[var(--shadow-md)] animate-in"
        >
          {content}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}
export const TooltipProvider = TooltipPrimitive.Provider;
