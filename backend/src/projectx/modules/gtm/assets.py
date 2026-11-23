"""The asset library: images, videos and documents a business can attach to posts.

Uploads are identified by their bytes, not by what the browser claims. JPEG metadata (EXIF, which can
carry GPS location and device serials) is stripped before storage. Files are served only through short-
lived signed URLs.
"""

from __future__ import annotations

import struct
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from projectx.api.deps import BusinessDep, EditorDep
from projectx.kernel.errors import Conflict, NotFound, ValidationFailed
from projectx.modules.gtm.models import Asset, ContentVariant
from projectx.services import Services

router = APIRouter(prefix="/api/v1/b/{business_id}/gtm/assets", tags=["gtm: assets"])

MB = 1024 * 1024
# kind, extension, size limit
SIGNATURES: list[tuple[bytes, int, str, str, str, int]] = [
    # magic, offset, mime, kind, ext, max bytes
    (b"\xff\xd8\xff", 0, "image/jpeg", "image", "jpg", 20 * MB),
    (b"\x89PNG\r\n\x1a\n", 0, "image/png", "image", "png", 20 * MB),
    (b"GIF87a", 0, "image/gif", "image", "gif", 15 * MB),
    (b"GIF89a", 0, "image/gif", "image", "gif", 15 * MB),
    (b"WEBP", 8, "image/webp", "image", "webp", 20 * MB),
    (b"%PDF-", 0, "application/pdf", "document", "pdf", 25 * MB),
    (b"ftyp", 4, "video/mp4", "video", "mp4", 200 * MB),
    (b"\x1a\x45\xdf\xa3", 0, "video/webm", "video", "webm", 200 * MB),
]
MAX_UPLOAD = max(s[5] for s in SIGNATURES)


def sniff(head: bytes) -> tuple[str, str, str, int] | None:
    """(mime, kind, extension, max size) from the first bytes, or None if it isn't a type we accept."""
    for magic, offset, mime, kind, ext, limit in SIGNATURES:
        if head[offset : offset + len(magic)] == magic:
            if magic == b"WEBP" and head[:4] != b"RIFF":
                continue
            if magic == b"ftyp" and head[8:12] == b"qt  ":
                return "video/quicktime", "video", "mov", limit
            return mime, kind, ext, limit
    return None


