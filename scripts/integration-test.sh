#!/usr/bin/env bash
# Automated Docker integration test: builds the real images, starts MySQL, runs the
# migrations, starts the backend and the frontend, then checks the running stack end to end.
# Email goes to a stub of the Mailjet API (compose.integration.yaml), so this is safe for CI
# and needs no credentials. Exit status is non-zero on the first failed check; the stack's
# logs are printed on failure and everything (containers, network, volume) is removed on exit.
#
# The stack runs under its own Compose project name and its own ports, so a development
# stack started with plain `docker compose up` is left untouched.
set -euo pipefail

cd "$(dirname "$0")/.."

# A fixed project of its own: this script ends with `down -v`, which must never be able to
# delete the volume of a stack someone started with plain `docker compose up`.
PROJECT="email-confirmation-it"
unset COMPOSE_PROJECT_NAME
export FRONTEND_PORT="${FRONTEND_PORT:-4300}"
export MYSQL_PORT="${MYSQL_PORT:-3307}"
export MAILJET_STUB_PORT="${MAILJET_STUB_PORT:-8025}"
# Pinned and exported, so this run uses the same credentials whatever a developer's own
# repository-root .env holds. The stack has its own project name, so it has its own volume.
export MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-integration_root_password}"
export MYSQL_DATABASE="${MYSQL_DATABASE:-email_confirmation}"
export MYSQL_USER="${MYSQL_USER:-email_app}"
export MYSQL_PASSWORD="${MYSQL_PASSWORD:-email_app_password}"
BASE="http://127.0.0.1:${FRONTEND_PORT}"
RECIPIENT="integration-$(date +%s)@example.com"
ADMIN_PATH="/q7Vf2mZ9Kx4Tn8Rb3Lp6Hs1Yw5Dc0Gj4Ua9Pe2Nr7Bk6Xv3Qm8Sf1Zh5Wt0Cy4Ld9Jp2En7"
ADMIN_USER="demo-admin"
ADMIN_PASS="demo-admin-2026"
TMP="$(mktemp -d)"
COOKIES="$TMP/cookies"

compose() { docker compose -p "$PROJECT" -f compose.yaml -f compose.integration.yaml "$@"; }
mysql_query() {
  # MYSQL_PWD keeps the password off the command line, so the client prints no warning and
  # its stderr (a real error, if any) can stay visible.
  compose exec -T -e MYSQL_PWD="$MYSQL_PASSWORD" mysql mysql -u"$MYSQL_USER" --silent \
    --skip-column-names "$MYSQL_DATABASE" -e "$1"
}

step() { printf '\n==> %s\n' "$*"; }
pass() { printf '    ok: %s\n' "$*"; }
fail() { printf '    FAILED: %s\n' "$*" >&2; exit 1; }

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    printf '\n==> Integration test failed (exit %s); recent service logs:\n' "$status" >&2
    compose logs --no-color --tail 60 >&2 || true
  fi
  rm -rf "$TMP"
  [ -n "${CREATED_BACKEND_ENV:-}" ] && rm -f backend/.env
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

# compose.yaml reads backend/.env; the placeholder file is enough because every Mailjet
# value is overridden by compose.integration.yaml.
if [ ! -f backend/.env ]; then
  cp backend/.env.example backend/.env
  CREATED_BACKEND_ENV=1
  echo "backend/.env was missing; created it from backend/.env.example for this run only."
fi

step "1-6. Build the images, start MySQL, migrate, start backend and frontend, wait for health"
compose up --build --detach --wait --wait-timeout 420
compose ps

step "7. Backend health through the frontend proxy"
health="$(curl -fsS "$BASE/api/v1/health")"
[ "$health" = '{"status":"ok"}' ] || fail "unexpected health body: $health"
pass "$health"

