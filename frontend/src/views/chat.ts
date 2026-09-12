/** chat.ts — the command center (Tier 1: you are watching).
 *
 * One composer, three kinds of work: the MODE selector decides whether a
 * message is answered here (chat), becomes a discrete agent task, or
 * launches deep research — matching the backend's /api/chat modes. Files
 * and images attach via /api/upload and ride as ids.
 *
 * Two rules of the conversation model:
 *
 *   - Every conversation HAS a kind and keeps it. Escalating a plain chat
 *     into a task mid-way spins the task off into a NEW conversation — a
 *     dialog offers "bring context / just this prompt / cancel" (never a
 *     silent kind change).
 *   - A task conversation WATCHES its work: agent/research progress
 *     streams into the conversation live, and the outcome lands in it as
 *     a message. The organizer tabs organize; they don't deliver.
 *
 * Streaming rules inherited from the guide's walking skeleton:
 *   - model output renders via textContent ONLY (XSS)
 *   - SSE frames are reassembled with proper buffering (api.readSSE)
 *   - Stop aborts the fetch → the backend cancels and frees the slot
 *   - destroy() aborts too — switching views mid-stream must not leave a
 *     ghost generation holding a Tier-1 slot
 */

import { get, post, readSSE } from "../api";
import { on } from "../bus";
import { el, mount } from "../dom";
import { icon } from "../icons";
import { renderMarkdown } from "../md";
import type { ViewHandle } from "../main";
import { drawScene } from "../avatar/scene";
import { openDocument } from "../docviewer";
import { openFile as openInCode } from "../code";
import { cardProgress, cardSettle, codeCard } from "../codecard";
import { todoPanel, todoUpdate, toolCard, toolCardSettle, type ToolResult } from "../toolcards";
import { refreshSessions } from "../sessions";
import { ACCENTS, prefs, resolvedTheme } from "../theme";

/** The active conversation id — module-level so it survives view switches
 *  (coming back to Chat reopens where you left off). */
let sessionId: string | null = null;

/** The active conversation's KIND ("chat" | "agent" | "research") — what
 *  the dedicated-conversation rule checks against. */
let sessionKind: string | null = null;

/** The composer mode, module-level so the organizer tabs' "start new"
 *  buttons can preselect it before switching to Chat. */
let mode: "chat" | "agent" | "research" = "chat";

/** Called by other views: open Chat with this mode already selected. */
export function presetMode(m: "chat" | "agent" | "research"): void {
  mode = m;
}

/** Called by the sessions rail / organizer tabs: open Chat ON this
 *  conversation ("every task is a special kind of chat"). */
export function presetSession(id: string): void {
  sessionId = id;
  sessionKind = null;                  // openSession learns the real kind
}

/** Called by the rail's "New conversation": next mount starts fresh. */
export function presetNew(): void {
  sessionId = null;
  sessionKind = null;
}

/** The rail asks: which conversation is open? (for its highlight). */
export function currentSessionId(): string | null {
  return sessionId;
}

/** One pending attachment chip: the upload's id + display name. */
interface Attachment { id: string; name: string; kind: string; }

/** Human names for conversation kinds (dialog copy + feed headers). */
const KIND_LABEL: Record<string, string> = {
  chat: "regular chat", agent: "agent task", research: "deep research",
};

/** Human phrasing for research phases (the live feed's vocabulary). */
const RESEARCH_PHASES: Record<string, string> = {
  planning: "breaking the question into sub-questions",
  searching: "deciding what to search",
  reading: "reading pages",
  writing: "updating the draft report",
  deciding: "deciding whether to keep going",
  finalizing: "polishing the final report",
};

