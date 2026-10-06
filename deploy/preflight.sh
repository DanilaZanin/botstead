#!/usr/bin/env bash
# Проверка Docker-хоста до установки Bot Hub. Ничего не меняет.
set -uo pipefail
if [ "$(uname -s)" != Linux ] && [ -z "${PREFLIGHT_BRIDGE_NF_PATH:-}" ]; then echo "preflight: запускайте на Linux-сервере, где работает Docker (сейчас: $(uname -s))" >&2; exit 2; fi

cd "$(dirname "$0")" || exit 2
failed=0
fail() { printf 'FAIL: %s\n' "$1" >&2; failed=$((failed + 1)); }
ok() { printf 'OK: %s\n' "$1"; }
warn() { printf 'WARN: %s\n' "$1" >&2; }

config_bin() {
    local value
    value=$(awk -F '"' -v key="$1" '$1 ~ "^[[:space:]]*" key "[[:space:]]*=" {print $2; exit}' launcher.toml)
    printf '%s' "${value:-$2}"
}

ipt=$(config_bin iptables_bin iptables)
ip6t=$(config_bin ip6tables_bin ip6tables)
bridge_nf=${PREFLIGHT_BRIDGE_NF_PATH:-/proc/sys/net/bridge/bridge-nf-call-iptables}

if [ "$(cat "$bridge_nf" 2>/dev/null)" = 1 ]; then
    ok "br_netfilter: net.bridge.bridge-nf-call-iptables=1"
else
    fail "br_netfilter не активен ($bridge_nf). Исправление: sudo modprobe br_netfilter && sudo sysctl -w net.bridge.bridge-nf-call-iptables=1; для сохранения после перезагрузки: echo br_netfilter | sudo tee /etc/modules-load.d/br_netfilter.conf"
fi

if ! docker_version=$(docker info --format '{{.ServerVersion}}' 2>/dev/null); then
    fail 'Docker daemon недоступен. Исправление: sudo systemctl start docker; проверьте права на docker.sock'
else
    ok 'Docker daemon доступен'
    docker_major=${docker_version#v}
    docker_major=${docker_major%%.*}
    case $docker_major in
        '' | *[!0-9]*) warn "версия Docker не разобрана ('$docker_version'): проверьте вручную, что это Docker 28+ (флаг --gw-priority)" ;;
        *)
            if [ "$docker_major" -lt 28 ]; then
                warn "Docker $docker_version старше 28: лаунчер не сможет задать приоритет шлюзовой сети (--gw-priority), и шлюзом ядра может стать сеть пользователя. Обновите Docker до 28+ (Compose 2.33+)"
            else
                ok "Docker $docker_version (нужен 28+)"
            fi
            ;;
    esac
    if ipv6=$(docker network inspect --format '{{.EnableIPv6}}' bridge 2>/dev/null); then
        case "$ipv6" in
            true) printf 'INFO: IPv6 у default bridge включён. Сети ботов должны создаваться с --ipv6=false.\n' ;;
            false) ok 'IPv6 у default bridge выключен' ;;
            *) fail "непонятный ответ о IPv6 default bridge: $ipv6" ;;
        esac
    else
        fail 'не удалось проверить IPv6 default bridge: docker network inspect bridge'
    fi
fi

backend() {
    case "$1" in
        *'(nf_tables)'*) printf 'nf_tables' ;;
        *'(legacy)'*) printf 'legacy' ;;
        *) return 1 ;;
    esac
}

v4=''
v6=''
if version=$("$ipt" --version 2>/dev/null) && v4=$(backend "$version"); then
    :
else
    fail "$ipt: backend не определён. Debian/Ubuntu: sudo apt-get install iptables; затем проверьте iptables --version и задайте iptables_bin в launcher.toml"
fi
if version=$("$ip6t" --version 2>/dev/null) && v6=$(backend "$version"); then
    :
else
    fail "$ip6t: backend не определён. Debian/Ubuntu: sudo apt-get install iptables; затем задайте ip6tables_bin с тем же backend, что $ipt"
fi
if [ -n "$v4" ] && [ -n "$v6" ]; then
    if [ "$v4" = "$v6" ]; then
        ok "iptables и ip6tables используют $v4"
    else
        fail "backend $ipt ($v4) и $ip6t ($v6) различается. Debian/Ubuntu: sudo update-alternatives --config iptables && sudo update-alternatives --config ip6tables; затем задайте согласованные iptables_bin/ip6tables_bin в launcher.toml"
    fi
fi

if "$ipt" -S DOCKER-USER >/dev/null 2>&1; then
    ok "цепочка DOCKER-USER доступна через $ipt"
    if "$ipt" -S FORWARD 2>/dev/null | grep -Fq -- '-j DOCKER-USER'; then
        ok 'FORWARD направляет трафик в DOCKER-USER'
    else
        fail "FORWARD не направляет трафик в DOCKER-USER через $ipt. Проверьте backend Docker (nftables не использует DOCKER-USER); после исправления: sudo systemctl restart docker"
    fi
else
    fail "цепочка DOCKER-USER недоступна через $ipt. Проверьте iptables backend Docker и daemon.json (iptables не должен быть false); после исправления: sudo systemctl restart docker"
fi
if "$ip6t" -S INPUT >/dev/null 2>&1; then
    ok "IPv6 INPUT доступна через $ip6t"
else
    fail "IPv6 INPUT недоступна через $ip6t. Debian/Ubuntu: sudo apt-get install iptables; затем выберите тот же backend, что у $ipt"
fi

if [ "$failed" -ne 0 ]; then
    printf 'Preflight: %d проверок не прошло\n' "$failed" >&2
    exit 1
fi
printf 'Preflight: готово\n'
