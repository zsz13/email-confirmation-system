"""What the admin panel shows: a read-only table of submissions and a summary dashboard."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqladmin import ModelView
from sqladmin.filters import StaticValuesFilter
from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.models.submission import EmailStatus, Submission
from app.services.delivery_status import IN_PROGRESS, UNDELIVERED


class SubmissionAdmin(ModelView, model=Submission):
    name = "Submission"
    name_plural = "Submissions"
    icon = "fa-solid fa-envelope"

    # Read-only: the panel is for looking at what the public form produced, not for
    # changing it. SQLAdmin answers 403 for the create, edit, delete, export and import
    # routes and hides their buttons. No custom actions are defined, so there is nothing
    # else that could mutate a row.
    can_create = False
    can_edit = False
    can_delete = False
    can_export = False
    can_import = False
    can_view_details = True

    column_list = [
        Submission.id,
        Submission.created_at,
        Submission.name,
        Submission.email,
        Submission.email_status,
    ]
    # The MessageID is for an operator matching a row to Mailjet's own records, so it is
    # shown on the details page only.
    column_details_list = [*column_list, Submission.mailjet_message_id]
    column_labels = {
        Submission.id: "ID (UUIDv7)",
        Submission.created_at: "Created (UTC)",
        Submission.email_status: "Status",
        Submission.mailjet_message_id: "Mailjet MessageID",
    }
    column_searchable_list = [Submission.name, Submission.email]
    column_sortable_list = [
        Submission.created_at,
        Submission.name,
        Submission.email,
        Submission.email_status,
    ]
    column_default_sort = (Submission.created_at, True)  # newest first
    column_filters = [
        StaticValuesFilter(
            Submission.email_status,
            title="Status",
            values=[
                (status.value, status.value.replace("_", " ").capitalize())
                for status in EmailStatus
            ],
        )
    ]
    page_size = 25
    page_size_options = [25, 50, 100]

    def search_query(self, stmt: Select[Any], term: str) -> Select[Any]:
        """Search by the UUIDv7 id as well, without touching the binary column textually.

        ``id`` is a ``BINARY(16)``; SQLAdmin's own search casts each searchable column to text
        and applies ``LIKE %term%``, which against those raw bytes matches by accident at
        best. So a term that parses as a UUID is compared with the key itself, and anything
        else - including something that only looks like a UUID - falls through to the
        name and email search unchanged.
        """
        try:
            submission_id = uuid.UUID(term.strip())
        except ValueError:
            return super().search_query(stmt, term)
        return stmt.filter(Submission.id == submission_id)

    def search_placeholder(self) -> str:
        """SQLAdmin builds this from ``column_searchable_list``, which the id is not in."""
        return f"{super().search_placeholder()}, ID (UUIDv7)"

    async def get_object_for_details(self, request: Request) -> Any:
        try:
            return await super().get_object_for_details(request)
        except ValueError:
            # Something that is not a UUID in the URL is "no such row", not a server error.
            return None


async def submission_summary(session: AsyncSession) -> dict[str, int]:
    """The dashboard counts by label, in one aggregate query."""

    def count_where(condition: Any) -> Any:
        # COUNT skips NULLs, so rows that fail the condition are not counted.
        return func.count(case((condition, 1)))

    since = datetime.now(UTC) - timedelta(hours=24)
    row = (
        await session.execute(
            select(
                func.count(Submission.id),
                count_where(Submission.email_status == EmailStatus.DELIVERED),
                count_where(Submission.email_status.in_(IN_PROGRESS)),
                count_where(Submission.email_status.in_(UNDELIVERED)),
                count_where(Submission.email_status == EmailStatus.PENDING),
                count_where(Submission.created_at >= since),
            )
        )
    ).one()
    total, delivered, in_progress, undelivered, pending, last_day = row
    return {
        "Total submissions": total,
        "Delivered": delivered,
        "Accepted, in progress": in_progress,
        "Bounced, blocked or failed": undelivered,
        "Pending": pending,
        "Last 24 hours": last_day,
    }
