"""Chat attachments: upload once, reference by id from a message.

The flow (a compact adaptation of Odysseus's upload → promote pipeline):

    POST /api/upload (multipart)
      → bytes land in uploads/ under a fresh uuid name
      → documents: text extracted NOW and stored on the row (a re-attach
        never re-parses)
      → images: also copied into the gallery (every image that passes
        through Seymour is findable later)
      → returns {id, kind, name} — the chat composer holds these ids and
        sends them with the next message

Security rules: extension allowlist, size cap, uuid filenames (the
original name is DATA on the row, never a path), and files are only ever
served back through id-checked routes.
"""

import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from seymour import documents
from seymour.config import settings
from seymour.db import GalleryImage, SessionLocal, Upload
from seymour.events import bus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# The images we accept (what llama.cpp multimodal can actually decode).
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# Upload ceiling: generous for documents, sane for a local app.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@router.post("/upload")
async def upload(file: UploadFile):
    """Accept one attachment; return the id chat messages reference."""
    original = file.filename or "unnamed"
    suffix = Path(original).suffix.lower()
    is_image = suffix in IMAGE_EXTENSIONS
    # Refuse what we can't honestly use, with the reason.
    if not is_image and not documents.extractable(original):
        raise HTTPException(
            422, f"unsupported file type: {suffix or '(none)'} — images "
                 f"(png/jpg/webp/gif), pdf, docx, csv, md and plain text work.")

    # Stream to disk under a uuid name (the original name is never a path).
    upload_id = str(uuid.uuid4())
    stored_name = f"{upload_id}{suffix}"
    target = settings.uploads_dir / stored_name
    size = 0
    with target.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                raise HTTPException(413, "file exceeds the 50 MB upload limit")
            out.write(chunk)

    # Documents: extract the text NOW, once, onto the row.
    text = "" if is_image else documents.extract_text(target, original)

    row = Upload(
        id=upload_id,
        filename=stored_name,
        original_name=original[:200],
        kind="image" if is_image else "document",
        mime=file.content_type or "",
        text=text,
        size_bytes=size,
    )
    with SessionLocal() as db:
        db.add(row)
        # Images join the gallery too: copy the file (the upload row and
        # the gallery row have independent lifecycles) and add its row.
        if is_image:
            gallery_name = f"{uuid.uuid4().hex[:12]}{suffix}"
            shutil.copyfile(target, settings.gallery_dir / gallery_name)
            db.add(GalleryImage(
                id=str(uuid.uuid4()),
                filename=gallery_name,
                name=Path(original).stem[:120],
                source="chat-upload",
            ))
        db.commit()
    if is_image:
        bus.publish("gallery", "added", name=original)

    return {"id": upload_id, "kind": row.kind, "name": original}


@router.get("/upload/{upload_id}/file")
async def upload_file(upload_id: str):
    """Serve an uploaded file back (the chat renders image previews)."""
    from fastapi.responses import FileResponse
    with SessionLocal() as db:
        row = db.get(Upload, upload_id)
    if row is None:
        raise HTTPException(404, "no such upload")
    path = settings.uploads_dir / row.filename
    if not path.exists():
        raise HTTPException(404, "file is gone")
    return FileResponse(path, media_type=row.mime or "application/octet-stream")
