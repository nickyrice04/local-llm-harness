"""read_image — look at a picture (when the loaded model can see).

Two uses (the brief, 1.2): the person pastes a screenshot and says "fix
this"; a vision-capable model checks its own rendered output (a page
screenshot from the eval renderer, a rasterized slide). The tool result
is TEXT (what every tool returns) plus a side-channel content part the
executor attaches to the next prompt message as an image_url data URI —
the wire form llama-server's and mlx-vlm's multimodal endpoints take.

Honesty first: whether the engine can see is MEASURED by the handshake
(EngineCapabilities.supports_vision, read from /props modalities),
never inferred from a model's name. Without vision the tool still
answers — the file's format and pixel size, read from its header — and
says plainly that the model cannot see it, so the run does not pretend.
"""

import base64
import struct

from seymour import runtime
from seymour.tools import context, paths

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def dimensions(data: bytes) -> tuple[int, int] | None:
    """Width × height from a PNG / GIF / JPEG header, without PIL."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return width, height
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + length
    return None


async def read_image(path: str) -> str:
    """Tool entry: attach the image for the model to see, or say why not."""
    try:
        target = paths.resolve(path)
    except ValueError as error:
        return f"Error: {error}"
    if not target.is_file():
        return f"Error: no such file: {path}"
    mime = MIMES.get(target.suffix.lower())
    if not mime:
        return f"Error: {path} is not an image I can read (png, jpg, gif, webp)."
    data = target.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        return f"Error: {path} is {len(data):,} bytes — too large to send to the model (limit {MAX_IMAGE_BYTES:,})."
    size = dimensions(data)
    facts = f"{paths.display(target)}: {mime}, {len(data):,} bytes" + (f", {size[0]}×{size[1]} px" if size else "")
    vision = bool(runtime.caps and getattr(runtime.caps, "supports_vision", False))
    if not vision:
        return (f"{facts}. The loaded model has NO vision (measured by the handshake), so you cannot see this "
                "image. Say so if your person asked about it; for a page you wrote, rely on check_page instead.")
    context.attach({"type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}})
    return f"{facts} — the image is attached below; describe what you see before acting on it."


from seymour.tools import Tool                      # noqa: E402  (registry type)

TOOLS = [
    Tool(
        name="read_image",
        description=("Look at an image file in the workspace (png/jpg/gif/webp): a screenshot your "
                     "person saved, a rendered slide, a chart you produced. Works only when the loaded "
                     "model has vision — the result says so either way."),
        args={"path": "workspace-relative path of the image"},
        tier="read", func=read_image,
    ),
]
