#!/bin/sh
# Entry point of the certbot service in docker-compose.prod.yml. Keeps a Let's Encrypt
# certificate for $DOMAIN issued and renewed, using the HTTP-01 challenge through the webroot
# that NGINX serves on port 80, and publishes it to the tls volume NGINX reads.
#
#     certbot.sh           the service itself: issue if missing, then check for renewal every 12 h
#     certbot.sh publish   certbot's deploy hook, run after every successful issuance or renewal
set -eu

live="/etc/letsencrypt/live/$DOMAIN"
webroot=/var/www/certbot
tls=/tls
# NGINX runs as this uid and gid (the unprivileged image) and cannot read certbot's
# root-only files, so it gets copies it owns.
nginx_id=101

log() { echo "certbot.sh: $*"; }

# Replace one file in the tls volume in a single rename, so NGINX never reads half of it. The
# copy starts private, and chmod comes before chown: once NGINX's user owns the file, changing
# its mode would need a capability this container does not have.
install_for_nginx() { # <source> <name> <mode>
    rm -f "$tls/.$2.new"  # left over from an interrupted run, and no longer writable here
    (umask 077 && cp "$1" "$tls/.$2.new")
    chmod "$3" "$tls/.$2.new"
    chown "$nginx_id:$nginx_id" "$tls/.$2.new"
    mv -f "$tls/.$2.new" "$tls/$2"
}

# The key goes first: NGINX reloads when the chain changes, and by then the key matches it.
publish() { # <key> <chain>
    install_for_nginx "$1" privkey.pem 600
    install_for_nginx "$2" fullchain.pem 644
}

if [ "${1:-}" = publish ]; then
    publish "$live/privkey.pem" "$live/fullchain.pem"
    log "published the certificate for $DOMAIN"
    exit 0
fi

# Brings the tls volume in line with certbot's current certificate. NGINX cannot start without
# one, so on the very first start a short-lived self-signed pair stands in until Let's Encrypt
# has issued the real one; browsers warn about it meanwhile. Run at start-up and after every
# certbot run, so a publish the deploy hook could not finish is repaired on the next round.
sync_tls() {
    if [ -s "$live/fullchain.pem" ]; then
        if ! cmp -s "$live/fullchain.pem" "$tls/fullchain.pem"; then
            publish "$live/privkey.pem" "$live/fullchain.pem"
            log "published the certificate for $DOMAIN"
        fi
    elif [ ! -s "$tls/fullchain.pem" ]; then
        tmp=$(mktemp -d)
        openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 7 \
            -subj "/CN=$DOMAIN" -keyout "$tmp/privkey.pem" -out "$tmp/fullchain.pem" 2>/dev/null
        publish "$tmp/privkey.pem" "$tmp/fullchain.pem"
        rm -rf "$tmp"
        log "no certificate for $DOMAIN yet; NGINX starts with a temporary self-signed one"
    fi
}

sync_tls

# Let's Encrypt is asked only once NGINX serves the challenge path: every failed validation
# counts against its rate limit of 5 an hour per host name.
challenge_path_served() {
    probe="$webroot/.well-known/acme-challenge/probe-$$"
    mkdir -p "$webroot/.well-known/acme-challenge"
    echo ok > "$probe"
    served=0
    wget -q -O /dev/null "http://nginx:8080/.well-known/acme-challenge/${probe##*/}" || served=1
    rm -f "$probe"
    return "$served"
}

while :; do
    if [ ! -s "$live/fullchain.pem" ]; then
        log "waiting for NGINX to serve the challenge path"
        until challenge_path_served; do sleep 5; done
        log "requesting a certificate for $DOMAIN"
        if ! certbot certonly --non-interactive --webroot --webroot-path "$webroot" \
            --domain "$DOMAIN" --cert-name "$DOMAIN" \
            --email "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email \
            --deploy-hook "/bin/sh /deploy/certbot.sh publish"; then
            log "issuance failed, reason above (usually the DNS A record, port 80 or" \
                "LETSENCRYPT_EMAIL); retrying in 1 h"
            sleep 3600
            continue
        fi
    else
        # Renews only when due. The deploy hook was stored with the certificate at issuance, so
        # a renewal is published at once whether this loop or an operator ran it.
        certbot renew --non-interactive || log "renewal check failed; retrying in 12 h"
    fi
    sync_tls
    sleep 43200
done
