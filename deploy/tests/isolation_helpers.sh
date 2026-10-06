#!/usr/bin/env bash
# Чистые парсеры и проверки исполнителя шагов процедур для isolation_check.sh.
# Проверки исполнителя (procedure_step_runtime_checks) зовут функции стенда: lc, dxb, ok, bad, eq, code_of, body_of
# и переменные BOT_A, CA. В test_isolation_helpers.sh их заменяют заглушки.
# Команды для контейнера намеренно в одинарных кавычках: переменные раскрываются внутри контейнера.
# shellcheck disable=SC2016

non_loopback_listeners() {
    awk '$1 == "tcp" && $2 !~ /7F$/ {print; next} $1 == "tcp6" && $2 != "00000000000000000000000001000000" {print}'
}

has_drop_rule() {
    awk -v bridge="$1" -v cidr="$2" '
        $1 == "-A" && $2 == "BOTHUB-ISO" {
            input = destination = drop = 0
            for (i = 3; i < NF; i++) {
                if ($i == "-i" && $(i + 1) == bridge) input = 1
                if ($i == "-d" && $(i + 1) == cidr) destination = 1
                if ($i == "-j" && $(i + 1) == "DROP") drop = 1
            }
            if (input && destination && drop) found = 1
        }
        END {exit !found}'
}

drop_packet_count() {
    awk -v bridge="$1" -v dest="$2" '$3 == "DROP" && $6 == bridge && $9 == dest {print $1; exit}'
}

# docker_major ВЕРСИЯ : основная версия Docker из `29.1.3`, `28.0.0-rc.1`, `v27.5.1`; для мусора пусто
docker_major() {
    local version=${1#v}
    [[ $version =~ ^([0-9]+)\.[0-9]+ ]] && printf '%s' "${BASH_REMATCH[1]}"
    return 0
}

# default_route : шлюз по умолчанию из /proc/net/route (stdin): «интерфейс шлюз-hex» с наименьшей метрикой
default_route() {
    awk 'NR > 1 && $2 == "00000000" && $8 == "00000000" {print $7, $1, $3}' | sort -n | head -n 1 | cut -d' ' -f2-
}

# core_networks : сети контейнера из `docker inspect -f '{{json .NetworkSettings.Networks}}'` (stdin) строками
# «сеть шлюз приоритет» (пустой шлюз это «-», приоритет без поля GwPriority это 0), по имени сети
core_networks() {
    python3 -c '
import json
import sys

nets = json.load(sys.stdin) or {}
for name in sorted(nets):
    net = nets[name] or {}
    print(name, net.get("Gateway") or "-", net.get("GwPriority") or 0)
'
}

# health_probe_loop URL КАТАЛОГ : пока в КАТАЛОГЕ есть файл run, раз в 0,1 с запрос по URL (таймаут 2 с).
# Счётчики «всего ошибок» лежат в КАТАЛОГ/count. Крутится в фоне, пока стенд создаёт сети ботов.
health_probe_loop() {
    local url=$1 dir=$2 total=0 failed=0
    while [ -e "$dir/run" ]; do
        total=$((total + 1))
        curl -sS -m 2 -f -o /dev/null "$url" >/dev/null 2>&1 || failed=$((failed + 1))
        printf '%d %d\n' "$total" "$failed" >"$dir/count"
        sleep 0.1
    done
}

blocked_cidr() {
    python3 - "$1" <<'PY'
import ipaddress
import sys

address = ipaddress.ip_address(sys.argv[1])
for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"):
    if address in ipaddress.ip_network(cidr):
        print(cidr)
        break
PY
}

# ------------------------------------------------------------------ исполнитель шагов процедур (procedure_step)

# Сверка страницы для шага над about:blank (payload.expected, см. docs/contracts.md, раздел 14).
PSTEP_BLANK_EXPECTED='{"expected":{"origin":null,"url":"about:blank","role":null,"name":null}}'
# Окружение исполнителя: env -i HOME=/nonexistent PATH=/usr/bin:/bin (docker_args.procedure_step_args), без остального.
PSTEP_ENV_EXPECTED='HOME=/nonexistent|PATH=/usr/bin:/bin|'
# Метка в payload долгого шага: по ней ищем утечку payload в командные строки процессов (он идёт только через stdin).
PSTEP_PAYLOAD_MARKER=isochk-payload-marker-7c1e
printf -v PSTEP_WAIT_STEP '{"action":"wait","target":null,"value":"20000","expect":{"text":"%s"}}' "$PSTEP_PAYLOAD_MARKER"

# procedure_step_body exec_id json-шага dry_run [json-добавка-к-payload] : тело запроса procedure-step
procedure_step_body() {
    local extra=${4:-}
    [ -n "$extra" ] || extra='{}'
    PSTEP_STEP="$2" PSTEP_ID="$1" PSTEP_DRY="$3" PSTEP_EXTRA="$extra" python3 -c '
import json, os
payload = {"step": json.loads(os.environ["PSTEP_STEP"]), **json.loads(os.environ["PSTEP_EXTRA"])}
print(json.dumps({"payload_json": json.dumps(payload), "dry_run": os.environ["PSTEP_DRY"] == "true", "timeout": 30,
                  "exec_id": os.environ["PSTEP_ID"]}))'
}

# procedure_result_field поле (stdin: тело ответа procedure-step) : поле result; строка как есть, остальное JSON,
# нет поля, нет result или не JSON дают null
procedure_result_field() {
    python3 -c '
import json, sys
try:
    result = json.load(sys.stdin).get("result") or {}
except (ValueError, AttributeError):
    result = {}
if not isinstance(result, dict):
    result = {}
value = result.get(sys.argv[1])
print(value if isinstance(value, str) else json.dumps(value))' "$1"
}

# tab_url_from_json (stdin: тело ответа browser-tab) : адрес первой вкладки, пусто если его нет или ответ не JSON
tab_url_from_json() {
    python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("url") or "")
except (ValueError, AttributeError):
    print("")'
}

