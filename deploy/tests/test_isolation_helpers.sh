#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=tests/isolation_helpers.sh
source "$(dirname "$0")/isolation_helpers.sh"

listeners=$'tcp 0100007F 5900\ntcp 0B00007F 53\ntcp 0200007F 9999\ntcp6 00000000000000000000000001000000 6080'
[ -z "$(printf '%s\n' "$listeners" | non_loopback_listeners)" ]
[ "$(printf 'tcp 00000000 1234\n' | non_loopback_listeners)" = 'tcp 00000000 1234' ]
[ "$(printf 'tcp6 00000000000000000000000000000000 1234\n' | non_loopback_listeners)" = 'tcp6 00000000000000000000000000000000 1234' ]

rules=$'-A BOTHUB-ISO -i bhua -d 10.0.0.0/8 -m comment --comment blocked -j DROP\n-A BOTHUB-ISO -i bhub -d 10.0.0.0/8 -j ACCEPT'
printf '%s\n' "$rules" | has_drop_rule bhua 10.0.0.0/8
if printf '%s\n' "$rules" | has_drop_rule bhub 10.0.0.0/8; then exit 1; fi

listing=$' pkts bytes target prot opt in out source destination\n 42 2520 DROP all -- bhua * 0.0.0.0/0 10.0.0.0/8\n 0 0 DROP all -- bhub * 0.0.0.0/0 10.0.0.0/8'
[ "$(printf '%s\n' "$listing" | drop_packet_count bhua 10.0.0.0/8)" = 42 ]
[ "$(printf '%s\n' "$listing" | drop_packet_count bhub 10.0.0.0/8)" = 0 ]
[ "$(blocked_cidr 172.18.0.3)" = '172.16.0.0/12' ]
[ "$(blocked_cidr 10.2.3.4)" = '10.0.0.0/8' ]
[ -z "$(blocked_cidr 8.8.8.8)" ]

# ---------------------------------------------------------------- шлюзовая сеть ядра (docs/isolation.md)
[ "$(docker_major 29.1.3)" = 29 ]
[ "$(docker_major 28.0.0-rc.1)" = 28 ]
[ "$(docker_major v27.5.1)" = 27 ]
[ -z "$(docker_major garbage)" ]
[ -z "$(docker_major '')" ]

route=$'Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\neth1\t00000000\t0215A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\neth0\t00000000\t010011AC\t0003\t0\t0\t10\t00000000\t0\t0\t0\neth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0'
[ "$(printf '%s\n' "$route" | default_route)" = 'eth1 0215A8C0' ]
[ "$(printf 'Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n' | default_route)" = '' ]
low_metric=$'Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\neth1\t00000000\t0215A8C0\t0003\t0\t0\t100\t00000000\neth0\t00000000\t010011AC\t0003\t0\t0\t5\t00000000'
[ "$(printf '%s\n' "$low_metric" | default_route)" = 'eth0 010011AC' ]

nets='{"bothub_default":{"Gateway":"172.18.0.1","GwPriority":100},"bothub_db":{"Gateway":""},"bothub-u-a":{"Gateway":"172.30.0.1","GwPriority":-100}}'
want=$'bothub-u-a 172.30.0.1 -100\nbothub_db - 0\nbothub_default 172.18.0.1 100'
[ "$(printf '%s\n' "$nets" | core_networks)" = "$want" ]
# Docker старше 28 не знает GwPriority: приоритет 0.
[ "$(printf '{"bothub_default":{"Gateway":"172.18.0.1"}}\n' | core_networks)" = 'bothub_default 172.18.0.1 0' ]
[ -z "$(printf 'null\n' | core_networks)" ]

