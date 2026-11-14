import type { AskResponse } from "../api/types";

export type TurnError = { message: string; kind: string };

export type Turn = {
  id: string;
  question: string;
  askedAt: number;
  state: "pending" | "done" | "failed";
  response?: AskResponse;
  error?: TurnError;
};

export type ChatState = { turns: Turn[] };

export type ChatAction =
  | { type: "asked"; id: string; question: string; at: number }
  | { type: "answered"; id: string; response: AskResponse }
  | { type: "failed"; id: string; error: TurnError }
  | { type: "retrying"; id: string; at: number }
  | { type: "cleared" };

export const initialChat: ChatState = { turns: [] };

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "asked":
      return {
        turns: [...state.turns, { id: action.id, question: action.question, askedAt: action.at, state: "pending" }],
      };
    case "answered":
      return update(state, action.id, { state: "done", response: action.response, error: undefined });
    case "failed":
      return update(state, action.id, { state: "failed", error: action.error });
    case "retrying":
      return update(state, action.id, { state: "pending", askedAt: action.at, error: undefined, response: undefined });
    case "cleared":
      return initialChat;
  }
}

function update(state: ChatState, id: string, patch: Partial<Turn>): ChatState {
  return { turns: state.turns.map((t) => (t.id === id ? { ...t, ...patch } : t)) };
}

const STORAGE_KEY = "procurement-qa.chat.v1";

/** Restore a saved conversation. A question still pending at reload is marked as interrupted. */
export function loadChat(): ChatState {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return initialChat;
    const saved = JSON.parse(raw) as ChatState;
    return {
      turns: saved.turns.map((t) =>
        t.state === "pending"
          ? { ...t, state: "failed", error: { kind: "interrupted", message: "The page was reloaded before this answer arrived." } }
          : t,
      ),
    };
  } catch {
    return initialChat;
  }
}

export function saveChat(state: ChatState): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    /* storage full or disabled: the conversation simply isn't kept across reloads */
  }
}
