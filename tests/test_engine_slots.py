"""Slot affinity must never put two live conversations on one slot.

Measured 2026-09-02 during the full eval: an agent task and a chat run
that shared a llama-server slot came back holding each other's text.
These proofs pin the rule that prevents it: a key keeps its slot while
that slot is idle or its own; a slot live for ANOTHER key is never
handed out; fresh keys prefer free slots; with nothing free a request
goes unpinned rather than shared.
"""

from seymour.engine.llamacpp import LlamaCppEngine


def engine(slots: int = 4) -> LlamaCppEngine:
    e = LlamaCppEngine()
    e._total_slots = slots
    return e


def test_key_keeps_its_slot_when_idle():
    e = engine()
    first = e._slot_for("chat:a")
    assert e._slot_for("chat:a") == first


def test_fresh_keys_spread_over_free_slots():
    e = engine()
    slots = [e._slot_for(f"chat:{i}") for i in range(4)]
    assert sorted(slots) == [0, 1, 2, 3]


def test_live_slot_is_never_shared():
    e = engine()
    a = e._slot_for("agent:t1")
    e._acquire(a, "agent:t1")                 # the agent is generating on it
    # A chat pinned to that same slot earlier must move, not share.
    e._slot_of["chat:x"] = a
    moved = e._slot_for("chat:x")
    assert moved is not None and moved != a
    # And a brand-new key never lands on the live slot either.
    assert e._slot_for("chat:y") != a
    e._release(a, "agent:t1")
    # Once released, the agent's own slot is its own again.
    assert e._slot_for("agent:t1") == a


def test_same_conversation_may_stack_on_its_own_slot():
    e = engine()
    s = e._slot_for("research:j1")
    e._acquire(s, "research:j1")
    assert e._slot_for("research:j1") == s     # a second request of the SAME key
    e._acquire(s, "research:j1")
    e._release(s, "research:j1")
    assert s in e._live                        # still one live request
    e._release(s, "research:j1")
    assert s not in e._live


def test_cache_reuse_only_for_the_same_conversation():
    from seymour.engine.adapter import GenerationRequest
    e = engine()
    req_a = GenerationRequest(messages=[{"role": "user", "content": "a"}],
                              max_tokens=8, cache_key="chat:a")
    slot = e._slot_for("chat:a")
    first = e._payload(req_a, stream=False, slot=slot, pin=False)
    assert first["id_slot"] == slot and first["cache_prompt"] is False   # cold slot
    second = e._payload(req_a, stream=False, slot=slot, pin=False)
    assert second["cache_prompt"] is True                                # warm, same key
    # Another conversation landing on that slot must NOT reuse the prefix.
    req_b = GenerationRequest(messages=[{"role": "user", "content": "b"}],
                              max_tokens=8, cache_key="chat:b")
    third = e._payload(req_b, stream=False, slot=slot, pin=False)
    assert third["cache_prompt"] is False


def test_server_reported_busy_slots_are_not_handed_out():
    e = engine()
    e._server_busy = {0, 1}                    # ghosts the server still runs
    assert e._slot_for("chat:new") in (2, 3)
    # …unless the busy slot's last occupant is this very conversation.
    e._last_key[0] = "chat:mine"
    e._slot_of["chat:mine"] = 0
    assert e._slot_for("chat:mine") == 0


def test_all_live_means_unpinned_not_shared():
    e = engine(slots=2)
    for key in ("a", "b"):
        e._acquire(e._slot_for(key), key)
    assert e._slot_for("c") is None            # no slot to give: go unpinned
    e._slot_of["d"] = 0                        # a key pinned to a live slot
    assert e._slot_for("d") is None