def image_size(data: bytes, mime: str) -> tuple[int | None, int | None]:
    try:
        if mime == "image/png" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return int(w), int(h)
        if mime == "image/gif" and len(data) >= 10:
            w, h = struct.unpack("<HH", data[6:10])
            return int(w), int(h)
        if mime == "image/webp" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return int(w & 0x3FFF), int(h & 0x3FFF)
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if mime == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    break
                marker, length = data[i + 1], struct.unpack(">H", data[i + 2 : i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return int(w), int(h)
                i += 2 + length
    except struct.error:
        pass
    return None, None


def strip_jpeg_metadata(data: bytes) -> bytes:
    """Drop APP1 (EXIF/XMP: GPS, device, owner) and APP13 (IPTC) segments; keep the image itself."""
    if not data.startswith(b"\xff\xd8"):
        return data
    out, i = bytearray(b"\xff\xd8"), 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return data  # not a layout we understand: store untouched rather than corrupt it
        marker = data[i + 1]
        if marker == 0xDA:  # start of scan: the rest is image data
            out += data[i:]
            return bytes(out)
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker not in (0xE1, 0xED):
            out += data[i : i + 2 + length]
        i += 2 + length
    return data


def asset_view(a: Asset, services: Services) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "kind": a.kind,
        "name": a.name,
        "mime": a.mime,
        "size_bytes": a.size_bytes,
        "width": a.width,
        "height": a.height,
        "tags": a.tags,
        "alt_text": a.alt_text,
        "approved": a.approved,
        "storage_key": a.storage_key,
        "url": services.blobs.signed_url(a.storage_key, ttl_s=3600),
        "created_at": a.created_at.isoformat(),
    }


@router.get("")
async def list_assets(
    ctx: BusinessDep, kind: str | None = None, q: str | None = None, limit: int = 100
) -> dict[str, Any]:
    stmt = select(Asset).where(Asset.business_id == ctx.business_id)
    if kind:
        stmt = stmt.where(Asset.kind == kind)
    if q:
        like = f"%{q.strip()[:100]}%"
        stmt = stmt.where(
            or_(Asset.name.ilike(like), Asset.alt_text.ilike(like), Asset.tags.contains([q.strip().lower()]))
        )
    rows = (await ctx.session.execute(stmt.order_by(Asset.created_at.desc()).limit(max(1, min(limit, 500))))).scalars()
    return {"items": [asset_view(a, ctx.services) for a in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload(
    ctx: EditorDep,
    file: UploadFile = File(...),
    alt_text: str | None = Form(None, max_length=500),
    tags: str = Form(""),
) -> dict[str, Any]:
    data = await file.read(MAX_UPLOAD + 1)
    if not data:
        raise ValidationFailed("The file is empty")
    kind_info = sniff(data[:32])
    if kind_info is None:
        raise ValidationFailed("Upload a JPEG, PNG, WebP or GIF image, an MP4, MOV or WebM video, or a PDF")
    mime, kind, ext, limit = kind_info
    if len(data) > limit:
        raise ValidationFailed(f"{kind.title()}s can be up to {limit // MB} MB")
    if mime == "image/jpeg":
        data = strip_jpeg_metadata(data)
    width, height = image_size(data, mime) if kind == "image" else (None, None)
    asset_id = uuid.uuid4()
    key = f"{ctx.business_id}/assets/{asset_id}.{ext}"
    await ctx.services.blobs.put(key, data, mime)
    name = (file.filename or f"upload.{ext}").rsplit("/", 1)[-1][:200]
    asset = Asset(
        id=asset_id,
        business_id=ctx.business_id,
        kind=kind,
        name=name,
        storage_key=key,
        mime=mime,
        size_bytes=len(data),
        width=width,
        height=height,
        tags=sorted({t.strip().lower()[:50] for t in tags.split(",") if t.strip()})[:20],
        alt_text=(alt_text or "").strip() or None,
        approved=True,  # a person uploaded it
        created_by=f"user:{ctx.user.id}",
    )
    ctx.session.add(asset)
    await ctx.session.flush()
    return asset_view(asset, ctx.services)


class AssetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    alt_text: str | None = Field(default=None, max_length=500)
    tags: list[str] | None = Field(default=None, max_length=20)
    approved: bool | None = None


async def _get(ctx: BusinessDep, asset_id: uuid.UUID) -> Asset:
    asset = await ctx.session.get(Asset, asset_id)
    if asset is None:
        raise NotFound("Asset not found")
    return asset


@router.patch("/{asset_id}")
async def update_asset(asset_id: uuid.UUID, body: AssetPatch, ctx: EditorDep) -> dict[str, Any]:
    asset = await _get(ctx, asset_id)
    if body.name is not None:
        asset.name = body.name
    if body.alt_text is not None:
        asset.alt_text = body.alt_text.strip() or None
    if body.tags is not None:
        asset.tags = sorted({t.strip().lower()[:50] for t in body.tags if t.strip()})
    if body.approved is not None:
        asset.approved = body.approved
    return asset_view(asset, ctx.services)


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset(asset_id: uuid.UUID, ctx: EditorDep) -> None:
    asset = await _get(ctx, asset_id)
    in_use = await ctx.session.scalar(
        select(func.count())
        .select_from(ContentVariant)
        .where(
            ContentVariant.business_id == ctx.business_id,
            ContentVariant.status.not_in(("published", "archived")),
            ContentVariant.media.contains([{"key": asset.storage_key}]),
        )
    )
    if in_use:
        raise Conflict(f"It's attached to {in_use} upcoming post{'s' if in_use != 1 else ''}. Remove it there first.")
    await ctx.services.blobs.delete(asset.storage_key)
    await ctx.session.delete(asset)
