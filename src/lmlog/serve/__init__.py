"""Optional HTTP API for lmlog (install the ``serve`` extra)."""

from .app import create_app

__all__ = ["create_app"]
