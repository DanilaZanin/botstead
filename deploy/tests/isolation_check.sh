#!/usr/bin/env bash
# Проверка изоляции ботов фактом, на настоящем Docker: два бота двух пользователей.
#
# Что проверяется (подробности и модель угроз: docs/isolation.md):
#   1. нет root и sudo, capabilities пусты, no-new-privileges, нет setuid, rootfs read-only, нет docker.sock;
#   2. mounts только именованные тома, лимиты memory/cpus/pids действуют на деле (форк-бомба и OOM упираются в них);
#   3. сеть: интернет и API ядра есть, а Postgres, лаунчер, хост, соседняя сеть, 169.254, RFC1918 недоступны;
#   4. бот A не видит том и процессы бота B, чужой том логинов, свой том логинов только для чтения;
#   5. CDP слушает только loopback, экран отдаётся unix-сокетом uid 1001 (TCP 5900 нет, код бота до сокета не
#      дотягивается), websockify отсутствует; openbox с конфигом образа, политики Chromium, noexec-каталог загрузок;
#   6. правила iptables стоят первыми в DOCKER-USER и INPUT и не размножаются;
#   7. лаунчер отказывается трогать контейнер без своей метки, exec и stop_exec работают;
#   8. пересоздание бота сохраняет его home;
#   9. PID 1 бота: cwd /, интерпретатор в изолированном режиме (-I), ctypes.py из /home/bot не исполняется;
#  10. защита ядра (docs/isolation.md, "Защита ядра"): seccomp-профиль лаунчера и второй фильтр bot-guard;
#      код бота не получает user namespaces и опасные сокеты, а Chromium (uid 1001) работает без --no-sandbox;
#      опции netfilter (setsockopt/getsockopt на SOL_IP и SOL_IPV6) закрыты и внутри user namespace браузера;
#      весь entrypoint идёт под bot-guard (окна без второго фильтра нет).
#  10а. исполнитель шагов процедур (bot-image/procedure-step.mjs): скрипт и playwright-core принадлежат root и лежат по
#      абсолютному пути; подложенный в $HOME/.node_modules модуль не загружается; процесс идёт с окружением
#      HOME=/nonexistent и PATH, cwd /, payload шага в командных строках процессов не виден; procedure_step_cancel
#      завершает его процесс под uid 1001. Браузер перед этим скрипт сам ведёт на about:blank (navigate через
#      procedure_step), проверки не зависят от страницы на входе и не пропускаются.
#  10б. свежий бот без предподготовки (до первого шага процедуры): политики NewTabPageLocation, HomepageLocation,
#      HomepageIsNewTabPage, RestoreOnStartup ведут на about:blank, первая вкладка about:blank, чтение navigate и
#      действие navigate с expected about:blank проходят (не page_ambiguous), вкладка становится запрошенной.
#  11. режим human (browser-mode): порт 9222 не слушается, в командных строках нет remote-debugging, --user-data-dir
#      указывает на ~/.config/botstead-browser-human, маркер в профиле человека недоступен uid 1000; после возврата в
#      bot профиль человека удалён, cookies перенесены (merged N), маркер в профиль бота не попал.
#      Весь экземпляр человека (профиль, кэш HTTP, дампы, XDG_*, TMPDIR, HOME) лежит в одном каталоге: вне его и вне
#      профиля бота в /home/browser, /tmp, /dev/shm новых файлов нет, после возврата нет ни каталога, ни маркера
#      сессии, ни канареек. Буфер обмена X (PRIMARY, CLIPBOARD и cut buffer CUT_BUFFER0, который заполняет x11vnc) пуст
#      при входе в human и после возврата.
#  12. шлюзовая сеть ядра (Docker 28+, docs/isolation.md, "Приоритет шлюзовой сети"): после создания сетей двух
#      пользователей шлюз по умолчанию ядра и его сети в docker inspect (шлюз, GwPriority) те же, что до создания,
#      сети пользователей имеют приоритет ниже основной; опубликованный порт ядра (api/health с хоста) отвечает без
#      единой ошибки всё время, пока лаунчер создаёт сети и подключает к ним ядро. На Docker старше 28 вместо провала
#      предупреждение.
#
# Запуск на Linux-сервере, где поднят стек (docker compose up -d) и собран образ bothub-bot:
#   cd deploy && ./tests/isolation_check.sh
# Переменные: KEEP=1 не удалять тестовые объекты, ISOCHK_SKIP_MEM=1 пропустить проверку OOM,
#   LAUNCHER_CONTAINER / CORE_CONTAINER / DB_CONTAINER / IPTABLES_BIN, HOST_PORT (порт слушателя на хосте),
#   BOT_IMAGE (образ ботов, по умолчанию bothub-bot; на стенде botstead-bot, как в image конфигурации лаунчера),
#   ISOCHK_CORE_URL (опубликованный порт ядра для проверки 12, по умолчанию http://127.0.0.1:8080/api/health).
# Тестовые объекты называются isochk-* / isochk*, после прогона удаляются. Код возврата: 0 все проверки
# прошли, 1 есть провалы, 2 стенд не готов.
# Команды для контейнера намеренно в одинарных кавычках: переменные раскрываются внутри контейнера.
# shellcheck disable=SC2016
set -uo pipefail
# shellcheck source=tests/isolation_helpers.sh
source "$(dirname "$0")/isolation_helpers.sh"

