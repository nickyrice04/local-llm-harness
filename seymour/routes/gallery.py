"""The gallery: every image that passes through Seymour, browsable.

A compact adaptation of Odysseus's gallery (see ACKNOWLEDGMENTS.md):
list, rename, favorite, delete, and serve — nothing speculative. Files
live in gallery/ under short hex names; rows carry the display metadata.

Two security rules carried over from the reference implementation:
filenames served back are validated against a strict pattern AND path-
confined to the gallery directory, so a crafted id can never read
elsewhere on disk.
"""

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from seymour.config import settings
from seymour.db import GalleryImage, SessionLocal

router = APIRouter(prefix="/api/gallery")

# Stored gallery names are 12 hex chars + a known image extension —
# anything else in a URL is refused before touching the filesystem.
_NAME_PATTERN = re.compile(r"^[a-f0-9]{12}\.(png|jpg|jpeg|webp|gif)$")


class GalleryPatch(BaseModel):
    """The editable bits of one gallery item."""

    name: str | None = None
    favorite: bool | None = None


@router.get("")
async def list_images():
    """Every image, newest first (favorites carry a flag; the UI sorts)."""
    with SessionLocal() as db:
        rows = (db.query(GalleryImage)
                .order_by(GalleryImage.created_at.desc()).limit(500).all())
        return [
            {
                "id": r.id, "name": r.name, "source": r.source,
                "favorite": r.favorite,
                "url": f"/api/gallery/file/{r.filename}",
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@router.get("/file/{filename}")
async def serve(filename: str):
    """Serve one gallery file (pattern-checked, path-confined)."""
    if not _NAME_PATTERN.match(filename):
        raise HTTPException(404, "no such image")
    path = (settings.gallery_dir / filename).resolve()
    # Belt and braces: even a pattern-passing name must resolve INSIDE
    # the gallery directory.
    if not path.is_relative_to(settings.gallery_dir.resolve()) or not path.exists():
        raise HTTPException(404, "no such image")
    return FileResponse(path)


@router.patch("/{image_id}")
async def patch(image_id: str, body: GalleryPatch):
    """Rename and/or (un)favorite one image."""
    with SessionLocal() as db:
        row = db.get(GalleryImage, image_id)
        if row is None:
            raise HTTPException(404, "no such image")
        if body.name is not None:
            row.name = body.name.strip()[:120]
        if body.favorite is not None:
            row.favorite = body.favorite
        db.commit()
        return {"id": row.id, "name": row.name, "favorite": row.favorite}


@router.delete("/{image_id}")
async def delete(image_id: str):
    """Delete one image: row first, then the file (the Odysseus ordering —
    deleting the file first risks a live row pointing at nothing if the
    row delete then fails)."""
    with SessionLocal() as db:
        row = db.get(GalleryImage, image_id)
        if row is None:
            raise HTTPException(404, "no such image")
        filename = row.filename
        db.delete(row)
        db.commit()
    (settings.gallery_dir / filename).unlink(missing_ok=True)
    return {"deleted": image_id}