step "8. Frontend serves the application"
code="$(curl -sS -o "$TMP/index.html" -w '%{http_code}' "$BASE/")"
[ "$code" = "200" ] || fail "GET / returned $code"
grep -q '<app-root' "$TMP/index.html" || fail "index.html does not contain <app-root"
[ "$(curl -fsS "$BASE/healthz")" = "ok" ] || fail "nginx /healthz did not answer ok"
pass "GET / is 200 and renders the Angular app"
# Swagger UI loads its bundle from the CDN and starts it inline; the policy on /docs must let
# it, or the page is blank. The SPA's stricter policy must stay everywhere else.
curl -fsS -D "$TMP/docs.headers" -o "$TMP/docs.html" "$BASE/docs" || fail "GET /docs failed"
grep -q 'swagger-ui' "$TMP/docs.html" || fail "/docs is not the Swagger UI page"
grep -iq "^content-security-policy:.*script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;" "$TMP/docs.headers" \
  || fail "the /docs policy blocks the Swagger UI bundle: $(grep -i '^content-security-policy' "$TMP/docs.headers")"
spa_headers="$(curl -fsS -D - -o /dev/null "$BASE/")"
grep -iq "^content-security-policy:.*script-src 'self';" <<<"$spa_headers" \
  || fail "the SPA lost its strict script policy"
openapi="$(curl -fsS "$BASE/openapi.json")"
grep -q '"/api/v1/submissions"' <<<"$openapi" || fail "/openapi.json is not proxied"
pass "API docs: /docs is Swagger UI with a policy that lets it load, /openapi.json is proxied"

step "9. API integration checks"
body="$(curl -sS -o "$TMP/body.json" -w '%{http_code}' -H 'Content-Type: application/json' \
  -d "{\"name\":\"Integration Test\",\"email\":\"$RECIPIENT\"}" "$BASE/api/v1/submissions")"
[ "$body" = "201" ] || fail "POST /api/v1/submissions returned $body: $(cat "$TMP/body.json")"
UUID7='[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'
grep -Eq "^\{\"detail\":\"Confirmation email accepted for delivery\.\",\"tracking_id\":\"$UUID7\"\}$" "$TMP/body.json" \
  || fail "response body is not exactly detail + a UUIDv7 tracking_id: $(cat "$TMP/body.json")"
tracking_id="$(grep -Eo "$UUID7" "$TMP/body.json")"
pass "201 with exactly detail and tracking_id ($tracking_id)"

code="$(curl -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
  -d '{"name":"","email":"not-an-email"}' "$BASE/api/v1/submissions")"
[ "$code" = "422" ] || fail "invalid payload returned $code"
pass "invalid payload is 422"

for _ in 1 2; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
    -d "{\"name\":\"Integration Test\",\"email\":\"$RECIPIENT\"}" "$BASE/api/v1/submissions")"
  [ "$code" = "201" ] || fail "repeat submission returned $code"
done
code="$(curl -sS -D "$TMP/headers.txt" -o "$TMP/body.json" -w '%{http_code}' -H 'Content-Type: application/json' \
  -d "{\"name\":\"Integration Test\",\"email\":\"$RECIPIENT\"}" "$BASE/api/v1/submissions")"
[ "$code" = "429" ] || fail "fourth submission for one mailbox returned $code"
grep -qi '^retry-after: [0-9]' "$TMP/headers.txt" || fail "429 without Retry-After header"
pass "fourth request for the same mailbox is 429 with Retry-After"

messages="$(curl -fsS "http://127.0.0.1:${MAILJET_STUB_PORT}/messages")"
count="$(printf '%s' "$messages" | { grep -o "$RECIPIENT" || true; } | wc -l | tr -d ' ')"
[ "$count" = "3" ] || fail "the email stub saw $count sends for $RECIPIENT, expected 3: $messages"
pass "the stubbed provider received exactly 3 messages for $RECIPIENT"

step "9b. Request correlation through the proxy, and log hygiene"
code="$(curl -sS -o /dev/null -D "$TMP/headers.txt" -w '%{http_code}' -H 'Content-Type: application/json' \
  -H 'X-Request-ID: integration-correlation-1' -d '{"name":"","email":"not-an-email"}' "$BASE/api/v1/submissions")"
[ "$code" = "422" ] || fail "the correlated request returned $code"
grep -qi '^x-request-id: integration-correlation-1' "$TMP/headers.txt" || fail "the client's request id did not come back through the proxy"
curl -sS -o /dev/null -D "$TMP/headers.txt" "$BASE/api/v1/health"
generated="$(grep -i '^x-request-id:' "$TMP/headers.txt" | tr -d '\r' | awk '{print $2}')"
grep -Eq '^[0-9a-f]{32}$' <<<"$generated" || fail "no request id was generated for a request without one: '$generated'"
backend_logs="$(compose logs --no-color backend)"
# The path is logged quoted (repr), so a client cannot forge a log line with a decoded newline.
grep -q "request completed method=POST path='/api/v1/submissions' status_code=422" <<<"$backend_logs" \
  || fail "the completed request was not logged"
