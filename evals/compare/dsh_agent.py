#!/usr/bin/env python3
"""Run one task through deepseek-harness's Python SDK and print a JSON
record — executed with the dsh venv's interpreter by evals/compare/run.py.

The SDK's `sdk` profile is its bundled unattended agent composition; we
point it at llama-server's OpenAI-compatible endpoint through base_url
and count tool activity from the runtime's notifications.
"""

import argparse
import json
import sys
import time

from deepseek_harness import DeepSeekHarness


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--path", default="", help="PATH for the agent's shell")
    parser.add_argument("--dsh-home", required=True,
                        help="the SDK's state dir (it refuses to use ~/.dsh implicitly)")
    args = parser.parse_args()

    events: list[str] = []
    kinds: dict[str, int] = {}
    samples: list[str] = []

    def on_notification(note) -> None:
        # Runtime notifications arrive as session.event / session.status;
        # the tool activity is INSIDE their params. Count any event whose
        # payload names a tool call (dsh's `tool/call` event type), and keep
        # a histogram of event types so the record shows what happened.
        method = getattr(note, "method", None) or ""
        params = getattr(note, "payload", None)
        try:
            text = json.dumps(params, default=str)
        except Exception:
            text = str(params)
        events.append(method)
        for key in ("type", "kind", "event"):
            if isinstance(params, dict) and isinstance(params.get(key), str):
                kinds[params[key]] = kinds.get(params[key], 0) + 1
                break
        else:
            inner = params.get("event") if isinstance(params, dict) else None
            if isinstance(inner, dict) and isinstance(inner.get("type"), str):
                kinds[inner["type"]] = kinds.get(inner["type"], 0) + 1
        if len(samples) < 3 and method == "session.event":
            samples.append(text[:300])
        if "tool/call" in text or '"toolCall"' in text or '"tool_call"' in text:
            events.append("tool/call")

    started = time.monotonic()
    status = "done"
    final = ""
    try:
        with DeepSeekHarness(
            provider="deepseek-official", model=args.model,
            base_url=args.base_url, api_key="local", cwd=args.workspace,
            dsh_home=args.dsh_home, request_timeout_seconds=args.timeout,
            env={"PATH": args.path} if args.path else {},
        ) as harness:
            result = harness.run(args.prompt, session_id=args.session_id,
                                 on_notification=on_notification)
            final = str(getattr(result, "final_response", result))
    except Exception as error:                     # the record must always print
        status = "failed"
        final = f"{type(error).__name__}: {error}"
    tool_events = [e for e in events if e == "tool/call"]
    print(json.dumps({"status": status, "seconds": round(time.monotonic() - started, 1),
                      "final": final[:2000], "tool_events": len(tool_events),
                      "event_kinds": kinds, "samples": samples}))


if __name__ == "__main__":
    main()
