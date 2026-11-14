import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { AskResponse, SummaryResponse } from "../api/types";
import { dataAnswer, outOfScope, policyAnswer, queryFailed, summaryNoFx, summaryOk } from "./fixtures";

type Handler = (url: string, init?: RequestInit) => Promise<Response>;
const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }));

function mockFetch(summary: SummaryResponse | Handler, answers: (AskResponse | Handler)[] = []) {
  const queue = [...answers];
  const fn = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/summary")) return typeof summary === "function" ? summary(url, init) : json(summary);
    const next = queue.shift();
    if (!next) throw new Error("unexpected /ask");
    return typeof next === "function" ? next(url, init) : json(next);
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

async function runAxe(container: HTMLElement) {
  const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } }); // jsdom has no layout
  return result.violations.map((v) => `${v.id}: ${v.nodes.length}`);
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => vi.unstubAllGlobals());

describe("summary panel", () => {
  it("shows something before any question: loading, then the ledger", async () => {
    let release!: () => void;
    mockFetch(() => new Promise((r) => (release = () => r(new Response(JSON.stringify(summaryOk))))));
    render(<App />);
    expect(screen.getByText("Loading the invoice overview…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /top 5 vendors/ })).toBeInTheDocument(); // examples usable while loading
    release();
    expect(await screen.findByText("EUR 6,887,110.91")).toBeInTheDocument();
    expect(screen.getByText("1,377,856.93")).toBeInTheDocument();
    expect(screen.getByText(/as the policy \(§5.1\) requires/)).toBeInTheDocument();
    expect(screen.getByText(/16 invoices fell on a weekend or TARGET holiday/)).toBeInTheDocument();
    expect(screen.getByText(/ECB reference rates/, { selector: "strong" })).toBeInTheDocument();
  });

  it("keeps original currencies usable and hides the EUR total when rates are missing", async () => {
    mockFetch(summaryNoFx);
    render(<App />);
    expect(await screen.findByText("No EUR total is shown")).toBeInTheDocument();
    expect(screen.getByText("4,493,726.12")).toBeInTheDocument();
    expect(screen.queryByText(/EUR 6,887,110.91/)).not.toBeInTheDocument();
    expect(screen.getAllByText("unavailable").length).toBeGreaterThan(0);
    expect(screen.getByText(/Ranking in EUR is unavailable/)).toBeInTheDocument();
  });

  it("explains a failed load and retries", async () => {
    const fetchFn = mockFetch((() => {
      let n = 0;
      return () => (n++ === 0 ? json({}, 500) : json(summaryOk));
    })());
    render(<App />);
    expect(await screen.findByText("The overview did not load")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load the overview again" }));
    expect(await screen.findByText("EUR 6,887,110.91")).toBeInTheDocument();
    expect(fetchFn).toHaveBeenCalledTimes(2);
  });
});

describe("asking questions", () => {
  it("shows a policy answer with its basis and the verbatim clause", async () => {
    mockFetch(summaryOk, [policyAnswer]);
    render(<App />);
    await userEvent.type(screen.getByLabelText("Your question"), "Who approves EUR 60,000?{Enter}");
    expect(await screen.findByText("Answered from the evidence")).toBeInTheDocument();
    const basedOn = screen.getByRole("list", { name: "Based on" });
    expect(within(basedOn).getByText("Procurement policy").closest("li")).toHaveClass("verified");
    expect(within(basedOn).getByText("Invoice and vendor records").closest("li")).toHaveClass("unused");
    expect(screen.getByText(/Required Approver: Finance Director/)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Policy clauses/ })).toHaveAttribute("aria-selected", "true");
  });

  it("shows limitations and never a partial total", async () => {
    mockFetch(summaryOk, [dataAnswer]);
    render(<App />);
    await userEvent.type(screen.getByLabelText("Your question"), "Total spend using exact rates?{Enter}");
    expect(await screen.findByText("Answered with limitations")).toBeInTheDocument();
    expect(screen.getByText(/1 invoice has no acceptable ECB reference rate/)).toBeInTheDocument();
    const result = screen.getByRole("table", { name: /Result/ });
    expect(within(result).getByText("unavailable")).toHaveClass("missing");
    await userEvent.click(screen.getByRole("tab", { name: /Invoices/ }));
    expect(screen.getByText("no rate")).toBeInTheDocument();
    expect(screen.getAllByText("missing").length).toBe(2); // missing PO and missing approver cells
    await userEvent.click(screen.getByRole("tab", { name: /Exchange rates/ }));
    expect(screen.getByText("ECB euro reference rates, as policy §5.1 requires")).toBeInTheDocument();
    expect(screen.getByText(/no reference rate on 2025-03-08/)).toBeInTheDocument();
    expect(screen.queryByText(/used an earlier date because/)).not.toBeInTheDocument();   // no fallback here
  });

  it("flags conversions that used an earlier ECB date", async () => {
    const fallback = structuredClone(dataAnswer);
    fallback.evidence.fx!.refs = [{ ...fallback.evidence.fx!.refs[0], status: "FALLBACK", observation_date: "2025-03-07",
      fallback_days: 1, legs: [{ currency: "USD", value: "1.0857" }], invoice_numbers: ["INV-1"], missing_evidence: [] }];
    fallback.evidence.fx!.unavailable_invoices = [];
    fallback.evidence.fx!.convention_note = "Prototype convention: weekend or TARGET holiday.";
    mockFetch(summaryOk, [fallback]);
    render(<App />);
    await userEvent.type(screen.getByLabelText("Your question"), "Total?{Enter}");
    await userEvent.click(await screen.findByRole("tab", { name: /Exchange rates/ }));
    expect(screen.getByText(/1 conversion used an earlier date because the ECB published no rate/)).toBeInTheDocument();
    expect(screen.getByText("No ECB rate that day, 1 day earlier")).toBeInTheDocument();
    expect(screen.getByText("1.0857")).toBeInTheDocument();
  });

  it("evidence tabs support arrow keys", async () => {
    mockFetch(summaryOk, [dataAnswer]);
    render(<App />);
    await userEvent.type(screen.getByLabelText("Your question"), "Total?{Enter}");
    const first = await screen.findByRole("tab", { name: "Result" });
    first.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: /Invoices/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /Invoices/ })).toHaveFocus();
  });

  it("turns a failed query into guidance with an edit action", async () => {
    mockFetch(summaryOk, [queryFailed]);
    render(<App />);
    await userEvent.type(screen.getByLabelText("Your question"), "Spend by region?{Enter}");
    expect(await screen.findByText("This question could not be run on the data")).toBeInTheDocument();
    expect(screen.getByText(/Available invoice fields/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Edit the question" }));
    expect(screen.getByLabelText("Your question")).toHaveValue("Spend by region?");
  });

  it("states out-of-scope plainly", async () => {
    mockFetch(summaryOk, [outOfScope]);
    render(<App />);
    await screen.findByText("EUR 6,887,110.91");
    await userEvent.click(screen.getByRole("button", { name: /top 5 vendors/ }));
    expect(await screen.findByText("Outside the available evidence")).toBeInTheDocument();
  });

  it("blocks a second question while one is pending, and can cancel", async () => {
    mockFetch(summaryOk, [
      (_u, init) => new Promise((_res, rej) => init?.signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")))),
    ]);
    render(<App />);
    await userEvent.type(screen.getByLabelText("Your question"), "Slow question{Enter}");
    expect(await screen.findByText("Working on it")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Answering…" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("You cancelled this question.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask again" })).toBeInTheDocument();
  });

  it("explains a network failure and retries", async () => {
    mockFetch(summaryOk, [() => Promise.reject(new TypeError("Failed to fetch")), policyAnswer]);
    render(<App />);
    await userEvent.type(screen.getByLabelText("Your question"), "Who approves EUR 60,000?{Enter}");
    expect(await screen.findByText(/could not be reached/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Ask again" }));
    expect(await screen.findByText("Answered from the evidence")).toBeInTheDocument();
  });

  it("explains a busy server", async () => {
    mockFetch(summaryOk, [() => json({ detail: "busy" }, 503)]);
    render(<App />);
    await userEvent.type(screen.getByLabelText("Your question"), "Who approves EUR 60,000?{Enter}");
    expect(await screen.findByText(/answering other questions right now/)).toBeInTheDocument();
  });

  it("rejects over-long questions before sending", async () => {
    const fetchFn = mockFetch(summaryOk);
    render(<App />);
    const box = screen.getByLabelText("Your question");
    await userEvent.click(box);
    await userEvent.paste("x".repeat(1001));
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    expect(box).toHaveAttribute("aria-invalid", "true");
    expect(fetchFn).toHaveBeenCalledTimes(1); // only /summary
  });
});

describe("accessibility", () => {
  it("has no axe violations on load and with a full answer", async () => {
    mockFetch(summaryOk, [dataAnswer]);
    const { container } = render(<App />);
    await screen.findByText("EUR 6,887,110.91");
    expect(await runAxe(container)).toEqual([]);
    await userEvent.type(screen.getByLabelText("Your question"), "Total?{Enter}");
    await screen.findByText("Answered with limitations");
    expect(await runAxe(container)).toEqual([]);
  });
});
