#!/bin/sh
# Escape stdin for a redis.conf double-quoted ``requirepass`` value.
#
# Compatible with BusyBox (redis:*-alpine) and GNU userland. Encodes every
# input byte as a redis.conf ``\xHH`` escape so the value round-trips under
# Redis's config parser — including backslash, quote, tab, CR, embedded
# newlines, and a trailing newline.
#
# Preconditions:
#   - Password bytes are read from stdin (may be empty).
# Postconditions:
#   - Writes the escaped form to stdout with no trailing newline added.
#   - Empty stdin yields empty stdout.
#
# Do not use BusyBox awk ``gsub`` to escape backslashes — it is a no-op for
# ``\`` on Alpine. Hex encoding avoids that class of bugs entirely.
set -eu
od -An -tx1 -v | tr -d ' \n' | sed 's/../\\x&/g'
