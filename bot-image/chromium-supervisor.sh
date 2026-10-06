#!/bin/bash
# Browser stack is owned by uid 1001; its profile and X socket are private.
set -euo pipefail

if [[ $(id -u) != 1001 || "$HOME" != /home/browser ]]; then
    echo 'chromium-supervisor: browser uid 1001 required' >&2
    exit 1
fi

SIZE="${BOT_BROWSER_SCREEN_SIZE:-1280x800}"
DISPLAY="${BOT_BROWSER_DISPLAY:-:99}"
if [[ ! "$SIZE" =~ ^([1-9][0-9]{2,3})x([1-9][0-9]{2,3})$ || ! "$DISPLAY" =~ ^:[0-9]{1,3}$ ]]; then
    echo 'chromium-supervisor: invalid display or screen size' >&2
    exit 2
fi
# Re-parse SIZE: BASH_REMATCH was overwritten by the DISPLAY validation.
[[ "$SIZE" =~ ^([1-9][0-9]{2,3})x([1-9][0-9]{2,3})$ ]]
WIDTH="${BASH_REMATCH[1]}"
HEIGHT="${BASH_REMATCH[2]}"
if (( WIDTH < 320 || WIDTH > 4096 || HEIGHT < 200 || HEIGHT > 2160 )); then
    echo 'chromium-supervisor: screen size out of range' >&2
    exit 2
fi

umask 077
LOCKFILE="$HOME/.browser-supervisor.lock"
exec 9>>"$LOCKFILE"
if ! flock -n 9; then
    echo 'chromium-supervisor: already running' >&2
    exit 0
fi
chmod 0600 "$LOCKFILE"

readonly BROWSER_UID=1001
cleanup_stale_stack() {
    local signal name attempt alive
    local -a processes=(Xvfb openbox socat x11vnc chromium chrome_crashpad)
    for signal in TERM KILL; do
        for name in "${processes[@]}"; do
            pkill -"$signal" -u "$BROWSER_UID" -x "$name" 2>/dev/null 9>&- || true
        done
        for ((attempt=0; attempt<25; attempt++)); do
            alive=0
            for name in "${processes[@]}"; do
                if pgrep -u "$BROWSER_UID" -x "$name" >/dev/null 2>&1 9>&-; then
                    alive=1
                    break
                fi
            done
            (( alive == 0 )) && return 0
            sleep 0.2 9>&-
        done
    done
    echo 'chromium-supervisor: old browser processes would not stop' >&2
    return 1
}

