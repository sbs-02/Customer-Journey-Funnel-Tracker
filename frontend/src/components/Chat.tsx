import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";

import { sendChat, fetchSuggestedPrompts, type Message } from "../api";
import { ProvenanceCard } from "./ProvenanceCard";

// Wide result tables shouldn't break the layout on a phone; give every table
// its own horizontal scroll rail so the message column itself never overflows.
const markdownComponents: Components = {
  table: ({ children }) => (
    <div className="table-wrap">
      <table>{children}</table>
    </div>
  ),
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noreferrer noopener">
      {children}
    </a>
  ),
};

/** The analyst's mark: a funnel narrowing through the stages it reports on. */
function Mark({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" aria-hidden="true">
      <rect x="2" y="3" width="12" height="2" rx="1" />
      <rect x="4" y="7" width="8" height="2" rx="1" />
      <rect x="6" y="11" width="4" height="2" rx="1" />
    </svg>
  );
}

export function Chat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [suggested, setSuggested] = useState<string[]>([]);
  const endRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { fetchSuggestedPrompts().then(setSuggested); }, []);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const mutation = useMutation({
    mutationFn: (text: string) => sendChat(text, messages),
    onSuccess: (data) => {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: data.answer, toolCalls: data.tool_calls },
      ]);
    },
    onError: (error: Error) => {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: `**Something went wrong.**\n\n${error.message}` },
      ]);
    },
  });

  function ask(text: string) {
    const trimmed = text.trim();
    if (!trimmed || mutation.isPending) return;
    setMessages((prev) => [...prev, { role: "user", content: trimmed }]);
    setInput("");
    if (boxRef.current) boxRef.current.style.height = "auto";
    mutation.mutate(trimmed);
  }

  // The composer grows with the question instead of scrolling a one-line field.
  function resize(el: HTMLTextAreaElement) {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }

  const empty = messages.length === 0;

  return (
    <div className="chat">
      <header className="chat__header">
        <span className="chat__brand">
          <Mark className="chat__brand-mark" />
        </span>
        <span className="chat__titles">
          <h1 className="chat__title">Funnel Analyst</h1>
          <span className="chat__source">Customer journey data · Iceberg lakehouse</span>
        </span>
      </header>

      <div className="chat__log" role="log" aria-live="polite">
        <div className="chat__stream">
          {empty && (
            <div className="chat__empty">
              <span className="chat__empty-mark">
                <Mark />
              </span>
              <h2 className="chat__empty-title">What do you want to know?</h2>
              <p className="chat__note">
                Visits, leads, opportunities, orders and revenue. Each answer
                shows the snapshot, date range and calculation it was read from.
              </p>

              <div className="chat__chips">
                {suggested.map((prompt) => (
                  <button key={prompt} className="chip" onClick={() => ask(prompt)}>
                    <span className="chip__dot" aria-hidden="true" />
                    <span className="chip__text">{prompt}</span>
                    <svg className="chip__go" viewBox="0 0 16 16" aria-hidden="true">
                      <path
                        d="M6 3.5 10.5 8 6 12.5"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.6"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m, i) => (
            <article key={i} className={`turn turn--${m.role}`}>
              {m.role === "assistant" && (
                <span className="turn__avatar" aria-hidden="true">
                  <Mark />
                </span>
              )}
              <div className="turn__main">
                <span className="turn__who">
                  {m.role === "user" ? "You" : "Analyst"}
                </span>
                <div className="turn__bubble">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={markdownComponents}
                  >
                    {m.content}
                  </ReactMarkdown>
                </div>
                {m.toolCalls?.map((call, j) => <ProvenanceCard key={j} call={call} />)}
              </div>
            </article>
          ))}

          {mutation.isPending && (
            <article className="turn turn--assistant">
              <span className="turn__avatar" aria-hidden="true">
                <Mark />
              </span>
              <div className="turn__main">
                <span className="turn__who">Analyst</span>
                <div className="turn__bubble">
                  <span className="reading">
                    <span className="reading__dots" aria-hidden="true">
                      <i /><i /><i />
                    </span>
                    Reading the lakehouse
                  </span>
                </div>
              </div>
            </article>
          )}
          <div ref={endRef} />
        </div>
      </div>

      <form
        className="chat__dock"
        onSubmit={(e) => { e.preventDefault(); ask(input); }}
      >
        <div className="composer">
          <textarea
            ref={boxRef}
            rows={1}
            value={input}
            onChange={(e) => { setInput(e.target.value); resize(e.target); }}
            onKeyDown={(e) => {
              // Enter sends; Shift+Enter starts a new line.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                ask(input);
              }
            }}
            placeholder="Ask about visits, leads, orders or revenue…"
            disabled={mutation.isPending}
            aria-label="Ask a question"
          />
          <button
            className="composer__send"
            type="submit"
            disabled={mutation.isPending || !input.trim()}
            aria-label="Ask"
          >
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path
                d="M8 13V3.5M8 3.5 4 7.5M8 3.5l4 4"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>
      </form>
    </div>
  );
}