LAUNCHER=${LAUNCHER_CONTAINER:-bothub-launcher}
CORE=${CORE_CONTAINER:-bothub-core}
DB=${DB_CONTAINER:-bothub-db}
IPT=${IPTABLES_BIN:-iptables}
BOT_IMAGE=${BOT_IMAGE:-bothub-bot}
HOST_PORT=${HOST_PORT:-18765}
CORE_URL=${ISOCHK_CORE_URL:-http://127.0.0.1:8080/api/health}

OWNER_A=isochka
OWNER_B=isochkb
BOT_A=isochk-a
BOT_B=isochk-b
FOREIGN=isochk-foreign
CA="bot-$BOT_A"
CB="bot-$BOT_B"
NET_A="bothub-u-$OWNER_A"
NET_B="bothub-u-$OWNER_B"

PASS=0
FAIL=0
WARN=0
HOST_LISTENER_PID=""
HEALTH_PID=""
HEALTH_DIR=""

ok()   { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$1"; }
warn() { WARN=$((WARN + 1)); printf '  warn  %s\n' "$1"; }
section() { printf '\n== %s\n' "$1"; }

# eq "описание" "получено" "ожидалось"
eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (получено '$2', ожидалось '$3')"; fi; }
# check "описание" команда...   проходит, если команда вернула 0
check() { local d=$1; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi; }
# refuse "описание" команда...  проходит, если команда провалилась
refuse() { local d=$1; shift; if "$@" >/dev/null 2>&1; then bad "$d"; else ok "$d"; fi; }

# dx контейнер 'команда bash' : выполнить от пользователя бота
dx() { docker exec --user 1000:1000 "$1" bash -c "$2"; }
# dxt контейнер хост порт : TCP-соединение изнутри контейнера (4 секунды)
dxt() { dx "$1" "timeout 4 bash -c 'exec 3<>/dev/tcp/$2/$3'"; }

# По счётчику доказываем, что соединение прервало именно наше правило.
drop_packets() {
    docker exec "$LAUNCHER" "$IPT" -L "$1" -v -n -x |
        drop_packet_count "$2" "$3"
}
refuse_with_drop() {
    local label=$1 chain=$2 bridge=$3 dest=$4 before after
    shift 4
    for _ in 1 2 3; do
        before=$(drop_packets "$chain" "$bridge" "$dest")
        if "$@" >/dev/null 2>&1; then bad "$label (соединение прошло)"; return; fi
        after=$(drop_packets "$chain" "$bridge" "$dest")
        if [[ $before =~ ^[0-9]+$ && $after =~ ^[0-9]+$ ]] && (( after > before )); then
            ok "$label (счётчик DROP вырос)"
            return
        fi
    done
    bad "$label (нет прироста счётчика DROP в $chain для $bridge -> $dest)"
}

# lc МЕТОД ПУТЬ [json] : запрос к лаунчеру, в последней строке вывода HTTP-код
lc() {
    # Секрет передаётся через stdin curl; в argv процессов на хосте его нет.
    docker exec "$LAUNCHER" sh -c \
        'printf "Authorization: Bearer %s\n" "$LAUNCHER_SECRET" | curl -sS -o - -w "\n%{http_code}" --unix-socket "$LAUNCHER_SOCKET" -X "$1" -H @- -H "Content-Type: application/json" ${3:+-d "$3"} "http://launcher$2"' \
        _ "$1" "$2" "${3:-}" 2>&1
}
code_of() { printf '%s' "$1" | tail -n1; }
body_of() { printf '%s' "$1" | sed '$d'; }

wait_running() {
    local _
    for _ in $(seq 1 60); do
        [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ] && return 0
        sleep 0.5
    done
    return 1
}

# wait_port контейнер порт : ждать, пока порт слушается (по /proc/net/tcp)
wait_port() {
    local _
    for _ in $(seq 1 40); do
        if dx "$1" "grep -qi ':$(printf '%04X' "$2") ' /proc/net/tcp"; then return 0; fi
        sleep 0.5
    done
    return 1
}

cleanup() {
    [ "${KEEP:-0}" = "1" ] && return
    [ -n "$HOST_LISTENER_PID" ] && kill "$HOST_LISTENER_PID" 2>/dev/null
    [ -n "$HEALTH_PID" ] && kill "$HEALTH_PID" 2>/dev/null
    [ -n "$HEALTH_DIR" ] && rm -rf "$HEALTH_DIR"
    lc DELETE "/v1/bots/$BOT_A?purge=true" >/dev/null 2>&1
    lc DELETE "/v1/bots/$BOT_B?purge=true" >/dev/null 2>&1
    lc DELETE "/v1/logins/$OWNER_A" >/dev/null 2>&1
    lc DELETE "/v1/logins/$OWNER_B" >/dev/null 2>&1
    docker rm -f "bot-$FOREIGN" >/dev/null 2>&1
    local n
    for n in "$NET_A" "$NET_B"; do
        docker network disconnect -f "$n" "$CORE" >/dev/null 2>&1
        docker network rm "$n" >/dev/null 2>&1
    done
    docker volume rm "bothub-login-$OWNER_A" "bothub-login-$OWNER_B" >/dev/null 2>&1
}
trap cleanup EXIT

# ------------------------------------------------------------------ готов ли стенд
section "Стенд"
command -v docker >/dev/null || { echo "нет docker"; exit 2; }
for c in "$LAUNCHER" "$CORE" "$DB"; do
    [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] || { echo "контейнер $c не запущен: docker compose up -d"; exit 2; }
done
docker image inspect "$BOT_IMAGE" >/dev/null 2>&1 || { echo "нет образа $BOT_IMAGE: ./build-bot-image.sh"; exit 2; }
KEEP=0 cleanup
ok "стенд поднят: $LAUNCHER, $CORE, $DB, образ $BOT_IMAGE"

# ------------------------------------------------------------------ шлюзовая сеть ядра: состояние до создания сетей
section "Шлюзовая сеть ядра до создания ботов"
DOCKER_VERSION=$(docker version --format '{{.Server.Version}}' 2>/dev/null)
DOCKER_MAJOR=$(docker_major "$DOCKER_VERSION")
GW_STRICT=0
if [[ $DOCKER_MAJOR =~ ^[0-9]+$ ]] && (( DOCKER_MAJOR >= 28 )); then
    GW_STRICT=1
    ok "Docker $DOCKER_VERSION: --gw-priority поддерживается, проверки шлюза строгие"
else
    warn "Docker '${DOCKER_VERSION:-неизвестна}' старше 28 или не определён: --gw-priority не передаётся, провалы проверок шлюза ниже будут предупреждениями"
fi
core_networks_now() { docker inspect -f '{{json .NetworkSettings.Networks}}' "$CORE" | core_networks; }
core_route_now() { docker exec "$CORE" cat /proc/net/route | default_route; }
# gw_check "описание" команда... : при Docker 28+ провал это FAIL, на старом Docker предупреждение
gw_check() {
    local d=$1; shift
    if "$@" >/dev/null 2>&1; then ok "$d"
    elif [ "$GW_STRICT" = 1 ]; then bad "$d"
    else warn "$d (Docker старше 28)"
    fi
}
GW_ROWS_BEFORE=$(core_networks_now)
GW_ROUTE_BEFORE=$(core_route_now)
if [ -n "$GW_ROWS_BEFORE" ] && [ -n "$GW_ROUTE_BEFORE" ]; then
    ok "снимок до: сети ядра $(printf '%s\n' "$GW_ROWS_BEFORE" | awk '{printf "%s%s", sep, $1; sep=", "}'), шлюз по умолчанию $GW_ROUTE_BEFORE"
else
    bad "не удалось снять шлюзовую сеть ядра (docker inspect $CORE, /proc/net/route)"
fi
# Опубликованный порт: запросы с хоста идут в фоне, пока лаунчер создаёт сети и подключает к ним ядро.
if ! command -v curl >/dev/null; then
    warn "на хосте нет curl: проверка опубликованного порта пропущена"
elif ! curl -sS -m 3 -f -o /dev/null "$CORE_URL" >/dev/null 2>&1; then
    warn "опубликованный порт ядра недоступен до проверки ($CORE_URL): задайте ISOCHK_CORE_URL, проверка порта пропущена"
else
    HEALTH_DIR=$(mktemp -d "${TMPDIR:-/tmp}/isochk-health.XXXXXX")
    : >"$HEALTH_DIR/run"
    health_probe_loop "$CORE_URL" "$HEALTH_DIR" &
    HEALTH_PID=$!
    ok "фоновые запросы к $CORE_URL запущены"
fi

# ------------------------------------------------------------------ создание
section "Создание ботов двух пользователей через лаунчер"
for spec in "$BOT_A:$OWNER_A" "$BOT_B:$OWNER_B"; do
    bot=${spec%%:*}
    owner=${spec##*:}
    out=$(lc POST /v1/bots "{\"bot_id\":\"$bot\",\"owner_id\":\"$owner\"}")
    eq "create_bot $bot (владелец $owner) вернул 201" "$(code_of "$out")" "201"
done
if wait_running "$CA"; then ok "бот A запущен"; else bad "бот A не запустился"; exit 1; fi
if wait_running "$CB"; then ok "бот B запущен"; else bad "бот B не запустился"; exit 1; fi

# ------------------------------------------------------------------ шлюзовая сеть ядра: состояние после
section "Шлюзовая сеть ядра после создания ботов"
if [ -n "$HEALTH_PID" ]; then
    rm -f "$HEALTH_DIR/run"
    wait "$HEALTH_PID" 2>/dev/null
    HEALTH_PID=""
    HEALTH_TOTAL=0
    HEALTH_FAILED=0
    read -r HEALTH_TOTAL HEALTH_FAILED <"$HEALTH_DIR/count" 2>/dev/null
    if [[ ${HEALTH_TOTAL:-0} =~ ^[0-9]+$ ]] && (( HEALTH_TOTAL > 0 )); then
        gw_check "опубликованный порт ядра не рвался при создании сетей ($HEALTH_TOTAL запросов, ошибок $HEALTH_FAILED)" \
            test "${HEALTH_FAILED:-1}" -eq 0
    else
        bad "фоновые запросы к $CORE_URL не выполнились ни разу"
    fi
fi
GW_ROWS_AFTER=$(core_networks_now)
GW_ROUTE_AFTER=$(core_route_now)
gw_check "шлюз по умолчанию ядра прежний (было '$GW_ROUTE_BEFORE', стало '$GW_ROUTE_AFTER')" \
    test "$GW_ROUTE_AFTER" = "$GW_ROUTE_BEFORE"
gw_check "прежние сети ядра не изменились: шлюз и GwPriority (docker inspect до и после)" \
    test -z "$(comm -23 <(printf '%s\n' "$GW_ROWS_BEFORE" | sort) <(printf '%s\n' "$GW_ROWS_AFTER" | sort))"
GW_MAIN_PRIORITY=$(printf '%s\n' "$GW_ROWS_BEFORE" | awk 'NR == 1 || $3 + 0 > max {max = $3 + 0} END {print max}')
for n in "$NET_A" "$NET_B"; do
    priority=$(printf '%s\n' "$GW_ROWS_AFTER" | awk -v net="$n" '$1 == net {print $3}')
    if [ "$GW_STRICT" = 1 ] && ! [[ $priority =~ ^-?[0-9]+$ ]]; then
        bad "ядро не подключено к $n или GwPriority не показан"
    else
        gw_check "сеть $n: приоритет шлюза ($priority) ниже основной сети ядра ($GW_MAIN_PRIORITY)" \
            test "${priority:-0}" -lt "$GW_MAIN_PRIORITY"
    fi
done

# ------------------------------------------------------------------ привилегии
section "Привилегии внутри бота A"
eq "uid не root" "$(dx "$CA" 'id -u')" "1000"
refuse "sudo отсутствует" dx "$CA" 'command -v sudo'
refuse "sudo -n не работает" dx "$CA" 'sudo -n true'
refuse "su в root без пароля не проходит" dx "$CA" 'echo | timeout 5 su -c id root'
eq "CapEff пуст" "$(dx "$CA" "awk '/^CapEff/ {print \$2}' /proc/self/status")" "0000000000000000"
eq "NoNewPrivs = 1" "$(dx "$CA" "awk '/^NoNewPrivs/ {print \$2}' /proc/self/status")" "1"
eq "seccomp-фильтр включён (Seccomp = 2)" "$(dx "$CA" "awk '/^Seccomp:/ {print \$2}' /proc/self/status")" "2"
eq "setuid/setgid файлов нет" "$(dx "$CA" 'find / -xdev -perm /6000 -type f 2>/dev/null | wc -l')" "0"
refuse "корневая ФС только для чтения" dx "$CA" 'touch /usr/isochk-write-test'
check "/tmp (tmpfs) доступен на запись" dx "$CA" 'touch /tmp/isochk && rm /tmp/isochk'
check "/run (tmpfs) доступен на запись" dx "$CA" 'touch /run/isochk && rm /run/isochk'
check "/home/bot (том) доступен на запись" dx "$CA" 'touch /home/bot/isochk && rm /home/bot/isochk'
check "docker.sock в боте нет" dx "$CA" '[ ! -e /var/run/docker.sock ] && [ ! -e /run/docker.sock ]'
refuse "/proc/sys/kernel недоступен на запись" dx "$CA" 'echo 1 > /proc/sys/kernel/hostname'
refuse "uid 1000 не читает профиль браузера" dx "$CA" '[ -r /home/browser ]'
refuse "uid 1000 не читает память PID 1" dx "$CA" 'python3 -c '\''open("/proc/1/mem", "rb")'\'''

section "Параметры контейнера A (docker inspect)"
insp() { docker inspect -f "$1" "$CA"; }
eq "Privileged = false" "$(insp '{{.HostConfig.Privileged}}')" "false"
eq "CapDrop = [ALL]" "$(insp '{{json .HostConfig.CapDrop}}')" '["ALL"]'
case "$(insp '{{json .HostConfig.SecurityOpt}}')" in *no-new-privileges*) ok "SecurityOpt: no-new-privileges" ;; *) bad "SecurityOpt без no-new-privileges" ;; esac
eq "ReadonlyRootfs = true" "$(insp '{{.HostConfig.ReadonlyRootfs}}')" "true"
eq "Memory = 2 ГиБ" "$(insp '{{.HostConfig.Memory}}')" "2147483648"
eq "NanoCpus = 2" "$(insp '{{.HostConfig.NanoCpus}}')" "2000000000"
eq "PidsLimit = 512" "$(insp '{{.HostConfig.PidsLimit}}')" "512"
eq "пользователь 1000:1000" "$(insp '{{.Config.User}}')" "1000:1000"
eq "бот не стартует до восстановления правил после reboot" "$(insp '{{.HostConfig.RestartPolicy.Name}}')" "no"
eq "сеть пользователя" "$(insp '{{.HostConfig.NetworkMode}}')" "$NET_A"
eq "сеть пользователя: IPv6 выключен" "$(docker network inspect -f '{{.EnableIPv6}}' "$NET_A")" "false"
eq "IPv6 в контейнере выключен" "$(dx "$CA" 'cat /proc/sys/net/ipv6/conf/all/disable_ipv6')" "1"
eq "метка лаунчера" "$(insp '{{index .Config.Labels "bothub.managed"}}')" "1"
mounts=$(insp '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Destination}}|{{.RW}}{{"\n"}}{{end}}' | sed '/^$/d' | sort)
eq "mounts: три именованных тома, логины read-only, host mounts нет" "$mounts" \
    "$(printf 'volume|bot-%s-home|/home/bot|true\nvolume|bot-%s-browser|/home/browser|true\nvolume|bothub-login-%s|/home/bot/.auth|false' "$BOT_A" "$BOT_A" "$OWNER_A" | sort)"
printf '  info  runtime: %s\n' "$(insp '{{.HostConfig.Runtime}}')"

section "Лимиты на деле (cgroup)"
mem_max=$(dx "$CA" 'cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes')
eq "cgroup memory.max = 2 ГиБ" "$mem_max" "2147483648"
eq "cgroup pids.max = 512" "$(dx "$CA" 'cat /sys/fs/cgroup/pids.max 2>/dev/null || cat /sys/fs/cgroup/pids/pids.max')" "512"
case "$(dx "$CA" 'cat /sys/fs/cgroup/cpu.max 2>/dev/null')" in "200000 100000") ok "cgroup cpu.max = 2 CPU" ;; "") warn "cpu.max не читается (cgroup v1?)" ;; *) bad "cpu.max не 2 CPU" ;; esac
forks=$(dx "$CA" 'python3 - <<"PY"
import subprocess
procs = []
try:
    for _ in range(1000):
        procs.append(subprocess.Popen(["sleep", "40"]))
    print("nolimit", len(procs))
except OSError:
    print("limit", len(procs))
for p in procs:
    p.kill()
PY' 2>&1 | tail -n1)
case "$forks" in
    limit\ *) n=${forks#limit }; if [ "$n" -lt 520 ]; then ok "форк-бомба упёрлась в pids-limit на $n процессах"; else bad "процессов создано $n при лимите 512"; fi ;;
    *) bad "форк-бомба не остановлена ($forks)" ;;
