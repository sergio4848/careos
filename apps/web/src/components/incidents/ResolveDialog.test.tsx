import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ResolveDialog } from "./ResolveDialog";

describe("ResolveDialog", () => {
  it("is an accessible modal that requires a category", async () => {
    const onSubmit = vi.fn();
    render(<ResolveDialog subject="Margaret Wilson" submitting={false} onSubmit={onSubmit} onCancel={vi.fn()} />);

    const dialog = screen.getByRole("dialog", { name: "Resolve incident" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByLabelText("Resolution category")).toHaveFocus();

    await userEvent.click(screen.getByRole("button", { name: "Resolve incident" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("Choose a resolution category.")).toBeInTheDocument();
  });

  it("allows resolving without notes", async () => {
    const onSubmit = vi.fn();
    render(<ResolveDialog subject="Margaret Wilson" submitting={false} onSubmit={onSubmit} onCancel={vi.fn()} />);
    await userEvent.selectOptions(screen.getByLabelText("Resolution category"), "USER_SAFE");
    await userEvent.click(screen.getByRole("button", { name: "Resolve incident" }));
    expect(onSubmit).toHaveBeenCalledWith({ category: "USER_SAFE", notes: undefined });
  });

  it("submits trimmed values", async () => {
    const onSubmit = vi.fn();
    render(<ResolveDialog subject="Margaret Wilson" submitting={false} onSubmit={onSubmit} onCancel={vi.fn()} />);

    await userEvent.selectOptions(screen.getByLabelText("Resolution category"), "FAMILY_RESPONDED");
    await userEvent.type(screen.getByLabelText("Notes (optional)"), "  Sarah attended; Margaret is safe.  ");
    await userEvent.click(screen.getByRole("button", { name: "Resolve incident" }));

    expect(onSubmit).toHaveBeenCalledWith({ category: "FAMILY_RESPONDED", notes: "Sarah attended; Margaret is safe." });
  });

  it("closes on Escape", async () => {
    const onCancel = vi.fn();
    render(<ResolveDialog subject="Margaret Wilson" submitting={false} onSubmit={vi.fn()} onCancel={onCancel} />);
    await userEvent.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalled();
  });
});
