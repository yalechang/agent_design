import { useCallback, useRef } from "react";
import { Masthead } from "./components/Masthead";
import { SummaryPanel } from "./components/SummaryPanel";
import { Conversation, type ConversationHandle } from "./components/Conversation";
import { useSummary } from "./state/useSummary";

export function App() {
  const summary = useSummary();
  const conversation = useRef<ConversationHandle>(null);
  const askExample = useCallback((q: string) => conversation.current?.ask(q), []);

  return (
    <>
      <a className="skip-link" href="#question-input">
        Skip to the question box
      </a>
      <div className="app">
        <Masthead summary={summary.state} />
        <aside className="ledger-column" aria-label="2025 spend overview">
          <SummaryPanel state={summary.state} onRetry={summary.reload} onAsk={askExample} />
        </aside>
        <Conversation ref={conversation} />
      </div>
    </>
  );
}