# ---------------------------------------------------------------- шаги процедур: заглушки окружения isolation_check.sh
BOT_A=isochk-a
CA=bot-isochk-a
TMP=$(mktemp -d "${TMPDIR:-/tmp}/isochk-test.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
CALLS=$TMP/calls
BLANK_TRIES=3
BLANK_SLEEP=0
PSTEP_PID_TRIES=3
PSTEP_PID_SLEEP=0.05
ok() { printf '%s\n' "$1" >>"$TMP/ok"; }
bad() { printf '%s\n' "$1" >>"$TMP/bad"; }
warn() { printf '%s\n' "$1" >>"$TMP/warn"; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (получено '$2', ожидалось '$3')"; fi; }
code_of() { printf '%s' "$1" | tail -n1; }
body_of() { printf '%s' "$1" | sed '$d'; }
# Лаунчер: состояние во файлах (вызовы идут из подоболочек). nav_works: navigate меняет вкладку на about:blank;
# no_proc: исполнитель шага не появляется; wait-шаг живёт, пока его не снимет cancel.
lc() {
    printf '%s %s %s\n' "$1" "$2" "${3:-}" >>"$CALLS"
    case "$1 $2" in
        "GET /v1/bots/$BOT_A/browser-tab")
            printf '{"bot_id":"%s","mode":"bot","url":"%s"}\n200' "$BOT_A" "$(cat "$TMP/tab")" ;;
        "POST /v1/bots/$BOT_A/procedure-step/cancel")
            rm -f "$TMP/pid"
            printf '{"stopped":true}\n200' ;;
        "POST /v1/bots/$BOT_A/procedure-step")
            case "$3" in
                *navigate*)
                    if [ -e "$TMP/ambiguous" ]; then printf '{"result":{"ok":false,"code":"page_ambiguous","url":null}}\n200'; return 0; fi
                    case "$3" in
                        *'"dry_run": true'*) ;;
                        *example.com*) if [ -e "$TMP/nav_works" ]; then printf 'https://example.com/' >"$TMP/tab"; fi ;;
                        *) if [ -e "$TMP/nav_works" ]; then printf 'about:blank' >"$TMP/tab"; fi ;;
                    esac
                    # исполнитель сообщает адрес страницы, на которой оказался: при неработающей навигации это прежний адрес
                    printf '{"result":{"ok":true,"code":null,"url":"%s"}}\n200' "$(cat "$TMP/tab")" ;;
                *)
                    if [ ! -e "$TMP/no_proc" ]; then printf '4242' >"$TMP/pid"; fi
                    for _ in $(seq 1 100); do [ -e "$TMP/pid" ] || break; sleep 0.05; done
                    printf '{"result":{"ok":false,"code":"timeout"}}\n200' ;;
            esac ;;
        *) printf '{}\n404' ;;
    esac
}
# Контейнер бота под uid 1001: ответы по характерному куску команды.
dxb() {
    printf 'dxb %s\n' "$2" >>"$CALLS"
    case "$2" in
        *'[b]othub-procedure-'*) if [ -e "$TMP/pid" ]; then cat "$TMP/pid"; fi ;;
        *'[i]sochk-payload'*) if [ -e "$TMP/leak_cmd" ]; then printf 1; else printf 0; fi ;;
        *'%u'*) printf 1001 ;;
        */environ*)
            if [ -e "$TMP/leak_env" ]; then printf 'HOME=/nonexistent|PATH=/usr/bin:/bin|NODE_OPTIONS=--require=/x|'
            else printf 'HOME=/nonexistent|PATH=/usr/bin:/bin|'; fi ;;
        */cwd*) printf / ;;
        *) return 1 ;;
    esac
}
# scenario вкладка nav_works(1|0) [флаг...] : чистое состояние заглушек
scenario() {
    local f
    : >"$CALLS"; : >"$TMP/ok"; : >"$TMP/bad"; : >"$TMP/warn"
    rm -f "$TMP/pid" "$TMP/nav_works" "$TMP/no_proc" "$TMP/leak_env" "$TMP/leak_cmd" "$TMP/ambiguous"
    printf '%s' "$1" >"$TMP/tab"
    if [ "$2" = 1 ]; then touch "$TMP/nav_works"; fi
    shift 2
    for f in "$@"; do touch "$TMP/$f"; done
}
has() { grep -qF -- "$2" "$TMP/$1"; }
count_in() { grep -cF -- "$2" "$TMP/$1" || true; }

