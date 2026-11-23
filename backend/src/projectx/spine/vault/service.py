"""The key vault. Agents never hold keys: connectors borrow a credential for one call through
``Vault.borrow``, and every borrow is written to the access log with its purpose."""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

import orjson
from cryptography.exceptions import InvalidTag
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import NotFound
from projectx.kernel.ids import uuid7
from projectx.kernel.time import utcnow
from projectx.spine.vault.crypto import KeyEncryptionKey, new_dek, open_sealed, seal
from projectx.spine.vault.models import VaultAccess, VaultKey, VaultSecret

_DEK_CACHE_TTL_S = 300.0


class VaultError(Exception):
    pass


def _aad(business_id: uuid.UUID, secret_id: uuid.UUID, kind: str) -> bytes:
    return f"{business_id}|{secret_id}|{kind}".encode()


class Vault:
    def __init__(self, kek: KeyEncryptionKey) -> None:
        self._kek = kek
        self._cache: dict[tuple[uuid.UUID, int], tuple[bytes, float]] = {}

    async def _dek(
        self, session: AsyncSession, business_id: uuid.UUID, version: int | None = None
    ) -> tuple[int, bytes]:
        if version is not None:
            cached = self._cache.get((business_id, version))
            if cached and cached[1] > time.monotonic():
                return version, cached[0]
        stmt = select(VaultKey).where(VaultKey.business_id == business_id)
        stmt = (
            stmt.where(VaultKey.version == version)
            if version is not None
            else stmt.where(VaultKey.retired_at.is_(None)).order_by(VaultKey.version.desc()).limit(1)
        )
        row = await session.scalar(stmt)
        if row is None:
            if version is not None:
                raise VaultError(f"missing data key version {version}")
            row = await self._create_key(session, business_id, 1)
        dek = self._kek.unwrap(row.wrapped_dek)
        self._cache[(business_id, row.version)] = (dek, time.monotonic() + _DEK_CACHE_TTL_S)
        return row.version, dek

    async def _create_key(self, session: AsyncSession, business_id: uuid.UUID, version: int) -> VaultKey:
        row = VaultKey(
            business_id=business_id, version=version, wrapped_dek=self._kek.wrap(new_dek()), kek_id=self._kek.kek_id
        )
        session.add(row)
        await session.flush()
        return row

    async def put(
        self,
        session: AsyncSession,
        business_id: uuid.UUID,
        *,
        kind: str,
        label: str,
        secret: dict[str, Any],
        expires_at: datetime | None = None,
    ) -> uuid.UUID:
        version, dek = await self._dek(session, business_id)
        secret_id = uuid7()
        nonce, ciphertext = seal(dek, orjson.dumps(secret), _aad(business_id, secret_id, kind))
        session.add(
            VaultSecret(
                id=secret_id,
                business_id=business_id,
                kind=kind,
                label=label,
                ciphertext=ciphertext,
                nonce=nonce,
                key_version=version,
                expires_at=expires_at,
            )
        )
        await session.flush()
        return secret_id

    async def update(
        self,
        session: AsyncSession,
        business_id: uuid.UUID,
        secret_id: uuid.UUID,
        secret: dict[str, Any],
        *,
        expires_at: datetime | None = None,
    ) -> None:
        row = await self._get(session, business_id, secret_id)
        version, dek = await self._dek(session, business_id)
        nonce, ciphertext = seal(dek, orjson.dumps(secret), _aad(business_id, secret_id, row.kind))
        row.ciphertext, row.nonce, row.key_version = ciphertext, nonce, version
        row.expires_at = expires_at
        await session.flush()

    async def borrow(
        self, session: AsyncSession, business_id: uuid.UUID, secret_id: uuid.UUID, *, purpose: str, actor: str
    ) -> dict[str, Any]:
        """Decrypt a credential for immediate use. Never return it to an agent or a client."""
        row = await self._get(session, business_id, secret_id)
        _, dek = await self._dek(session, business_id, row.key_version)
        try:
            plaintext = open_sealed(dek, row.nonce, row.ciphertext, _aad(business_id, secret_id, row.kind))
        except InvalidTag as exc:
            raise VaultError("credential failed integrity check") from exc
        session.add(VaultAccess(business_id=business_id, secret_id=secret_id, purpose=purpose[:100], actor=actor[:100]))
        await session.execute(update(VaultSecret).where(VaultSecret.id == secret_id).values(last_used_at=func.now()))
        result: dict[str, Any] = orjson.loads(plaintext)
        return result

    async def delete(self, session: AsyncSession, business_id: uuid.UUID, secret_id: uuid.UUID) -> None:
        row = await self._get(session, business_id, secret_id)
        await session.delete(row)
        await session.flush()

    async def rotate(self, session: AsyncSession, business_id: uuid.UUID) -> int:
        """Issue a new data key and re-encrypt every secret with it. Returns the new version."""
        current_version, _ = await self._dek(session, business_id)
        await session.execute(
            update(VaultKey)
            .where(VaultKey.business_id == business_id, VaultKey.retired_at.is_(None))
            .values(retired_at=utcnow())
        )
        await self._create_key(session, business_id, current_version + 1)
        rows = (
            (await session.execute(select(VaultSecret).where(VaultSecret.business_id == business_id))).scalars().all()
        )
        for row in rows:
            _, old = await self._dek(session, business_id, row.key_version)
            plain = open_sealed(old, row.nonce, row.ciphertext, _aad(business_id, row.id, row.kind))
            version, dek = await self._dek(session, business_id)
            row.nonce, row.ciphertext = seal(dek, plain, _aad(business_id, row.id, row.kind))
            row.key_version = version
        await session.flush()
        return current_version + 1

    async def _get(self, session: AsyncSession, business_id: uuid.UUID, secret_id: uuid.UUID) -> VaultSecret:
        row = await session.scalar(
            select(VaultSecret).where(VaultSecret.id == secret_id, VaultSecret.business_id == business_id)
        )
        if row is None:
            raise NotFound("Credential not found")
        return row
