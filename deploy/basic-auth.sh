#!/bin/sh
# Entry point of the one-shot basic-auth service in docker-compose.prod.yml. Before NGINX starts,
# it turns BASIC_AUTH_USERNAME and BASIC_AUTH_PASSWORD from .env.prod into the htpasswd file
# NGINX checks. Only a bcrypt hash is stored, in a Docker volume; neither value is printed.
set -eu

if [ -z "${BASIC_AUTH_USERNAME:-}" ] || [ -z "${BASIC_AUTH_PASSWORD:-}" ]; then
    echo "basic-auth: BASIC_AUTH_USERNAME and BASIC_AUTH_PASSWORD must both be set in .env.prod" >&2
    exit 1
fi
if [ "$BASIC_AUTH_USERNAME" = change-me ] || [ "$BASIC_AUTH_PASSWORD" = change-me ]; then
    echo "basic-auth: replace the change-me placeholders in .env.prod with real values" >&2
    exit 1
fi

# -i reads the password from stdin (printf is a shell builtin), so it never appears as a process
# argument. -B -C 10 is bcrypt at cost 10; NGINX verifies it on every request, so each step up
# in cost doubles the CPU time of every request.
printf '%s' "$BASIC_AUTH_PASSWORD" | htpasswd -niBC 10 "$BASIC_AUTH_USERNAME" > /auth/htpasswd.new
mv -f /auth/htpasswd.new /auth/htpasswd
echo "basic-auth: credentials file written"