grep -q 'integration-correlation-1' <<<"$backend_logs" || fail "the request id is not in the backend log"
forged="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/v1/x%0A2026-01-01%20CRITICAL%20forged")"
[ "$forged" = "404" ] || fail "the forged-path request returned $forged"
forged_logs="$(compose logs --no-color backend)"
# The decoded newline is escaped inside one quoted value...
grep -qF "path='/api/v1/x\\n2026-01-01 CRITICAL forged'" <<<"$forged_logs" \
  || fail "the path with a decoded newline was not escaped into a single log line"
# ...so no log line of its own begins with what the client sent.
grep -qE '^[a-z0-9-]+ +\| *2026-01-01 CRITICAL' <<<"$forged_logs" \
  && fail "a client wrote its own line into the backend log"
pass "the client's request id is echoed through the proxy and logged, one is generated when absent, and a decoded newline in the path cannot forge a log line"

step "10. MySQL persistence and the UUIDv7 primary key"
rows="$(mysql_query "SELECT BIN_TO_UUID(id), email, email_status, IFNULL(mailjet_message_id, '') FROM submissions WHERE email='$RECIPIENT' ORDER BY id")"
# Captured once, then searched with here-strings: a pipe into `grep -q` fails under pipefail
# when grep stops reading early and the writer gets a broken pipe.
submission_logs="$(compose logs --no-color backend)"
[ "$(printf '%s\n' "$rows" | grep -c .)" = "3" ] || fail "expected 3 rows, got: $rows"
printf '%s\n' "$rows" | while IFS=$'\t' read -r id email status message_id; do
  grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$' <<<"$id" || fail "id is not a UUIDv7: $id"
  # The background reconciler may already have moved the row on (step 10a checks how).
  case "$status" in accepted | queued | delivered) ;; *) fail "row $id has status $status" ;; esac
  grep -Eq '^[0-9]+$' <<<"$message_id" || fail "row $id has no Mailjet MessageID"
  # Send API acceptance is not delivery: the request itself stored accepted, nothing more.
  grep -q "delivery_status_updated submission_id=$id previous_status=pending new_status=accepted final=false source=send_api" <<<"$submission_logs" \
    || fail "the request did not store accepted for row $id"
done
column="$(mysql_query "SELECT COLUMN_TYPE, COLUMN_KEY FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='$MYSQL_DATABASE' AND TABLE_NAME='submissions' AND COLUMN_NAME='id'")"
[ "$column" = "$(printf 'binary(16)\tPRI')" ] || fail "id column is not a BINARY(16) primary key: $column"
first_id="$(printf '%s\n' "$rows" | head -n1 | cut -f1)"
pass "3 rows stored by the request as 'accepted' with their Mailjet MessageID; id is BINARY(16) PRIMARY KEY holding UUIDv7 (e.g. $first_id)"

step "10a. Background reconciliation: rows move on by themselves (the stub answers queued, then sent)"
# Nobody runs app.check_delivery here: the running backend polls the stub on its own.
for _ in $(seq 1 30); do
  [ "$(mysql_query "SELECT COUNT(*) FROM submissions WHERE email='$RECIPIENT' AND email_status='delivered'")" = "3" ] && break
  sleep 1
done
[ "$(mysql_query "SELECT COUNT(*) FROM submissions WHERE email='$RECIPIENT' AND email_status='delivered'")" = "3" ] \
  || fail "the reconciler did not deliver all 3 rows: $(mysql_query "SELECT email_status FROM submissions WHERE email='$RECIPIENT'")"
reconciler_logs="$(compose logs --no-color backend)"
# No request started this work, so its lines carry the "-" request id.
grep -Eq "\[-\] app\.services\.delivery_status: delivery_status_updated submission_id=$first_id previous_status=accepted new_status=queued final=false source=reconciler provider_duration_ms=[0-9.]+" <<<"$reconciler_logs" \
  || fail "accepted -> queued was not stored by the reconciler for $first_id"
