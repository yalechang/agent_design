import { useImperativeHandle, useRef, useState, type FormEvent, type KeyboardEvent, type Ref } from "react";

export type ComposerHandle = { fill: (q: string) => void };
const MAX = 1000;

type Props = { busy: boolean; onSubmit: (q: string) => void; onClear?: () => void; ref?: Ref<ComposerHandle> };

export function Composer({ busy, onSubmit, onClear, ref }: Props) {
  const [text, setText] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  const trimmed = text.trim();
  const tooLong = text.length > MAX;
  const canSend = !busy && trimmed.length > 0 && !tooLong;

  useImperativeHandle(ref, () => ({
    fill: (q: string) => {
      setText(q);
      input.current?.focus();
    },
  }));

  const send = (e?: FormEvent) => {
    e?.preventDefault();
    if (!canSend) return;
    onSubmit(trimmed);
    setText("");
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="composer">
      <form onSubmit={send} aria-describedby="composer-hint">
        <label htmlFor="question-input">Your question</label>
        <div className="composer-row">
          <textarea
            id="question-input"
            ref={input}
            value={text}
            rows={2}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKey}
            placeholder="For example: total spend with vendors on hold, in EUR"
            aria-invalid={tooLong}
          />
          <button type="submit" className="btn primary" disabled={!canSend}>
            {busy ? "Answering…" : "Ask"}
          </button>
        </div>
        <div className="composer-hint" id="composer-hint">
          <span>
            {busy
              ? "Wait for the current answer, or cancel it, before asking the next question."
              : "Press Enter to ask, Shift+Enter for a new line."}
          </span>
          <span>
            <span className={tooLong ? "over" : undefined}>
              {text.length.toLocaleString("en-GB")} / {MAX.toLocaleString("en-GB")}
            </span>
            {onClear && (
              <>
                {"  "}
                <button type="button" className="link-btn" onClick={onClear} style={{ marginLeft: "1rem" }}>
                  Clear conversation
                </button>
              </>
            )}
          </span>
        </div>
      </form>
    </div>
  );
}
