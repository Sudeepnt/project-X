from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class VaultKey(Base):
    """A business's data-encryption key (DEK), wrapped by the master key (KEK). Never stored in clear."""

    __tablename__ = "vault_keys"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    kek_id: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    retired_at: Mapped[datetime | None]


class VaultSecret(IdMixin, TimestampMixin, TenantMixin, Base):
    """An encrypted credential (OAuth tokens, API keys). AES-256-GCM, bound to its row by AAD."""

    __tablename__ = "vault_secrets"

    kind: Mapped[str] = mapped_column(String(30))
    label: Mapped[str] = mapped_column(String(200))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]


class VaultAccess(IdMixin, TenantMixin, Base):
    """Every time a credential is borrowed: which one, by whom, for what."""

    __tablename__ = "vault_access_log"

    secret_id: Mapped[uuid.UUID] = mapped_column(index=True)
    purpose: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100))
    at: Mapped[datetime] = mapped_column(server_default=func.now())
