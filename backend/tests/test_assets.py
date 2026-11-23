"""Asset library: type sniffing, size limits, EXIF stripping, dimensions, in-use guard."""

from __future__ import annotations

import struct
import zlib
from typing import Any

from projectx.modules.gtm.assets import image_size, sniff, strip_jpeg_metadata
from projectx.services import Services
from projectx.spine.jobs.worker import Worker


def _png(w: int, h: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    return b"\x89PNG\r\n\x1a\n" + header + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _jpeg_with_exif(w: int, h: int) -> bytes:
    exif = b"Exif\x00\x00" + b"GPS-SECRET-LOCATION" * 4
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    sos = b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00" + b"\x12\x34\x56" + b"\xff\xd9"
    return b"\xff\xd8" + app0 + app1 + sof + sos


def test_sniffing_ignores_what_the_browser_claims() -> None:
    assert sniff(_png(2, 2)[:32])[:3] == ("image/png", "image", "png")  # type: ignore[index]
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ")[0] == "image/webp"  # type: ignore[index]
    assert sniff(b"\x00\x00\x00\x18ftypqt  ")[0] == "video/quicktime"  # type: ignore[index]
    assert sniff(b"<html><script>alert(1)</script>") is None
    assert sniff(b"MZ\x90\x00 executable") is None


def test_exif_is_stripped_and_dimensions_read() -> None:
    jpeg = _jpeg_with_exif(640, 480)
    clean = strip_jpeg_metadata(jpeg)
    assert b"GPS-SECRET-LOCATION" in jpeg and b"GPS-SECRET-LOCATION" not in clean
    assert clean.endswith(b"\xff\xd9") and b"JFIF" in clean
    assert image_size(clean, "image/jpeg") == (640, 480)
    assert image_size(_png(30, 20), "image/png") == (30, 20)
    assert strip_jpeg_metadata(b"\xff\xd8garbage") == b"\xff\xd8garbage"  # unknown layout: untouched


async def test_upload_list_attach_and_delete_guard(services: Services, founder: Any, business: dict[str, Any]) -> None:
    base = f"/api/v1/b/{business['id']}/gtm/assets"
    bad = await founder.post(base, files={"file": ("evil.png", b"<svg onload=alert(1)>", "image/png")})
    assert bad.status_code == 422
    up = await founder.post(
        base,
        files={"file": ("photo.jpg", _jpeg_with_exif(64, 48), "image/png")},  # the claimed type is ignored
        data={"alt_text": "Our team at work", "tags": "Team, office"},
    )
    assert up.status_code == 201, up.text
    asset = up.json()
    assert asset["mime"] == "image/jpeg" and asset["width"] == 64 and asset["tags"] == ["office", "team"]
    assert "/files/" in asset["url"]
    found = (await founder.get(f"{base}?q=team")).json()["items"]
    assert [a["id"] for a in found] == [asset["id"]]

    gtm = f"/api/v1/b/{business['id']}/gtm"
    await founder.post(f"{gtm}/channels/practice", json={"platform": "instagram"})
    content = (await founder.post(f"{gtm}/content", json={"title": "Team day", "platforms": ["instagram"]})).json()
    await Worker(services).run_until_idle()  # writes the platform variants
    item = (await founder.get(f"{gtm}/content/{content['id']}")).json()
    assert item["variants"], item
    variant = item["variants"][0]
    media = [{"key": asset["storage_key"], "kind": "image"}]
    assert (await founder.patch(f"{gtm}/variants/{variant['id']}", json={"media": media})).status_code == 200
    blocked = await founder.delete(f"{base}/{asset['id']}")
    assert blocked.status_code == 409  # an upcoming post still uses it
    await founder.patch(f"{gtm}/variants/{variant['id']}", json={"media": []})
    assert (await founder.delete(f"{base}/{asset['id']}")).status_code == 204


async def test_posts_cant_reference_other_businesses_files(
    founder: Any, business: dict[str, Any], services: Services
) -> None:
    from tests.conftest import new_business

    other = await new_business(founder)
    up = await founder.post(f"/api/v1/b/{other['id']}/gtm/assets", files={"file": ("x.png", _png(4, 4), "image/png")})
    foreign_key = up.json()["storage_key"]
    gtm = f"/api/v1/b/{business['id']}/gtm"
    await founder.post(f"{gtm}/channels/practice", json={"platform": "instagram"})
    content = (await founder.post(f"{gtm}/content", json={"title": "Hello", "platforms": ["instagram"]})).json()
    await Worker(services).run_until_idle()
    variant = (await founder.get(f"{gtm}/content/{content['id']}")).json()["variants"][0]
    for media in ([{"key": foreign_key}], [{"url": "http://insecure.example/x.png"}], [{"url": "javascript:alert(1)"}]):
        res = await founder.patch(f"{gtm}/variants/{variant['id']}", json={"media": media})
        assert res.status_code == 422, media
    ok = await founder.patch(f"{gtm}/variants/{variant['id']}", json={"media": [{"url": "https://cdn.example/x.png"}]})
    assert ok.status_code == 200
