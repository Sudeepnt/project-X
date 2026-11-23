"""Command line: ``projectx api | worker | migrate | seed-demo | verify-ledger | openapi``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _run_api(args: argparse.Namespace) -> None:
    import uvicorn

    from projectx.settings import get_settings

    uvicorn.run(
        "projectx.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        proxy_headers=True,
        # Only trust X-Forwarded-For from your own proxy; otherwise clients could fake their IP.
        forwarded_allow_ips=get_settings().forwarded_allow_ips,
        log_config=None,
        access_log=False,
    )


async def _worker_main(concurrency: int | None) -> None:
    from projectx.logconfig import configure_logging
    from projectx.services import Services
    from projectx.settings import get_settings
    from projectx.spine.jobs.worker import Scheduler, Worker
    from projectx.spine.modules import load_modules

    settings = get_settings()
    configure_logging(settings)
    load_modules()
    services = Services.create(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    worker = Worker(
        services,
        concurrency=concurrency or settings.worker_concurrency,
        poll_interval=settings.worker_poll_interval_s,
        lease_s=settings.worker_lease_s,
    )
    tasks = [asyncio.create_task(worker.run(stop))]
    if settings.scheduler_enabled:
        tasks.append(asyncio.create_task(Scheduler(services).run(stop)))
    try:
        await asyncio.gather(*tasks)
    finally:
        await services.close()


def _migrate(_: argparse.Namespace) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    command.upgrade(cfg, "head")


async def _verify_ledger(business_id: str) -> int:
    from projectx.services import Services
    from projectx.settings import get_settings
    from projectx.spine.ledger.service import verify_chain

    services = Services.create(get_settings())
    try:
        async with services.db.tenant(uuid.UUID(business_id)) as session:
            report = await verify_chain(session, uuid.UUID(business_id))
    finally:
        await services.close()
    print(json.dumps(report.__dict__, indent=2))
    return 0 if report.ok else 1


async def _discord_commands() -> int:
    from projectx.modules.gtm.community.discord import register_commands
    from projectx.services import Services
    from projectx.settings import get_settings

    services = Services.create(get_settings())
    try:
        count = await register_commands(services)
    finally:
        await services.close()
    print(f"Registered {count} Discord slash commands.")
    return 0


def _generate_keys() -> None:
    import base64
    import secrets

    print("# Store these in your secret manager. Losing PX_VAULT_MASTER_KEY makes stored credentials unreadable.")
    print(f"PX_SECRET_KEY={secrets.token_urlsafe(48)}")
    print(f"PX_VAULT_MASTER_KEY={base64.b64encode(secrets.token_bytes(32)).decode()}")


def _openapi(args: argparse.Namespace) -> None:
    from projectx.api.app import create_app

    spec = create_app().openapi()
    out = json.dumps(spec, indent=2)
    if args.output:
        Path(args.output).write_text(out)
    else:
        print(out)


def main() -> None:
    parser = argparse.ArgumentParser(prog="projectx")
    sub = parser.add_subparsers(dest="cmd", required=True)
    api = sub.add_parser("api", help="run the HTTP API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8010)
    api.add_argument("--reload", action="store_true")
    worker = sub.add_parser("worker", help="run background jobs and the scheduler")
    worker.add_argument("--concurrency", type=int, default=None)
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("seed-demo", help="create a practice company with sample data")
    verify = sub.add_parser("verify-ledger", help="check a business's ledger hash chain")
    verify.add_argument("business_id")
    sub.add_parser("discord-commands", help="install or update the Discord bot's slash commands")
    sub.add_parser("generate-keys", help="print fresh PX_SECRET_KEY and PX_VAULT_MASTER_KEY values")
    openapi = sub.add_parser("openapi", help="print the OpenAPI document")
    openapi.add_argument("--output", default=None)
    args = parser.parse_args()

    if args.cmd == "api":
        _run_api(args)
    elif args.cmd == "worker":
        asyncio.run(_worker_main(args.concurrency))
    elif args.cmd == "migrate":
        _migrate(args)
    elif args.cmd == "seed-demo":
        from projectx.seed import run_seed

        asyncio.run(run_seed())
    elif args.cmd == "verify-ledger":
        sys.exit(asyncio.run(_verify_ledger(args.business_id)))
    elif args.cmd == "generate-keys":
        _generate_keys()
    elif args.cmd == "discord-commands":
        sys.exit(asyncio.run(_discord_commands()))
    elif args.cmd == "openapi":
        _openapi(args)


if __name__ == "__main__":
    main()
