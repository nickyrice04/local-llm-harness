"""Reading a GGUF file's own metadata header — facts, not filename guesses.

A GGUF begins with a key/value header describing the model: its
architecture, block count, context length, and (the reason this module
exists) whether it ships **MTP** — Multi-Token Prediction — heads.

MTP is a small extra layer trained to predict the token AFTER the next
one. llama.cpp can use it as a *self-speculative* draft: the MTP head
proposes tokens, the full model verifies a batch of them in one pass,
and every accepted token is one the big model never had to decode alone.
No separate draft model is involved — the head is inside these weights.

Whether a given file has one is a question only the file can answer:
`<arch>.nextn_predict_layers > 0` plus real `blk.N.nextn.*` tensors.
Reading 55 key/value pairs off the front of the file costs microseconds
and replaces a guess with a fact — the same rule the handshake follows
for everything else.
"""

import logging
import struct
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# GGUF's type tags for header values. Only the sizes matter here: this
# reader SKIPS values it doesn't need rather than decoding them all.
_FIXED_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
                10: 8, 11: 8, 12: 8}
_STRING, _ARRAY = 8, 9


@dataclass(frozen=True)
class GgufInfo:
    """What the header told us (all optional — an unreadable file yields
    an empty record and every caller degrades gracefully)."""

    arch: str = ""
    block_count: int = 0
    context_length: int = 0
    # The MTP verdict: how many next-token-prediction layers the file
    # declares, and whether matching tensors are actually present.
    nextn_layers: int = 0
    has_mtp_tensors: bool = False

    @property
    def supports_mtp(self) -> bool:
        """True only when the file declares MTP layers AND carries the
        tensors for them — a declaration alone is not a capability."""
        return self.nextn_layers > 0 and self.has_mtp_tensors


def read_info(path: Path) -> GgufInfo:
    """Parse a GGUF header. Never raises: a malformed or missing file
    returns an empty GgufInfo, and the caller carries on without the
    optimisation rather than failing to load a model."""
    try:
        with open(path, "rb") as handle:
            return _parse(handle)
    except Exception:
        logger.debug("could not read GGUF header from %s", path, exc_info=True)
        return GgufInfo()


def _parse(handle) -> GgufInfo:
    """The header walk: magic, counts, then the key/value pairs and the
    tensor NAMES (whose shapes and offsets we skip — the names are the
    only part that proves a tensor exists)."""
    if handle.read(4) != b"GGUF":
        return GgufInfo()
    struct.unpack("<I", handle.read(4))                  # format version
    tensor_count = struct.unpack("<Q", handle.read(8))[0]
    kv_count = struct.unpack("<Q", handle.read(8))[0]

    def read_string() -> str:
        length = struct.unpack("<Q", handle.read(8))[0]
        return handle.read(length).decode("utf-8", "replace")

    def read_value(type_tag: int):
        """Decode the value types we care about; skip the rest cheaply."""
        if type_tag == _STRING:
            return read_string()
        if type_tag == _ARRAY:
            element_type = struct.unpack("<I", handle.read(4))[0]
            count = struct.unpack("<Q", handle.read(8))[0]
            for _ in range(count):
                read_value(element_type)
            return None
        if type_tag == 4:                                # uint32
            return struct.unpack("<I", handle.read(4))[0]
        if type_tag == 5:                                # int32
            return struct.unpack("<i", handle.read(4))[0]
        if type_tag == 10:                               # uint64
            return struct.unpack("<Q", handle.read(8))[0]
        if type_tag == 11:                               # int64
            return struct.unpack("<q", handle.read(8))[0]
        handle.read(_FIXED_SIZES.get(type_tag, 4))
        return None

    values: dict[str, object] = {}
    for _ in range(kv_count):
        key = read_string()
        type_tag = struct.unpack("<I", handle.read(4))[0]
        value = read_value(type_tag)
        if value is not None:
            values[key] = value

    arch = str(values.get("general.architecture", ""))

    def arch_int(suffix: str) -> int:
        """Architecture-scoped keys are prefixed with the arch name
        (e.g. "qwen35moe.nextn_predict_layers") — look it up by prefix
        so this works for any architecture, not one hard-coded family."""
        for key, value in values.items():
            if key.endswith("." + suffix) and isinstance(value, int):
                return int(value)
        return 0

    # The tensor names: proof that a declared capability is really here.
    has_mtp = False
    for _ in range(tensor_count):
        name = read_string()
        dims = struct.unpack("<I", handle.read(4))[0]
        handle.read(8 * dims)                            # shape
        handle.read(4)                                   # dtype
        handle.read(8)                                   # offset
        if not has_mtp and ".nextn." in name:
            has_mtp = True

    return GgufInfo(
        arch=arch,
        block_count=arch_int("block_count"),
        context_length=arch_int("context_length"),
        nextn_layers=arch_int("nextn_predict_layers"),
        has_mtp_tensors=has_mtp,
    )
