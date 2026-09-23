"""Version 1 of the public API, mounted at ``/api/v1``.

Routing is versioned: this package owns the v1 routes and their wiring. The request and
response schemas in ``app.schemas`` are shared for now; a v2 that changes a contract adds its
own schemas next to its routes and is mounted alongside this router in ``app.api.router``.
"""

from fastapi import APIRouter

from app.api.v1.routes import health, submissions

router = APIRouter(prefix="/v1")
router.include_router(health.router, tags=["health"])
router.include_router(submissions.router, tags=["submissions"])
