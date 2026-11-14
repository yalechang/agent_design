import { useCallback, useEffect, useReducer, useRef } from "react";
import { ApiError, ask } from "../api/client";
import { chatReducer, loadChat, saveChat } from "./chatReducer";

let counter = 0;
const newId = () => `q${Date.now().toString(36)}${(counter++).toString(36)}`;

export function useChat() {
  const [state, dispatch] = useReducer(chatReducer, undefined, loadChat);
  const controllers = useRef(new Map<string, AbortController>());

  useEffect(() => saveChat(state), [state]);
  useEffect(() => () => controllers.current.forEach((c) => c.abort()), []);

  const run = useCallback(async (id: string, question: string) => {
    const ctrl = new AbortController();
    controllers.current.set(id, ctrl);
    try {
      const response = await ask(question, undefined, ctrl.signal);
      dispatch({ type: "answered", id, response });
    } catch (e) {
      const err = e instanceof ApiError ? e : new ApiError(String(e), "network");
      dispatch({ type: "failed", id, error: { message: err.message, kind: err.kind } });
    } finally {
      controllers.current.delete(id);
    }
  }, []);

  const submit = useCallback(
    (question: string) => {
      const id = newId();
      dispatch({ type: "asked", id, question, at: Date.now() });
      void run(id, question);
    },
    [run],
  );

  const retry = useCallback(
    (id: string) => {
      const turn = state.turns.find((t) => t.id === id);
      if (!turn) return;
      dispatch({ type: "retrying", id, at: Date.now() });
      void run(id, turn.question);
    },
    [run, state.turns],
  );

  const cancel = useCallback((id: string) => controllers.current.get(id)?.abort(), []);
  const clear = useCallback(() => {
    controllers.current.forEach((c) => c.abort());
    dispatch({ type: "cleared" });
  }, []);

  const pending = state.turns.some((t) => t.state === "pending");
  return { turns: state.turns, pending, submit, retry, cancel, clear };
}
