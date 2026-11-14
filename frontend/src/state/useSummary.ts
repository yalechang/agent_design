import { useCallback, useEffect, useState } from "react";
import { ApiError, getSummary } from "../api/client";
import type { SummaryResponse } from "../api/types";

export type SummaryState =
  | { status: "loading" }
  | { status: "ready"; data: SummaryResponse }
  | { status: "error"; message: string };

export function useSummary() {
  const [state, setState] = useState<SummaryState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const ctrl = new AbortController();
    setState({ status: "loading" });
    getSummary(ctrl.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.kind === "cancelled") return;
        setState({ status: "error", message: e instanceof ApiError ? e.message : "The overview could not be loaded." });
      });
    return () => ctrl.abort();
  }, [attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  return { state, reload };
}
