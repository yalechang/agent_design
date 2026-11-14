import { describe, expect, it } from "vitest";
import { sourceSlots } from "../sources";
import { dataAnswer, outOfScope, policyAnswer, queryFailed } from "./fixtures";

const states = (r: Parameters<typeof sourceSlots>[0]) => sourceSlots(r).map((s) => `${s.key}:${s.state}:${s.stateText}`);

describe("sourceSlots", () => {
  it("policy answer uses only the policy", () => {
    expect(states(policyAnswer)).toEqual(["policy:verified:Quoted and checked", "records:unused:Not used", "fx:unused:Not used"]);
  });
  it("missing rates make records and rates limited", () => {
    expect(states(dataAnswer)).toEqual([
      "policy:unused:Not used",
      "records:limited:Some figures unavailable",
      "fx:limited:Some rates missing",
    ]);
  });
  it("out of scope uses nothing", () => {
    expect(sourceSlots(outOfScope).every((s) => s.state === "unused")).toBe(true);
  });
  it("failed query is shown as limited records, rates not reached", () => {
    expect(states(queryFailed)).toEqual(["policy:unused:Not used", "records:limited:Query could not run", "fx:unused:Not reached"]);
  });
});
