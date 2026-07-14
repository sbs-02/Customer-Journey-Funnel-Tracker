export interface Provenance {
  snapshot_id: string;
  snapshot_committed_at: string;
  as_of_date: string;
  date_range: Record<string, unknown>;
  source_tables: string[];
  calculation: string;
}

export interface ToolCall {
  name: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown> & { provenance?: Provenance; error?: string };
}

export interface ChatResponse {
  answer: string;
  tool_calls: ToolCall[];
  model: string;
}

export interface Message {
  role: "user" | "assistant";
  content: string;
  toolCalls?: ToolCall[];
}

const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export async function sendChat(
  message: string,
  history: Message[],
): Promise<ChatResponse> {
  const res = await fetch(`${API}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      // The backend re-attaches its own system prompt, so only the plain
      // conversation turns are sent. Tool calls stay client-side for display.
      history: history.map(({ role, content }) => ({ role, content })),
    }),
  });

  if (!res.ok) {
    throw new Error(`Backend returned ${res.status}. Is the API running on ${API}?`);
  }
  return res.json();
}

export async function fetchSuggestedPrompts(): Promise<string[]> {
  const res = await fetch(`${API}/suggested-prompts`);
  if (!res.ok) return [];
  const data = await res.json();
  return data.prompts ?? [];
}