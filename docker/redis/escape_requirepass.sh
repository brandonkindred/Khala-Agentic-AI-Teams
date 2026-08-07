#!/bin/sh
# Escape stdin for a redis.conf double-quoted ``requirepass`` value.
#
# Compatible with BusyBox (redis:*-alpine) and GNU userland. Escapes
# backslash, double-quote, tab, and CR; joins physical newlines as ``\n``.
#
# Preconditions:
#   - Password bytes are read from stdin (may be empty).
# Postconditions:
#   - Writes the escaped form to stdout with no trailing newline added.
#   - Backslash is doubled so Redis does not treat ``\a`` / ``\n`` / ``\xHH``
#     as escapes when parsing the conf double-quoted string.
#
# BusyBox awk ``gsub(/\\/, ...)`` is a no-op for backslash — do not use awk to
# escape ``\``. Use sed for ``\`` / ``"`` / tab / CR; awk only joins lines.
set -eu
sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/	/\\t/g' -e 's/\r/\\r/g' | awk '
BEGIN { ORS = "" }
NR > 1 { printf "\\n" }
{ printf "%s", $0 }
'
