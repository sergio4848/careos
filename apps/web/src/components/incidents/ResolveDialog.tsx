"use client";

import { RESOLUTION_CATEGORIES, type ResolutionCategory } from "@careos/contracts";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { humanise } from "@/lib/format";

import styles from "./ResolveDialog.module.css";

export function ResolveDialog({
  subject,
  submitting,
  error,
  onSubmit,
  onCancel,
}: {
  subject: string;
  submitting: boolean;
  error?: string | null;
  onSubmit: (values: { category: ResolutionCategory; notes?: string }) => void;
  onCancel: () => void;
}) {
  const [category, setCategory] = useState<ResolutionCategory | "">("");
  const [notes, setNotes] = useState("");
  const [touched, setTouched] = useState(false);

  const categoryError = touched && !category ? "Choose a resolution category." : null;

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (!category) return;
    onSubmit({ category, notes: notes.trim() || undefined });
  };

  return (
    <Modal
      title="Resolve incident"
      description={`Record how the alarm for ${subject} was resolved. The incident stays open for review until it is closed.`}
      onClose={onCancel}
    >
      <form className={styles.form} onSubmit={handleSubmit} noValidate>
        <label className={styles.field}>
          <span>Resolution category</span>
          <select
            value={category}
            onChange={(event) => setCategory(event.target.value as ResolutionCategory)}
            aria-invalid={Boolean(categoryError)}
            aria-describedby={categoryError ? "resolve-category-error" : undefined}
            required
          >
            <option value="">Select…</option>
            {RESOLUTION_CATEGORIES.map((value) => (
              <option key={value} value={value}>
                {humanise(value)}
              </option>
            ))}
          </select>
          {categoryError ? (
            <span id="resolve-category-error" className={styles.error}>
              {categoryError}
            </span>
          ) : null}
        </label>

        <label className={styles.field}>
          <span>Notes (optional)</span>
          <textarea
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            rows={4}
            maxLength={2000}
            placeholder="What happened and who responded?"
          />
        </label>

        {error ? (
          <p role="alert" className={styles.error}>
            {error}
          </p>
        ) : null}

        <div className={styles.actions}>
          <Button variant="ghost" onClick={onCancel} disabled={submitting}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={submitting}>
            Resolve incident
          </Button>
        </div>
      </form>
    </Modal>
  );
}
