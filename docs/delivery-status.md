# Delivery status

## Three different facts

| What is known | State | Who reports it |
|---------------|-------|----------------|
| Mailjet's Send API accepted the request | `accepted` | the `POST` itself, synchronously |
| The recipient's mail server accepted the message | `delivered` | Mailjet, later, through the reconciler |
| The message is in the Inbox rather than Spam/Junk | never known | nobody tells the sender |

A `201` from `POST /api/v1/submissions` establishes only the first. `delivered` establishes the
second and still says nothing about the third: the receiving provider files the message where it
chooses. The demo sender makes Spam/Junk placement more likely (see
[security.md](security.md#email-authentication-and-deliverability)), and the page tells the person
to look there.

## States

`submissions.email_status` (`backend/app/models/submission.py`), in lifecycle order:

| State | Meaning | Final |
|-------|---------|-------|
| `pending` | The row is stored; Mailjet has not been called yet (or the database failed right after Mailjet answered). | no |
| `accepted` | Mailjet's Send API accepted the request. Nothing is known about the recipient's server yet. | no |
| `queued` | Mailjet is still processing or holding the message. | no |
| `retrying` | The recipient's server refused temporarily (Mailjet `deferred`); Mailjet keeps retrying, for up to about 24 hours. | no |
| `delivered` | The recipient's mail server accepted the message (Mailjet `sent`). Not a statement about Inbox placement. | yes |
| `soft_bounced` | Mailjet reports a soft bounce: a temporary kind of rejection (for example a full mailbox) that lasted until Mailjet stopped retrying. The application records Mailjet's end result, so it is final here; trying again means a new submission. | yes |
| `hard_bounced` | Permanent rejection, for example a mailbox that does not exist. | yes |
| `blocked` | Mailjet itself refused to send the message (provider-side blocking). | yes |
| `failed` | Sending failed: the Send API call returned an error or could not be made, and the API answered `502`. | yes |

How Mailjet's reports map onto these (`backend/app/services/delivery_status.py`):

| Mailjet message `Status` (polled) | Mailjet event (webhook, not wired up) | State |
|-----------------------------------|----------------------------------------|-------|
| `queued` | | `queued` |
| `deferred` | | `retrying` |
| `sent`, `opened`, `clicked`, `spam`, `unsub` | `sent`, `open`, `click`, `spam`, `unsub` | `delivered` |
| `softbounced`, `bounce` | `bounce` with `hard_bounce: false` | `soft_bounced` |
| `hardbounced` | `bounce` with `hard_bounce: true` | `hard_bounced` |
| `blocked` | `blocked`, or `bounce` with `blocked: true` | `blocked` |
| `unknown` or anything new | anything else | no change |

Opens, clicks, spam complaints and unsubscribes all happen after the server took the message, so
they imply `delivered`. A plain `bounce`, which Mailjet does not qualify, is recorded as
`soft_bounced`: "hard" (the address is bad) is the stronger claim and is left to Mailjet.

## Transition rules

Reports can arrive late or out of order, so `next_status()` applies two rules:

- A final state is never replaced by an in-progress one.
- An undelivered final state (`soft_bounced`, `hard_bounced`, `blocked`, `failed`) is never
  replaced by `delivered`. `delivered` can still become a bounce or a block, because a server may
  accept a message and reject it afterwards.

`record_status()` decides the transition against the row as it is in MySQL at that moment and
writes it with a conditional `UPDATE ... WHERE email_status = <the state just read>`. If another
writer changed the row in between, the update matches nothing and the newer state stands, so an
older result can never overwrite a newer final state.

## Background reconciliation

Mailjet can only push status changes (webhooks) to a public HTTPS endpoint, which a local demo does
not have. The `DeliveryReconciler` (`backend/app/services/delivery_reconciler.py`) polls instead:

- It is an asyncio task that the FastAPI lifespan starts with the application and cancels
  cleanly at shutdown, mid-lookup if need be.
- Every `DELIVERY_RECONCILE_INTERVAL_SECONDS` (default 3, at least 1) it selects the unfinished
  rows: `accepted`, `queued` or `retrying`, with a stored MessageID, newest first, at most 100. It
  looks each one up with `GET /v3/REST/message/{MessageID}`, one after another, and stores every
  change. The MessageID comes from the Send API's reply; if an accepted reply ever lacked one, that
  row could not be looked up and would stay `accepted`.
- Final rows are never selected again. When every row is final, a cycle is a single database query
  that returns nothing, and no Mailjet API call is made.
- One failed lookup (network error, timeout, `429`, `5xx`, unreadable reply, refusal) or a database
  error on one row is logged, and the cycle continues with the next row. A failed cycle is logged
  and the next one runs on schedule. A `404` means Mailjet has nothing to report yet. Cycles never
  overlap.
- All state lives in MySQL. After a restart the first cycle runs immediately and picks the
  unfinished rows up again, still at most 100 per cycle, newest first.
- `POST /api/v1/submissions` never waits for any of this.

**Single process.** The backend runs one Uvicorn process, so there is exactly one reconciler.
Several replicas would each poll the same rows; scaling out would need a lock or a work queue, or
Mailjet webhooks.

### Checking one submission by hand

`python -m app.check_delivery` follows one row with the same lookup, mapping and transition rules
(`check_submission()`), so it and the reconciler can follow the same row at once without
disagreeing. The real-email E2E test uses it. Pass a submission's UUID (the `tracking_id`);
`--timeout` and `--interval` are optional, in seconds:

```bash
docker compose exec backend python -m app.check_delivery 019a0cac-19d1-7715-8846-1258380249ff --timeout 120 --interval 2
```

It prints each change and ends with `RESULT: <STATE>`:

| Result | Exit code |
|--------|-----------|
| `DELIVERED` | 0 |
| `SOFT_BOUNCED`, `HARD_BOUNCED`, `BLOCKED` | 10 |
| `TIMEOUT/PENDING` (still in progress when time ran out) | 11 |
| `CANNOT_CHECK`: no such row, no MessageID (so every `failed` row), or Mailjet refused the lookup | 12 |

## Public tracking flow

1. The page posts the form to `POST /api/v1/submissions`, which stores the row and sends the email.
2. The API answers `201` with a message and the row's UUIDv7 as `tracking_id`.
3. The page shows "Accepted for delivery" and the address it sent, from its own form state.
4. Independently, the backend's reconciler moves the row on as Mailjet reports.
5. The page polls `GET /api/v1/submissions/{tracking_id}/status` every 3 seconds, one request at a
   time: the first read is due 3 seconds after the `201`, and each next one 3 seconds after the
   previous answer.
6. Automatic polling stops at a final state, or after 2 minutes with a note that delivery is still
   being processed.
7. A **Check delivery status** button reads the status once at any time, also after polling
   stopped. A failed read shows an error and keeps the last known state.

The status endpoint reads MySQL only and never calls Mailjet, so neither the 3-second polling nor
the button can turn into provider traffic. Mailjet is polled only by the reconciler, on its own
interval (`DELIVERY_RECONCILE_INTERVAL_SECONDS`, 3 seconds by default), whether or not a page is
watching. The endpoint returns `{"status": "<state>"}` and nothing else (no name, address,
timestamp or provider data), sent with `Cache-Control: no-store`. Its privacy and rate limit are
described in [security.md](security.md#public-api-data-and-tracking-ids).

The two 3-second defaults are deliberate: a state Mailjet reports is usually on screen within about
6 seconds. They remain separate settings, `DELIVERY_RECONCILE_INTERVAL_SECONDS` in `backend/.env`
for the backend and `STATUS_POLL_INTERVAL_MS` in
`frontend/src/app/features/submission/delivery-status.ts` for the page.

What the page says (`frontend/src/app/features/submission/delivery-status.ts`):

| State | Badge, and the gist of its explanation | Tone |
|-------|----------------------------------------|------|
| `pending` | Preparing your email | in progress |
| `accepted` | Accepted for delivery | in progress |
| `queued` | Queued by email provider | in progress |
| `retrying` | Delivery is being retried | warning |
| `delivered` | Delivered to recipient mail server, with "Check your Inbox, Spam, or Junk folder." | success |
| `soft_bounced` | Temporarily rejected; the provider stopped retrying | warning |
| `hard_bounced` | Delivery failed; check the address | danger |
| `blocked` | Blocked by email provider | danger |
| `failed` | Could not send email | danger |

None of these texts claims Inbox placement.

## Production: webhooks

In production, Mailjet's Event API is the better source: it POSTs `sent`, `bounce`, `blocked`,
`spam`, `unsub`, `open` and `click` events with the `MessageID` as they happen. No webhook endpoint
exists yet because it needs a public HTTPS URL and a way to authenticate Mailjet's calls. The code
is shaped for it: an endpoint would find the row by `mailjet_message_id`, translate the event with
`status_from_event()` and store it with `record_status()`, while the reconciler stays as a safety
net for events that never arrive.
