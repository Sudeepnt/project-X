"""File storage for brand assets and media: local disk for development and single-server installs,
any S3-compatible store (AWS S3, Cloudflare R2, MinIO, Backblaze B2…) in production. Files are private;
URLs to them are signed and expire."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from pathlib import Path
from typing import Any, Protocol

from projectx.kernel.errors import NotFound, ValidationFailed


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> tuple[bytes, str]: ...

    async def delete(self, key: str) -> None: ...

    def signed_url(self, key: str, *, ttl_s: int = 3600) -> str: ...

    def verify(self, key: str, expires: int, signature: str) -> bool: ...


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

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.with_suffix(path.suffix + ".type").write_text(content_type)

        await asyncio.to_thread(write)

    async def get(self, key: str) -> tuple[bytes, str]:
        path = self._path(key)

        def read() -> tuple[bytes, str]:
            if not path.exists():
                raise NotFound("File not found")
            type_file = path.with_suffix(path.suffix + ".type")
            content_type = type_file.read_text() if type_file.exists() else "application/octet-stream"
            return path.read_bytes(), content_type

        return await asyncio.to_thread(read)

    async def delete(self, key: str) -> None:
        path = self._path(key)

        def remove() -> None:
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".type").unlink(missing_ok=True)

        await asyncio.to_thread(remove)

    def _signature(self, key: str, expires: int) -> str:
        return hmac.new(self._secret, f"{key}:{expires}".encode(), hashlib.sha256).hexdigest()[:40]

    def signed_url(self, key: str, *, ttl_s: int = 3600) -> str:
        expires = int(time.time()) + ttl_s
        return f"{self._base_url}/files/{key}?e={expires}&s={self._signature(key, expires)}"

    def verify(self, key: str, expires: int, signature: str) -> bool:
        if expires < int(time.time()):
            return False
        return hmac.compare_digest(self._signature(key, expires), signature)


class S3BlobStore:
    """Private bucket; reads go through short-lived presigned URLs. boto3 calls run in worker threads."""

    def __init__(
        self,
        *,
        bucket: str,
        region: str | None,
        endpoint_url: str | None,
        access_key_id: str | None,
        secret_access_key: str | None,
        client: Any | None = None,
    ) -> None:
        import boto3
        from botocore.config import Config

        self._bucket = bucket
        self._client = client or boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
                connect_timeout=10,
                read_timeout=60,
            ),
        )

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=_safe_key(key),
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )

    async def get(self, key: str) -> tuple[bytes, str]:
        from botocore.exceptions import ClientError

        try:
            obj = await asyncio.to_thread(self._client.get_object, Bucket=self._bucket, Key=_safe_key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise NotFound("File not found") from exc
            raise
        body = await asyncio.to_thread(obj["Body"].read)
        return body, str(obj.get("ContentType") or "application/octet-stream")

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=_safe_key(key))

    def signed_url(self, key: str, *, ttl_s: int = 3600) -> str:
        url: str = self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": _safe_key(key)}, ExpiresIn=ttl_s
        )
        return url

    def verify(self, key: str, expires: int, signature: str) -> bool:
        return False  # S3 serves its own signed URLs; the API's /files route isn't used
