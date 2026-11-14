import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";
import { useChat } from "../state/useChat";
import { Composer, type ComposerHandle } from "./Composer";
import { TurnView } from "./TurnView";

export type ConversationHandle = { ask: (q: string) => void };

export function Conversation({ ref }: { ref?: Ref<ConversationHandle> }) {
  const chat = useChat();
  const composer = useRef<ComposerHandle>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const [announcement, setAnnouncement] = useState("");
  const lastCount = useRef(chat.turns.length);

  useImperativeHandle(ref, () => ({
    ask: (q: string) => {
      if (chat.pending) composer.current?.fill(q);
      else chat.submit(q);
    },
  }));

  // Scroll to a newly asked question.
  useEffect(() => {
    if (chat.turns.length > lastCount.current) endRef.current?.scrollIntoView({ block: "end" });
    lastCount.current = chat.turns.length;
  }, [chat.turns.length]);

  // Announce finished answers to screen readers.
  const settled = chat.turns.filter((t) => t.state !== "pending").length;
  useEffect(() => {
    const last = [...chat.turns].reverse().find((t) => t.state !== "pending");
    if (last?.state === "done") setAnnouncement("An answer is ready.");
    if (last?.state === "failed") setAnnouncement("The question was not answered.");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settled]);

  return (
    <main className="conversation" aria-label="Questions and answers">
      <div className="thread">
        <div className="thread-inner">
          {chat.turns.length === 0 ? (
            <div className="empty-thread">
              <h2>Ask about 2025 spend, vendors or the procurement policy.</h2>
              <p>
                Each answer shows what it is based on: the policy clauses it quotes, the invoices it used, and the
                exchange rates behind any converted amount.
              </p>
            </div>
          ) : (
            chat.turns.map((t) => (
              <TurnView
                key={t.id}
                turn={t}
                onRetry={() => chat.retry(t.id)}
                onCancel={() => chat.cancel(t.id)}
                onEdit={() => composer.current?.fill(t.question)}
              />
            ))
          )}
          <div ref={endRef} />
        </div>
      </div>
      <div className="visually-hidden" aria-live="polite">
        {announcement}
      </div>
      <Composer ref={composer} busy={chat.pending} onSubmit={chat.submit} onClear={chat.turns.length ? chat.clear : undefined} />
    </main>
  );
}
