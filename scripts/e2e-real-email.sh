#!/usr/bin/env bash
# Opt-in end-to-end test against the REAL Mailjet API. It starts the real Docker stack (real
# MySQL, real migrations, the credentials from backend/.env), submits one address through
# the frontend proxy, verifies the API response and the database row, and then follows the
# message until Mailjet reports a final delivery state. The address is a unique Mailinator
# one unless E2E_RECIPIENT_EMAIL names another (see e2e-recipient.sh). Never run by CI; run
# it deliberately:
#
#   RUN_REAL_EMAIL_E2E=1 ./scripts/e2e-real-email.sh
#   RUN_REAL_EMAIL_E2E=1 E2E_RECIPIENT_EMAIL=user@example.com ./scripts/e2e-real-email.sh
#
# HTTP 201 only means Mailjet's Send API accepted the message. The final state comes from
# polling Mailjet's message resource (python -m app.check_delivery, run inside the backend
# container, so the credentials stay there) every 2 seconds for up to
# E2E_DELIVERY_TIMEOUT_SECONDS (default 120). The run passes only on DELIVERED: the recipient's
# mail server accepted the message, which still says nothing about Inbox placement.
# SOFT_BOUNCED, HARD_BOUNCED, BLOCKED and TIMEOUT/PENDING all fail it. No credential is ever
# printed.
set -euo pipefail

cd "$(dirname "$0")/.."

# Resolved before the opt-in check because it has no side effects: a malformed override
# fails on its own, whether or not the run was opted into.
RECIPIENT="$(./scripts/e2e-recipient.sh)"
DELIVERY_TIMEOUT="${E2E_DELIVERY_TIMEOUT_SECONDS:-120}"
if ! [[ "$DELIVERY_TIMEOUT" =~ ^[1-9][0-9]{0,4}$ ]]; then
  echo "E2E_DELIVERY_TIMEOUT_SECONDS must be a whole number of seconds, 1 to 99999." >&2
  exit 1
fi

# Opt-in, and a refusal is a failure: a runner that reaches this must stop, not report a
# pass for a test that never ran.
if [ "${RUN_REAL_EMAIL_E2E:-}" != "1" ]; then
  echo "Refusing to run: this test sends a real email through Mailjet." >&2
  echo "Run it deliberately:  RUN_REAL_EMAIL_E2E=1 make test-e2e" >&2
  exit 1
fi

[ -f backend/.env ] || { echo "backend/.env is missing; copy backend/.env.example and fill in Mailjet credentials." >&2; exit 1; }
if grep -Eq '^MAILJET_(API|SECRET)_KEY=(replace_me|)$' backend/.env; then
  echo "backend/.env still holds placeholder Mailjet credentials." >&2
  exit 1
fi

# A fixed project of its own: this script ends with `down -v`, which must never be able to
# delete the volume of a stack someone started with plain `docker compose up`.
PROJECT="email-confirmation-e2e"
unset COMPOSE_PROJECT_NAME
export FRONTEND_PORT="${FRONTEND_PORT:-4400}"
export MYSQL_PORT="${MYSQL_PORT:-3308}"
# Pinned and exported so the run does not depend on a developer's repository-root .env.
export MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-e2e_root_password}"
export MYSQL_DATABASE="${MYSQL_DATABASE:-email_confirmation}"
export MYSQL_USER="${MYSQL_USER:-email_app}"
export MYSQL_PASSWORD="${MYSQL_PASSWORD:-email_app_password}"
BASE="http://127.0.0.1:${FRONTEND_PORT}"
if [ -n "${E2E_RECIPIENT_EMAIL:-}" ]; then
  RECIPIENT_SOURCE="custom, from E2E_RECIPIENT_EMAIL"
else
  RECIPIENT_SOURCE="generated temporary Mailinator inbox"
fi

compose() { docker compose -p "$PROJECT" "$@"; }
mysql_query() {
  # MYSQL_PWD keeps the password off the command line, so the client prints no warning and
  # its stderr (a real error, if any) stays visible.
  compose exec -T -e MYSQL_PWD="$MYSQL_PASSWORD" mysql mysql -u"$MYSQL_USER" --silent \
    --skip-column-names "$MYSQL_DATABASE" -e "$1"
}
fail() { printf 'FAILED: %s\n' "$*" >&2; exit 1; }
TMP="$(mktemp -d)"

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    # Backend logs only: they never contain credentials (see app/services/email.py).
    compose logs --no-color --tail 40 backend >&2 || true
  fi
  rm -rf "$TMP"
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

echo "==> Recipient: $RECIPIENT ($RECIPIENT_SOURCE)"
echo "==> Starting the real stack (project $PROJECT, frontend on port $FRONTEND_PORT)"
compose up --build --detach --wait --wait-timeout 420