esac
if [ "${ISOCHK_SKIP_MEM:-0}" = "1" ]; then
    warn "проверка OOM пропущена (ISOCHK_SKIP_MEM=1)"
else
    dx "$CA" 'python3 -c "
x = bytearray(3 << 30)
for i in range(0, len(x), 4096):
    x[i] = 1
"' >/dev/null 2>&1
    rc=$?
    if [ "$rc" -ne 0 ] && [ "$(docker inspect -f '{{.State.Running}}' "$CA")" = "true" ]; then ok "3 ГиБ в боте убиты OOM (код $rc), контейнер жив"; else bad "выделение 3 ГиБ не остановлено лимитом (код $rc)"; fi
fi

# ------------------------------------------------------------------ сеть
section "Сеть бота A"
IP_B=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NET_B\").IPAddress}}" "$CB")
GW_A=$(docker network inspect -f '{{(index .IPAM.Config 0).Gateway}}' "$NET_A")
BR_A=$(docker network inspect -f '{{index .Options "com.docker.network.bridge.name"}}' "$NET_A")
BR_B=$(docker network inspect -f '{{index .Options "com.docker.network.bridge.name"}}' "$NET_B")
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
DB_IPS=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$DB")

check "DNS и интернет работают (https://example.com)" dx "$CA" 'curl -sS -m 15 -o /dev/null https://example.com'
check "ядро отвечает на API-порту (core:8080)" dx "$CA" 'curl -sS -m 5 -f -o /dev/null http://core:8080/api/health'

docker exec -d "$CORE" timeout 120 python -m http.server 9999 --bind 0.0.0.0 >/dev/null 2>&1
sleep 1
check "контрольная проверка: порт 9999 ядра слушается" docker exec "$CORE" python -c "import socket; socket.create_connection(('127.0.0.1', 9999), 3)"
refuse "ядро: любой порт кроме API закрыт (core:9999)" dxt "$CA" core 9999

refuse "имя db не резолвится (Postgres в другой сети)" dx "$CA" 'getent hosts db'
check "контрольная проверка: Postgres слушает 5432 внутри своего контейнера" docker exec "$DB" pg_isready -h 127.0.0.1 -p 5432
for ip in $DB_IPS; do
    refuse_with_drop "Postgres $ip:5432 недоступен" BOTHUB-ISO "$BR_A" "$(blocked_cidr "$ip")" dxt "$CA" "$ip" 5432
done
refuse "лаунчер недоступен по имени" dx "$CA" 'getent hosts bothub-launcher'

timeout 120 python3 -m http.server "$HOST_PORT" --bind 0.0.0.0 --directory /tmp >/dev/null 2>&1 &
HOST_LISTENER_PID=$!
sleep 1
check "контрольная проверка: слушатель хоста отвечает на хосте" curl -sS -m 3 -o /dev/null "http://127.0.0.1:$HOST_PORT/"
refuse_with_drop "хост через шлюз сети ($GW_A:$HOST_PORT) недоступен" BOTHUB-IN "$BR_A" 0.0.0.0/0 dxt "$CA" "$GW_A" "$HOST_PORT"
if [ -n "$HOST_IP" ]; then
    refuse_with_drop "хост по своему адресу ($HOST_IP:$HOST_PORT) недоступен" BOTHUB-IN "$BR_A" 0.0.0.0/0 dxt "$CA" "$HOST_IP" "$HOST_PORT"
fi

docker exec -d --user 1000:1000 "$CB" timeout 120 python3 -m http.server 18080 --bind 0.0.0.0 >/dev/null 2>&1
sleep 1
check "контрольная проверка: слушатель бота B отвечает у B" dx "$CB" 'curl -sS -m 3 -o /dev/null http://127.0.0.1:18080/'
# Счётчик DROP здесь не показателен: Docker 28+ сам режет прямой доступ к адресам чужих bridge-сетей (таблица raw)
# раньше, чем пакет дойдёт до BOTHUB-ISO. Контроль: слушатель бота B отвечает у B (проверка выше).
refuse "соседняя сеть: бот B ($IP_B:18080) недоступен из A" dxt "$CA" "$IP_B" 18080
refuse_with_drop "метаданные 169.254.169.254 недоступны" BOTHUB-ISO "$BR_A" 169.254.0.0/16 dx "$CA" 'curl -sS -m 4 -o /dev/null http://169.254.169.254/'
refuse_with_drop "RFC1918 10.255.255.1 недоступен" BOTHUB-ISO "$BR_A" 10.0.0.0/8 dxt "$CA" 10.255.255.1 80
refuse_with_drop "RFC1918 192.168.255.254 недоступен" BOTHUB-ISO "$BR_A" 192.168.0.0/16 dxt "$CA" 192.168.255.254 80
refuse_with_drop "CGNAT/tailnet 100.64.0.1 недоступен" BOTHUB-ISO "$BR_A" 100.64.0.0/10 dxt "$CA" 100.64.0.1 80

# ------------------------------------------------------------------ чужие данные
section "Бот A не видит данные бота B"
docker exec --user 1000:1000 "$CB" bash -c 'echo B-secret > /home/bot/secret-b'
docker exec -d --user 1000:1000 "$CB" sleep 4242
sleep 1
check "контроль: бот B видит свой файл" dx "$CB" '[ -f /home/bot/secret-b ]'
check "контроль: бот B видит свой процесс sleep 4242" dx "$CB" 'for f in /proc/[0-9]*/cmdline; do tr "\0" " " < "$f" 2>/dev/null; echo; done | grep -q "[s]leep 4242"'
refuse "файл бота B не виден в A" dx "$CA" '[ -e /home/bot/secret-b ]'
eq "поиск secret-b по файловой системе A пуст" "$(dx "$CA" 'find / -xdev -name secret-b 2>/dev/null | wc -l')" "0"
refuse "том бота B не примонтирован в A" dx "$CA" "grep -q 'bot-$BOT_B-home' /proc/self/mountinfo"
refuse "процессы бота B не видны в A" dx "$CA" 'for f in /proc/[0-9]*/cmdline; do tr "\0" " " < "$f" 2>/dev/null; echo; done | grep -q "[s]leep 4242"'

out=$(lc POST "/v1/logins/$OWNER_B")
eq "login-контейнер пользователя B создан (201)" "$(code_of "$out")" "201"
docker exec --user 1000:1000 "login-$OWNER_B" bash -c 'echo login-B > /home/bot/login-marker-b'
check "контроль: бот B видит свой том логинов" dx "$CB" '[ -f /home/bot/.auth/login-marker-b ]'
refuse "чужой том логинов не виден боту A" dx "$CA" '[ -e /home/bot/.auth/login-marker-b ]'
refuse "свой том логинов в A только для чтения" dx "$CA" 'touch /home/bot/.auth/isochk'
refuse "бот A не может записать чужой логин" dx "$CA" 'echo overwritten > /home/bot/.auth/login-marker-b'
eq "логин пользователя B не изменён" "$(docker exec --user 1000:1000 "login-$OWNER_B" cat /home/bot/login-marker-b)" "login-B"