grep -Eq "\[-\] app\.services\.delivery_status: delivery_status_updated submission_id=$first_id previous_status=queued new_status=delivered final=true source=reconciler provider_duration_ms=[0-9.]+" <<<"$reconciler_logs" \
  || fail "queued -> delivered was not stored by the reconciler for $first_id"
grep -q 'delivery reconciler started interval_s=1 ' <<<"$reconciler_logs" || fail "the reconciler did not log its start"
pass "the running backend moved all 3 rows accepted -> queued -> delivered by itself, logged without a request id"

step "10a'. The manual delivery check shares the same lookup (python -m app.check_delivery)"
if ! check="$(compose exec -T backend python -m app.check_delivery "$first_id" --timeout 20 --interval 1)"; then
  fail "the delivery check did not report delivery: $check"
fi
grep -q '^RESULT: DELIVERED' <<<"$check" || fail "unexpected delivery check output: $check"
[ "$(mysql_query "SELECT email_status FROM submissions WHERE id=UUID_TO_BIN('$first_id')")" = "delivered" ] \
  || fail "the delivered state was not stored"
pass "python -m app.check_delivery polled the message resource, printed RESULT: DELIVERED and stored 'delivered'"

step "10a''. Public delivery status: read from MySQL only, nothing private, never a Mailjet lookup"
[ "$tracking_id" = "$first_id" ] || fail "the tracking id $tracking_id is not the first row's id $first_id"
status_url="$BASE/api/v1/submissions/$tracking_id/status"
code="$(curl -sS -D "$TMP/headers.txt" -o "$TMP/status.json" -w '%{http_code}' "$status_url")"
[ "$code" = "200" ] || fail "GET status returned $code"
[ "$(cat "$TMP/status.json")" = '{"status":"delivered"}' ] || fail "unexpected status body: $(cat "$TMP/status.json")"
grep -qi '^cache-control: no-store' "$TMP/headers.txt" || fail "the status response may be cached"
message_id="$(mysql_query "SELECT mailjet_message_id FROM submissions WHERE id=UUID_TO_BIN('$tracking_id')")"
for private in "$RECIPIENT" "Integration Test" "\"$message_id\"" created_at; do
  grep -qF -- "$private" "$TMP/status.json" && fail "the status response leaks $private"
done
# Every row is final now, so the reconciler has nothing to look up; any Mailjet lookup from
# here on would have been caused by a status read.
lookups() { compose logs --no-color mailjet-stub | { grep -c 'GET /v3/REST/message/' || true; }; }
before="$(lookups)"
# The reconciler's own lookups (steps 10a, 10a') are in there: proof the counter counts.
[ "$before" -gt 0 ] || fail "no Mailjet lookup found in the stub log; the lookup counter is broken"
# Written straight into MySQL behind the backend's back: the endpoint must show it at once.
mysql_query "UPDATE submissions SET email_status='soft_bounced' WHERE id=UUID_TO_BIN('$tracking_id')"
[ "$(curl -fsS "$status_url")" = '{"status":"soft_bounced"}' ] || fail "the status endpoint did not read the database"
mysql_query "UPDATE submissions SET email_status='delivered' WHERE id=UUID_TO_BIN('$tracking_id')"
for _ in 1 2 3 4 5; do
  [ "$(curl -fsS "$status_url")" = '{"status":"delivered"}' ] || fail "the status endpoint did not read the database"
done
[ "$(lookups)" = "$before" ] || fail "status reads caused Mailjet lookups ($before before, $(lookups) after)"
not_found='{"detail":"No submission matches this tracking ID."}'
for id in not-a-uuid 1 "$(printf '%s' "$tracking_id" | sed 's/^......../00000000/')"; do
  code="$(curl -sS -o "$TMP/status.json" -w '%{http_code}' "$BASE/api/v1/submissions/$id/status")"
  [ "$code" = "404" ] || fail "status for '$id' returned $code"
  [ "$(cat "$TMP/status.json")" = "$not_found" ] || fail "status 404 for '$id' differs: $(cat "$TMP/status.json")"
done
code="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/v1/submissions")"
[ "$code" = "405" ] || fail "GET /api/v1/submissions returned $code; there must be no public listing"
pass "status reads MySQL (a direct UPDATE shows at once), returns only {\"status\"} with no-store, caused 0 Mailjet lookups in 6 reads (stub log: $before lookups before and after); invalid and unknown ids share one 404; no listing"