echo "==> Submitting $RECIPIENT"
code="$(curl -sS -o "$TMP/body.json" -w '%{http_code}' -H 'Content-Type: application/json' \
  -d "{\"name\":\"E2E Test\",\"email\":\"$RECIPIENT\"}" "$BASE/api/v1/submissions")"
[ "$code" = "201" ] || fail "POST returned $code: $(cat "$TMP/body.json")"
UUID7='[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'
grep -Eq "^\{\"detail\":\"Confirmation email accepted for delivery\.\",\"tracking_id\":\"$UUID7\"\}$" "$TMP/body.json" \
  || fail "response is not exactly detail + a UUIDv7 tracking_id: $(cat "$TMP/body.json")"
tracking_id="$(grep -Eo "$UUID7" "$TMP/body.json")"
echo "    201, body contains only 'detail' and tracking_id $tracking_id"

echo "==> Checking the database row"
row="$(mysql_query "SELECT BIN_TO_UUID(id), email_status, IFNULL(mailjet_message_id, '') FROM submissions WHERE email='$RECIPIENT'")"
[ -n "$row" ] || fail "no row stored for $RECIPIENT"
IFS=$'\t' read -r id status message_id <<<"$row"
grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-7' <<<"$id" || fail "id is not a UUIDv7: $id"
[ "$id" = "$tracking_id" ] || fail "the tracking id $tracking_id is not the stored row's id $id"
# Accepted, not delivered: the Send API answer says nothing about the recipient's server.
# The backend's reconciler may already have moved the row on; the request stored accepted.
case "$status" in accepted | queued | retrying | delivered) ;; *) fail "email_status is '$status'" ;; esac
[ -n "$message_id" ] || fail "no Mailjet MessageID was stored"
# Captured first, then searched with a here-string: a pipe into `grep -q` fails under pipefail
# when grep stops reading early and the writer gets a broken pipe.
backend_logs="$(compose logs --no-color backend)"
grep -q "delivery_status_updated submission_id=$id previous_status=pending new_status=accepted" <<<"$backend_logs" \
  || fail "the request did not store accepted"
echo "    row $id was stored as accepted with a Mailjet MessageID (now: $status)"

echo "==> Where the request spent its time"
compose logs --no-color backend | grep -o 'submission timings submission_id='"$id"'.*' \
  | sed 's/^/    /' || echo "    (timings line not found)"

echo "==> Following the message until Mailjet reports a final state"
# The running backend's reconciler follows the row too; both use the same lookup and
# transition rules, so whichever stores a state first, the other agrees with it.
set +e
compose exec -T backend python -m app.check_delivery "$id" \
  --timeout "$DELIVERY_TIMEOUT" --interval 2 | tee "$TMP/delivery.txt" | sed 's/^/    /'
delivery="${PIPESTATUS[0]}"
set -e
result="$(sed -n 's/^RESULT: \([A-Z_/]*\).*/\1/p' "$TMP/delivery.txt" | tail -n1)"

# The public status endpoint reports what MySQL now holds, and nothing else.
stored_status="$(mysql_query "SELECT email_status FROM submissions WHERE id=UUID_TO_BIN('$tracking_id')")"
public_status="$(curl -fsS "$BASE/api/v1/submissions/$tracking_id/status")"
[ "$public_status" = "{\"status\":\"$stored_status\"}" ] \
  || fail "status endpoint says $public_status, the database holds $stored_status"
echo "    GET /api/v1/submissions/$tracking_id/status -> $public_status (matches MySQL)"

printf '\nMailjet final state for %s: %s\n' "$RECIPIENT" "${result:-UNKNOWN}"
case "$delivery" in
  0)
    printf 'Real Mailjet E2E passed: the recipient'"'"'s mail server accepted the message.\n'
    printf 'That is delivery to the server, not a guarantee of Inbox placement.\n'
    domain="$(printf '%s' "${RECIPIENT#*@}" | tr '[:upper:]' '[:lower:]')"
    if [ "$domain" = "mailinator.com" ]; then
      printf 'See it in the temporary inbox:\n\n'
      printf '    https://www.mailinator.com/v4/public/inboxes.jsp?to=%s\n\n' "${RECIPIENT%%@*}"
    else
      printf 'Check that inbox (and its spam folder).\n\n'
    fi
    ;;
  # The check's own codes (see backend/app/check_delivery.py); anything else is a crash.
  10) fail "Mailjet reports $result: the message was not delivered" ;;
  11) fail "no final state within ${DELIVERY_TIMEOUT}s (TIMEOUT/PENDING); Mailjet may still deliver or bounce it" ;;
  *) fail "the delivery state could not be checked (exit $delivery)" ;;
esac
