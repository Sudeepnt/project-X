"""ASGI entry point: ``uvicorn projectx.api.main:app``."""

from projectx.api.app import create_app

app = create_app()
