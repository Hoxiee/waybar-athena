#!/usr/bin/env bash
# Optional adapter for an external New API monitor. With no adapter installed,
# emit nothing so Waybar hides all six slots. Private account data stays there.
set -uo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || exit 0
adapter=${NEWAPI_WAYBAR_SCRIPT:-"$script_dir/newapi.local.sh"}
[[ -x "$adapter" && ! "$adapter" -ef "${BASH_SOURCE[0]}" ]] || exit 0
exec "$adapter" "$@"
