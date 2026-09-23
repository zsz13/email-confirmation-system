#!/usr/bin/env bash
# Prints the recipient for scripts/e2e-real-email.sh on stdout, and nothing else.
#
# E2E_RECIPIENT_EMAIL, when set and non-empty, is used exactly as given once it passes the
# check below. Otherwise a unique Mailinator address is generated, whose public inbox needs
# no account. Kept apart from the E2E script so tests can run it without Docker or Mailjet.
set -euo pipefail
export LC_ALL=C  # ASCII-only character ranges in the pattern below, whatever the locale

if [ -z "${E2E_RECIPIENT_EMAIL:-}" ]; then
  printf 'ecs-e2e-%s-%s@mailinator.com\n' "$(date +%Y%m%d-%H%M%S)" "$RANDOM"
  exit 0
fi

# Deliberately stricter than RFC 5322 (a plain dot-atom address is all a test needs): the
# E2E script puts the address into a JSON body and an SQL query, so quotes, backslashes,
# whitespace and control characters must never get through.
atom='[A-Za-z0-9_%+-]+'
label='[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?'
pattern="^${atom}(\\.${atom})*@(${label}\\.)+[A-Za-z]{2,}\$"
address="$E2E_RECIPIENT_EMAIL"
local_part="${address%%@*}"

if ! [[ "$address" =~ $pattern ]] || [ "${#address}" -gt 254 ] || [ "${#local_part}" -gt 64 ]; then
  printf 'E2E_RECIPIENT_EMAIL is not a valid email address: %q\n' "$address" >&2
  echo "Unset it to use a generated Mailinator inbox, or set it to e.g. user@example.com." >&2
  exit 1
fi

printf '%s\n' "$address"
