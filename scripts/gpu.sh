#!/usr/bin/env bash
# Waybar custom/gpu — NVIDIA readout via nvidia-smi (~21 ms per call, polled every 5 s).
#
# Prints exactly one JSON object on stdout. On *any* failure it prints nothing and
# exits 0, which makes Waybar hide the module rather than show a stale or bogus
# value — the same failure posture as a missing sensor.
#
# Classes match the thresholds in tokens/state.css.

set -uo pipefail

command -v nvidia-smi >/dev/null 2>&1 || exit 0

line=$(nvidia-smi \
  --query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,name \
  --format=csv,noheader,nounits 2>/dev/null | head -n1)

[ -n "$line" ] || exit 0

IFS=',' read -r temp util mem_used mem_total power name <<<"$line"

trim() { printf '%s' "${1#"${1%%[![:space:]]*}"}"; }
temp=$(trim "$temp");           util=$(trim "$util")
mem_used=$(trim "$mem_used");   mem_total=$(trim "$mem_total")
power=$(trim "$power");         name=$(trim "$name")

# nvidia-smi reports [N/A] for some fields while the driver is settling or on
# headless/vGPU setups. Bail rather than render "[N/A]°C".
case "$temp $util $mem_used" in *'N/A'*) exit 0 ;; esac
[[ "$temp" =~ ^[0-9]+$ && "$util" =~ ^[0-9]+$ ]] || exit 0

class="normal"
if   ((temp >= 84)); then class="critical"
elif ((temp >= 72)); then class="warning"
fi

read -r vram_used vram_total < <(
  awk -v u="$mem_used" -v t="$mem_total" 'BEGIN { printf "%.1f %.1f", u/1024, t/1024 }'
)

text="<span font='Material Symbols Rounded 21px' rise='-4pt'>developer_board</span> ${util}%  <span fgalpha='70%'>·</span>  ${temp}°C"
tooltip="${name}\n\nTemp    ${temp}°C\nLoad    ${util}%\nVRAM    ${vram_used} / ${vram_total} GiB\nPower   ${power} W"

printf '{"text":"%s","tooltip":"%s","class":"%s"}\n' "$text" "$tooltip" "$class"
