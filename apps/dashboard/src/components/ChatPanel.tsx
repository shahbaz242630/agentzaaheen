import { Bot, Mic, SendHorizontal } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { ApiError, api, type ChatAnswer, type ModelChoice } from "../api";

interface Message {
  id: number;
  from: "you" | "agentzaaheen";
  text: string;
  pending?: boolean;
}

export const SUGGESTIONS = [
  "Summarise yesterday's actions",
  "What is escalated to me?",
  "How is revenue this week?",
] as const;

// A model that is not connected answers honestly instead of pretending.
export function notConnectedReply(model: ModelChoice | undefined, projectName: string | null): string {
  const who = model ? model.label : "The model";
  const where = projectName === null ? "all your workspaces" : projectName;
  return (
    `${who} is not connected yet, so I can't answer for real. ` +
    `Once it is, I'll answer from the audit log and the cards for ${where}, and anything I would do ` +
    "becomes a proposal for you to approve."
  );
}

export const SAMPLE_NOTE = "(Some of the numbers behind this answer are sample data.)";

// The answer as shown: the model's words, then the sample note and what it used (skills, reads).
export function answerText(answer: ChatAnswer): string {
  const notes = [answer.from_sample ? SAMPLE_NOTE : null, answer.used.length > 0 ? `Used: ${answer.used.join(" · ")}` : null];
  return [answer.answer, ...notes.filter((note) => note !== null)].join("\n\n");
}

// What went wrong, in words: the server sends a code, never the question or answer.
export function failureReply(error: unknown, model: ModelChoice | undefined, projectName: string | null): string {
  const reason = error instanceof ApiError ? error.code : null;
  switch (reason) {
    case "chat.not_connected":
      return notConnectedReply(model, projectName);
    case "chat.busy":
      return "I'm still answering the last question. Ask again in a moment.";
    case "net.timeout":
      return "The model took too long to answer. Try a shorter question, or ask again.";
    case "net.unavailable":
      return "The model server isn't running. Start it with the local model tool, then ask again.";
    default:
      return `Something went wrong asking the model${reason === null ? "" : ` (${reason})`}. Nothing was changed.`;
  }
}

interface Props {
  models: readonly ModelChoice[];
  projectId: string | null;
  projectName: string | null;
}

export function ChatPanel({ models, projectId, projectName }: Props) {
  // Never fall back to an arbitrary (possibly cloud) model: only the local default.
  const fallback = models.find((m) => m.default && m.where === "local");
  const [modelId, setModelId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [waiting, setWaiting] = useState(false);
  const nextId = useRef(1);
  const endRef = useRef<HTMLDivElement>(null);
  const selectId = useId();
  const inputId = useId();

  const model = models.find((m) => m.id === modelId) ?? fallback;

  useEffect(() => {
    const end = endRef.current;
    // jsdom (tests) has no scrollIntoView.
    if (end !== null && "scrollIntoView" in end) end.scrollIntoView({ block: "end" });
  }, [messages]);

  function reply(id: number, text: string) {
    setMessages((previous) => previous.map((m) => (m.id === id ? { id, from: "agentzaaheen", text } : m)));
  }

  function send(text: string) {
    const question = text.trim();
    if (question === "" || waiting) return;
    const asked: Message = { id: nextId.current++, from: "you", text: question };
    setDraft("");
    if (model?.available !== true) {
      const answer: Message = { id: nextId.current++, from: "agentzaaheen", text: notConnectedReply(model, projectName) };
      setMessages((previous) => [...previous, asked, answer]);
      return;
    }
    const id = nextId.current++;
    setMessages((previous) => [...previous, asked, { id, from: "agentzaaheen", text: "Thinking…", pending: true }]);
    setWaiting(true);
    api
      .ask(projectId, model.id, question)
      .then(
        (answer) => {
          reply(id, answerText(answer));
        },
        (error: unknown) => {
          reply(id, failureReply(error, model, projectName));
        },
      )
      .finally(() => {
        setWaiting(false);
      });
  }

  return (
    <section aria-label="Ask Agent Zaaheen" className="flex h-full min-h-0 flex-col">
      <header className="flex flex-col gap-3 border-b border-line px-5 py-4">
        <div className="flex items-center gap-2">
          <Bot className="size-5 text-brand" aria-hidden="true" />
          <h2 className="font-semibold text-ink">Ask Agent Zaaheen</h2>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={selectId} className="text-xs text-muted">
            Model
          </label>
          <select
            id={selectId}
            value={model?.id ?? ""}
            onChange={(event) => {
              setModelId(event.target.value);
            }}
            className="rounded-lg border border-line bg-panel px-3 py-2 text-sm text-ink focus-visible:outline-2 focus-visible:outline-brand"
          >
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
                {m.default ? " · default" : ""}
                {m.available ? "" : " · not connected"}
              </option>
            ))}
          </select>
          {model ? (
            <p className="text-xs text-faint">{model.note}</p>
          ) : (
            <p className="text-xs text-faint">No model list yet. The chat still answers that it is not connected.</p>
          )}
        </div>
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-5 py-4" aria-live="polite">
        {messages.length === 0 ? (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-muted">
              Ask about {projectName ?? "all your workspaces (the Board)"} by typing. Voice comes after typing works.
            </p>
            <div className="flex flex-col items-start gap-2">
              {SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  disabled={waiting}
                  onClick={() => {
                    send(suggestion);
                  }}
                  className="rounded-full border border-line px-3 py-1.5 text-left text-sm text-ink transition-colors hover:border-brand hover:bg-brand-soft focus-visible:outline-2 focus-visible:outline-brand"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((message) => (
            <p
              key={message.id}
              aria-busy={message.pending === true ? true : undefined}
              className={[
                "max-w-[90%] rounded-2xl px-3.5 py-2.5 text-sm whitespace-pre-wrap",
                message.from === "you" ? "self-end bg-brand text-on-brand" : "self-start bg-raised text-ink",
                message.pending === true ? "animate-pulse text-muted" : "",
              ].join(" ")}
            >
              <span className="sr-only">{message.from === "you" ? "You: " : "Agent Zaaheen: "}</span>
              {message.text}
            </p>
          ))
        )}
        <div ref={endRef} />
      </div>

      <form
        className="flex items-end gap-2 border-t border-line p-3"
        onSubmit={(event) => {
          event.preventDefault();
          send(draft);
        }}
      >
        <label htmlFor={inputId} className="sr-only">
          Message
        </label>
        <textarea
          id={inputId}
          rows={2}
          value={draft}
          maxLength={2000}
          placeholder="Type a question…"
          onChange={(event) => {
            setDraft(event.target.value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              send(draft);
            }
          }}
          className="min-h-11 flex-1 resize-none rounded-xl border border-line bg-panel px-3 py-2 text-sm text-ink placeholder:text-faint focus-visible:outline-2 focus-visible:outline-brand"
        />
        <button
          type="button"
          disabled
          title="Voice comes after typing works"
          aria-label="Voice (coming later)"
          className="grid size-11 place-items-center rounded-xl border border-line text-faint disabled:cursor-not-allowed"
        >
          <Mic className="size-5" aria-hidden="true" />
        </button>
        <button
          type="submit"
          aria-label="Send"
          disabled={draft.trim() === "" || waiting}
          className="grid size-11 place-items-center rounded-xl bg-brand text-on-brand transition-opacity disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
        >
          <SendHorizontal className="size-5" aria-hidden="true" />
        </button>
      </form>
    </section>
  );
}
