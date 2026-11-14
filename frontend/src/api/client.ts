import type { AskResponse, ReportingCurrency, SummaryResponse } from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "/api";
export const ASK_TIMEOUT_MS = 120_000;

/** A failure the UI can explain: what happened and whether retrying makes sense. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly kind: "network" | "timeout" | "server" | "invalid" | "cancelled" | "busy",
    readonly status?: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit, timeoutMs: number, signal?: AbortSignal): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort("timeout"), timeoutMs);
  const onAbort = () => ctrl.abort("cancelled");
  signal?.addEventListener("abort", onAbort);
  try {
    const res = await fetch(`${BASE}${path}`, { ...init, signal: ctrl.signal });
    if (res.status === 422) {
      throw new ApiError("The question was not accepted. Use 1 to 1,000 characters.", "invalid", 422);
    }
    if (res.status === 503) {
      throw new ApiError("The assistant is answering other questions right now. Try again in a moment.", "busy", 503);
    }
    if (!res.ok) {
      throw new ApiError(`The server returned an error (HTTP ${res.status}).`, "server", res.status);
    }
    return (await res.json()) as T;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    if (ctrl.signal.aborted) {
      throw ctrl.signal.reason === "cancelled"
        ? new ApiError("You cancelled this question.", "cancelled")
        : new ApiError(`No answer within ${Math.round(timeoutMs / 1000)} seconds.`, "timeout");
    }
    throw new ApiError("The server could not be reached. Check that the backend is running.", "network");
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
}

export function getSummary(signal?: AbortSignal): Promise<SummaryResponse> {
  return request<SummaryResponse>("/summary", { method: "GET" }, 30_000, signal);
}

export function ask(question: string, reportingCurrency?: ReportingCurrency, signal?: AbortSignal) {
  return request<AskResponse>(
    "/ask",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, reporting_currency: reportingCurrency ?? null }),
    },
    ASK_TIMEOUT_MS,
    signal,
  );
}