step "10b. Migration round trip on live data (downgrade to the integer key and back)"
# Schema and code change together (see the migration), so the backend - and with it the
# reconciler - is stopped for the round trip, as a real deploy would do.
compose stop backend >/dev/null
compose run --rm --no-deps -T migrate alembic downgrade 877af0a23a74 >/dev/null
legacy="$(mysql_query "SELECT COLUMN_TYPE, EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='$MYSQL_DATABASE' AND TABLE_NAME='submissions' AND COLUMN_NAME='id'")"
[ "$legacy" = "$(printf 'int\tauto_increment')" ] || fail "downgrade did not restore the integer key: $legacy"
[ "$(mysql_query "SELECT GROUP_CONCAT(id ORDER BY id) FROM submissions")" = "1,2,3" ] || fail "downgrade did not renumber rows 1..3"
compose run --rm --no-deps -T migrate alembic upgrade head >/dev/null
again="$(mysql_query "SELECT BIN_TO_UUID(id) FROM submissions ORDER BY created_at")"
[ "$(printf '%s\n' "$again" | grep -c .)" = "3" ] || fail "rows lost across the round trip: $again"
grep -Evq '^[0-9a-f]{8}-[0-9a-f]{4}-7' <<<"$again" && fail "upgrade produced a non-v7 id: $again"
[ "$(printf '%s\n' "$again" | sort)" = "$again" ] || fail "re-generated ids are not in created_at order"
current="$(compose run --rm --no-deps -T migrate alembic current 2>/dev/null | { grep -o '[0-9a-f]\{12\}' || true; } | head -n1)"
[ "$current" = "c3d8e1f4a6b2" ] || fail "alembic current is not the delivery-status revision: $current"
# Downgrading folds the new states back into 'sent'; upgrading renames 'sent' to 'accepted'.
statuses="$(mysql_query "SELECT GROUP_CONCAT(DISTINCT email_status) FROM submissions")"
[ "$statuses" = "accepted" ] || fail "statuses after the round trip: $statuses, expected accepted"
enum="$(mysql_query "SELECT COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='$MYSQL_DATABASE' AND TABLE_NAME='submissions' AND COLUMN_NAME='email_status'")"
[ "$enum" = "enum('pending','accepted','queued','retrying','delivered','soft_bounced','hard_bounced','blocked','failed')" ] \
  || fail "email_status after the round trip: $enum"
# A second upgrade on an already-migrated schema must be a no-op (the migration is retryable).
compose run --rm --no-deps -T migrate alembic upgrade head >/dev/null
[ "$(mysql_query "SELECT BIN_TO_UUID(id) FROM submissions ORDER BY created_at")" = "$again" ] || fail "re-running upgrade changed the keys"
check="$(compose run --rm --no-deps -T migrate alembic check 2>&1 | tail -n1)"
grep -q 'No new upgrade operations detected' <<<"$check" || fail "models and migrations differ: $check"
pass "downgrade renumbered 1..3, upgrade re-keyed all 3 rows with ordered UUIDv7 ids, re-upgrade is a no-op, alembic check is clean"

step "10c. A restarted backend resumes a row it left unfinished"
# The downgrade dropped the MessageIDs; give the newest row its MessageID back while the
# backend is down, as if it had stopped before Mailjet reported anything.
resume_id="$(printf '%s\n' "$again" | tail -n1)"
mysql_query "UPDATE submissions SET mailjet_message_id=3, email_status='accepted' WHERE id=UUID_TO_BIN('$resume_id')"
compose start backend >/dev/null
for _ in $(seq 1 60); do
  [ "$(mysql_query "SELECT email_status FROM submissions WHERE id=UUID_TO_BIN('$resume_id')")" = "delivered" ] && break
  sleep 1
done
[ "$(mysql_query "SELECT email_status FROM submissions WHERE id=UUID_TO_BIN('$resume_id')")" = "delivered" ] \
  || fail "the restarted backend did not resume row $resume_id"
resumed_logs="$(compose logs --no-color backend)"
grep -q "delivery_status_updated submission_id=$resume_id previous_status=accepted new_status=delivered final=true source=reconciler" <<<"$resumed_logs" \
  || fail "the resumed transition was not logged"