# ------------------------------------------------------------------ экран
section "Экран: unix-сокет uid 1001, CDP на loopback"
VNC_SOCKET=/home/browser/.vnc/rfb.sock
out=$(lc POST "/v1/bots/$BOT_A/browser")
eq "запуск браузера через лаунчер (200)" "$(code_of "$out")" "200"
# dxb определён в разделе «Защита ядра» ниже; здесь нужен раньше.
dxb() { docker exec --user 1001:1001 -e HOME=/home/browser "$1" bash -c "$2"; }
vnc_ready() {
    local _
    for _ in $(seq 1 40); do
        if dxb "$1" "[ -S $VNC_SOCKET ]"; then return 0; fi
        sleep 0.5
    done
    return 1
}
if vnc_ready "$CA" && wait_port "$CA" 9222; then
    listeners=$(dx "$CA" 'python3 - <<"PY"
for path in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        rows = open(path).read().splitlines()[1:]
    except OSError:
        continue
    for row in rows:
        f = row.split()
        if f[3] == "0A":
            addr, port = f[1].split(":")
            print(path.rsplit("/", 1)[1], addr, int(port, 16))
PY')
    # /proc/net/tcp хранит IPv4 адрес в little-endian: весь 127.0.0.0/8 начинается с 7F в конце.
    bad_lines=$(printf '%s\n' "$listeners" | non_loopback_listeners)
    if [ -z "$bad_lines" ]; then ok "все слушающие порты бота на loopback"; else bad "порты вне loopback: $bad_lines"; fi
    if printf '%s\n' "$listeners" | grep -q ' 5900$'; then bad "x11vnc слушает TCP 5900 (экран должен быть только unix-сокетом)"; else ok "TCP 5900 не слушается"; fi
    if printf '%s\n' "$listeners" | grep -q ' 9222$'; then ok "CDP слушает 9222"; else bad "CDP не слушает 9222"; fi
    if printf '%s\n' "$listeners" | grep -q ' 6080$'; then bad "websockify слушает 6080"; else ok "websockify не запущен"; fi
    # Сокет и его каталог: только uid 1001. Код бота (uid 1000) не может ни найти сокет, ни подключиться.
    eq "сокет экрана: владелец browser, режим 600" "$(dxb "$CA" "stat -c '%U %a' $VNC_SOCKET")" "browser 600"
    eq "каталог сокета экрана: владелец browser, режим 700" "$(dxb "$CA" "stat -c '%U %a' ${VNC_SOCKET%/*}")" "browser 700"
    refuse "бот (uid 1000) не видит каталог сокета экрана" dx "$CA" "ls ${VNC_SOCKET%/*}"
    refuse "бот (uid 1000) не может подключиться к сокету экрана" dx "$CA" \
        "python3 -c 'import socket; s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(\"$VNC_SOCKET\")'"
    refuse "бот (uid 1000) не подключается к экрану через socat" dx "$CA" "timeout 3 socat - UNIX-CONNECT:$VNC_SOCKET </dev/null"
    refuse "бот (uid 1000) не достаёт x11vnc по TCP 5900" dxt "$CA" 127.0.0.1 5900
    # Контроль: сам сокет работает, x11vnc отвечает версией RFB.
    eq "контроль: uid 1001 получает приветствие RFB через сокет" \
        "$(dxb "$CA" "sleep 2 | timeout 8 socat - UNIX-CONNECT:$VNC_SOCKET | head -c 4" 2>/dev/null)" "RFB "
    # openbox: конфиг образа (root, read-only), без Execute и меню.
    eq "openbox запущен с /etc/bothub/openbox-rc.xml" \
        "$(dxb "$CA" 'for f in /proc/[0-9]*/cmdline; do tr "\0" " " < "$f" 2>/dev/null; echo; done | grep -c "[o]penbox --config-file /etc/bothub/openbox-rc.xml"')" "1"
    eq "openbox-rc.xml принадлежит root, режим 644" "$(dx "$CA" 'stat -c "%U:%G %a" /etc/bothub/openbox-rc.xml')" "root:root 644"
    refuse "бот не может изменить openbox-rc.xml" dx "$CA" 'echo x >> /etc/bothub/openbox-rc.xml'
    refuse "uid 1001 не может изменить openbox-rc.xml" dxb "$CA" 'echo x >> /etc/bothub/openbox-rc.xml'
    check "в openbox-rc.xml нет Execute и keybind" dx "$CA" '! grep -Eq "name=\"Execute\"|<keybind" /etc/bothub/openbox-rc.xml'
    # Политики Chromium: root, read-only; расширения, native messaging и загрузки закрыты.
    eq "политики Chromium принадлежат root, режим 644" \
        "$(dx "$CA" 'stat -c "%U:%G %a" /etc/chromium/policies/managed/bothub.json')" "root:root 644"
    refuse "uid 1001 не может изменить политики Chromium" dxb "$CA" 'echo x >> /etc/chromium/policies/managed/bothub.json'
    for key in ExtensionInstallBlocklist NativeMessagingBlocklist DownloadRestrictions DefaultDownloadDirectory \
        SearchSuggestEnabled DefaultSearchProviderEnabled MetricsReportingEnabled SyncDisabled BrowserSignin URLBlocklist \
        NewTabPageLocation HomepageLocation HomepageIsNewTabPage RestoreOnStartup; do
        check "политика Chromium $key задана" dx "$CA" "grep -q '\"$key\"' /etc/chromium/policies/managed/bothub.json"
    done
    # Страница новой вкладки, домашняя и запуск ведут на about:blank: chrome://* закрыт URLBlocklist, иначе свежий бот
    # стартовал со страницей ошибки, и первый navigate любой процедуры падал с page_ambiguous.
    for pair in NewTabPageLocation:about:blank HomepageLocation:about:blank; do
        check "политика Chromium ${pair%%:*} равна about:blank" dx "$CA" \
            "grep -Eq '\"${pair%%:*}\": *\"about:blank\"' /etc/chromium/policies/managed/bothub.json"
    done
    check "политика Chromium RestoreOnStartup равна 5 (страница новой вкладки, сессия не восстанавливается)" dx "$CA" \
        "grep -Eq '\"RestoreOnStartup\": *5' /etc/chromium/policies/managed/bothub.json"
    check "политика Chromium HomepageIsNewTabPage равна false" dx "$CA" \
        "grep -Eq '\"HomepageIsNewTabPage\": *false' /etc/chromium/policies/managed/bothub.json"
    # Свежий бот без предподготовки (до любого шага процедуры): вкладка about:blank, navigate читает и действует.
    fresh_browser_navigation_checks
    # Каталог загрузок на noexec-tmpfs, доступен только uid 1001.
    case "$(dxb "$CA" 'awk "\$2 == \"/home/browser/Downloads\" {print \$3, \$4}" /proc/mounts')" in
        tmpfs*noexec*) ok "каталог загрузок Chromium: tmpfs с noexec" ;;
        *) bad "каталог загрузок Chromium не tmpfs с noexec" ;;
    esac
    refuse "бот (uid 1000) не видит каталог загрузок браузера" dx "$CA" 'ls /home/browser/Downloads'
else
    bad "сокет экрана или CDP не поднялись за 20 секунд"
fi

# ------------------------------------------------------------------ режим human
section "Режим human: чистый Chromium без CDP, отдельный профиль"
HUMAN_PROFILE=/home/browser/.config/botstead-browser-human
BOT_PROFILE=/home/browser/.config/botstead-browser
HUMAN_MARKER="$HUMAN_PROFILE/isochk-human-marker"
# port_listening контейнер порт : TCP-порт в состоянии LISTEN (по /proc/net/tcp и tcp6), любой адрес
port_listening() {
    dx "$1" "awk -v want=$(printf '%04X' "$2") 'FNR > 1 && \$4 == \"0A\" { n = split(\$2, a, \":\"); if (toupper(a[n]) == want) found = 1 } END { exit !found }' /proc/net/tcp /proc/net/tcp6 2>/dev/null"
}
# human_ready контейнер : файл активного режима говорит human и процесс прожил >= 2 секунд (окно есть)
human_ready() {
    local _
    for _ in $(seq 1 60); do
        if dxb "$1" 'read -r am cp ts < "$HOME/.browser-active" 2>/dev/null && [ "$am" = human ] && kill -0 "$cp" 2>/dev/null && [ $(( $(date +%s) - ts )) -ge 2 ]'; then return 0; fi
        sleep 0.5
    done
    return 1
}
# sel_text контейнер primary|clipboard : текст выделения X (пусто, если выделения нет)
sel_text() {
    dxb "$1" 'export DISPLAY=:99 XAUTHORITY="$HOME/.Xauthority"; timeout 5 xsel --output --'"$2" 2>/dev/null
}
# sel_set контейнер primary|clipboard текст : занять выделение X от uid 1001 (xsel остаётся владельцем в фоне; его
# потоки уходят в /dev/null, иначе docker exec не вернётся)
sel_set() {
    dxb "$1" "export DISPLAY=:99 XAUTHORITY=\"\$HOME/.Xauthority\"; printf %s '$3' | xsel --input --$2 >/dev/null 2>&1"
}
# cut_text контейнер : содержимое CUT_BUFFER0 корневого окна X (xprop; x11vnc кладёт туда текст из буфера клиента)
cut_text() {
    dxb "$1" 'export DISPLAY=:99 XAUTHORITY="$HOME/.Xauthority"; timeout 5 xprop -root CUT_BUFFER0 2>/dev/null'
}
# cut_set контейнер текст : записать CUT_BUFFER0 от uid 1001
cut_set() {
    dxb "$1" "export DISPLAY=:99 XAUTHORITY=\"\$HOME/.Xauthority\"; timeout 5 xprop -root -f CUT_BUFFER0 8s -set CUT_BUFFER0 '$2'"
}
HUMAN_REF=/tmp/isochk-human-ref
# Бот копировал в буфер до перехвата: человек этого получить не должен.
check "контроль: uid 1001 занимает PRIMARY и CLIPBOARD до перехвата" dxb "$CA" \
    'export DISPLAY=:99 XAUTHORITY="$HOME/.Xauthority"; printf isochk-clip-bot | xsel --clipboard --input >/dev/null 2>&1 && printf isochk-prim-bot | xsel --primary --input >/dev/null 2>&1'