# Тело запроса procedure-step и разбор ответа.
body=$(procedure_step_body isochk-x '{"action":"wait","target":null,"value":"5"}' false "$PSTEP_BLANK_EXPECTED")
[ "$(printf '%s' "$body" | python3 -c 'import json, sys; b = json.load(sys.stdin); p = json.loads(b["payload_json"]); print(b["exec_id"], b["dry_run"], b["timeout"], p["step"]["action"], p["expected"]["url"], p["expected"]["origin"])')" = 'isochk-x False 30 wait about:blank None' ]
body=$(procedure_step_body isochk-y '{"action":"wait","target":null,"value":"1"}' true)
[ "$(printf '%s' "$body" | python3 -c 'import json, sys; b = json.load(sys.stdin); print(b["dry_run"], sorted(json.loads(b["payload_json"])))')" = "True ['step']" ]
[ "$(printf '%s' '{"result":{"ok":true,"code":null}}' | procedure_result_field ok)" = true ]
[ "$(printf '%s' '{"result":{"ok":false,"code":"changed"}}' | procedure_result_field code)" = changed ]
[ "$(printf '%s' '{"result":null}' | procedure_result_field ok)" = null ]
[ "$(printf '%s' 'не json' | procedure_result_field code)" = null ]
[ "$(printf '%s' '{"bot_id":"a","mode":"bot","url":"https://example.com/x"}' | tab_url_from_json)" = 'https://example.com/x' ]
[ -z "$(printf '%s' '{"mode":"human","url":null}' | tab_url_from_json)" ]
[ -z "$(printf '%s' 'мусор' | tab_url_from_json)" ]
scenario about:blank 0
[ "$(pstep isochk-z '{"action":"navigate","target":{"url":"about:blank"},"value":null}' false)" = '200 null true' ]
has calls 'POST /v1/bots/isochk-a/procedure-step '

# reset_browser_blank: браузер на чужой странице приводится в about:blank через procedure_step (navigate).
scenario https://example.com/secret 1
reset_browser_blank
has calls 'POST /v1/bots/isochk-a/procedure-step ' && has calls 'about:blank' && has calls '"dry_run": false'
[ "$(count_in calls 'POST /v1/bots/isochk-a/procedure-step ')" = 1 ]
scenario about:blank 1
reset_browser_blank
# Страница не меняется: функция сдаётся после BLANK_TRIES попыток и сообщает неуспех, а не молчит.
scenario https://example.com/stuck 0
if reset_browser_blank; then exit 1; fi
[ "$(count_in calls 'POST /v1/bots/isochk-a/procedure-step ')" = 3 ]

# procedure_step_runtime_checks: проверки окружения и остановки идут всегда, независимо от страницы в браузере.
scenario https://example.com/secret 1
procedure_step_runtime_checks
[ ! -s "$TMP/bad" ] && [ ! -s "$TMP/warn" ]
has ok 'окружение исполнителя: только HOME=/nonexistent и PATH'
has ok 'исполнитель шага запущен под uid 1001'
has ok 'рабочий каталог исполнителя /'
has ok 'payload шага не попал в командные строки процессов'
has ok 'procedure_step_cancel вернул 200'
has ok 'после procedure_step_cancel процесса исполнителя нет'
has calls 'POST /v1/bots/isochk-a/procedure-step/cancel {"exec_id":"isochk-pstep-2"}'
# Шаг-ожидание идёт с проверкой страницы about:blank, а payload несёт метку для поиска в cmdline.
has calls "\\\"origin\\\": null, \\\"url\\\": \\\"about:blank\\\""
has calls "$PSTEP_PAYLOAD_MARKER"

# Вкладку в about:blank привести не удалось: это провал, но проверки окружения и остановки всё равно выполнены.
scenario https://example.com/stuck 0
procedure_step_runtime_checks
has bad 'about:blank'
[ ! -s "$TMP/warn" ]
has ok 'окружение исполнителя: только HOME=/nonexistent и PATH'
has ok 'procedure_step_cancel вернул 200'
has ok 'после procedure_step_cancel процесса исполнителя нет'

# Исполнитель не появился: провал (bad), не предупреждение; шаг на всякий случай снят через cancel.
scenario about:blank 1 no_proc
procedure_step_runtime_checks
has bad 'исполнитель шага (метка bothub-procedure-isochk-pstep-2) не найден'
[ ! -s "$TMP/warn" ]
has calls 'POST /v1/bots/isochk-a/procedure-step/cancel'

# Утечки ловятся: лишняя переменная в окружении исполнителя, payload в командной строке.
scenario about:blank 1 leak_env
procedure_step_runtime_checks
has bad 'окружение исполнителя: только HOME=/nonexistent и PATH'
scenario about:blank 1 leak_cmd
procedure_step_runtime_checks
has bad 'payload шага не попал в командные строки процессов'


# fresh_browser_navigation_checks: свежий бот без предподготовки. Вкладка about:blank, чтение navigate и действие navigate
# с expected about:blank проходят (не page_ambiguous), вкладка становится запрошенной; после проверки браузер снова about:blank.
scenario about:blank 1
fresh_browser_navigation_checks
[ ! -s "$TMP/bad" ] && [ ! -s "$TMP/warn" ]
has ok 'свежий бот: первая вкладка about:blank'
has ok 'свежий бот: чтение navigate без предподготовки'
has ok 'свежий бот: navigate с expected about:blank выполнен'
has ok 'свежий бот: вкладка стала запрошенной'
has ok 'браузер приведён в известное состояние'
# чтение идёт с dry_run, действие без него и несёт expected об about:blank
has calls '"dry_run": true'
has calls '"dry_run": false'
has calls 'https://example.com/'
[ "$(count_in calls 'POST /v1/bots/isochk-a/procedure-step ')" -ge 3 ]
# Свежая вкладка на странице ошибки: провал.
scenario chrome-error://chromewebdata/ 1
fresh_browser_navigation_checks
has bad 'свежий бот: первая вкладка about:blank'
# Прежняя ошибка: исполнитель отвечает page_ambiguous.
scenario about:blank 1 ambiguous
fresh_browser_navigation_checks
has bad 'свежий бот: чтение navigate без предподготовки'
has bad 'свежий бот: navigate с expected about:blank выполнен'
# Навигация молча не меняет вкладку: провал на последней проверке.
scenario about:blank 0
fresh_browser_navigation_checks
has bad 'свежий бот: вкладка стала запрошенной'

# ---------------------------------------------------------------- фоновые запросы к опубликованному порту
HEALTH=$TMP/health
mkdir "$HEALTH"
: >"$HEALTH/run"
CURL_CALLS=0
# Заглушка curl: второй запрос неудачный, на четвёртом цикл останавливают (как rm run в isolation_check.sh).
curl() {
    CURL_CALLS=$((CURL_CALLS + 1))
    [ "$CURL_CALLS" -ge 4 ] && rm -f "$HEALTH/run"
    [ "$CURL_CALLS" -ne 2 ]
}
health_probe_loop http://127.0.0.1:8080/api/health "$HEALTH"
[ "$(cat "$HEALTH/count")" = '4 1' ]
unset -f curl

printf 'isolation parser tests passed\n'
