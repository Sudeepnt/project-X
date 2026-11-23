/**
 * In-app confirmations. `await ask({...})` resolves to true/false, or to the typed text when `input` is set
 * (null if cancelled). Replaces window.confirm/prompt, which block the page and can't be styled.
 */
import * as AlertDialog from "@radix-ui/react-alert-dialog";
import { useEffect, useId, useState } from "react";
import { Button, Input } from "./index";

interface AskOptions {
  title: string;
  body?: string;
  confirmLabel?: string;
  destructive?: boolean;
  input?: { label: string; placeholder?: string; required?: boolean };
}
type Pending = AskOptions & { resolve: (value: boolean | string | null) => void };

let show: ((p: Pending) => void) | null = null;

export function ask(options: AskOptions & { input: AskOptions["input"] & object }): Promise<string | null>;
export function ask(options: AskOptions): Promise<boolean>;
export function ask(options: AskOptions): Promise<boolean | string | null> {
  return new Promise((resolve) => {
    if (!show) {
      resolve(options.input ? null : false);
      return;
    }
    show({ ...options, resolve });
  });
}

export function ConfirmHost() {
  const [pending, setPending] = useState<Pending | null>(null);
  const [text, setText] = useState("");
  const inputId = useId();
  useEffect(() => {
    show = (p) => {
      setText("");
      setPending(p);
    };
    return () => {
      show = null;
    };
  }, []);
  const close = (value: boolean | string | null) => {
    pending?.resolve(value);
    setPending(null);
  };
  const cancelValue = pending?.input ? null : false;
  const blocked = !!pending?.input?.required && !text.trim();
  return (
    <AlertDialog.Root open={!!pending} onOpenChange={(o) => !o && close(cancelValue)}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="fixed inset-0 z-[60] bg-black/30 animate-in" />
        <AlertDialog.Content className="fixed top-1/2 left-1/2 z-[60] w-[calc(100vw-32px)] max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-[20px] bg-elevated p-6 shadow-[var(--shadow-lg)] ring-1 ring-hairline [animation:px-scale-in_0.2s_cubic-bezier(0.2,0.8,0.2,1)]">
          <AlertDialog.Title className="text-[17px] font-semibold tracking-[-0.02em]">
            {pending?.title}
          </AlertDialog.Title>
          {pending?.body ? (
            <AlertDialog.Description className="mt-1.5 text-[14px] text-muted">{pending.body}</AlertDialog.Description>
          ) : (
            <AlertDialog.Description className="sr-only">{pending?.title}</AlertDialog.Description>
          )}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (!blocked) close(pending?.input ? text.trim() : true);
            }}
          >
            {pending?.input ? (
              <div className="mt-4">
                <label htmlFor={inputId} className="mb-1.5 block text-[13px] font-medium text-text-2">
                  {pending.input.label}
                </label>
                <Input
                  id={inputId}
                  autoFocus
                  placeholder={pending.input.placeholder}
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                />
              </div>
            ) : null}
            <div className="mt-6 flex justify-end gap-2">
              <AlertDialog.Cancel asChild>
                <Button>Cancel</Button>
              </AlertDialog.Cancel>
              <Button
                type="submit"
                variant={pending?.destructive ? "danger" : "primary"}
                disabled={blocked}
                autoFocus={!pending?.input}
              >
                {pending?.confirmLabel ?? "Continue"}
              </Button>
            </div>
          </form>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