eq "контроль: CLIPBOARD держит текст бота" "$(sel_text "$CA" clipboard)" "isochk-clip-bot"
check "контроль: uid 1001 записывает CUT_BUFFER0 до перехвата" cut_set "$CA" isochk-cut-bot
eq "контроль: CUT_BUFFER0 держит текст бота" "$(cut_text "$CA" | grep -c isochk-cut-bot)" "1"
out=$(lc POST "/v1/bots/$BOT_A/browser-mode" '{"mode":"human","url":"about:blank"}')
eq "browser-mode human вернул 200" "$(code_of "$out")" "200"
case "$(body_of "$out")" in *'"mode":"human"'*) ok "ответ лаунчера: mode human" ;; *) bad "ответ лаунчера без mode human: $(body_of "$out")" ;; esac
if human_ready "$CA"; then
    # Отдельный процесс Chromium: прежний процесс с CDP остановлен, живёт один главный процесс и он в профиле человека.
    main_cmd='read -r am cp ts < "$HOME/.browser-active"; tr "\0" "\n" < "/proc/$cp/cmdline"'
    refuse "порт 9222 не слушается (/proc/net/tcp)" port_listening "$CA" 9222
    refuse "бот (uid 1000) не подключается к 127.0.0.1:9222" dxt "$CA" 127.0.0.1 9222
    refuse "CDP не отвечает версией браузера" dxb "$CA" 'curl -fsS -m 3 --noproxy "*" http://127.0.0.1:9222/json/version'
    eq "в командных строках процессов нет remote-debugging" \
        "$(dxb "$CA" 'for f in /proc/[0-9]*/cmdline; do tr "\0" " " < "$f" 2>/dev/null; echo; done | grep -c "[r]emote-debugging"')" "0"
    eq "главный процесс Chromium: --user-data-dir указывает на профиль человека" \
        "$(dxb "$CA" "$main_cmd" | grep -Fx -- "--user-data-dir=$HUMAN_PROFILE")" "--user-data-dir=$HUMAN_PROFILE"
    # [-] в шаблоне: иначе grep находит в /proc собственную командную строку.
    eq "ни один процесс не открывает профиль бота" \
        "$(dxb "$CA" 'for f in /proc/[0-9]*/cmdline; do tr "\0" "\n" < "$f" 2>/dev/null; done | grep -xc -- "[-]-user-data-dir='"$BOT_PROFILE"'"')" "0"
    eq "каталог профиля человека: владелец browser, режим 700" "$(dxb "$CA" "stat -c '%U %a' $HUMAN_PROFILE")" "browser 700"
    # Единый каталог: все пути экземпляра человека (флаги и окружение главного процесса) внутри него.
    eq "--disk-cache-dir внутри каталога человека" \
        "$(dxb "$CA" "$main_cmd" | grep -Fx -- "--disk-cache-dir=$HUMAN_PROFILE/cache")" "--disk-cache-dir=$HUMAN_PROFILE/cache"
    eq "--crash-dumps-dir внутри каталога человека" \
        "$(dxb "$CA" "$main_cmd" | grep -Fx -- "--crash-dumps-dir=$HUMAN_PROFILE/crash")" "--crash-dumps-dir=$HUMAN_PROFILE/crash"
    for var in HOME XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME XDG_STATE_HOME TMPDIR; do
        eq "окружение Chromium человека: $var внутри каталога человека" \
            "$(dxb "$CA" 'read -r am cp ts < "$HOME/.browser-active"; tr "\0" "\n" < "/proc/$cp/environ" | sed -n "s/^'"$var"'=//p"' | grep -c "^$HUMAN_PROFILE/")" "1"
    done
    eq "маркер сессии человека есть (обычный файл, 600)" "$(dxb "$CA" 'stat -c "%F %a" "$HOME/.browser-human-session"')" "regular file 600"
    # Токен сессии: лаунчер пишет его в файл режима, супервизор в маркер; по нему каталог человека возобновляется.
    eq "файл режима: human с токеном сессии (32 hex)" "$(dxb "$CA" 'grep -Ec "^human [0-9a-f]{32}$" "$HOME/.browser-mode"')" "1"
    eq "маркер сессии несёт тот же токен" \
        "$(dxb "$CA" 'read -r _ t < "$HOME/.browser-mode"; grep -Fxc "session $t" "$HOME/.browser-human-session"')" "1"
    # Буфер обмена очищен при входе.
    eq "CLIPBOARD очищен при входе в human" "$(sel_text "$CA" clipboard)" ""
    eq "PRIMARY очищен при входе в human" "$(sel_text "$CA" primary)" ""
    eq "CUT_BUFFER0 очищен при входе в human (xprop -root под uid 1001 без текста бота)" "$(cut_text "$CA" | grep -c isochk-cut-bot)" "0"
    # Всё, что экземпляр человека пишет, за несколько секунд должно оказаться в его каталоге: вне его, вне профиля бота и
    # вне файлов стека (X, openbox, x11vnc, супервизор) новых файлов нет. Канарейки кладём в каталоги экземпляра.
    check "контроль: точка отсчёта и канарейки в каталогах экземпляра человека" dxb "$CA" \
        "touch $HUMAN_REF && for d in cache crash tmp home xdg-cache; do echo x > $HUMAN_PROFILE/\$d/isochk-canary; done"
    sleep 3
    stray=$(dxb "$CA" "find /home/browser /tmp /dev/shm \\( -path $HUMAN_PROFILE -o -path $BOT_PROFILE -o -path /home/browser/.vnc -o -path /tmp/.X11-unix -o -path '/home/browser/.browser-*' -o -path /home/browser/.Xauthority -o -path /tmp/.X99-lock -o -path $HUMAN_REF \\) -prune -o -newer $HUMAN_REF ! -type d -print 2>/dev/null")
    if [ -z "$stray" ]; then ok "вне каталога человека и профиля бота новых файлов нет (/home/browser, /tmp, /dev/shm)"; else bad "экземпляр человека пишет вне своего каталога: $(printf '%s' "$stray" | head -5 | tr '\n' ' ')"; fi
    sel_set "$CA" clipboard isochk-clip-human
    sel_set "$CA" primary isochk-prim-human
    check "контроль: человек (x11vnc) оставляет текст в CUT_BUFFER0" cut_set "$CA" isochk-cut-human
    eq "контроль: CUT_BUFFER0 держит текст человека" "$(cut_text "$CA" | grep -c isochk-cut-human)" "1"
    # Маркер лежит в профиле человека: uid 1001 его видит, код бота (uid 1000) нет.
    check "контроль: uid 1001 пишет и читает маркер в профиле человека" dxb "$CA" "echo isochk > $HUMAN_MARKER && [ \"\$(cat $HUMAN_MARKER)\" = isochk ]"
    refuse "маркер недоступен для uid 1000 (test -e)" dx "$CA" "[ -e $HUMAN_MARKER ]"
    refuse "маркер недоступен для uid 1000 (cat)" dx "$CA" "cat $HUMAN_MARKER"
    refuse "каталог профиля человека недоступен для uid 1000 (ls)" dx "$CA" "ls $HUMAN_PROFILE"
    # Экран в human тот же: unix-сокет uid 1001, TCP-порта нет.
    refuse "бот (uid 1000) не подключается к сокету экрана в human" dx "$CA" \
        "python3 -c 'import socket; s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(\"$VNC_SOCKET\")'"
    if port_listening "$CA" 5900; then bad "в human слушается TCP 5900"; else ok "в human TCP 5900 не слушается"; fi
else
    bad "Chromium в режиме human не поднялся за 30 секунд (файл ~/.browser-active)"
fi
# Возврат в bot: cookies переносятся, всё остальное в профиле человека исчезает вместе с ним.
out=$(lc POST "/v1/bots/$BOT_A/browser-mode" '{"mode":"bot"}')
eq "browser-mode bot вернул 200" "$(code_of "$out")" "200"
if wait_port "$CA" 9222; then
    ok "после возврата CDP бота снова слушает 9222"
    case "$(dxb "$CA" 'cat "$HOME/.browser-merge-status" 2>/dev/null')" in
        'merged '[0-9]*) ok "cookies профиля человека перенесены (merged N)" ;;
        *) bad "перенос cookies не отчитался: $(dxb "$CA" 'cat "$HOME/.browser-merge-status" 2>&1')" ;;
    esac
    check "каталог профиля человека удалён" dxb "$CA" "[ ! -e $HUMAN_PROFILE ]"
    check "маркер сессии человека удалён" dxb "$CA" '[ ! -e "$HOME/.browser-human-session" ]'
    eq "канарейки экземпляра человека не пережили возврат" \
        "$(dxb "$CA" "find /home/browser /tmp /dev/shm -name 'isochk-canary*' 2>/dev/null | wc -l")" "0"
    eq "после возврата нет файлов экземпляра человека (ни по пути каталога, ни по имени)" \
        "$(dxb "$CA" "find /home/browser /tmp /dev/shm -newer $HUMAN_REF \\( -path '*botstead-browser-human*' -o -name 'human-*' \\) 2>/dev/null | wc -l")" "0"
    eq "CLIPBOARD очищен после возврата" "$(sel_text "$CA" clipboard)" ""
    eq "PRIMARY очищен после возврата" "$(sel_text "$CA" primary)" ""
    eq "CUT_BUFFER0 очищен после возврата (xprop -root под uid 1001 без текста человека)" "$(cut_text "$CA" | grep -c isochk-cut-human)" "0"
    dxb "$CA" "rm -f $HUMAN_REF" || true
    eq "маркер из профиля человека не попал в профиль бота" \
        "$(dxb "$CA" "find $BOT_PROFILE -name isochk-human-marker 2>/dev/null | wc -l")" "0"
    eq "Chromium бота снова в профиле бота с CDP" \
        "$(dxb "$CA" 'read -r am cp ts < "$HOME/.browser-active"; tr "\0" "\n" < "/proc/$cp/cmdline" | grep -Fxc -- "--user-data-dir='"$BOT_PROFILE"'"')" "1"
else
    bad "CDP бота не вернулся после browser-mode bot"
fi