PROFILE="$HOME/.config/botstead-browser"
# Экземпляр человека (режим human) целиком лежит в одном каталоге и живёт только пока человек за экраном: профиль
# (--user-data-dir это сам каталог), дисковый кэш HTTP, дампы падений, XDG_CONFIG/CACHE/DATA/STATE, TMPDIR и HOME
# (~/.pki и прочее). При возврате cookies уходят в PROFILE, каталог удаляется целиком; вне его экземпляр человека
# ничего не пишет, поэтому кэш и история не переживают возврат.
HUMAN_ROOT="$HOME/.config/botstead-browser-human"
# Маркер сессии человека (вне каталога): `session <токен>`, есть только пока сессия идёт. Токен лаунчер кладёт в файл режима
# (`human <32 hex>`) при перехвате и генерирует заново на каждый перехват. Каталог без маркера не доверенный: перед входом в
# human его стирают, при выходе его cookies не переносят. Каталог переживает рестарт контейнера, супервизора и падение
# Chromium посреди той же сессии, но только если токен маркера равен токену файла режима: Chromium бота (тот же uid 1001) может
# подложить и каталог, и маркер, а токен будущей сессии угадать не может. Режим human без токена (старый лаунчер) не
# возобновляется никогда.
HUMAN_SESSION="$HOME/.browser-human-session"
# Предельное время слияния cookies (секунды); сбой или таймаут слияния не мешают ни удалению каталога, ни старту бота.
readonly MERGE_TIMEOUT=20
readonly MERGE_OUT="$HOME/.browser-merge-output"
# Каталог человека не удалился: Chromium бота не стартует (иначе бот получил бы доступ к данным человека), повтор через
# столько секунд.
readonly BLOCKED_RETRY=5
readonly XSEL_TIMEOUT=3
READY="$HOME/.browser-ready"
PIDFILE="$HOME/.browser-supervisor.pid"
# Режим браузера пишет лаунчер (docker exec --user 1001, атомарно через rename), супервизор только читает. Файл лежит в томе
# /home/browser и переживает рестарт контейнера: в human после рестарта Chromium бота с CDP не поднимается никогда.
# Нет файла: bot (свежий контейнер). Строка `bot`, `human` или `human <32 hex>` (токен сессии человека, см. HUMAN_SESSION);
# любое другое содержимое: Chromium не стартует вовсе (закрыто по умолчанию).
MODE_FILE="$HOME/.browser-mode"
HUMAN_URL_FILE="$HOME/.browser-human-url"
# `<режим> <pid> <время старта>` пишет супервизор после старта Chromium: по нему лаунчер проверяет готовность режима.
ACTIVE="$HOME/.browser-active"
MERGE_STATUS="$HOME/.browser-merge-status"
COOKIE_MERGE=/usr/local/libexec/cookie-merge.py
PYTHON3=/usr/bin/python3
X_SOCKET_DIR=/tmp/.X11-unix
X_SOCKET="$X_SOCKET_DIR/X${DISPLAY#:}"
X_LOCKFILE="${X_SOCKET_DIR%/*}/.X${DISPLAY#:}-lock"
# Экран отдаётся только через unix-сокет в каталоге uid 1001 (home 0700, сокет 0600): TCP-порта нет, код бота
# (uid 1000) до него не дотянется. x11vnc работает в режиме inetd (одно соединение, без своего порта).
VNC_DIR="$HOME/.vnc"
VNC_SOCKET="$VNC_DIR/rfb.sock"
OPENBOX_RC=/etc/bothub/openbox-rc.xml
rm -f "$READY" "$PIDFILE" "$ACTIVE"
cleanup_stale_stack
mkdir -p "$X_SOCKET_DIR"
chmod 0700 "$X_SOCKET_DIR"
rm -f "$X_SOCKET"
if [[ -e "$X_LOCKFILE" ]]; then
    if [[ ! -O "$X_LOCKFILE" ]]; then
        echo 'chromium-supervisor: refusing to remove an X lock file owned by another user' >&2
        exit 1
    fi
    lock_pid=$(<"$X_LOCKFILE")
    if [[ ! "$lock_pid" =~ ^[1-9][0-9]*$ ]] || kill -0 "$lock_pid" 2>/dev/null; then
        echo 'chromium-supervisor: refusing to remove a live or invalid X lock file' >&2
        exit 1
    fi
    rm -f "$X_LOCKFILE"
fi
mkdir -p "$HOME/.config" "$PROFILE" "$VNC_DIR"
chmod 0700 "$HOME" "$HOME/.config" "$PROFILE" "$VNC_DIR"
rm -f "$VNC_SOCKET"
export XAUTHORITY="$HOME/.Xauthority"
cookie=$(od -An -N16 -tx1 /dev/urandom 9>&- | tr -d ' \n' 9>&-)
xauth -f "$XAUTHORITY" add "$DISPLAY" . "$cookie" 9>&-
chmod 0600 "$XAUTHORITY"
# The pathname socket is protected by this directory; abstract clients use the MIT cookie in XAUTHORITY.
export DISPLAY HOME
printf '%s\n' "$$" > "$PIDFILE"
chmod 0600 "$PIDFILE"
# Песочница Chromium живёт на user namespaces, --no-sandbox не используется никогда. Стек uid 1001 стартует
# без bot-guard: user namespaces ему открывает профиль deploy/seccomp/bot.json. Если профиль не применён или
# хост запрещает namespaces, Chromium будет падать в цикле; одна строка в журнале сэкономит поиск причины.
if command -v unshare >/dev/null 2>&1 && ! timeout 5 unshare -U true 9>&- 2>/dev/null; then
    echo 'chromium-supervisor: unshare -U не проходит: user namespaces закрыты (seccomp-профиль лаунчера bot.json или sysctl хоста), Chromium не стартует' >&2
