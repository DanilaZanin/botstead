#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"

cat > "$tmp/bin/docker" <<'EOF'
#!/usr/bin/env bash
[ "$1" = exec ]
shift 2
exec "$@"
EOF
cat > "$tmp/bin/curl" <<'EOF'
#!/usr/bin/env bash
[[ " $* " != *"$LAUNCHER_SECRET"* ]] || { echo 'secret in curl argv' >&2; exit 1; }
[[ " $* " == *' -H @- '* ]] || { echo 'header is not read from stdin' >&2; exit 1; }
header=$(cat)
[ "$header" = "Authorization: Bearer $LAUNCHER_SECRET" ] || { echo 'incorrect stdin header' >&2; exit 1; }
printf '{"ok":true}\n'
if [[ " $* " == *' -w '* ]]; then printf '200\n'; fi
EOF
chmod +x "$tmp/bin/"*
export PATH="$tmp/bin:$PATH" LAUNCHER_SECRET='test-secret-visible-only-on-stdin' LAUNCHER_SOCKET=/tmp/fake.sock

out=$(./launcherctl.sh info)
[ "$out" = '{"ok":true}' ]
export LAUNCHER=bothub-launcher
sed -n '/^lc() {/,/^}/p' tests/isolation_check.sh > "$tmp/lc.sh"
# shellcheck disable=SC1090,SC1091
source "$tmp/lc.sh"
out=$(lc GET /v1/info)
[ "$out" = $'{"ok":true}\n200' ]
printf 'launcher secret stdin test passed\n'
