import { describe, expect, it } from "vitest";
import { chatReducer, initialChat, loadChat, saveChat } from "../state/chatReducer";
import { policyAnswer } from "./fixtures";

describe("chatReducer", () => {
  it("moves a turn through pending, answered, failed and retry", () => {
    let s = chatReducer(initialChat, { type: "asked", id: "a", question: "Q", at: 1 });
    expect(s.turns[0].state).toBe("pending");
    s = chatReducer(s, { type: "failed", id: "a", error: { kind: "network", message: "down" } });
    expect(s.turns[0]).toMatchObject({ state: "failed", error: { kind: "network" } });
    s = chatReducer(s, { type: "retrying", id: "a", at: 2 });
    expect(s.turns[0]).toMatchObject({ state: "pending", askedAt: 2, error: undefined });
    s = chatReducer(s, { type: "answered", id: "a", response: policyAnswer });
    expect(s.turns[0].response?.trace_id).toBe("trace-policy-1");
    expect(chatReducer(s, { type: "cleared" }).turns).toEqual([]);
  });

  it("marks a question pending at reload as interrupted", () => {
    saveChat({ turns: [{ id: "x", question: "Q", askedAt: 1, state: "pending" }] });
    const restored = loadChat();
    expect(restored.turns[0].state).toBe("failed");
    expect(restored.turns[0].error?.kind).toBe("interrupted");
  });

  it("ignores corrupt storage", () => {
    sessionStorage.setItem("procurement-qa.chat.v1", "{not json");
    expect(loadChat()).toEqual(initialChat);
  });
});
