"""Blob stores: local disk and S3-compatible (exercised against moto's in-process S3)."""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from moto import mock_aws

from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.spine.blobs import LocalBlobStore, S3BlobStore


async def test_local_store_round_trip_and_signed_urls(tmp_path: Path) -> None:
    store = LocalBlobStore(str(tmp_path), base_url="http://x", secret="s")
    await store.put("b1/assets/a.png", b"png-bytes", "image/png")
    assert await store.get("b1/assets/a.png") == (b"png-bytes", "image/png")
    url = store.signed_url("b1/assets/a.png", ttl_s=60)
    expires, sig = int(url.split("e=")[1].split("&")[0]), url.split("s=")[1]
    assert store.verify("b1/assets/a.png", expires, sig)
    assert not store.verify("b1/assets/other.png", expires, sig)
    with pytest.raises(ValidationFailed):
        await store.put("../escape", b"x", "text/plain")
    await store.delete("b1/assets/a.png")
    with pytest.raises(NotFound):
        await store.get("b1/assets/a.png")


async def test_s3_store_round_trip_encrypted_and_presigned() -> None:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
        client.create_bucket(Bucket="px-test")
        store = S3BlobStore(
            bucket="px-test",
            region="us-east-1",
            endpoint_url=None,
            access_key_id="k",
            secret_access_key="s",
            client=client,
        )
        await store.put("b1/assets/v.mp4", b"video", "video/mp4")
        head = client.head_object(Bucket="px-test", Key="b1/assets/v.mp4")
        assert head["ServerSideEncryption"] == "AES256" and head["ContentType"] == "video/mp4"
        assert await store.get("b1/assets/v.mp4") == (b"video", "video/mp4")
        url = store.signed_url("b1/assets/v.mp4", ttl_s=120)
        assert "X-Amz-Signature=" in url and "X-Amz-Expires=120" in url
        await store.delete("b1/assets/v.mp4")
        with pytest.raises(NotFound):
            await store.get("b1/assets/v.mp4")
        with pytest.raises(ValidationFailed):
            await store.put("/abs/key", b"x", "text/plain")
