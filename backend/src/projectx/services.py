"""The services container shared by the API process and the worker."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx

from projectx.db.database import Database
from projectx.settings import Settings
from projectx.spine.blobs import LocalBlobStore
from projectx.spine.gateway.gateway import ModelGateway
from projectx.spine.mail import Mailer
from projectx.spine.vault.crypto import LocalKEK
from projectx.spine.vault.service import Vault


@dataclass
class Services:
    settings: Settings
    db: Database
    vault: Vault
    gateway: ModelGateway
    mailer: Mailer
    blobs: LocalBlobStore
    http: httpx.AsyncClient

    @classmethod
    def create(cls, settings: Settings) -> Services:
        db = Database(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            echo=settings.db_echo,
        )
        services = cls(
            settings=settings,
            db=db,
            vault=Vault(LocalKEK(settings.vault_master_key.get_secret_value())),
            gateway=ModelGateway(settings, db),
            mailer=Mailer(settings),
            blobs=LocalBlobStore(
                settings.blob_dir, base_url=settings.base_url, secret=settings.secret_key.get_secret_value()
            ),
            http=httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers={"User-Agent": "ProjectX/0.1 (+https://github.com/Sudeepnt/project-X)"},
                follow_redirects=False,
            ),
        )
        services.gateway.set_cap_resolver(services.compute_cap_micro_usd)
        return services

    async def compute_cap_micro_usd(self, business_id: uuid.UUID) -> int:
        from projectx.spine.billing.plans import plan_for
        from projectx.spine.tenancy.models import Business

        async with self.db.session() as session:
            business = await session.get(Business, business_id)
        cents = plan_for(business.plan if business else "free").limits.get("ai_daily_cents")
        if cents is None:
            return int(self.settings.llm_default_daily_cap_usd * 1_000_000)
        return int(cents) * 10_000

    async def close(self) -> None:
        await self.http.aclose()
        await self.db.dispose()