fi
children=()
chromium_pid=""
active_mode=""
chromium_started=$SECONDS
desired=bot
desired_token=""
# Режим, который был до остановки Chromium из-за падения или отказа подготовки каталога: без него следующий вход в human после
# падения Chromium бота выглядел бы как рестарт (previous пуст) и мог возобновить подложенный каталог.
pending_previous=""
human_url=about:blank
blocked_until=0
cleanup() {
    trap - TERM INT EXIT
    rm -f "$READY" "$PIDFILE" "$ACTIVE"
    for pid in "${children[@]:-}" "$chromium_pid"; do
        [[ -n "$pid" ]] || continue
        kill -TERM "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
}
trap 'cleanup; exit 0' TERM INT
trap cleanup EXIT

pid_alive() {
    [[ -n "$1" ]] && kill -0 "$1" 2>/dev/null && [[ "$(ps -o stat= -p "$1" 2>/dev/null)" != Z* ]]
}

# Результат в $desired: bot, human или invalid, токен сессии человека в $desired_token (пусто, если его нет). Строка режима:
# `bot`, `human` или `human <32 hex>`; всё остальное, в том числе токен у bot, invalid. Не обычный файл (ссылка, FIFO,
# каталог) тоже invalid.
read_mode() {
    local value="" token="" extra=""
    desired=invalid
    desired_token=""
    if [[ ! -e "$MODE_FILE" && ! -L "$MODE_FILE" ]]; then
        desired=bot
        return
    fi
    if [[ -f "$MODE_FILE" && ! -L "$MODE_FILE" ]]; then
        read -r value token extra < "$MODE_FILE" || true
        case "$value" in
            bot)
                if [[ -z "$token$extra" ]]; then
                    desired=bot
                fi
                ;;
            human)
                if [[ -z "$token$extra" ]]; then
                    desired=human
                elif [[ -z "$extra" && "$token" =~ ^[0-9a-f]{32}$ ]]; then
                    desired=human
                    desired_token=$token
                fi
                ;;
        esac
    fi
}