# pstep exec_id json-шага dry_run [json-добавка-к-payload] : шаг через лаунчер, печатает "HTTP-код код-результата ok"
pstep() {
    local body out
    body=$(procedure_step_body "$@") || return 1
    out=$(lc POST "/v1/bots/$BOT_A/procedure-step" "$body")
    printf '%s %s %s' "$(code_of "$out")" "$(body_of "$out" | procedure_result_field code)" \
        "$(body_of "$out" | procedure_result_field ok)"
}

bot_tab_url() { body_of "$(lc GET "/v1/bots/$BOT_A/browser-tab")" | tab_url_from_json; }

# reset_browser_blank : привести браузер бота в известное состояние. Шаг navigate на about:blank идёт через procedure_step
# (первой вкладке не нужна сверка страницы). Успех подтверждается адресом, который вернул сам исполнитель: он и лаунчер
# могут называть «первой» разные вкладки, когда их несколько (после смены режима браузера Chromium восстанавливает
# вкладки), а следующие проверки идут через того же исполнителя. До BLANK_TRIES попыток; последний ответ шага остаётся
# в RESET_LAST. Код 0, если исполнитель стоит на about:blank.
reset_browser_blank() {
    local i body out
    RESET_LAST=
    for i in $(seq 1 "${BLANK_TRIES:-5}"); do
        body=$(procedure_step_body "isochk-pstep-blank-$i" '{"action":"navigate","target":{"url":"about:blank"},"value":null}' false) || return 1
        out=$(lc POST "/v1/bots/$BOT_A/procedure-step" "$body")
        RESET_LAST="$(code_of "$out") $(body_of "$out" | procedure_result_field code) $(body_of "$out" | procedure_result_field ok) $(body_of "$out" | procedure_result_field url)"
        if [ "$(body_of "$out" | procedure_result_field url)" = about:blank ] && [ "$(body_of "$out" | procedure_result_field ok)" = true ]; then return 0; fi
        sleep "${BLANK_SLEEP:-1}"
    done
    return 1
}

# pstep_result exec_id json-шага dry_run [json-добавка] : как pstep, плюс адрес страницы из ответа: "HTTP-код код ok адрес"
pstep_result() {
    local body out
    body=$(procedure_step_body "$@") || return 1
    out=$(lc POST "/v1/bots/$BOT_A/procedure-step" "$body")
    printf '%s %s %s %s' "$(code_of "$out")" "$(body_of "$out" | procedure_result_field code)" \
        "$(body_of "$out" | procedure_result_field ok)" "$(body_of "$out" | procedure_result_field url)"
}

# fresh_browser_navigation_checks : свежий бот БЕЗ предподготовки (вызывать до первого шага процедуры и до смены режима).
# Политика закрывает chrome://*, и раньше первая вкладка была chrome-error://, а первый navigate падал с page_ambiguous.
# Теперь: вкладка about:blank; чтение navigate (dry_run) отвечает ok, а не page_ambiguous; navigate с expected about:blank
# выполняется, вкладка становится запрошенной (адрес из ответа исполнителя и из лаунчера). В конце браузер снова about:blank.
fresh_browser_navigation_checks() {
    local target=https://example.com/ read acted tab _
    local nav_blank='{"action":"navigate","target":{"url":"about:blank"},"value":null}'
    local nav_target="{\"action\":\"navigate\",\"target\":{\"url\":\"$target\"},\"value\":null}"
    # CDP уже слушает, но вкладка может появиться на секунду позже
    for _ in $(seq 1 "${FRESH_TRIES:-10}"); do
        tab=$(bot_tab_url)
        [ -n "$tab" ] && break
        sleep "${FRESH_SLEEP:-1}"
    done
    eq "свежий бот: первая вкладка about:blank" "$tab" "about:blank"
    read=$(pstep_result isochk-pstep-fresh-1 "$nav_blank" true)
    eq "свежий бот: чтение navigate без предподготовки" "$read" "200 null true about:blank"
    acted=$(pstep_result isochk-pstep-fresh-2 "$nav_target" false "$PSTEP_BLANK_EXPECTED")
    eq "свежий бот: navigate с expected about:blank выполнен" "$acted" "200 null true $target"
    tab=$(bot_tab_url)
    eq "свежий бот: вкладка стала запрошенной" "${tab%/}" "${target%/}"
    if reset_browser_blank; then
        ok "браузер приведён в известное состояние после проверки свежего бота"
    else
        bad "браузер не вернулся в about:blank после проверки свежего бота (последний ответ: ${RESET_LAST:-нет})"
    fi
}