[ "$(curl -fsS "$BASE/api/v1/health")" = '{"status":"ok"}' ] || fail "the restarted backend is not healthy"
pass "after a restart the backend found row $resume_id still accepted and stored delivered without being asked"

step "11. Admin panel: authentication, read-only access, search, filter, sort, pagination, details"
code="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE$ADMIN_PATH/submission/list")"
[ "$code" = "302" ] || fail "unauthenticated list returned $code (expected redirect to login)"
code="$(curl -sS -o /dev/null -w '%{http_code}' -c "$COOKIES" -d "username=$ADMIN_USER&password=wrong" "$BASE$ADMIN_PATH/login")"
[ "$code" = "400" ] || fail "wrong password returned $code"
code="$(curl -sS -o /dev/null -w '%{http_code}' -D "$TMP/headers.txt" -c "$COOKIES" -d "username=$ADMIN_USER&password=$ADMIN_PASS" "$BASE$ADMIN_PATH/login")"
[ "$code" = "302" ] || fail "demo login returned $code"
grep -q 'admin_session' "$COOKIES" || fail "no session cookie after login"
# The panel builds absolute URLs from the Host header; through the proxy they must keep the
# port the browser used, or every redirect and asset would point at port 80.
location="$(grep -i '^location:' "$TMP/headers.txt" | tr -d '\r' | awk '{print $2}')"
[ "$location" = "$BASE$ADMIN_PATH/" ] || fail "login redirected to '$location', expected $BASE$ADMIN_PATH/"
pass "unauthenticated access redirected, wrong password rejected, demo credentials accepted"

# Give the three rows distinct names and statuses, and add 30 more, so search, filter,
# sort and pagination can be checked for their results on MySQL, not just for a 200.
ids="$(mysql_query "SELECT BIN_TO_UUID(id) FROM submissions ORDER BY created_at")"
id_alice="$(printf '%s\n' "$ids" | sed -n 1p)"
id_bob="$(printf '%s\n' "$ids" | sed -n 2p)"
id_carol="$(printf '%s\n' "$ids" | sed -n 3p)"
mysql_query "UPDATE submissions SET name='Alice Adams', email_status='accepted' WHERE id=UUID_TO_BIN('$id_alice');
UPDATE submissions SET name='Bob Brown', email_status='failed' WHERE id=UUID_TO_BIN('$id_bob');
UPDATE submissions SET name='Carol Clark', email_status='pending' WHERE id=UUID_TO_BIN('$id_carol');
INSERT INTO submissions (id, name, email, email_status, created_at)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 30)
SELECT UNHEX(CONCAT(LPAD(HEX(FLOOR(UNIX_TIMESTAMP(NOW(3)) * 1000) - n), 12, '0'), '7', LPAD(HEX(n), 3, '0'), '8', LPAD(HEX(n), 15, '0'))),
       CONCAT('Zulu ', LPAD(n, 2, '0')), CONCAT('bulk', n, '@example.net'), 'accepted', NOW(6) - INTERVAL n DAY
