import { describe, expect, it } from "vitest";
import { ERROR_ALERT_THRESHOLD } from "./config";

describe("alert threshold", () => {
  it("fires at 600 critical logs in the last 60 seconds", () => {
    expect(ERROR_ALERT_THRESHOLD).toBe(600);
  });
});