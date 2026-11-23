"""File storage for brand assets and media. Local disk in development; the same interface fits
S3-compatible storage in production. URLs to stored files are signed and expire."""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from typing import Protocol

from projectx.kernel.errors import NotFound, ValidationFailed


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> tuple[bytes, str]: ...

    async def delete(self, key: str) -> None: ...

    def signed_url(self, key: str, *, ttl_s: int = 3600) -> str: ...


def _safe_key(key: str) -> str:
    if ".." in key or key.startswith("/") or "\\" in key:
        raise ValidationFailed("Invalid file key")
    return key


class LocalBlobStore:
    def __init__(self, root: str, *, base_url: str, secret: str) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._base_url = base_url.rstrip("/")
        self._secret = secret.encode()

    def _path(self, key: str) -> Path:
        path = (self._root / _safe_key(key)).resolve()
        if self._root not in path.parents:
            raise ValidationFailed("Invalid file key")
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.with_suffix(path.suffix + ".type").write_text(content_type)

    async def get(self, key: str) -> tuple[bytes, str]:
        path = self._path(key)
        if not path.exists():
            raise NotFound("File not found")
        type_file = path.with_suffix(path.suffix + ".type")
        content_type = type_file.read_text() if type_file.exists() else "application/octet-stream"
        return path.read_bytes(), content_type

    async def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".type").unlink(missing_ok=True)

    def _signature(self, key: str, expires: int) -> str:
        return hmac.new(self._secret, f"{key}:{expires}".encode(), hashlib.sha256).hexdigest()[:40]

    def signed_url(self, key: str, *, ttl_s: int = 3600) -> str:
        expires = int(time.time()) + ttl_s
        return f"{self._base_url}/files/{key}?e={expires}&s={self._signature(key, expires)}"

    def verify(self, key: str, expires: int, signature: str) -> bool:
        if expires < int(time.time()):
            return False
        return hmac.compare_digest(self._signature(key, expires), signature)