FROM seq"
[ "$(mysql_query "SELECT COUNT(*) FROM submissions")" = "33" ] || fail "seeding the admin checks did not produce 33 rows"
new_id="$id_alice"
uuids_in_order() { printf '%s' "$1" | { grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}' || true; } | awk '!seen[$0]++'; }
dashboard="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/")"
grep -q 'Total submissions' <<<"$dashboard" || fail "dashboard has no summary cards"
listing="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list")"
grep -q "$RECIPIENT" <<<"$listing" || fail "list does not show $RECIPIENT"
assets="$(printf '%s' "$listing" | { grep -oE '(src|href)="[^"]*/statics/[^"]*"' || true; } | cut -d'"' -f2 | sort -u)"
[ -n "$assets" ] || fail "list page references no static assets"
for asset in $assets; do
  case "$asset" in "$BASE"/*) ;; *) fail "asset URL does not use the browser's origin: $asset" ;; esac
  code="$(curl -sS -o /dev/null -w '%{http_code}' "$asset")"
  [ "$code" = "200" ] || fail "asset $asset returned $code"
done
grep -q "$new_id" <<<"$listing" || fail "list does not show the UUIDv7 id $new_id"
grep -q '/submission/create' <<<"$listing" && fail "list offers a create action"
grep -q '/submission/export/' <<<"$listing" && fail "list offers an export action"
search="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list?search=bob+br")"
[ "$(uuids_in_order "$search")" = "$id_bob" ] || fail "search 'bob br' did not return exactly Bob: $(uuids_in_order "$search")"
search="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list?search=$RECIPIENT")"
[ "$(uuids_in_order "$search" | wc -l | tr -d ' ')" = "3" ] || fail "search by email did not return the 3 matching rows"
# Searching the BINARY(16) key: the comparison is against the key itself, not a text LIKE.
search="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list?search=$id_carol")"
[ "$(uuids_in_order "$search")" = "$id_carol" ] || fail "search by UUID did not return exactly Carol: $(uuids_in_order "$search")"
grep -q 'Alice Adams' <<<"$search" && fail "search by UUID returned another row"
missing="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list?search=01234567-89ab-7cde-8f01-23456789abcd")"
grep -qE 'Alice Adams|Bob Brown|Carol Clark' <<<"$missing" && fail "a UUID that matches no row returned rows"
for term in "0199c0e1-1234-7abc-89de" "zzzzzzzz-zzzz-7zzz-zzzz-zzzzzzzzzzzz" "' OR 1=1 --"; do
  code="$(curl -sS -o "$TMP/search.html" -w '%{http_code}' -b "$COOKIES" --get --data-urlencode "search=$term" "$BASE$ADMIN_PATH/submission/list")"
  [ "$code" = "200" ] || fail "search for '$term' returned $code"
  grep -qE 'Alice Adams|Bob Brown|Carol Clark' "$TMP/search.html" && fail "search for '$term' returned rows"
done
filtered="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list?email_status=failed")"
[ "$(uuids_in_order "$filtered")" = "$id_bob" ] || fail "status filter 'failed' did not return exactly Bob"
filtered="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list?email_status=pending")"
[ "$(uuids_in_order "$filtered")" = "$id_carol" ] || fail "status filter 'pending' did not return exactly Carol"
sorted="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list?sortBy=name&sort=asc")"
[ "$(uuids_in_order "$sorted" | head -n3 | tr '\n' ' ')" = "$id_alice $id_bob $id_carol " ] || fail "sort by name asc is wrong"
sorted="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list?email_status=__all&sortBy=email_status&sort=asc&search=$RECIPIENT")"
# MySQL orders an ENUM by its declared order: pending, accepted, ..., failed.
[ "$(uuids_in_order "$sorted" | tr '\n' ' ')" = "$id_carol $id_alice $id_bob " ] || fail "sort by status asc is not pending,accepted,failed: $(uuids_in_order "$sorted")"
sorted="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list?sortBy=created_at&sort=desc")"
[ "$(uuids_in_order "$sorted" | head -n1)" = "$id_carol" ] || fail "sort by created_at desc does not start with the newest row"
page1="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list")"
page2="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list?page=2")"
all50="$(curl -fsS -b "$COOKIES" "$BASE$ADMIN_PATH/submission/list?pageSize=50")"
[ "$(uuids_in_order "$page1" | wc -l | tr -d ' ')" = "25" ] || fail "page 1 does not hold 25 rows"
[ "$(uuids_in_order "$page2" | wc -l | tr -d ' ')" = "8" ] || fail "page 2 does not hold the remaining 8 rows"
[ "$(uuids_in_order "$all50" | wc -l | tr -d ' ')" = "33" ] || fail "pageSize=50 does not show all 33 rows"
details="$(curl -sS -b "$COOKIES" -o "$TMP/details.html" -w '%{http_code}' "$BASE$ADMIN_PATH/submission/details/$new_id")"
[ "$details" = "200" ] || fail "details page returned $details"
grep -q "$RECIPIENT" "$TMP/details.html" || fail "details page does not show the submission"
for path in "submission/create" "submission/edit/$new_id" "submission/export/csv"; do
  code="$(curl -sS -b "$COOKIES" -o /dev/null -w '%{http_code}' "$BASE$ADMIN_PATH/$path")"
  [ "$code" = "403" ] || fail "$path returned $code, expected 403"
done
code="$(curl -sS -b "$COOKIES" -o /dev/null -w '%{http_code}' -X DELETE "$BASE$ADMIN_PATH/submission/delete?pks=$new_id")"
[ "$code" = "403" ] || fail "delete returned $code, expected 403"
[ "$(mysql_query "SELECT COUNT(*) FROM submissions")" = "33" ] || fail "a refused mutation changed the table"
pass "dashboard, list (+$(printf '%s\n' "$assets" | grep -c .) assets), search by name, email and UUIDv7 id (and nothing for UUID-like junk), status filter, sorting (incl. MySQL enum order), 25/8/33 pagination and details work; create/edit/export/delete are 403"

step "12. Credentials: the backend uses the application account, and nothing leaks into a log"
# Exit status only: the URL holds a password, so it is never printed.
compose exec -T backend sh -c 'case "$DATABASE_URL" in mysql+asyncmy://'"$MYSQL_USER"':*@mysql:*) exit 0 ;; *) exit 1 ;; esac' \
  || fail "the backend does not connect as $MYSQL_USER"
compose exec -T backend sh -c '[ -z "$MYSQL_ROOT_PASSWORD" ]' || fail "the root password reached the backend container"
# Everything the run produced, including the failed admin login from step 11.
all_logs="$(compose logs --no-color)"
grep -qi 'GENERATED ROOT PASSWORD' <<<"$all_logs" && fail "MySQL printed a generated root password"
# No credential in any service's log.
for secret in "$MYSQL_PASSWORD" "$MYSQL_ROOT_PASSWORD" "$ADMIN_PASS" "integration-stub-secret" "integration-stub-key"; do
  grep -qF "$secret" <<<"$all_logs" && fail "a credential reached a log"
done
# Addresses are checked against what the application itself logs; the Mailjet stub exists to
# record the messages it was sent.
backend_only="$(compose logs --no-color backend)"
grep -qF "$RECIPIENT" <<<"$backend_only" && fail "a full recipient address reached the backend log"
grep -q "recipient=i\\*\\*\\*@example.com" <<<"$backend_only" || fail "the masked recipient is not in the backend log"
grep -q 'admin login failed' <<<"$backend_only" || fail "the failed admin login was not logged"
# The panel's location stays out of the backend log: startup only says it is on, and the
# admin requests of step 11 are logged under a placeholder.
grep -q 'application startup .*admin_enabled=true' <<<"$backend_only" || fail "startup did not log admin_enabled=true"
grep -qF "${ADMIN_PATH#/}" <<<"$backend_only" && fail "the admin path reached the backend log"
grep -qF "path='<admin>/login'" <<<"$backend_only" || fail "admin requests are not logged under the placeholder"
# NGINX keeps no access log for the panel, so neither its path nor the address typed into its
# search box in step 11 reaches the access log, while the rest of the site is still logged.
# Only access lines are checked: the error log is on for every location, and a warning such
# as a large admin asset being buffered to disk quotes the request line (see docs/security.md).
frontend_access="$(compose logs --no-color frontend | { grep -vE '[0-9]{4}/[0-9]{2}/[0-9]{2} [0-9:]{8} \[[a-z]+\] ' || true; })"
grep -qF "${ADMIN_PATH#/}" <<<"$frontend_access" && fail "the admin path reached the NGINX access log"
grep -qF "$RECIPIENT" <<<"$frontend_access" && fail "an admin search term reached the NGINX access log"
grep -qF 'GET /api/v1/health' <<<"$frontend_access" || fail "the NGINX access log no longer records the API"
# curl sends no Referer, so the browser side is checked through the policy: the panel's
# pages must not hand their URL (search terms included) to requests NGINX does log, such
# as the browser's own /favicon.ico.
admin_headers="$(curl -fsS -D - -o /dev/null "$BASE$ADMIN_PATH/login")"
grep -iq '^referrer-policy: no-referrer' <<<"$admin_headers" \
  || fail "the admin pages would send their URL as Referer to logged locations"
# Every submission logs where its time went.
grep -Eq 'submission timings submission_id=[0-9a-f-]{36} db_create_duration_ms=[0-9.]+ mailjet_request_duration_ms=[0-9.]+ db_status_update_duration_ms=[0-9.]+ duration_ms=[0-9.]+' <<<"$backend_only" \
  || fail "the stage timings of a submission were not logged"
pass "backend connects as $MYSQL_USER with no root password in its environment; no generated root password and no credential in any service log; no full address in the backend log, only the masked form; no admin path in the backend log or the NGINX access log; stage timings logged"

printf '\nIntegration test passed.\n'
