#!/bin/sh
# Mounted into the edge NGINX container's /docker-entrypoint.d, so the image runs it just before
# NGINX starts. It leaves a background loop behind that reloads NGINX whenever the certbot
# service publishes a new certificate: the first Let's Encrypt certificate replacing the
# temporary one, and every renewal after that. A reload is graceful; open connections finish
# on the old workers.
set -eu

cert=/etc/nginx/tls/fullchain.pem

(
    current=$(cksum < "$cert")
    while sleep 60; do
        latest=$(cksum < "$cert") || continue
        [ "$latest" = "$current" ] && continue
        # nginx -t loads the new pair first. certbot replaces the key and then the chain, so a
        # check that lands in between fails and the next round tries again, while the running
        # configuration keeps serving.
        if nginx -t -q && nginx -s reload; then
            current=$latest
            echo "$0: certificate changed, NGINX reloaded"
        fi
    done
) &
