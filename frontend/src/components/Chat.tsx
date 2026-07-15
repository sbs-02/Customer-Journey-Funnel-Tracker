import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { sendChat, fetchSuggestedPrompts, type Message } from "../api";
import { ProvenanceCard } from "./ProvenanceCard";

export function Chat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [suggested, setSuggested] = useState<string[]>([]);
  const endRef = useRef<HTMLDivElement>(null);

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
    mutation.mutate(trimmed);
  }

  return (
    <div className="chat">
      <header className="chat__header">
        <h1>Funnel Analyst</h1>
        <p>Ask about visits, leads, opportunities, orders and revenue.
           Every number is read live from the Iceberg lakehouse.</p>
      </header>

      <div className="chat__log">
        {messages.length === 0 && (
          <div className="chat__empty">
            <p>Try one of these:</p>
            {suggested.map((prompt) => (
              <button key={prompt} className="chip" onClick={() => ask(prompt)}>
                {prompt}
              </button>
            ))}
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={`msg msg--${m.role}`}>
            <div className="msg__body">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
            </div>
            {m.toolCalls?.map((call, j) => <ProvenanceCard key={j} call={call} />)}
          </div>
        ))}

        {mutation.isPending && (
          <div className="msg msg--assistant">
            <div className="msg__body typing"><span/><span/><span/></div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form
        className="chat__input"
        onSubmit={(e) => { e.preventDefault(); ask(input); }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="How does this week's lead funnel compare to the same week last year?"
          disabled={mutation.isPending}
          aria-label="Ask a question"
        />
        <button type="submit" disabled={mutation.isPending || !input.trim()}>
          {mutation.isPending ? "Thinking…" : "Ask"}
        </button>
      </form>
    </div>
  );
}