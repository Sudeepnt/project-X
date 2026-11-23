/** The asset library grid, uploads, and the picker used to attach media to a post. */
import { FileText, ImagePlus, Search, Trash2, Upload, Video } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";
import { Badge, Button, Dialog, DialogContent, EmptyState, Input } from "@/components/ui";
import { ask } from "@/components/ui/confirm";
import { api, errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { gpath, useAction, useApi } from "@/lib/queries";

export interface Asset {
  id: string;
  kind: "image" | "video" | "document";
  name: string;
  mime: string;
  size_bytes: number;
  width: number | null;
  height: number | null;
  tags: string[];
  alt_text: string | null;
  storage_key: string;
  url: string;
}

const kb = (n: number) =>
  n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;

function Thumb({ a, className }: { a: Asset; className?: string }) {
  if (a.kind === "image")
    return (
      <img src={a.url} alt={a.alt_text ?? ""} loading="lazy" className={cn("size-full object-cover", className)} />
    );
  return (
    <div className={cn("flex size-full items-center justify-center bg-fill-2 text-muted", className)}>
      {a.kind === "video" ? <Video className="size-8" /> : <FileText className="size-8" />}
    </div>
  );
}

export function useUpload(bid: string, onDone?: (a: Asset) => void) {
  const [busy, setBusy] = useState(0);
  const upload = useAction(
    async (files: File[]) => {
      setBusy(files.length);
      const out: Asset[] = [];
      for (const file of files) {
        const form = new FormData();
        form.append("file", file);
        try {
          out.push(await api.post<Asset>(gpath(bid, "/assets"), form));
        } catch (e) {
          toast.error(`${file.name}: ${errorMessage(e)}`);
        } finally {
          setBusy((n) => n - 1);
        }
      }
      return out;
    },
    {
      invalidate: [gpath(bid, "/assets")],
      onSuccess: (r) => {
        if (r.length) toast.success(`Uploaded ${r.length}`);
        for (const a of r) onDone?.(a);
      },
    },
  );
  return { upload: (files: File[]) => upload.mutate(files), busy: busy > 0 };
}

function DropZone({ bid, onDone, compact }: { bid: string; onDone?: (a: Asset) => void; compact?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const { upload, busy } = useUpload(bid, onDone);
  return (
    <button
      type="button"
      onClick={() => input.current?.click()}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        upload(Array.from(e.dataTransfer.files));
      }}
      className={cn(
        "flex w-full flex-col items-center justify-center gap-1 rounded-[14px] border border-dashed text-center transition-colors",
        compact ? "py-4" : "py-8",
        over ? "border-accent bg-accent-soft" : "border-[var(--hairline-strong)] hover:bg-fill-2",
      )}
    >
      <Upload className="size-5 text-muted" />
      <span className="text-[14px] font-medium">{busy ? "Uploading…" : "Drop files or click to upload"}</span>
      <span className="text-[12px] text-muted">
        Images up to 20 MB, video up to 200 MB, PDF. Photo location data is removed.
      </span>
      <input
        ref={input}
        type="file"
        multiple
        accept="image/jpeg,image/png,image/webp,image/gif,video/mp4,video/quicktime,video/webm,application/pdf"
        className="sr-only"
        onChange={(e) => {
          upload(Array.from(e.target.files ?? []));
          e.target.value = "";
        }}
      />
    </button>
  );
}

