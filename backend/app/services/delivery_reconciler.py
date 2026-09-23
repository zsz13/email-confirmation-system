"""Background delivery reconciliation: move accepted messages on as Mailjet reports them.

``POST /api/v1/submissions`` returns as soon as Mailjet's Send API accepts the message and
the row says ``accepted``. What happens afterwards is looked up here, in the running
backend: every few seconds (``DELIVERY_RECONCILE_INTERVAL_SECONDS``) each row still
``accepted``, ``queued`` or ``retrying`` is checked with the same lookup-and-store step
``python -m app.check_delivery`` uses (:func:`~app.services.delivery_status.check_submission`),
until it reaches a final state and drops out of the query.

All state lives in MySQL, so a restarted backend simply picks the unfinished rows up again.
One task per process, started and stopped by the application lifespan; the backend runs as
a single process, so there is one reconciler per deployment. Scaling out would need a lock
or a queue, or Mailjet's webhooks instead of polling.
"""

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.submission import Submission
from app.services.delivery_status import (
    IN_PROGRESS,
    MailjetStatusClient,
    MailjetStatusError,
    check_submission,
)

logger = logging.getLogger(__name__)

# Rows looked up per cycle, newest first so a fresh submission is never stuck behind a
# backlog. Lookups run one after another, which keeps the load on Mailjet's API modest.
BATCH_SIZE = 100
STOP_TIMEOUT_SECONDS = 10.0


class DeliveryReconciler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mailjet: MailjetStatusClient,
        *,
        interval_seconds: float,
    ) -> None:
        self._sessions = session_factory
        self._mailjet = mailjet
        self._interval = interval_seconds
        self._cycle_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the polling task; a second call while it runs does nothing."""
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="delivery-reconciler")
        logger.info(
            "delivery reconciler started interval_s=%g batch_size=%d",
            self._interval,
            BATCH_SIZE,
        )

    async def stop(self) -> None:
        """Cancel the polling task, mid-lookup if need be, and wait until it has ended."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        # wait, not await: the task's own CancelledError is expected and stays in the task,
        # while a cancellation of this coroutine still propagates. Bounded, so shutdown
        # cannot hang on a task that failed to end.
        await asyncio.wait({task}, timeout=STOP_TIMEOUT_SECONDS)
        if task.done():
            logger.info("delivery reconciler stopped")
        else:
            logger.warning(
                "delivery reconciler did not stop within %gs", STOP_TIMEOUT_SECONDS
            )

    async def _run(self) -> None:
        # The first cycle runs at once, so rows left unfinished by a previous process are
        # picked up on startup rather than one interval later.
        while True:
            try:
                await self.run_cycle()
            except SQLAlchemyError as exc:
                _reraise_if_stopping()
                # The database being briefly away is no reason to stop; the next cycle retries.
                logger.warning(
                    "delivery_reconciliation_failed error=%s", type(exc).__name__
                )
            except Exception:
                _reraise_if_stopping()
                logger.exception("delivery_reconciliation_failed")
            await asyncio.sleep(self._interval)

    async def run_cycle(self) -> int:
        """Look every in-progress row up once; the number of rows checked.

        A cycle that finds the previous one still running does nothing, so cycles never
        overlap, however long Mailjet takes to answer.
        """
        if self._cycle_lock.locked():
            logger.debug(
                "delivery_reconciliation_skipped reason=previous_cycle_running"
            )
            return 0
        async with self._cycle_lock:
            ids = await self._pending_ids()
            if ids:
                logger.debug("delivery_reconciliation_started rows=%d", len(ids))
            for submission_id in ids:
                await self._check(submission_id)
            return len(ids)

    async def _pending_ids(self) -> list[uuid.UUID]:
        async with self._sessions() as session:
            result = await session.scalars(
                select(Submission.id)
                .where(
                    Submission.email_status.in_(IN_PROGRESS),
                    Submission.mailjet_message_id.is_not(None),
                )
                .order_by(Submission.created_at.desc())
                .limit(BATCH_SIZE)
            )
            return list(result)

    async def _check(self, submission_id: uuid.UUID) -> None:
        """One row, in a session of its own: a failure here never reaches the other rows."""
        previous = "-"
        try:
            async with self._sessions() as session:
                submission = await session.get(Submission, submission_id)
                # Gone, or finished by another writer since the cycle's query.
                if submission is None or submission.email_status not in IN_PROGRESS:
                    return
                previous = submission.email_status
                await check_submission(
                    session, submission, self._mailjet, source="reconciler"
                )
        except MailjetStatusError:
            pass  # logged by check_submission, with the lookup's duration
        except Exception as exc:
            _reraise_if_stopping()
            logger.warning(
                "delivery_status_check_failed submission_id=%s previous_status=%s "
                "error=%s source=reconciler",
                submission_id,
                previous,
                type(exc).__name__,
                # A traceback for the unexpected; a database error's type says enough.
                exc_info=not isinstance(exc, SQLAlchemyError),
            )


def _reraise_if_stopping() -> None:
    """Honour a pending stop even when cleanup replaced its ``CancelledError``.

    Cancelling a query can make the session's cleanup raise an ordinary error instead of
    the cancellation; caught as a row or cycle failure, it would leave the loop running
    after :meth:`DeliveryReconciler.stop`.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError
