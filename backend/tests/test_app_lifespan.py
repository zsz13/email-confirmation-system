from fastapi import FastAPI
from sqlalchemy import text

from app.core.config import Settings
from app.main import create_app, lifespan
from app.services.delivery_reconciler import DeliveryReconciler
from app.services.email import EmailService


async def test_lifespan_wires_database_and_email_service(settings: Settings) -> None:
    """The real dependencies read app.state; make sure the lifespan populates what they expect."""
    app: FastAPI = create_app(settings)

    async with lifespan(app):
        async with app.state.session_factory() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        assert isinstance(app.state.email_service, EmailService)


async def test_lifespan_runs_the_delivery_reconciler_only_while_the_app_does(
    settings: Settings,
) -> None:
    app: FastAPI = create_app(settings)

    async with lifespan(app):
        reconciler = app.state.delivery_reconciler
        assert isinstance(reconciler, DeliveryReconciler)
        assert reconciler.running

    assert not reconciler.running