export function show(container: HTMLElement): ViewHandle {
  const subs: (() => void)[] = [];             // bus unsubscribes for destroy()
  let aborter: AbortController | null = null;  // the in-flight stream, if any
  let attachments: Attachment[] = [];          // chips waiting on the composer
  let dialogScrim: HTMLElement | null = null;  // the mode-switch dialog, if open
  let reloadTimer: number | null = null;       // post-completion refresh delay
  let pendingReload = false;                   // a job finished mid-stream: reload after
  let destroyed = false;                       // guards async work landing post-teardown

  // ---- The message column ------------------------------------------------
  const log = el("div.chat-log");
  let heroTicker: number | null = null;        // the greeting scene's clock

  /** The empty-conversation greeting: Seymour playing in the sand while
   *  he waits, plus a time-of-day hello. Purely decorative (the STATUS
   *  avatar in his panel stays the honest one); cleared on first message. */
  function renderHero(): void {
    const hour = new Date().getHours();
    const hello = hour < 5 ? "Up late? Me too."
      : hour < 12 ? "Good morning!"
      : hour < 18 ? "Good afternoon!"
      : "Good evening!";
    const canvas = el("canvas.hero-canvas") as unknown as HTMLCanvasElement;
    canvas.width = 256;
    canvas.height = 140;
    let frame = 0;
    const paint = () => drawScene(canvas.getContext("2d")!, {
      state: "play", frame, blink: frame === 1 && Math.random() < 0.3,
      onBattery: false, theme: resolvedTheme(),
      avatarHue: prefs().avatarHue, screen: prefs().avatarScreen, accentHue: ACCENTS[prefs().accent] ?? 90,
    });
    if (heroTicker !== null) clearInterval(heroTicker);
    heroTicker = setInterval(() => { frame = 1 - frame; paint(); },
                             600) as unknown as number;
    paint();
    mount(log, el("div.hero", {},
      canvas,
      el("h2", {}, hello),
      el("p.muted", {}, "Ask me anything, hand me a task, or send me "
        + "researching — I'll be in the sand."),
    ));
  }

  /** First real content: clear the greeting (and stop its clock). */
  function clearHero(): void {
    if (heroTicker !== null) { clearInterval(heroTicker); heroTicker = null; }
    log.querySelector(".hero")?.remove();
  }

  /** Append one message row; returns the body element.
   *  Assistant text renders as MARKDOWN through md.ts's sanitizing gate
   *  (4.5); the user's own words stay verbatim plain text. */
  function row(role: "user" | "assistant", text: string): HTMLElement {
    clearHero();
    const body = el("div.msg-body", {});
    if (role === "assistant") {
      if (text) renderMarkdown(body, text);
    } else {
      body.textContent = text;
    }
    const rowEl = role === "assistant"
      ? el("div.msg-row.assistant", {}, el("div.msg-avatar", {}, icon("eye")), body)
      : el("div.msg-row.user", {}, body);
    log.append(rowEl);
    rowEl.scrollIntoView({ block: "end" });
    return body;
  }

  /** The live approval card, while one is pending. */
  let approvalCard: HTMLElement | null = null;

  /** Ask permission for a write, inline. One yes covers the whole run
   *  (Nick's rule: a real checkpoint, not a nag on every write). */
  function askApproval(ask: { run_id: string; summary: string }): void {
    clearHero();
    approvalCard?.remove();
    const answer = async (allow: boolean) => {
      // Optimistically retire the card; the run's own "approved" frame
      // is the authority and would retire it anyway.
      approvalCard?.remove();
      approvalCard = null;
      try {
        await post(`/api/runs/${ask.run_id}/approve`, { allow });
      } catch { /* the run moved on or timed out — nothing to undo */ }
    };
    const card = el("div.sys-note.approval", {},
      el("div", {}, `Seymour wants to ${ask.summary}.`),
      el("div.approval-actions", {},
        el("button.primary", { type: "button", onclick: () => answer(true) },
           "Allow for this task"),
        el("button", { type: "button", onclick: () => answer(false) },
           "Not this time"),
      ));
    approvalCard = card;
    log.append(card);
    card.scrollIntoView({ block: "end" });
  }

  /** The run answered (or timed out): the card's job is done. */
  function resolveApproval(allowed: boolean): void {
    approvalCard?.remove();
    approvalCard = null;
    if (!allowed) sysNote("Declined — Seymour left your files alone.");
  }

  /** A SYSTEM notice in the conversation: Seymour's own voice, not the
   *  model's — used for authoritative facts like the vision diagnosis. */
  function sysNote(text: string): void {
    clearHero();
    const note = el("div.sys-note", {}, text);
    log.append(note);
    note.scrollIntoView({ block: "end" });
  }

  // ---- The live job feed -------------------------------------------------
  // When this conversation IS a task (agent/research), its progress
  // streams here: every real event the run publishes, as it happens.
  let liveJobId: string | null = null;         // the run being watched
  let liveKind: "agent" | "research" = "agent";
  let feedCard: HTMLElement | null = null;     // the whole feed message row
  let feedBody: HTMLElement | null = null;     // where lines append

  /** Create (once) and return the feed's line container. */
  function ensureFeed(kind: "agent" | "research"): HTMLElement {
    if (feedBody) return feedBody;
    clearHero();
    liveKind = kind;
    const head = el("div.feed-head", {});
    head.append(icon(kind),
                document.createTextNode(`${KIND_LABEL[kind]} — live`));
    // The inline cancel (4.1): a live run is stoppable FROM ITS THREAD,
    // not just from a tab somewhere else. The run's own lifecycle events
    // then finish the feed and persist the outcome, as with any end.
    head.append(el("button.feed-cancel", {
      type: "button",
      title: "stop this run (drafts and progress are kept)",
      onclick: async () => {
        if (!liveJobId) return;
        try {
          await post(liveKind === "research"
            ? `/api/research/${liveJobId}/cancel`
            : `/api/tasks/${liveJobId}/cancel`);
        } catch { /* already finished — the feed will say so */ }
      },
    }, "Cancel"));
    feedBody = el("div.job-feed");
    feedCard = el("div.msg-row.assistant", {},
      el("div.msg-avatar", {}, icon(kind)),
      el("div.msg-body.job-live", {}, head, feedBody));
    log.append(feedCard);
    feedCard.scrollIntoView({ block: "end" });
    return feedBody;
  }

  /** Append one line to the live feed (bounded; auto-scrolls). */
  function feedLine(text: string): void {
    const body = ensureFeed(liveKind);
    body.append(el("div.feed-line", {}, text));
    while (body.children.length > 200) body.firstChild?.remove();
    body.scrollTop = body.scrollHeight;
  }

  /** The run ended (any way): note it, then reload the conversation so
   *  the PERSISTED outcome message (report/result) replaces the feed. */
  function finishFeed(text: string): void {
    feedLine(text);
    feedCard?.classList.add("done");
    liveJobId = null;
    if (reloadTimer !== null) clearTimeout(reloadTimer);
    reloadTimer = setTimeout(() => {
      // A chat reply streaming in THIS conversation must not be wiped
      // off the screen by the reload — defer it to the stream's end
      // (dispatch's finally picks pendingReload up).
      if (aborter) { pendingReload = true; return; }
      if (sessionId) openSession(sessionId);
      refreshSessions();
    }, 400) as unknown as number;
  }

  /** Drop the feed (used when re-rendering the whole conversation). */
  function clearFeed(): void {
    feedCard?.remove();
    feedCard = null;
    feedBody = null;
  }

  /** The measured-speed footer under an assistant reply (the TPS marker). */
  function statsFooter(stats: Record<string, any>): HTMLElement {
    const parts: string[] = [];
    if (stats.decode_tps) parts.push(`${Number(stats.decode_tps).toFixed(1)} tok/s`);
    if (stats.generated_tokens) parts.push(`${stats.generated_tokens} tokens`);
    if (stats.ttft_s != null) parts.push(`first token ${stats.ttft_s}s`);
    if (stats.tool_rounds) parts.push(`${stats.tool_rounds} tool call${stats.tool_rounds > 1 ? "s" : ""}`);
    parts.push(stats.tps_source === "engine" ? "engine-timed" : "wall-clock");
    const footer = el("div.msg-stats", {}, "");
    footer.append(icon("bolt"), document.createTextNode(parts.join(" · ")));
    return footer;
  }

  /** Open a conversation: history, kind, and — when its work is still
   *  live in a slot — the re-attached progress feed. */
  async function openSession(id: string): Promise<void> {
    type SessionDetail = {
      id: string; kind: string; job_id: string; active: boolean; live_run_id?: string;
      messages: { role: string; content: string }[];
    };
    let session: SessionDetail;
    try {
      session = await get<SessionDetail>(`/api/sessions/${id}`);
    } catch {
      // 4.3: the conversation is GONE (deleted while an organizer row
      // or stale state still pointed at it). Never fabricate one — the
      // backend deliberately 404s — say so and reset to a fresh thread.
      if (destroyed || sessionId !== id) return;
      sessionId = null;
      sessionKind = null;
      liveJobId = null;
      clearFeed();
      log.replaceChildren();
      sysNote("That conversation no longer exists — it may have been "
        + "deleted. Start a new one whenever you're ready.");
      refreshSessions();
      return;
    }
    // Staleness guard: while this GET was in flight the user may have
    // clicked New conversation (sessionId → null) or another row
    // (sessionId → other id), or left the view. A stale response must
    // not clobber module state — the NEXT message would silently land
    // in the wrong conversation.
    if (destroyed || sessionId !== id) return;
    sessionId = session.id;
    sessionKind = session.kind || "chat";
    liveJobId = null;
    clearFeed();
    log.replaceChildren();
    for (const message of session.messages) {
      if (message.role === "user" || message.role === "assistant") {
        row(message.role as "user" | "assistant", message.content);
      }
    }
    // A chat run still going (detached server-side): re-attach to it —
    // its frames so far replay, then the rest arrive live.
    if (session.live_run_id && !aborter) void reattach(session.live_run_id);
    // A task conversation whose run still holds a slot: watch it again.
    if (session.active && session.job_id
        && (sessionKind === "agent" || sessionKind === "research")) {
      liveJobId = session.job_id;
      ensureFeed(sessionKind as "agent" | "research");
      feedLine("re-attached — live progress appears here");
    }
    refreshSessions();
  }

  // ---- The mode selector + attachments row -------------------------------
  const modeRow = el("div.mode-row");
  /** This conversation's thinking override: "auto" follows the saved
   *  inference settings; "on"/"off" ride along on each message as an
   *  override the run's trace records. Module-level state would leak
   *  between conversations, so it lives with the view. */
  let thinking: "auto" | "on" | "off" = "auto";
  /** Redraw the three mode pills with the current one active. */
  function renderModes(): void {
    const label: Record<string, string> = {
      chat: "Chat", research: "Deep research",
    };
    // TWO options only (stage 4's mode collapse): a chat carries the
    // full tool catalog and the model decides what an input needs —
    // declaring "this is an agent task" was ceremony. Deep Research
    // stays because it is a genuinely different SHAPE of work.
    mount(modeRow, ...(["chat", "research"] as const).map((m) =>
      el("button", {
        className: `choice mode-pill${mode === m ? " active" : ""}`,
        onclick: () => { mode = m; renderModes(); },
        title: m === "chat"
          ? "Answer here — Seymour uses tools (web, workspace) when the "
            + "message needs them"
          : "Becomes a deep-research run — searches, reads sources, "
            + "synthesizes a cited report",
      }, label[m])),
      // The thinking toggle cycles auto → on → off. It is a per-message
      // OVERRIDE of the Inference settings, so it reads as one.
      el("button", {
        className: `choice mode-pill think-pill${thinking !== "auto" ? " active" : ""}`,
        onclick: () => {
          thinking = thinking === "auto" ? "on" : thinking === "on" ? "off" : "auto";
          renderModes();
        },
        title: "Thinking for this conversation: auto follows Settings → "
          + "Inference; on/off override it for every message you send here",
      }, `thinking: ${thinking}`));
  }

  const chipsRow = el("div.chips-row");
  /** Redraw the attachment chips. */
  function renderChips(): void {
    mount(chipsRow, ...attachments.map((a) =>
      el("span.chip", {},
        a.kind === "image" ? "🖼 " : "📄 ", a.name,
        el("button.del", {
          onclick: () => {
            attachments = attachments.filter((x) => x.id !== a.id);
            renderChips();
          },
        }, "×"))));
  }

  // The hidden file input the paperclip button clicks for us.
  const fileInput = el("input", { autocomplete: "off" }) as HTMLInputElement;
  fileInput.type = "file";
  fileInput.multiple = true;
  fileInput.hidden = true;
  fileInput.onchange = async () => {
    for (const file of Array.from(fileInput.files ?? [])) {
      const form = new FormData();
      form.append("file", file);
      try {
        const response = await fetch("/api/upload", { method: "POST", body: form });
        if (!response.ok) throw new Error((await response.json()).detail);
        const uploaded = await response.json();
        attachments.push({ id: uploaded.id, name: uploaded.name, kind: uploaded.kind });
      } catch (error: any) {
        alert(`Upload failed: ${error?.message ?? "unknown error"}`);
      }
    }
    fileInput.value = "";
    renderChips();
  };

  // ---- The mode-switch dialog --------------------------------------------
  /** Escalating a dedicated conversation into a different task kind: ask
   *  what the user meant, with the three honest options. The input is NOT
   *  cleared until they choose — Cancel must cost nothing. */
  function openModeDialog(message: string, target: "agent" | "research"): void {
    closeDialog();
    const fromKind = KIND_LABEL[sessionKind ?? "chat"] ?? "regular chat";
    const scrim = el("div.dialog-scrim", {
      onclick: (event: Event) => {       // clicking the backdrop = Cancel
        if (event.target === scrim) closeDialog();
      },
    });
    scrim.append(el("div.card.dialog", {},
      el("h3", {}, "Start a new conversation?"),
      el("p", {}, `You've picked ${KIND_LABEL[target]} for this message, `
        + `but this is a dedicated ${fromKind} conversation — every `
        + `conversation keeps one kind.`),
      el("div.dialog-actions", {},
        el("button.primary", {
          type: "button",
          onclick: () => {
            const from = sessionId;      // grab BEFORE starting fresh
            closeDialog();
            startFresh();
            commitAndDispatch(message, target, from);
          },
        }, "Yes — bring context from this conversation"),
        el("button", {
          type: "button",
          onclick: () => {
            closeDialog();
            startFresh();
            commitAndDispatch(message, target, null);
          },
        }, "Yes — just this prompt"),
        el("button", { type: "button", onclick: closeDialog }, "Cancel"),
      )));
    dialogScrim = scrim;
    document.body.append(scrim);
  }

  function closeDialog(): void {
    dialogScrim?.remove();
    dialogScrim = null;
  }

  /** Reset to a brand-new conversation (the dialog's "yes" paths). */
  function startFresh(): void {
    sessionId = null;
    sessionKind = null;
    liveJobId = null;
    clearFeed();
    log.replaceChildren();
  }

  // ---- The composer ------------------------------------------------------
  // A multi-line, auto-growing textarea (4.6): Enter sends, Shift+Enter
  // inserts a newline; height follows content up to a cap, then scrolls.
  // Newlines ride verbatim all the way to the model.
  const input = el("textarea.chat-input", {
    placeholder: "Ask, or describe a task… (Shift+Enter for a new line)",
  }) as HTMLTextAreaElement;
  input.rows = 1;
  const autosize = () => {
    input.style.height = "auto";               // measure fresh each time
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      (composer as HTMLFormElement).requestSubmit();
    }
  });
  // type:"button" is LOAD-BEARING: a button inside a form defaults to
  // type=submit, and the form's IMPLICIT submission (pressing Enter)
  // "clicks" the first submit button — which was the paperclip, so
  // hitting Send opened the file picker (a confirmed bug).
  const attach = el("button", {
    type: "button", title: "Attach files (pdf, docx, csv, md, images…)",
  });
  attach.append(icon("clip"));
  attach.onclick = () => fileInput.click();
  const send = el("button.primary", {}, "Send");
  const stop = el("button", { type: "button", hidden: true }, "Stop");

  /** Clear the composer, then send (the point of no return). */
  function commitAndDispatch(message: string, sentMode: typeof mode,
                             contextFrom: string | null): void {
    input.value = "";
    autosize();                          // a grown textarea shrinks back
    const sentAttachments = attachments.map((a) => a.id);
    attachments = [];
    renderChips();
    void dispatch(message, sentMode, sentAttachments, contextFrom);
  }

  /** Send one message: task modes get a live feed, chat mode streams. */
  async function dispatch(message: string, sentMode: typeof mode,
                          sentAttachments: string[],
                          contextFrom: string | null): Promise<void> {
    row("user", message
      + (sentAttachments.length ? `  (${sentAttachments.length} attached)` : ""));
    const body = JSON.stringify({
      session_id: sessionId, message, mode: sentMode,
      attachments: sentAttachments, context_from: contextFrom,
      // Only a real override travels; "auto" sends nothing so the saved
      // settings decide (and the trace says so).
      inference: thinking === "auto" ? null : { thinking },
    });

    // Agent/research modes return JSON (a job pointer), not a stream —
    // then the conversation WATCHES the run via the live feed.
    if (sentMode !== "chat") {
      try {
        const response = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
        });
        if (!response.ok) throw new Error((await response.json()).detail);
        const started = await response.json();
        sessionId = started.session_id;
        sessionKind = sessionKind ?? sentMode;
        liveJobId = started.job.id;
        ensureFeed(sentMode);
        feedLine(sentMode === "research"
          ? "deep research started — planning…"
          : "agent task started — thinking…");
        refreshSessions();
      } catch (error: any) {
        row("assistant", `[${error?.message ?? "failed to start the job"}]`);
      }
      return;
    }

    // Plain chat: stream. The run is DETACHED server-side (live_runs):
    // this response is only its first consumer, Stop is an explicit
    // cancel route, and a tab that comes back re-attaches through
    // openSession. What the frames become in the thread is one place —
    // makeConsumer — shared by this path and the re-attach path.
    const consumer = makeConsumer();
    send.hidden = true; stop.hidden = false;
    aborter = new AbortController();
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
        signal: aborter.signal,
      });
      await consumeRun(response, consumer);
    } finally {
      finishRun();
    }
  }

  /** What a run's frames become in the thread: one assistant row, its
   *  cards (code, diff, terminal, search, result) and the plan panel.
   *  The reply's SOURCE OF TRUTH is the `raw` markdown string; the
   *  bubble re-renders from it on a throttle through md.ts's sanitizing
   *  gate. Re-rendering the whole (small) message is deliberate: with no
   *  frozen prefix there is nothing to corrupt, and the final paint is
   *  always canonical. */
  interface Consumer { handle(frame: any): void; finish(): void; lost(text: string): void }
  let currentRunId: string | null = null;      // the run this tab is watching

  function makeConsumer(): Consumer {
    const reply = row("assistant", "");
    // Cards live beside the prose (renderMarkdown replaces the prose
    // node's children, and a card must survive every repaint).
    const cards = el("div.cards");
    reply.parentElement!.after(cards);       // below the bubble, full width — not a flex sibling beside it
    const htmlOutputs: string[] = [];        // .html files this run wrote
    // Until the first visible token: a pulsing ellipsis. Reasoning
    // models can think for many seconds before anything visible
    // arrives — dead air reads as "broken", a pulse reads as thinking.
    reply.classList.add("thinking");
    reply.textContent = "…";
    let raw = "";                            // the accumulated markdown
    let liveEl: HTMLElement | null = null;   // the code card of the call being written, live
    let pendingTool: HTMLElement | null = null; // the tool card of the call now executing
    let todo: HTMLElement | null = null;     // the plan panel, once todo_write has run
    let paintTimer: number | null = null;
    const paint = (final = false) => {
      if (paintTimer !== null) { clearTimeout(paintTimer); paintTimer = null; }
      renderMarkdown(reply, raw, !final);
      reply.scrollIntoView({ block: "end" });
    };
    const queuePaint = () => {               // ~8 paints/s, not per-token
      if (paintTimer !== null) return;
      paintTimer = setTimeout(() => { paintTimer = null; paint(); },
                              120) as unknown as number;
    };
    const wake = () => {                     // first real content: drop the pulse
      if (reply.classList.contains("thinking")) { reply.classList.remove("thinking"); reply.textContent = ""; }
    };
    return {
      handle(frame: any) {
        // The run names itself first: that id is what Stop cancels.
        if (frame.run_id) currentRunId = frame.run_id;
        // Then the session (so turn 2 can reference it).
        if (frame.session_id) {
          const isNew = sessionId !== frame.session_id;
          sessionId = frame.session_id;
          if (isNew) { sessionKind = "chat"; refreshSessions(); }
        }
        // Authoritative system notices (e.g. the vision diagnosis) render
        // in Seymour's own voice, not as model output.
        if (frame.notice) sysNote(frame.notice);
        // The context economy did something this round: one quiet line
        // (the trace has the full account).
        if (frame.economy) {
          const e = frame.economy;
          sysNote(e.what === "compacted"
            ? `compacted ${e.messages} earlier messages into a summary · ~${e.tokens} → ~${e.now_tokens} tokens`
            : `pruned ${e.results} older result(s) (${Number(e.chars).toLocaleString()} chars) · ~${e.now_tokens} tokens`);
        }
        // The write gate: Seymour is about to change the person's files
        // and is asking — IN the thread, once per run, never a modal.
        if (frame.approval) askApproval(frame.approval);
        // Answered (by these buttons or by timeout): retire the card.
        if (frame.approved !== undefined) resolveApproval(frame.approved);
        // A mid-prose tool call was trimmed server-side: the trimmed
        // text replaces the source outright.
        if (frame.replace !== undefined) {
          reply.classList.remove("thinking");
          raw = frame.replace;
          paint();
        }
        // The plan (todo_write): outside the context window, visible at
        // a glance, above the cards.
        if (frame.todos) {
          wake();
          if (!todo) { todo = todoPanel(); cards.prepend(todo); }
          todoUpdate(todo, frame.todos);
        }
        if (frame.tool_progress) {
          // The call is being written: the code card shows the file
          // growing, in the chat, in place of the pulsing placeholder.
          const p = frame.tool_progress;
          wake();
          if (!liveEl) { liveEl = codeCard(p.path ?? null, p.name); cards.append(liveEl); }
          cardProgress(liveEl, p);
        }
        if (frame.tool) {
          // The call executes now. File writes keep their code card;
          // every other tool gets its own card (terminal, diff, search,
          // result) — running, then settled by tool_result.
          const name: string = frame.tool.name;
          const path = String(frame.tool.args?.path ?? "");
          wake(); paint();
          if (name === "write_file" || name === "append_file") {
            // Remember HTML files this run writes: they get an "open" chip
            // when the run ends (the viewer shows them as a sandboxed app).
            if (/\.html?$/i.test(path) && !htmlOutputs.includes(path)) htmlOutputs.push(path);
          } else {
            liveEl?.remove(); liveEl = null;   // an edit's live text becomes its diff card
            pendingTool = toolCard(name, frame.tool.args ?? {}, frame.tool.summary ?? `running ${name}`);
            cards.append(pendingTool);
            pendingTool.scrollIntoView({ block: "end" });
          }
        }
        if (frame.tool_result) {
          const r = frame.tool_result as ToolResult;
          if (pendingTool) {
            toolCardSettle(pendingTool, r, (p) => void openInCode(p));
            pendingTool = null;
          } else if (r.path && (r.name === "write_file" || r.name === "append_file")) {
            // The card (or a new one, for calls that streamed too fast to
            // show) settles into its final state with the check's verdict.
            const card = liveEl ?? codeCard(r.path, r.name);
            if (!card.isConnected) cards.append(card);
            void cardSettle(card, r);
            liveEl = null;
          } else {
            liveEl?.remove(); liveEl = null;
          }
        }
        if (frame.delta) {
          wake();
          liveEl = null;                          // a settled card stays in the thread
          raw += frame.delta;
          queuePaint();
        }
        if (frame.stats) {
          reply.parentElement!.append(statsFooter(frame.stats));
          if (htmlOutputs.length) {
            // One chip per produced page: opens in the document viewer.
            reply.parentElement!.append(el("div.chips-row", {},
              ...htmlOutputs.flatMap((path) => [el("button.chip", {
                onclick: () => openDocument({
                  title: path,
                  fileUrl: `/api/workspace/file?path=${encodeURIComponent(path)}`,
                  meta: "produced by this run · runs sandboxed",
                }),
              }, `▶ open ${path}`),
              // The same file in the Code pane: editor + preview + check.
              el("button.chip", { onclick: () => void openInCode(path) }, `✎ edit ${path}`)])));
          }
        }
        if (frame.error) {
          wake();
          raw += frame.error === "stopped" ? "\n[stopped]" : `\n[${frame.error}]`;
          paint();
        }
      },
      finish() { paint(true); },             // the canonical final render
      lost(text: string) {
        // The connection went, the run did not: say so and keep the partial.
        (liveEl as HTMLElement | null)?.remove(); liveEl = null;
        wake();
        raw += `\n[${text}]`;
        paint(true);
      },
    };
  }

  /** Read a run's SSE stream into a consumer until it ends. */
  async function consumeRun(response: Response, consumer: Consumer): Promise<void> {
    try {
      await readSSE(response, (payload) => {
        if (payload === "[DONE]") return;
        consumer.handle(JSON.parse(payload));
      });
      consumer.finish();
    } catch (error: any) {
      // An AbortError is this view going away (destroy): the run keeps
      // going server-side and openSession re-attaches later — nothing to
      // say here. Anything else is a real connection loss.
      if (error?.name !== "AbortError") {
        consumer.lost(`${error?.message ?? "connection lost"} — the run continues on the server; reopen this conversation to re-attach`);
      }
    }
  }

  /** The composer's state after a run (started here or re-attached). */
  function finishRun(): void {
    aborter = null;
    currentRunId = null;
    send.hidden = false; stop.hidden = true;
    // A job finished while this reply streamed: run its deferred
    // reload now (the persisted outcome AND this reply both show).
    if (pendingReload && !destroyed) {
      pendingReload = false;
      if (sessionId) void openSession(sessionId);
      refreshSessions();
    }
    input.focus();
  }

  /** Re-attach to a run that is already going (this conversation was
   *  reopened, or another tab started it): its frames so far replay,
   *  coalesced, then the rest arrive live. */
  async function reattach(runId: string): Promise<void> {
    const consumer = makeConsumer();
    currentRunId = runId;
    send.hidden = true; stop.hidden = false;
    aborter = new AbortController();
    try {
      const response = await fetch(`/api/runs/${runId}/live`, { signal: aborter.signal });
      if (response.status === 404) {
        // Finished and gone from the live registry between the session
        // read and now: the persisted message is the record — reload.
        consumer.lost("that run has finished — reloading");
        if (sessionId) void openSession(sessionId);
        return;
      }
      await consumeRun(response, consumer);
    } finally {
      finishRun();
    }
  }

  const composer = el("form.composer", {
    onsubmit: (event: Event) => {
      event.preventDefault();
      const message = input.value.trim();
      if (!message || aborter) return;         // empty, or already streaming
      const sentMode = mode;
      // The dedicated-conversation rule: choosing a TASK mode inside an
      // existing conversation of a different kind never mutates it —
      // the dialog offers a new conversation instead. An UNKNOWN kind
      // (openSession still in flight) counts as "chat": skipping the
      // dialog in that window would silently graft a task onto a
      // conversation whose kind never changes server-side.
      if (sessionId && sentMode !== "chat"
          && sentMode !== (sessionKind ?? "chat")) {
        openModeDialog(message, sentMode);
        return;                                // input stays put until chosen
      }
      commitAndDispatch(message, sentMode, null);
    },
  }, attach, input, send, stop);

  // Stop is an EXPLICIT cancel of the run (a closed tab is no longer one):
  // the server records it with the partial call's head, as before.
  stop.onclick = () => {
    if (currentRunId) void post(`/api/runs/${currentRunId}/cancel`).catch(() => { /* already over */ });
    else aborter?.abort();
  };

  // ---- Mount + subscriptions --------------------------------------------
  mount(container, log, el("div.composer-block", {}, chipsRow, modeRow, composer, fileInput));
  renderModes();
  renderChips();
  if (sessionId) openSession(sessionId);       // returning to a live convo
  else renderHero();                           // a fresh chat greets you
  input.focus();

  // The live feed follows the run's REAL events (research pipeline).
  subs.push(on("research", (e) => {
    if (!liveJobId || e.data.job_id !== liveJobId) return;
    if (e.type === "phase") {
      const text = RESEARCH_PHASES[e.data.phase] ?? e.data.phase;
      feedLine(e.data.round ? `round ${e.data.round} — ${text}…` : `${text}…`);
    } else if (e.type === "query") {
      feedLine(`searching: “${e.data.query}”`);
    } else if (e.type === "read") {
      feedLine(`reading ${e.data.url}`);
    } else if (e.type === "read_failed") {
      feedLine(`couldn't read ${e.data.url} — skipping`);
    } else if (e.type === "round_read") {
      feedLine(`round ${e.data.round}: ${e.data.useful} useful page(s) of ${e.data.pages}`);
    } else if (e.type === "finished") {
      finishFeed(`finished (${e.data.status})`);
    }
  }));
  // Discrete task lifecycle (started/blocked/done/…) on the tasks topic…
  subs.push(on("tasks", (e) => {
    if (!liveJobId || e.data.task_id !== liveJobId) return;
    if (e.type === "blocked") finishFeed(`needs your input: ${e.data.question ?? ""}`);
    else if (e.type === "done") finishFeed("task complete");
    else if (e.type === "paused") finishFeed("paused");
    else if (e.type === "cancelled") finishFeed("cancelled");
  }));
  // …and its step-by-step journal lines on the shared agent topic.
  subs.push(on("agent", (e) => {
    if (!liveJobId || e.type !== "step" || e.data.task_id !== liveJobId) return;
    const content = String(e.data.content ?? "");
    feedLine(`[${e.data.kind}] `
      + (content.length > 220 ? content.slice(0, 220) + "…" : content));
  }));

  return {
    destroy() {
      destroyed = true;                // stale async work must not land
      // Close this view's stream. The run itself is detached and keeps
      // going; reopening the conversation re-attaches to it.
      aborter?.abort();
      // Stop the greeting scene's clock (nothing should tick unseen).
      if (heroTicker !== null) clearInterval(heroTicker);
      if (reloadTimer !== null) clearTimeout(reloadTimer);
      closeDialog();                   // a body-level overlay must not outlive us
      subs.forEach((u) => u());
    },
  };
}
