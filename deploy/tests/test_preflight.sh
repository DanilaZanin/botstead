#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
printf '1\n' > "$tmp/bridge"

cat > "$tmp/bin/docker" <<'EOF'
#!/usr/bin/env bash
case "$1" in
    info) [ "${FAKE_DOCKER_DOWN:-0}" = 0 ] && printf '%s\n' "${FAKE_DOCKER_VERSION:-29.0.1}" ;;
    network) [ "$2" = inspect ] && printf '%s\n' "${FAKE_IPV6:-false}" ;;
    *) exit 1 ;;
esac
EOF
cat > "$tmp/bin/iptables" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = --version ]; then printf 'iptables v1.8.11 (%s)\n' "${FAKE_IPT_VERSION:-nf_tables}"; exit; fi
if [ "$1" = -S ] && [ "$2" = FORWARD ]; then
    [ "${FAKE_FORWARD:-1}" = 1 ] && printf '%s\n' '-A FORWARD -j DOCKER-USER'
    exit
fi
[ "$1" = -S ] && [ "$2" = DOCKER-USER ] && [ "${FAKE_CHAIN:-1}" = 1 ]
EOF
cat > "$tmp/bin/ip6tables" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = --version ]; then printf 'ip6tables v1.8.11 (%s)\n' "${FAKE_IP6_VERSION:-nf_tables}"; exit; fi
[ "$1" = -S ] && [ "$2" = INPUT ]
EOF
chmod +x "$tmp/bin/"*
export PATH="$tmp/bin:$PATH" PREFLIGHT_BRIDGE_NF_PATH="$tmp/bridge"

expect() {
    local want=$1 needle=$2 out rc=0
    shift 2
    out=$(env "$@" ./preflight.sh 2>&1) || rc=$?
    [ "$rc" -eq "$want" ] || { printf 'expected rc=%s, got %s: %s\n' "$want" "$rc" "$out" >&2; exit 1; }
    [[ $out == *"$needle"* ]] || { printf 'missing %s in: %s\n' "$needle" "$out" >&2; exit 1; }
}

expect 0 'Preflight: готово'
printf '0\n' > "$tmp/bridge"
expect 1 'sudo modprobe br_netfilter'
printf '1\n' > "$tmp/bridge"
expect 1 'DOCKER-USER недоступна' FAKE_CHAIN=0
expect 1 'FORWARD не направляет трафик' FAKE_FORWARD=0
expect 1 'Docker daemon недоступен' FAKE_DOCKER_DOWN=1
expect 1 'backend не определён' FAKE_IPT_VERSION=unknown
expect 1 'backend' FAKE_IP6_VERSION=legacy
expect 1 'непонятный ответ о IPv6' FAKE_IPV6=unknown
expect 0 'IPv6 у default bridge включён' FAKE_IPV6=true
expect 0 'OK: Docker 29.0.1 (нужен 28+)'
expect 0 'OK: Docker 28.0.0-rc.1 (нужен 28+)' FAKE_DOCKER_VERSION=28.0.0-rc.1
expect 0 'WARN: Docker 27.5.1 старше 28' FAKE_DOCKER_VERSION=27.5.1
expect 0 'WARN: версия Docker не разобрана' FAKE_DOCKER_VERSION=garbage
expect 1 'FAIL: iptables' FAKE_DOCKER_VERSION=27.5.1 FAKE_IPT_VERSION=unknown
printf 'preflight tests passed\n'