# ------------------------------------------------------------------ защита ядра
section "Защита ядра: seccomp-профиль лаунчера и bot-guard"
GUARD=/usr/local/libexec/bot-guard
RUNTIME=$(docker inspect -f '{{.HostConfig.Runtime}}' "$CA")
# dxg контейнер 'команда bash' : как dx, но через bot-guard (так лаунчер запускает все команды бота)
dxg() { docker exec --user 1000:1000 "$1" "$GUARD" /usr/bin/bash -c "$2"; }
# dxb контейнер 'команда bash' : от пользователя браузера (uid 1001), без bot-guard
dxb() { docker exec --user 1001:1001 -e HOME=/home/browser "$1" bash -c "$2"; }
# sock_probe контейнер семейство тип протокол : socket() из python кода бота, печатает ok или err:<errno>
sock_probe() {
    dxg "$1" "python3 -c 'import socket
try:
    socket.socket($2, $3, $4)
    print(\"ok\")
except OSError as e:
    print(\"err:%d\" % e.errno)'" 2>&1 | tail -n1
}
# status_field контейнер поле pid : значение поля /proc/<pid>/status для процесса бота (self: сам процесс)
status_field() { dxg "$1" "awk '/^$2:/ {print \$2}' /proc/$3/status"; }
# filters_at_least_2 описание число : Seccomp_filters >= 2 (профиль Docker и bot-guard)
filters_at_least_2() {
    if [ -z "$2" ]; then
        warn "$1: ядро хоста не показывает Seccomp_filters (нужно Linux 5.9+)"
    elif [ "$RUNTIME" = runsc ]; then
        warn "$1: runtime runsc, фильтры считает gVisor ($2); профиль Docker под runsc не применяется"
    elif [ "$2" -ge 2 ] 2>/dev/null; then
        ok "$1 ($2)"
    else
        bad "$1 (получено '$2', ожидалось не меньше 2)"
    fi
}
# lx бот json-argv exec_id : exec через лаунчер, печатает "stdout код" (переводы строк заменены на |)
lx() {
    local out
    out=$(lc POST "/v1/bots/$1/exec" "{\"argv\":$2,\"exec_id\":\"$3\",\"timeout\":30}")
    body_of "$out" | python3 -c '
import base64, json, sys
text, code = b"", None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    frame = json.loads(line)
    if frame["t"] == "out" and frame["s"] == "stdout":
        text += base64.b64decode(frame["d"])
    if frame["t"] == "exit":
        code = frame["code"]
print(text.decode().replace("\n", "|"), code)
'
}

case "$(docker inspect -f '{{json .HostConfig.SecurityOpt}}' "$CA")" in
    *seccomp=*) ok "контейнер бота создан с профилем seccomp лаунчера (SecurityOpt: seccomp=...)" ;;
    *) bad "у контейнера бота нет профиля seccomp лаунчера: Chromium не стартует, user namespaces не открыты" ;;
esac
case "$(docker inspect -f '{{json .HostConfig.SecurityOpt}}' "login-$OWNER_B")" in
    *seccomp=*) bad "login-контейнер получил профиль бота (должен идти на стандартном профиле Docker)" ;;
    *) ok "login-контейнер на стандартном профиле Docker" ;;
esac
eq "bot-guard принадлежит root:root, режим 755" "$(dx "$CA" "stat -c '%U:%G %a' $GUARD")" "root:root 755"
refuse "бот не может изменить bot-guard" dx "$CA" "echo x >> $GUARD"

# Процесс бота и PID 1 несут два фильтра: профиль Docker и bot-guard.
eq "процесс бота: Seccomp = 2" "$(status_field "$CA" Seccomp self)" "2"
eq "процесс бота: NoNewPrivs = 1" "$(status_field "$CA" NoNewPrivs self)" "1"
filters_at_least_2 "процесс бота: Seccomp_filters" "$(status_field "$CA" Seccomp_filters self)"
eq "PID 1: Seccomp = 2" "$(status_field "$CA" Seccomp 1)" "2"
eq "PID 1: NoNewPrivs = 1" "$(status_field "$CA" NoNewPrivs 1)" "1"
filters_at_least_2 "PID 1: Seccomp_filters" "$(status_field "$CA" Seccomp_filters 1)"
# Весь entrypoint запускается под bot-guard (ENTRYPOINT образа), а затем интерпретатор PID 1 идёт через него ещё раз:
# профиль Docker, bot-guard ENTRYPOINT и bot-guard интерпретатора, то есть три фильтра.
pid1_filters=$(status_field "$CA" Seccomp_filters 1)
if [ "$RUNTIME" = runsc ]; then
    warn "PID 1: runtime runsc, число фильтров ($pid1_filters) не сравнивается с 3"
elif [ "$pid1_filters" -ge 3 ] 2>/dev/null; then
    ok "PID 1: Seccomp_filters = $pid1_filters (entrypoint стартовал под bot-guard)"
else
    bad "PID 1: Seccomp_filters '$pid1_filters', ожидалось не меньше 3 (ENTRYPOINT образа не через bot-guard?)"
fi
eq "ENTRYPOINT образа: bot-guard, затем entrypoint.sh" \
    "$(docker inspect -f '{{json .Config.Entrypoint}}' "$CA")" '["/usr/local/libexec/bot-guard","/usr/local/bin/entrypoint.sh"]'

# Команда через лаунчер (а не ручной docker exec) тоже идёт под bot-guard.
res=$(lx "$BOT_A" '["awk","/^Seccomp_filters:/ {print $2}","/proc/self/status"]' isochk-filters)
if [ "$RUNTIME" = runsc ]; then
    warn "exec через лаунчер: runtime runsc, число фильтров не сравнивается ($res)"
elif [ "${res%%|*}" -ge 2 ] 2>/dev/null; then
    ok "exec через лаунчер: Seccomp_filters = ${res%%|*} (bot-guard стоит перед командой)"
else
    bad "exec через лаунчер: Seccomp_filters '${res}', ожидалось не меньше 2"
fi
res=$(lx "$BOT_A" '["unshare","-U","true"]' isochk-unshare)
case "${res##* }" in 0|""|None) bad "exec через лаунчер: unshare -U true прошёл ($res)" ;; *) ok "exec через лаунчер: unshare -U true отклонён (код ${res##* })" ;; esac

# Код бота: user namespaces недоступны, опасные семейства сокетов закрыты, обычные работают.
refuse "бот: unshare -U true отклонён" dxg "$CA" 'unshare -U true'
refuse "бот: unshare -r true отклонён" dxg "$CA" 'unshare -r true'
refuse "бот: unshare -Urnp true отклонён" dxg "$CA" 'unshare -Urnp true'
eq "бот: unshare(CLONE_NEWUSER) из libc даёт -1 и EPERM" \
    "$(dxg "$CA" 'python3 -c "import ctypes; l = ctypes.CDLL(None, use_errno=True); r = l.unshare(0x10000000); print(r, ctypes.get_errno())"')" "-1 1"
# clone(CLONE_NEWNET) без CLONE_NEWUSER фильтр пропускает, отказывает ядро: у бота нет CAP_SYS_ADMIN.
eq "бот: сырой clone(CLONE_NEWNET) без CLONE_NEWUSER даёт -1 и EPERM от ядра" \
    "$(dxg "$CA" 'python3 -c "
import ctypes, os, platform
libc = ctypes.CDLL(None, use_errno=True)
nr = {\"x86_64\": 56, \"aarch64\": 220}[platform.machine()]
r = libc.syscall(nr, 0x40000000 | 17, 0, 0, 0, 0)
if r == 0:
    os._exit(0)
print(r, ctypes.get_errno())
"')" "-1 1"
check "бот: потоки и fork работают под bot-guard" dxg "$CA" \
    'python3 -c "import os, threading; t = threading.Thread(target=int); t.start(); t.join(); pid = os.fork(); os._exit(0) if pid == 0 else os.waitpid(pid, 0)"'
eq "бот: socket(AF_UNIX) работает" "$(sock_probe "$CA" 1 1 0)" "ok"
eq "бот: socket(AF_INET) работает" "$(sock_probe "$CA" 2 1 0)" "ok"
eq "бот: socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE) работает" "$(sock_probe "$CA" 16 3 0)" "ok"
case "$(sock_probe "$CA" 17 3 0)" in err:*) ok "бот: socket(AF_PACKET) закрыт" ;; *) bad "бот: socket(AF_PACKET) открыт" ;; esac
eq "бот: socket(AF_ALG) закрыт профилем (EPERM)" "$(sock_probe "$CA" 38 5 0)" "err:1"
eq "бот: socket(AF_NETLINK, SOCK_RAW, 12) закрыт профилем (EPERM)" "$(sock_probe "$CA" 16 3 12)" "err:1"

# Браузер: Chromium без --no-sandbox, песочница на user namespaces.
out=$(lc POST "/v1/bots/$BOT_A/browser")
eq "ensure_browser вернул 200" "$(code_of "$out")" "200"
if wait_port "$CA" 9222; then
    case "$(dxb "$CA" 'curl -fsS -m 5 http://127.0.0.1:9222/json/version' 2>/dev/null)" in
        *'"Browser"'*) ok "CDP отвечает на 127.0.0.1:9222" ;;
        *) bad "CDP на 127.0.0.1:9222 не ответил версией браузера" ;;
    esac
else
    bad "CDP не поднялся (Chromium не стартовал: проверьте профиль seccomp, sysctl user.max_user_namespaces, AppArmor)"
fi
cmdlines=$(dxb "$CA" 'for f in /proc/[0-9]*/cmdline; do tr "\0" " " < "$f" 2>/dev/null; echo; done')
if printf '%s\n' "$cmdlines" | grep -q '[c]hromium'; then ok "процессы Chromium запущены"; else bad "процессов Chromium нет"; fi
if printf '%s\n' "$cmdlines" | grep -q -e '--no-sandbox' -e '--disable-setuid-sandbox'; then
    bad "Chromium запущен с --no-sandbox или --disable-setuid-sandbox"
else
    ok "Chromium запущен без --no-sandbox"
