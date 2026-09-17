import { describe, expect, it } from "vitest";

import { describeElapsed, formatElapsed, humanise } from "./format";

describe("formatElapsed", () => {
  it.each([
    [0, "00:00"],
    [4_000, "00:04"],
    [42_900, "00:42"],
    [61_000, "01:01"],
    [59 * 60_000 + 59_000, "59:59"],
    [3_600_000, "1:00:00"],
    [3_723_000, "1:02:03"],
  ])("formats %i ms as %s", (ms, expected) => {
    expect(formatElapsed(ms)).toBe(expected);
  });

  it("clamps negative values caused by clock skew", () => {
    expect(formatElapsed(-5_000)).toBe("00:00");
  });
});

describe("describeElapsed", () => {
  it("produces a screen-reader friendly phrase", () => {
    expect(describeElapsed(102_000)).toBe("1 minute 42 seconds");
    expect(describeElapsed(1_000)).toBe("1 second");
  });
});

describe("humanise", () => {
  it("keeps acronyms and sentence-cases enum values", () => {
    expect(humanise("SOS_BUTTON")).toBe("SOS button");
    expect(humanise("IN_PROGRESS")).toBe("In progress");
    expect(humanise("AI_CALL_FAILED")).toBe("AI call failed");
  });
});
