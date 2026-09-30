#!/bin/sh
# s5-battery-watch.sh - log power-supply state over time.
# Answers: at 100% on cable, does the pack sit still, drain, or cycle?
LOG=/var/log/s5-battery.log
INTERVAL=${S5_INTERVAL:-60}
DURATION=${S5_DURATION:-0}      # seconds; 0 = run until killed

B=/sys/class/power_supply/battery
C=/sys/class/power_supply/sec-charger

rd() { cat "$1" 2>/dev/null || echo NA; }

if [ ! -f "$LOG" ]; then
  echo "epoch,iso,cap,bat_status,bat_uA,chg_status,chg_uA,volt_uV" > "$LOG"
fi

start=$(date +%s)
echo "watcher up, interval=${INTERVAL}s duration=${DURATION}s" >&2
while :; do
  now=$(date +%s)
  echo "$now,$(date -Iseconds 2>/dev/null || date),$(rd $B/capacity),$(rd $B/status),$(rd $B/current_avg),$(rd $C/status),$(rd $C/current_now),$(rd $B/voltage_now)" >> "$LOG"
  if [ "$DURATION" -gt 0 ] && [ $((now - start)) -ge "$DURATION" ]; then
    echo "duration reached, exiting" >&2
    break
  fi
  sleep "$INTERVAL"
done