fi
# Читать /proc/<pid>/ns/user чужого недампабельного процесса можно не всегда, поэтому запасной признак: uid_map
# не равен "0 0 4294967295" (так выглядит init user namespace). Свой namespace процесса проверки совпадает с
# namespace PID 1: оба живут в user namespace контейнера.
ns_report=$(dxb "$CA" 'python3 - <<"PY"
import os

own = os.readlink("/proc/self/ns/user")
INIT_MAP = "0 0 4294967295"
for pid in sorted(int(p) for p in os.listdir("/proc") if p.isdigit()):
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        continue
    if ("--type=zygote" not in cmd and "--type=renderer" not in cmd) or "--no-zygote-sandbox" in cmd:
        continue
    kind = "renderer" if "--type=renderer" in cmd else "zygote"
    try:
        ns = os.readlink(f"/proc/{pid}/ns/user")
    except OSError:
        ns = None
    try:
        umap = " ".join(open(f"/proc/{pid}/uid_map").read().split())
    except OSError:
        umap = None
    if ns is not None:
        verdict = "same-ns" if ns == own else "other-ns"
    elif umap is not None:
        verdict = "same-ns" if umap == INIT_MAP else "other-ns"
    else:
        verdict = "unknown"
    print(pid, kind, verdict)
PY')
if printf '%s\n' "$ns_report" | grep -q ' same-ns$'; then
    bad "рендерер или zygote Chromium в user namespace контейнера (песочница не включена): $(printf '%s' "$ns_report" | tr '\n' ';')"
elif printf '%s\n' "$ns_report" | grep -q ' other-ns$'; then
    ok "zygote/рендерер Chromium в своём user namespace, отличном от PID 1"
else
    bad "zygote или рендерер Chromium не найдены или их namespace не читается ($ns_report)"
fi
# Контроль: профиль открывает user namespaces браузерному uid, но не даёт ничего сверх них.
check "контроль: браузерный uid 1001: unshare -U true работает" dxb "$CA" 'unshare -U true'
check "контроль: браузерный uid 1001: unshare -Urnp true работает" dxb "$CA" 'unshare -Urnp true'
refuse "mount закрыт профилем и внутри user namespace браузера" dxb "$CA" 'unshare -Urm mount -t tmpfs none /mnt'
refuse "AF_PACKET закрыт профилем и внутри user namespace браузера" dxb "$CA" \
    'unshare -Urn python3 -c "import socket; socket.socket(17, 3)"'

# Опции netfilter из user namespace (CVE-2021-22555): внутри unshare -Urn у процесса есть CAP_NET_ADMIN над своим
# сетевым namespace, и без правил профиля ядро приняло бы setsockopt(SOL_IP, IPT_SO_SET_REPLACE). EPERM здесь
# может прийти только от профиля: ядро при CAP_NET_ADMIN отвечает другой ошибкой (EINVAL, ENOENT, ENOPROTOOPT).
sockopt_report=$(docker exec -i --user 1001:1001 -e HOME=/home/browser "$CA" unshare -Urn python3 - 2>&1 <<'PY'
import ctypes
import platform
import socket

SOL_IP, SOL_IPV6 = 0, 41


def attempt(fn):
    try:
        fn()
        return "ok"
    except OSError as e:
        return "err:%d" % e.errno


s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
buf = b"\0" * 64
print("reuseaddr", attempt(lambda: s4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)))
print("so_timestamping_new", attempt(lambda: s4.setsockopt(socket.SOL_SOCKET, 65, 0)))
print("ip_tos", attempt(lambda: s4.setsockopt(SOL_IP, 1, 16)))
print("ipv6_tclass_get", attempt(lambda: s6.getsockopt(SOL_IPV6, 67, 4)))
print("ipt_set_replace", attempt(lambda: s4.setsockopt(SOL_IP, 64, buf)))
print("ipt_add_counters", attempt(lambda: s4.setsockopt(SOL_IP, 65, buf)))
print("arpt_set_replace", attempt(lambda: s4.setsockopt(SOL_IP, 96, buf)))
print("ebt_set_entries", attempt(lambda: s4.setsockopt(SOL_IP, 128, buf)))
print("ip6t_set_replace", attempt(lambda: s6.setsockopt(SOL_IPV6, 64, buf)))
print("ipt_get_info", attempt(lambda: s4.getsockopt(SOL_IP, 64, 64)))
print("ipt_get_revision", attempt(lambda: s4.getsockopt(SOL_IP, 66, 64)))
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
nr = {"x86_64": 54, "aarch64": 208}[platform.machine()]
cbuf = ctypes.create_string_buffer(64)
# Ядро берёт level и optname как int: старшие биты регистра не должны обходить фильтр.
r = libc.syscall(ctypes.c_long(nr), ctypes.c_long(s4.fileno()), ctypes.c_ulong(1 << 32), ctypes.c_ulong(64),
                 cbuf, ctypes.c_ulong(64))
print("wide_level", r, ctypes.get_errno())
r = libc.syscall(ctypes.c_long(nr), ctypes.c_long(s4.fileno()), ctypes.c_ulong(0), ctypes.c_ulong((1 << 32) | 64),
                 cbuf, ctypes.c_ulong(64))
print("wide_optname", r, ctypes.get_errno())
PY
)
sockopt_expect() { eq "uid 1001 в unshare -Urn: $1" "$(printf '%s\n' "$sockopt_report" | awk -v k="$2" '$1 == k {$1 = ""; print substr($0, 2)}')" "$3"; }
sockopt_expect "setsockopt(SOL_SOCKET, SO_REUSEADDR) работает" reuseaddr ok
# SOL_SOCKET с номером 65 (SO_TIMESTAMPING_NEW) профиль не трогает: допустим любой ответ, кроме EPERM профиля.
if [ "$(printf '%s\n' "$sockopt_report" | awk '$1 == "so_timestamping_new" {print $2}')" != "err:1" ]; then
    ok "uid 1001 в unshare -Urn: setsockopt(SOL_SOCKET, 65) профилем не закрыт"
else
    bad "uid 1001 в unshare -Urn: setsockopt(SOL_SOCKET, 65) закрыт профилем (другие уровни не должны страдать)"
fi
sockopt_expect "setsockopt(SOL_IP, IP_TOS) работает" ip_tos ok
sockopt_expect "getsockopt(SOL_IPV6, IPV6_TCLASS) работает" ipv6_tclass_get ok
sockopt_expect "setsockopt(SOL_IP, 64) даёт EPERM" ipt_set_replace err:1
sockopt_expect "setsockopt(SOL_IP, 65) даёт EPERM" ipt_add_counters err:1
sockopt_expect "setsockopt(SOL_IP, 96) даёт EPERM" arpt_set_replace err:1
sockopt_expect "setsockopt(SOL_IP, 128) даёт EPERM" ebt_set_entries err:1
sockopt_expect "setsockopt(SOL_IPV6, 64) даёт EPERM" ip6t_set_replace err:1
sockopt_expect "getsockopt(SOL_IP, 64) даёт EPERM" ipt_get_info err:1
sockopt_expect "getsockopt(SOL_IP, 66) даёт EPERM" ipt_get_revision err:1
sockopt_expect "setsockopt(level = 1<<32) не обходит фильтр" wide_level "-1 1"
sockopt_expect "setsockopt(optname = (1<<32)|64) не обходит фильтр" wide_optname "-1 1"

# ------------------------------------------------------------------ правила хоста
section "Правила iptables"
hx() { docker exec "$LAUNCHER" "$@"; }
eq "DOCKER-USER: наш прыжок первый" "$(hx "$IPT" -S DOCKER-USER | grep '^-A' | head -n1)" "-A DOCKER-USER -j BOTHUB-ISO"
eq "INPUT: наш прыжок первый" "$(hx "$IPT" -S INPUT | grep '^-A' | head -n1)" "-A INPUT -j BOTHUB-IN"
rules_before=$(hx "$IPT" -S BOTHUB-ISO; hx "$IPT" -S BOTHUB-IN)
for cidr in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 127.0.0.0/8; do
    for bridge in "$BR_A" "$BR_B"; do
        if printf '%s\n' "$rules_before" | has_drop_rule "$bridge" "$cidr"; then
            ok "DROP на $cidr для $bridge"
        else
            bad "DROP на $cidr для $bridge отсутствует"
        fi
    done
done
if hx ip6tables -S BOTHUB-ISO 2>/dev/null | grep -q 'fc00::/7'; then ok "IPv6: ULA и link-local закрыты"; else bad "IPv6-правил нет"; fi

# ------------------------------------------------------------------ лаунчер
section "Лаунчер: метка, exec, остановка"
docker run -d --pull never --name "bot-$FOREIGN" --entrypoint sleep "$BOT_IMAGE" 300 >/dev/null 2>&1
for req in "POST /v1/bots/$FOREIGN/recreate" "DELETE /v1/bots/$FOREIGN" "GET /v1/bots/$FOREIGN"; do
    out=$(lc "${req%% *}" "${req#* }")
    eq "контейнер без метки: $req отклонён (403)" "$(code_of "$out")" "403"
done
out=$(lc POST "/v1/bots/$FOREIGN/exec" '{"argv":["id"]}')
eq "контейнер без метки: exec отклонён (403)" "$(code_of "$out")" "403"
check "контейнер без метки остался жить" docker inspect "bot-$FOREIGN"
out=$(lc POST /v1/bots "{\"bot_id\":\"$BOT_A\",\"owner_id\":\"$OWNER_B\"}")
eq "создание бота A под чужим владельцем отклонено (409)" "$(code_of "$out")" "409"
out=$(lc POST /v1/bots '{"bot_id":"x --privileged","owner_id":"o"}')
eq "инъекция в bot_id отклонена (400)" "$(code_of "$out")" "400"
out=$(lc POST /v1/bots "{\"bot_id\":\"isochk-evil\",\"owner_id\":\"$OWNER_A\",\"image\":\"alpine\"}")
eq "лишнее поле image отклонено (400)" "$(code_of "$out")" "400"

