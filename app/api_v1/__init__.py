"""NITT Library storefront and payments API, mounted at /api/v1. The older /api routes keep working unchanged."""
from fastapi import FastAPI

from . import checkouts, ebooks, errors, fines, library, payments, sandbox

PREFIX = errors.PREFIX


def install(app: FastAPI) -> None:
    errors.install(app)
    for module in (library, fines, checkouts, payments, ebooks):
        app.include_router(module.router, prefix=PREFIX)
    app.include_router(sandbox.customer, prefix=PREFIX)
    app.include_router(sandbox.admin, prefix=PREFIX)
