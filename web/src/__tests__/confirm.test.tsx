import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ask, ConfirmHost } from "@/components/ui/confirm";

describe("ask()", () => {
  it("resolves true on confirm and false on cancel", async () => {
    render(<ConfirmHost />);
    let result: Promise<boolean>;
    act(() => {
      result = ask({ title: "Stop this goal?", confirmLabel: "Stop goal", destructive: true });
    });
    fireEvent.click(await screen.findByRole("button", { name: "Stop goal" }));
    await expect(result!).resolves.toBe(true);

    act(() => {
      result = ask({ title: "Remove Asha?", confirmLabel: "Remove" });
    });
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await expect(result!).resolves.toBe(false);
  });

  it("returns typed text, and blocks empty required input", async () => {
    render(<ConfirmHost />);
    let result: Promise<string | null>;
    act(() => {
      result = ask({
        title: "Why was it lost?",
        confirmLabel: "Mark lost",
        input: { label: "Reason", required: true },
      });
    });
    const submit = await screen.findByRole("button", { name: "Mark lost" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "  Budget  " } });
    fireEvent.click(submit);
    await expect(result!).resolves.toBe("Budget");
  });
});
