"""ASGI entrypoint: `uvicorn ratelimiter.service.asgi:app`."""

from .app import create_app

app = create_app()