out=$(lc POST "/v1/bots/$BOT_A/exec" '{"argv":["bash","-c","echo hello; id -u"],"exec_id":"isochk-exec-1","timeout":30}')
eq "exec через лаунчер вернул 200" "$(code_of "$out")" "200"
decoded=$(body_of "$out" | python3 -c '
import base64, json, sys
text, code = b"", None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    frame = json.loads(line)
    if frame["t"] == "out" and frame["s"] == "stdout":
        text += base64.b64decode(frame["d"])
    if frame["t"] == "exit":
        code = frame["code"]
print(text.decode().replace("\n", "|"), code)
')
eq "stdout и код выхода из стрима" "$decoded" "hello|1000| 0"

(lc POST "/v1/bots/$BOT_A/exec" '{"argv":["sleep","300"],"exec_id":"isochk-sleep","timeout":600}' >/dev/null 2>&1 &)
sleep 3
count_sleep() { dx "$CA" 'for f in /proc/[0-9]*/cmdline; do tr "\0" " " < "$f" 2>/dev/null; echo; done | grep -c "[b]othub-exec-isochk-sleep"'; }
eq "команда запущена внутри бота" "$(count_sleep)" "1"
out=$(lc POST /v1/execs/isochk-sleep/stop)
eq "stop_exec вернул 200" "$(code_of "$out")" "200"
sleep 2
eq "после stop_exec процесса нет" "$(count_sleep)" "0"

out=$(lc POST "/v1/bots/$BOT_A/freeze")
eq "freeze_bot вернул 200" "$(code_of "$out")" "200"
out=$(lc POST "/v1/bots/$BOT_A/exec" '{"argv":["id"],"exec_id":"isochk-frozen","timeout":5}')
eq "exec замороженного бота отклонён (409)" "$(code_of "$out")" "409"
out=$(lc POST "/v1/bots/$BOT_A/unfreeze")
eq "unfreeze_bot вернул 200" "$(code_of "$out")" "200"

# ------------------------------------------------------------------ исполнитель шагов процедур
section "Исполнитель шагов процедур: модуль по абсолютному пути, чистое окружение, остановка (docs/isolation.md)"
PSTEP=/usr/local/libexec/procedure-step.mjs
eq "скрипт исполнителя принадлежит root:root, режим 644" "$(dx "$CA" "stat -c '%U:%G %a' $PSTEP")" "root:root 644"
refuse "бот не может изменить скрипт исполнителя" dx "$CA" "echo x >> $PSTEP"
runtime=$(dx "$CA" 'for p in /usr/lib/node_modules/@playwright/mcp/node_modules/playwright-core /usr/lib/node_modules/playwright-core /usr/local/lib/node_modules/@playwright/mcp/node_modules/playwright-core /usr/local/lib/node_modules/playwright-core; do [ -f "$p/index.js" ] && { echo "$p"; break; }; done')
if [ -n "$runtime" ]; then
    ok "playwright-core лежит по пути из RUNTIME_PATHS: $runtime"
    eq "playwright-core принадлежит root" "$(dx "$CA" "stat -c '%U' $runtime/index.js")" "root"
    refuse "бот не может изменить playwright-core" dx "$CA" "echo x >> $runtime/index.js"
else
    bad "playwright-core не найден ни по одному пути RUNTIME_PATHS: исполнитель ответит runtime_missing (поправить RUNTIME_PATHS или образ)"
fi

# pstep, procedure_step_runtime_checks и остальное: isolation_helpers.sh (там же их тесты).

# Код под uid 1001 (скомпрометированный Chromium) кладёт свой playwright-core туда, где Node ищет модули по имени.
dxb "$CA" 'mkdir -p /home/browser/.node_modules/playwright-core /home/browser/.node_libraries/playwright-core && for d in .node_modules .node_libraries; do printf "require(\"fs\").writeFileSync(\"/home/browser/.planted-loaded\", \"x\"); exports.chromium = {};\n" > /home/browser/$d/playwright-core/index.js; done; rm -f /home/browser/.planted-loaded'
res=$(pstep isochk-pstep-1 '{"action":"wait","target":null,"value":"1"}' true)
case "$res" in
    "200 runtime_missing "*) bad "исполнитель не нашёл playwright-core по абсолютному пути ($res)" ;;
    200*) ok "шаг-чтение прошёл через лаунчер ($res)" ;;
    *) bad "шаг-чтение: неожиданный ответ ($res)" ;;
esac
check "подложенный playwright-core из HOME не загружен" dxb "$CA" '[ ! -e /home/browser/.planted-loaded ]'
dxb "$CA" 'rm -rf /home/browser/.node_modules /home/browser/.node_libraries /home/browser/.planted-loaded'

# Окружение, payload в командных строках и остановка работающего шага. Проверки идут всегда: браузер сам приводится в
# about:blank (navigate через procedure_step), состояние вкладки на входе не важно (isolation_helpers.sh).
procedure_step_runtime_checks

# ------------------------------------------------------------------ PID 1
section "PID 1 бота: изолированный интерпретатор"
# cmdline читается всеми; /proc/1/cwd у недампабельного PID 1 только у root хоста.
eq "PID 1 запущен как /usr/bin/python3 -I - (через bot-guard)" "$(dx "$CA" 'tr "\0" " " < /proc/1/cmdline')" "/usr/bin/python3 -I - "
pid1_host=$(docker inspect -f '{{.State.Pid}}' "$CA" 2>/dev/null)
pid1_cwd=$(readlink "/proc/$pid1_host/cwd" 2>/dev/null || sudo -n readlink "/proc/$pid1_host/cwd" 2>/dev/null)
if [ -n "$pid1_cwd" ]; then
    eq "cwd PID 1 = /" "$pid1_cwd" "/"
else
    warn "cwd PID 1 не прочитан: нужен root на хосте (проверка по ctypes.py ниже остаётся)"
fi
eq "PID 1 пережил freeze_bot (контейнер работает)" "$(docker inspect -f '{{.State.Running}}' "$CA")" "true"
# Подмена модулей: ctypes.py и signal.py в /home/bot должны остаться неисполненными после пересоздания.
dx "$CA" 'printf "open(\"/home/bot/ctypes-pwned\", \"w\").close()\n" > /home/bot/ctypes.py; cp /home/bot/ctypes.py /home/bot/signal.py; rm -f /home/bot/ctypes-pwned'

# ------------------------------------------------------------------ пересоздание
section "Пересоздание сохраняет home"
dx "$CA" 'echo keep-me-isochk > /home/bot/keep-me'
id_before=$(docker inspect -f '{{.Id}}' "$CA")
out=$(lc POST "/v1/bots/$BOT_A/recreate")
eq "recreate_bot вернул 200" "$(code_of "$out")" "200"
wait_running "$CA" || bad "бот A не поднялся после пересоздания"
id_after=$(docker inspect -f '{{.Id}}' "$CA")
if [ "$id_before" != "$id_after" ]; then ok "контейнер действительно новый"; else bad "контейнер не пересоздан"; fi
eq "файл в home на месте" "$(dx "$CA" 'cat /home/bot/keep-me')" "keep-me-isochk"
eq "после пересоздания бот снова в своей сети" "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$CA")" "$NET_A"
check "после пересоздания API ядра доступен" dx "$CA" 'curl -sS -m 5 -f -o /dev/null http://core:8080/api/health'
sleep 2
check "ctypes.py из /home/bot не исполнен в PID 1 после пересоздания" dx "$CA" '[ -f /home/bot/ctypes.py ] && [ ! -e /home/bot/ctypes-pwned ]'
eq "PID 1 после пересоздания жив и изолирован" "$(dx "$CA" 'tr "\0" " " < /proc/1/cmdline')" "/usr/bin/python3 -I - "
dx "$CA" 'rm -f /home/bot/ctypes.py /home/bot/signal.py /home/bot/ctypes-pwned'
rules_after=$(hx "$IPT" -S BOTHUB-ISO; hx "$IPT" -S BOTHUB-IN)
eq "правила iptables после пересоздания те же (идемпотентность)" "$(printf '%s' "$rules_after" | md5sum)" "$(printf '%s' "$rules_before" | md5sum)"
eq "прыжок в DOCKER-USER один" "$(hx "$IPT" -S DOCKER-USER | grep -c -e '-j BOTHUB-ISO')" "1"

# Только на отдельном стенде. Рестарт Docker затрагивает другие контейнеры.
if [ "${ISOCHK_DISRUPTIVE:-0}" = 1 ]; then
    section "Восстановление после iptables -F BOTHUB-ISO"
    check "тестовые боты остановлены перед flush" docker stop "$CA" "$CB"
    check "цепочка BOTHUB-ISO очищена" hx "$IPT" -F BOTHUB-ISO
    restored=0
    for _ in $(seq 1 90); do
        if hx "$IPT" -S BOTHUB-ISO 2>/dev/null | has_drop_rule "$BR_A" 10.0.0.0/8; then
            restored=1
            break
        fi
        sleep 1
    done
    eq "правила восстановлены после flush" "$restored" "1"
    check "тестовые боты вновь запущены после восстановления правил" docker start "$CA" "$CB"
    refuse_with_drop "после flush пакет дошёл до нашего DROP" BOTHUB-ISO "$BR_A" 10.0.0.0/8 dxt "$CA" 10.255.255.1 80

    if [ -n "${ISOCHK_DOCKER_RESTART_CMD:-}" ]; then
        section "Восстановление после рестарта Docker"
        check "тестовые боты остановлены перед рестартом Docker" docker stop "$CA" "$CB"
        check "Docker daemon перезапущен" bash -c "$ISOCHK_DOCKER_RESTART_CMD"
        daemon_up=0
        for _ in $(seq 1 120); do
            if docker info >/dev/null 2>&1 && [ "$(docker inspect -f '{{.State.Running}}' "$LAUNCHER" 2>/dev/null)" = true ]; then
                daemon_up=1
                break
            fi
            sleep 1
        done
        eq "Docker daemon и launcher поднялись" "$daemon_up" "1"
        if wait_running "$CA" && wait_running "$CB"; then
            ok "остановленные боты поднялись после правил"
            refuse_with_drop "после рестарта Docker пакет дошёл до нашего DROP" BOTHUB-ISO "$BR_A" 10.0.0.0/8 dxt "$CA" 10.255.255.1 80
        else
            bad "остановленные боты не поднялись после рестарта Docker"
        fi
    else
        warn "рестарт Docker не проверен: задайте ISOCHK_DOCKER_RESTART_CMD на отдельном стенде"
    fi
fi

# ------------------------------------------------------------------ итог
printf '\n== Итог: %d прошло, %d провалено, %d предупреждений\n' "$PASS" "$FAIL" "$WARN"
[ "$FAIL" -eq 0 ]