# Адрес человеку даёт ядро через лаунчер. Здесь он проверяется ещё раз: только http(s) без пробелов и обратной косой
# черты, иначе about:blank. Адрес идёт после `--`, так что флагом Chromium стать не может.
read_human_url() {
    local url="" url_re='^https?://[^[:space:][:cntrl:]\\]+$'
    human_url=about:blank
    if [[ -f "$HUMAN_URL_FILE" && ! -L "$HUMAN_URL_FILE" ]]; then
        IFS= read -r url < "$HUMAN_URL_FILE" || true
        if [[ ${#url} -le 2048 && "$url" =~ $url_re ]]; then
            human_url=$url
        fi
    fi
}

# TERM даёт Chromium сбросить cookies на диск; через 10 секунд KILL. Зигота, рендереры и crashpad могут пережить главный
# процесс, поэтому семейство добивается целиком: после остановки порт 9222 и старый профиль никто не держит.
stop_chromium() {
    local pid=$chromium_pid attempt
    chromium_pid=""
    rm -f "$READY" "$ACTIVE"
    [[ -n "$pid" ]] || return 0
    kill -TERM "$pid" 2>/dev/null || true
    for ((attempt=0; attempt<50; attempt++)); do
        pid_alive "$pid" || break
        sleep 0.2 9>&-
    done
    kill -KILL "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    pkill -KILL -u "$BROWSER_UID" -x chromium 2>/dev/null 9>&- || true
    pkill -KILL -u "$BROWSER_UID" -x chrome_crashpad 2>/dev/null 9>&- || true
}

# run_limited СЕКУНДЫ ФАЙЛ команда...: stdout и stderr команды в ФАЙЛ; по истечении времени команду убивает (KILL) и
# возвращает 124. Не зависит от GNU timeout (его нет на macOS, где идут тесты) и от того, что команда реагирует на сигналы.
run_limited() {
    local limit=$1 out=$2 pid attempt
    shift 2
    "$@" >"$out" 2>&1 9>&- &
    pid=$!
    for ((attempt=0; attempt<limit*5; attempt++)); do
        pid_alive "$pid" || break
        sleep 0.2 9>&-
    done
    if pid_alive "$pid"; then
        kill -KILL "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        return 124
    fi
    wait "$pid"
}

# Буфер обмена X: PRIMARY, CLIPBOARD и cut buffers корневого окна очищаются на обеих границах сессии человека (то, что он
# скопировал, бот не получит, и бот не оставит человеку своё). `xsel --clear` cut buffers не трогает, а x11vnc кладёт
# текст из буфера клиента в CUT_BUFFER0, поэтому CUT_BUFFER0..7 удаляются отдельно (xprop из x11-utils). Сбой, отсутствие
# или зависание xsel и xprop не мешают браузеру; зависший xprop прерывает обход (X-сервер не отвечает, ждать восемь раз бесполезно).
clear_clipboard() {
    local selection n status
    if command -v xsel >/dev/null 2>&1; then
        for selection in --primary --clipboard; do
            run_limited "$XSEL_TIMEOUT" /dev/null xsel --clear "$selection" \
                || echo "chromium-supervisor: clearing X selection $selection failed" >&2
        done
    else
        echo 'chromium-supervisor: xsel not found, X selections not cleared' >&2
    fi
    if command -v xprop >/dev/null 2>&1; then
        for ((n=0; n<8; n++)); do
            status=0
            run_limited "$XSEL_TIMEOUT" /dev/null xprop -root -remove "CUT_BUFFER$n" || status=$?
            if (( status == 124 )); then
                echo "chromium-supervisor: removing CUT_BUFFER$n timed out, other cut buffers skipped" >&2
                break
            elif (( status != 0 )); then
                echo "chromium-supervisor: removing CUT_BUFFER$n failed" >&2
            fi
        done
    else
        echo 'chromium-supervisor: xprop not found, X cut buffers not cleared' >&2
    fi
    return 0
}

human_session_is_live() {
    [[ -f "$HUMAN_SESSION" && ! -L "$HUMAN_SESSION" && -d "$HUMAN_ROOT" && ! -L "$HUMAN_ROOT" ]]
}

# Каталог можно возобновить только в своей сессии: маркер живой и его токен равен токену файла режима.
human_session_is_resumable() {
    local line=""
    [[ -n "$desired_token" ]] && human_session_is_live || return 1
    IFS= read -r line < "$HUMAN_SESSION" || true
    [[ "$line" == "session $desired_token" ]]
}

# Каталог человека удалён целиком (в том числе ссылка на его месте)? Ссылка удаляется сама, цель не трогается.
wipe_human_root() {
    rm -rf -- "$HUMAN_ROOT" 2>/dev/null || true
    [[ ! -e "$HUMAN_ROOT" && ! -L "$HUMAN_ROOT" ]]
}

# Вход в human, `enter_human_instance fresh|resume`. fresh (переключение с bot): каталог и маркер стираются всегда.
# resume (старт супервизора, падение Chromium, рестарт контейнера): та же сессия, если каталог на месте, а маркер несёт
# токен файла режима, иначе тоже чистый старт. Не удалось стереть старый каталог: возврат 1, человеческий Chromium не
# стартует.
enter_human_instance() {
    local how=$1 sub
    rm -f "$MERGE_STATUS"
    if [[ "$how" != resume ]] || ! human_session_is_resumable; then
        rm -f -- "$HUMAN_SESSION"
        if ! wipe_human_root; then
            echo 'chromium-supervisor: human profile could not be removed, human browser not started' >&2
            return 1
        fi
        clear_clipboard
    fi
    mkdir -p "$HUMAN_ROOT"
    for sub in cache crash tmp home xdg-config xdg-cache xdg-data xdg-state; do
        mkdir -p "$HUMAN_ROOT/$sub"
        chmod 0700 "$HUMAN_ROOT/$sub"
    done
    chmod 0700 "$HUMAN_ROOT"
    if ! human_session_is_resumable; then
        printf 'session%s\n' "${desired_token:+ $desired_token}" > "$HUMAN_SESSION"
        chmod 0600 "$HUMAN_SESSION"
    fi
}

# Итог слияния для лаунчера (`merged N` или `failed`). Каталог мог не удалиться, и тогда возврат повторяется: повтор находит
# уже перенесённые cookies и пишет `merged 0`. Первый успешный итог перехода остаётся до конца перехода (стирается при
# следующем входе в human), его не заменяет ни повторное слияние, ни его сбой; `failed` заменяется успешным повтором.
write_merge_status() {
    if [[ -f "$MERGE_STATUS" && ! -L "$MERGE_STATUS" ]] && grep -q '^merged ' "$MERGE_STATUS" 2>/dev/null; then
        return 0
    fi
    printf '%s\n' "$1" > "$MERGE_STATUS"
}

# Выход из human (и старт Chromium бота): persistent cookies доверенного каталога человека (есть маркер сессии) сливаются в
# профиль бота (cookie-merge.py, под python -I, не дольше $MERGE_TIMEOUT), каталог удаляется целиком, буфер очищается.
# Вызывается только когда Chromium не запущен. Сбой или таймаут слияния каталог не сохраняют: логин потерян, это видно по
# $MERGE_STATUS, утечки между профилями нет. Каталог не удалился: возврат 1, Chromium бота не стартует; маркер остаётся.
leave_human_instance() {
    local result="" status=0
    if [[ ! -e "$HUMAN_ROOT" && ! -L "$HUMAN_ROOT" && ! -e "$HUMAN_SESSION" && ! -L "$HUMAN_SESSION" ]]; then
        return 0
    fi
    if human_session_is_live; then
        run_limited "$MERGE_TIMEOUT" "$MERGE_OUT" "$PYTHON3" -I "$COOKIE_MERGE" "$HUMAN_ROOT" "$PROFILE" || status=$?
        result=$(head -c 300 "$MERGE_OUT" 2>/dev/null | tr '\n' ' ' 9>&-) || true
        rm -f "$MERGE_OUT"
        if (( status == 0 )) && [[ "$result" =~ ^merged\ [0-9]+\ ?$ ]]; then
            write_merge_status "${result% }"
        else
            (( status == 124 )) && result="timed out after ${MERGE_TIMEOUT}s"
            echo "chromium-supervisor: cookie merge failed: $result" >&2
            write_merge_status failed
        fi
    fi
    clear_clipboard
    if ! wipe_human_root; then
        echo 'chromium-supervisor: human profile could not be removed, bot browser not started' >&2
        return 1
    fi
    rm -f -- "$HUMAN_SESSION"
}

# prepare_instance РЕЖИМ ПРЕДЫДУЩИЙ: всё, что нужно сделать с каталогом человека до старта Chromium этого режима.
prepare_instance() {
    local mode=$1 previous=$2
    if [[ "$mode" == human ]]; then
        if [[ "$previous" == bot ]]; then
            enter_human_instance fresh
        else
            enter_human_instance resume
        fi
    else
        leave_human_instance
    fi
}

# start_chromium bot|human (каталог уже подготовлен prepare_instance). human: чистый экземпляр, ни порта отладки, ни
# pipe, ровно одна вкладка, все пути внутри $HUMAN_ROOT. bot: профиль бота с CDP на loopback. Оба экземпляра с одним
# --password-store=basic, иначе cookies не переживут перенос между профилями.
start_chromium() {
    local mode=$1 dir
    local -a common=(--window-size="${WIDTH},${HEIGHT}" --password-store=basic
        --no-first-run --no-default-browser-check --disable-dev-shm-usage)
    if [[ "$mode" == human ]]; then
        read_human_url
        dir=$HUMAN_ROOT
        rm -f "$dir/SingletonLock" "$dir/SingletonSocket" "$dir/SingletonCookie"
        # Без --no-sandbox: песочница Chromium держит user namespace (см. выше).
        env HOME="$dir/home" XDG_CONFIG_HOME="$dir/xdg-config" XDG_CACHE_HOME="$dir/xdg-cache" \
            XDG_DATA_HOME="$dir/xdg-data" XDG_STATE_HOME="$dir/xdg-state" TMPDIR="$dir/tmp" \
            chromium --user-data-dir="$dir" --disk-cache-dir="$dir/cache" --crash-dumps-dir="$dir/crash" \
            "${common[@]}" -- "$human_url" 9>&- &
    else
        dir=$PROFILE
        mkdir -p "$dir"
        chmod 0700 "$dir"
        rm -f "$dir/SingletonLock" "$dir/SingletonSocket" "$dir/SingletonCookie"
        chromium --user-data-dir="$dir" --remote-debugging-address=127.0.0.1 \
            --remote-debugging-port=9222 "${common[@]}" -- about:blank 9>&- &
    fi
    chromium_pid=$!
    active_mode=$mode
    chromium_started=$SECONDS
    printf '%s %s %s\n' "$mode" "$chromium_pid" "$(date +%s)" > "$ACTIVE.tmp"
    mv -f "$ACTIVE.tmp" "$ACTIVE"
    if kill -0 "${children[1]}" "${children[2]}" 2>/dev/null && pid_alive "$chromium_pid"; then
        touch "$READY"
    fi
}

pause=1
while true; do
    rm -f "$READY" "$ACTIVE"
    chromium_pid=""
    active_mode=""
    started=$SECONDS
    Xvfb "$DISPLAY" -screen 0 "${SIZE}x24" -nolisten tcp -auth "$XAUTHORITY" 9>&- &
    children=("$!")
    socket="$X_SOCKET"
    for ((i=0; i<50; i++)); do
        [[ -S "$socket" ]] && break
        kill -0 "${children[0]}" 2>/dev/null || break
        sleep 0.2 9>&-
    done
    chmod 0700 "$X_SOCKET_DIR"
    if [[ -S "$socket" ]] && kill -0 "${children[0]}" 2>/dev/null; then
        # Оконный менеджер с конфигом образа (root, read-only): без привязок клавиш, Execute и меню приложений.
        openbox --config-file "$OPENBOX_RC" 9>&- & children+=("$!")
        rm -f "$VNC_SOCKET"
        # Без -display: x11vnc берёт DISPLAY из окружения, а двоеточие в аргументе EXEC socat разбирает как разделитель.
        socat "UNIX-LISTEN:$VNC_SOCKET,fork,mode=0600" "EXEC:x11vnc -inetd -nopw -quiet" 9>&- &
        children+=("$!")
        # Xvfb, openbox и x11vnc живут весь цикл стека: смена режима и падение Chromium перезапускают только Chromium.
        while true; do
            for pid in "${children[@]}"; do
                if ! pid_alive "$pid"; then
                    break 2
                fi
            done
            read_mode
            if (( SECONDS < blocked_until )); then
                : # каталог человека не удалялся: пауза перед повтором, Chromium не запущен
            elif [[ "$desired" != "$active_mode" ]]; then
                # Смена режима (или первый запуск). Старый Chromium остановлен полностью до запуска нового.
                previous=${active_mode:-$pending_previous}
                pending_previous=""
                stop_chromium
                active_mode=""
                if [[ "$desired" == invalid ]]; then
                    # Chromium не стартует; active_mode=invalid, чтобы не разбирать файл заново каждые 0.2 секунды.
                    echo "chromium-supervisor: $MODE_FILE has unknown content, browser stays stopped" >&2
                    active_mode=invalid
                elif prepare_instance "$desired" "$previous"; then
                    # Перехват (previous bot): чистый каталог. После рестарта стека или контейнера (previous пуст)
                    # каталог человека остаётся, пока идёт та же сессия (маркер).
                    start_chromium "$desired"
                else
                    # Каталог человека не удалился: никакого Chromium, повтор через $BLOCKED_RETRY секунд.
                    pending_previous=$previous
                    blocked_until=$((SECONDS + BLOCKED_RETRY))
                fi
            elif [[ "$active_mode" != invalid ]] && ! pid_alive "$chromium_pid"; then
                # Chromium упал: перезапуск только его, в том же режиме, с тем же ростом паузы.
                stop_chromium
                if (( SECONDS - chromium_started >= 60 )); then pause=1; fi
                sleep "$pause" 9>&-
                (( pause = pause < 16 ? pause * 2 : 30 ))
                read_mode
                if [[ "$desired" == "$active_mode" ]]; then
                    if prepare_instance "$active_mode" "$active_mode"; then
                        start_chromium "$active_mode"
                    else
                        pending_previous=$active_mode
                        active_mode=""
                        blocked_until=$((SECONDS + BLOCKED_RETRY))
                    fi
                else
                    # Режим сменился за время паузы: предыдущий режим сохраняется, иначе вход в human после падения
                    # Chromium бота считался бы возобновлением, а не перехватом.
                    pending_previous=$active_mode
                    active_mode=""
                fi
                continue
            fi
            sleep 0.2 9>&-
        done
    fi
    rm -f "$READY" "$ACTIVE"
    stop_chromium
    for pid in "${children[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done
    wait 2>/dev/null || true
    children=()
    if (( SECONDS - started >= 60 )); then pause=1; fi
    sleep "$pause" 9>&-
    (( pause = pause < 16 ? pause * 2 : 30 ))
done
