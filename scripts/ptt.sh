#!/usr/bin/env bash
# Waybar custom/ptt — yaptt-indicator with its class copied into "alt".
#
# The indicator draws a Nerd Font glyph in "text"; the bar picks a Material
# Symbols icon from format-icons by "alt" instead. "text" keeps the class so it
# is never empty (Waybar hides a custom module with empty text).
# Prints nothing if the indicator is missing or fails, so the pill self-hides.

set -uo pipefail

command -v yaptt-indicator >/dev/null 2>&1 || exit 0
yaptt-indicator 2>/dev/null | jq -c 'select(.class) | .alt = .class | .text = .class' 2>/dev/null
exit 0