# pidof_step exec_id : PID процессов исполнителя шага (по метке в командной строке), по одному в строке
pidof_step() {
    dxb "$CA" 'for p in /proc/[0-9]*; do if tr "\0" " " < "$p/cmdline" 2>/dev/null | grep -q "[b]othub-procedure-'"$1"'"; then echo "${p#/proc/}"; fi; done'
}

# wait_step_pid exec_id : ждать появления процесса исполнителя (PSTEP_PID_TRIES раз по PSTEP_PID_SLEEP), печатает PID
wait_step_pid() {
    local _ pid
    for _ in $(seq 1 "${PSTEP_PID_TRIES:-20}"); do
        pid=$(pidof_step "$1" | head -n1)
        if [ -n "$pid" ]; then printf '%s' "$pid"; return 0; fi
        sleep "${PSTEP_PID_SLEEP:-0.5}"
    done
    return 1
}

# wait_step_gone exec_id : ждать, пока процессов исполнителя не останется
wait_step_gone() {
    local _
    for _ in $(seq 1 "${PSTEP_PID_TRIES:-20}"); do
        [ -z "$(pidof_step "$1")" ] && return 0
        sleep "${PSTEP_PID_SLEEP:-0.5}"
    done
    return 1
}

# procedure_step_runtime_checks : окружение исполнителя (env -i, HOME=/nonexistent, cwd /), payload не в командных строках
# и остановка procedure_step_cancel. Выполняется всегда: сначала браузер приводится в about:blank, но если не удалось,
# это провал (bad), а проверки всё равно идут. Исполнитель не нашёлся: тоже bad, не пропуск.
procedure_step_runtime_checks() {
    local id=isochk-pstep-2 spid bgpid out
    local pattern="[${PSTEP_PAYLOAD_MARKER:0:1}]${PSTEP_PAYLOAD_MARKER:1}"
    if reset_browser_blank; then
        ok "браузер приведён в известное состояние: исполнитель стоит на about:blank (navigate через procedure_step)"
    else
        bad "браузер не приведён в about:blank за ${BLANK_TRIES:-5} попыток (последний ответ: ${RESET_LAST:-нет})"
    fi
    # wait без цели над about:blank: живёт 20 секунд; действие требует expected (сверка страницы).
    pstep "$id" "$PSTEP_WAIT_STEP" false "$PSTEP_BLANK_EXPECTED" >/dev/null 2>&1 &
    bgpid=$!
    spid=$(wait_step_pid "$id" || true)
    if [ -z "$spid" ]; then
        bad "исполнитель шага (метка bothub-procedure-$id) не найден среди процессов uid 1001"
        lc POST "/v1/bots/$BOT_A/procedure-step/cancel" "{\"exec_id\":\"$id\"}" >/dev/null 2>&1
        wait "$bgpid" 2>/dev/null || true
        return 0
    fi
    eq "исполнитель шага запущен под uid 1001" "$(dxb "$CA" "stat -c '%u' /proc/$spid")" "1001"
    eq "окружение исполнителя: только HOME=/nonexistent и PATH" "$(dxb "$CA" "tr '\0' '|' < /proc/$spid/environ")" "$PSTEP_ENV_EXPECTED"
    eq "рабочий каталог исполнителя /" "$(dxb "$CA" "readlink /proc/$spid/cwd")" "/"
    # [x] в шаблоне: иначе grep находит в /proc собственную командную строку.
    eq "payload шага не попал в командные строки процессов" \
        "$(dxb "$CA" 'for f in /proc/[0-9]*/cmdline; do tr "\0" " " < "$f" 2>/dev/null; echo; done | grep -c "'"$pattern"'"')" "0"
    out=$(lc POST "/v1/bots/$BOT_A/procedure-step/cancel" "{\"exec_id\":\"$id\"}")
    eq "procedure_step_cancel вернул 200" "$(code_of "$out")" "200"
    wait_step_gone "$id"
    eq "после procedure_step_cancel процесса исполнителя нет" "$(pidof_step "$id" | wc -l | tr -d ' ')" "0"
    wait "$bgpid" 2>/dev/null || true
    return 0
}
