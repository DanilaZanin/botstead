#!/usr/bin/env bash
# Шлюз моделей не должен быть доступен снаружи: для /bots/gateway/ в nginx стоит return 404 и нет proxy_pass.
set -euo pipefail
conf="$(dirname "$0")/../nginx/bothub-locations.conf"
block=$(awk '/location \^~ \/bots\/gateway\/ \{/{f=1} f{print} f&&/^\}/{exit}' "$conf")
[ -n "$block" ] || { echo "FAIL: нет location для /bots/gateway/" >&2; exit 1; }
echo "$block" | grep -q 'return 404;' || { echo "FAIL: /bots/gateway/ не закрыт (нет return 404)" >&2; exit 1; }
if echo "$block" | grep -q 'proxy_pass'; then echo "FAIL: /bots/gateway/ проксируется наружу" >&2; exit 1; fi
echo "ok: /bots/gateway/ закрыт снаружи"