function AssetEditor({ bid, a, onClose }: { bid: string; a: Asset; onClose: () => void }) {
  const [alt, setAlt] = useState(a.alt_text ?? "");
  const [tags, setTags] = useState(a.tags.join(", "));
  const save = useAction(
    () =>
      api.patch(gpath(bid, `/assets/${a.id}`), {
        alt_text: alt,
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      }),
    { invalidate: [gpath(bid, "/assets")], success: "Saved", onSuccess: onClose },
  );
  const remove = useAction(() => api.delete(gpath(bid, `/assets/${a.id}`)), {
    invalidate: [gpath(bid, "/assets")],
    success: "Deleted",
    onSuccess: onClose,
  });
  return (
    <div className="space-y-4">
      <div className="aspect-video overflow-hidden rounded-[12px] bg-fill-2">
        <Thumb a={a} className="object-contain" />
      </div>
      <div className="text-[12px] text-muted">
        {a.mime} · {kb(a.size_bytes)}
        {a.width ? ` · ${a.width}×${a.height}` : ""}
      </div>
      <label className="block text-[13px] font-medium text-text-2" htmlFor="alt">
        Alt text <span className="font-normal text-muted">(read aloud to people who can't see it)</span>
      </label>
      <Input
        id="alt"
        value={alt}
        onChange={(e) => setAlt(e.target.value)}
        placeholder="Describe what's in the picture"
      />
      <label className="block text-[13px] font-medium text-text-2" htmlFor="tags">
        Tags
      </label>
      <Input id="tags" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="team, product, event" />
      <div className="flex justify-between">
        <Button
          variant="ghost"
          className="text-critical"
          icon={<Trash2 className="size-4" />}
          loading={remove.isPending}
          onClick={async () =>
            (await ask({ title: `Delete ${a.name}?`, confirmLabel: "Delete", destructive: true })) && remove.mutate()
          }
        >
          Delete
        </Button>
        <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
      </div>
    </div>
  );
}

export function AssetGrid({
  bid,
  onPick,
  picked = [],
}: {
  bid: string;
  onPick?: (a: Asset) => void;
  picked?: string[];
}) {
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<Asset | null>(null);
  const { data } = useApi<{ items: Asset[] }>(gpath(bid, `/assets${q ? `?q=${encodeURIComponent(q)}` : ""}`));
  return (
    <div className="space-y-4">
      <DropZone bid={bid} onDone={onPick} compact={!!onPick} />
      <div className="relative">
        <Search className="absolute top-3 left-3 size-4 text-faint" />
        <Input
          className="pl-9"
          placeholder="Search name, alt text or tag"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      {data?.items.length ? (
        <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {data.items.map((a) => {
            const on = picked.includes(a.storage_key);
            return (
              <li key={a.id}>
                <button
                  type="button"
                  onClick={() => (onPick ? onPick(a) : setEditing(a))}
                  aria-pressed={onPick ? on : undefined}
                  className={cn(
                    "group block w-full overflow-hidden rounded-[14px] bg-surface text-left ring-1 transition-shadow hover:shadow-[var(--shadow-md)]",
                    on ? "ring-2 ring-accent" : "ring-hairline",
                  )}
                >
                  <div className="aspect-square overflow-hidden">
                    <Thumb a={a} className="transition-transform group-hover:scale-[1.02]" />
                  </div>
                  <div className="px-2.5 py-2">
                    <div className="truncate text-[12px] font-medium">{a.name}</div>
                    <div className="flex items-center gap-1 text-[11px] text-muted">
                      {kb(a.size_bytes)}
                      {!a.alt_text && a.kind === "image" ? <Badge tone="warning">No alt text</Badge> : null}
                    </div>
                  </div>
                </button>
              </li>
            );
          })}
        </ul>
      ) : data ? (
        <EmptyState
          icon={<ImagePlus className="size-6" />}
          title={q ? "Nothing matches" : "No files yet"}
          body="Upload photos, videos and documents to use in posts."
        />
      ) : null}
      <Dialog open={!!editing} onOpenChange={(o) => !o && setEditing(null)}>
        {editing ? (
          <DialogContent title={editing.name}>
            <AssetEditor bid={bid} a={editing} onClose={() => setEditing(null)} />
          </DialogContent>
        ) : null}
      </Dialog>
    </div>
  );
}

export function MediaPicker({
  bid,
  open,
  setOpen,
  onPick,
}: {
  bid: string;
  open: boolean;
  setOpen: (o: boolean) => void;
  onPick: (a: Asset) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent title="Add media" description="Pick from your library, or upload something new." wide>
        <AssetGrid
          bid={bid}
          onPick={(a) => {
            onPick(a);
            setOpen(false);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}
