"""Run scope for tools: which run is calling, and its side channels.

Most tools are pure functions of their arguments. A few need to know
WHICH run they serve — todo_write keeps a plan per run, the persistent
shell keeps a session per run, ask_user_question must reach the person
watching THIS run, read_image hands back an image the executor must
attach to the next message. Passing a run object through every tool
signature would make the registry's contract ugly for the 95 % that do
not care; a ContextVar carries it instead. The executor (chat) and the
agent loop set `run_id` before executing a call and clear it after; a
tool called outside any run (the workspace route's run-tool, a test)
sees "" and behaves as a plain function.
"""

import contextvars
from typing import Any, Awaitable, Callable

# The run (chat run id or agent task id) the current tool call serves.
run_id: contextvars.ContextVar[str] = contextvars.ContextVar("seymour_run_id", default="")
# How deep in subagents this call is: 0 for a person's run, 1 inside a
# `task` child. A child may not spawn children (bounded recursion).
depth: contextvars.ContextVar[int] = contextvars.ContextVar("seymour_depth", default=0)
# Attachments a tool wants carried into the NEXT prompt message as
# non-text content (read_image): a list of OpenAI-style content parts.
_attachments: contextvars.ContextVar[list | None] = contextvars.ContextVar("seymour_attachments", default=None)
# The channel through which a tool asks the person a question and gets
# the answer (ask_user_question): set by the chat executor per run.
# Signature: async (question: dict) -> str answer.
_asker: contextvars.ContextVar[Callable[[dict], Awaitable[str]] | None] = contextvars.ContextVar("seymour_asker", default=None)


def attach(part: dict) -> None:
    """A tool adds a content part for the next message (an image)."""
    current = _attachments.get()
    if current is None:
        current = []
        _attachments.set(current)
    current.append(part)


def take_attachments() -> list[dict]:
    """The executor collects (and clears) what the last call attached."""
    current = _attachments.get() or []
    _attachments.set(None)
    return list(current)


def set_asker(fn: Callable[[dict], Awaitable[str]] | None) -> None:
    _asker.set(fn)


def asker() -> Callable[[dict], Awaitable[str]] | None:
    return _asker.get()


def scope(new_run_id: str, new_depth: int | None = None) -> list[contextvars.Token]:
    """Enter a run's scope; returns the tokens `unscope` restores."""
    tokens = [run_id.set(new_run_id)]
    if new_depth is not None:
        tokens.append(depth.set(new_depth))
    return tokens


def unscope(tokens: list[contextvars.Token]) -> None:
    for token in reversed(tokens):
        try:
            token.var.reset(token)
        except ValueError:
            pass                      # reset from a different context: leave as is


__all__ = ["run_id", "depth", "attach", "take_attachments", "set_asker", "asker", "scope", "unscope"]
Any  # noqa: B018  (typing import kept for readers of the side-channel shapes)
