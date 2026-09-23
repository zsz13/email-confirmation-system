"""Read-only admin panel built with SQLAdmin.

SQLAdmin was chosen because it sits directly on the SQLAlchemy models and the async engine
this service already has: one process, one ORM, no second application to build or deploy.
The panel is mounted at ``Settings.admin_path`` and protected by a session-cookie login.
"""

import types
from pathlib import Path

from fastapi import FastAPI
from sqladmin import Admin
from sqladmin.authentication import login_required
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.admin.auth import SessionAuth
from app.admin.views import SubmissionAdmin, submission_summary
from app.core.config import Settings

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class SubmissionsAdmin(Admin):
    """SQLAdmin with a summary dashboard in place of the empty default landing page."""

    def __init__(
        self, app: FastAPI, *, engine: AsyncEngine, settings: Settings
    ) -> None:
        # A session factory of its own on the shared engine: SQLAdmin reconfigures the
        # factory it is given, and the request path must keep its own settings.
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        super().__init__(
            app,
            engine,
            session_maker=self._sessions,
            base_url=settings.admin_path,
            title="Submissions admin",
            templates_dir=str(TEMPLATES_DIR),
            authentication_backend=SessionAuth(settings),
        )
        # Drives the warning banner on the login page.
        self.demo_credentials = settings.admin_uses_demo_defaults
        self.admin_username = settings.admin_username

    @login_required
    async def index(self, request: Request) -> Response:
        async with self._sessions() as session:
            cards = await submission_summary(session)
        return await self.templates.TemplateResponse(
            request, "sqladmin/index.html", {"cards": cards}
        )


def mount_admin(app: FastAPI, *, engine: AsyncEngine, settings: Settings) -> None:
    admin = SubmissionsAdmin(app, engine=engine, settings=settings)
    # SQLAdmin stores the session factory on the view *class* it is given. A subclass per
    # admin keeps two applications in one process (the test suite builds many) from
    # silently sharing whichever database was wired up last.
    admin.add_view(types.new_class(SubmissionAdmin.__name__, (SubmissionAdmin,)))

    # The panel lives under "<path>/"; the application itself never redirects on slashes, so
    # the bare path (what a person types) gets one fixed, relative redirect of its own.
    @app.get(settings.admin_path, include_in_schema=False)
    async def admin_root() -> RedirectResponse:
        return RedirectResponse(f"{settings.admin_path}/", status_code=308)
