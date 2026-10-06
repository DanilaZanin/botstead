# bot-guard (второй seccomp-фильтр для uid 1000, docs/isolation.md): gcc нужен только в этой стадии,
# в итоговый образ попадает один статический бинарник.
FROM ubuntu:24.04 AS guard-build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY bot-image/bot-guard.c /build/bot-guard.c
RUN gcc -O2 -static -Wall -Wextra -Werror -o /build/bot-guard /build/bot-guard.c \
    && strip /build/bot-guard \
    && install -m 0755 -o root -g root -D /build/bot-guard /out/usr/local/libexec/bot-guard

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH="/opt/venv/bin:$PATH"
# Rootfs контейнера read-only (лаунчер), поэтому ни .pyc, ни автообновления CLI писать некуда.
ENV PYTHONDONTWRITEBYTECODE=1
ENV DISABLE_AUTOUPDATER=1

# Install base dependencies. sudo намеренно нет: бот работает без root и без повышения привилегий.
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common \
    curl \
    wget \
    gnupg \
    xvfb \
    xauth \
    x11-utils \
    xsel \
    procps \
    util-linux \
    openbox \
    x11vnc \
    socat \
    python3 \
    python3-venv \
    python3-pip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Add NodeSource repository for Node.js 22
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list

# Add PPA for Chromium
# In Ubuntu 24.04, the standard chromium-browser package is a snap, which is problematic inside Docker.
# We use the xtradeb/apps PPA as the simplest working way to get a deb-packaged Chromium.
RUN add-apt-repository -y ppa:xtradeb/apps

RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    chromium \
    && rm -rf /var/lib/apt/lists/*

# Add bot (uid 1000 для лаунчера) и browser (uid 1001); оба без sudo.
RUN (userdel -r ubuntu 2>/dev/null || true) && useradd -m -s /bin/bash -u 1000 bot \
    && useradd -m -U -s /bin/bash -u 1001 browser \
    && chmod 0700 /home/browser \
    && mkdir -p /home/browser/.config /etc/chromium/policies/managed \
    && chown -R browser:browser /home/browser

# Политики Chromium (root, 0644, rootfs read-only): нет расширений и native messaging, DownloadRestrictions=3 закрывает
# загрузки в обычной вкладке. Через CDP они НЕ закрыты: Browser.setDownloadBehavior обходит политику и пишет в
# /home/browser. DefaultDownloadDirectory указывает на noexec-tmpfs (его монтирует лаунчер): загрузки по умолчанию не
# окажутся там, откуда их запустит или подхватит автозапуск.
COPY bot-image/chromium-policy.json /etc/chromium/policies/managed/bothub.json
RUN chmod 0644 /etc/chromium/policies/managed/bothub.json

# Конфиг openbox экрана бота: без привязок клавиш, Execute и меню приложений (root, read-only).
RUN mkdir -p /etc/bothub
COPY bot-image/openbox/rc.xml /etc/bothub/openbox-rc.xml
COPY bot-image/openbox/menu.xml /etc/bothub/openbox-menu.xml
RUN chown -R root:root /etc/bothub && chmod 0755 /etc/bothub && chmod 0644 /etc/bothub/*

# Install global npm packages with pinned versions via ARG
ARG CLAUDE_CODE_VERSION=2.1.281
ARG CODEX_VERSION=0.156.1
ARG PLAYWRIGHT_MCP_VERSION=0.0.82

RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} @openai/codex@${CODEX_VERSION} @playwright/mcp@${PLAYWRIGHT_MCP_VERSION} \
    && npm cache clean --force

# Antigravity CLI (agy): необязательный слой. Скачивается только при заданных AGY_URL и AGY_SHA256,
# сумма проверяется до установки. Без обоих аргументов слой пропускается (раннер gemini тогда недоступен).
# AGY_URL ведёт либо на сам бинарник, либо на архив .tar.gz/.tgz (расширение берётся из пути, без query):
# сумма в обоих случаях считается от скачанного файла, из архива ставится первый обычный файл с именем agy или
# antigravity (под именем /usr/local/bin/agy).
# Как получить сумму: deploy/README.md, раздел "Образ бота".
ARG AGY_URL=""
ARG AGY_SHA256=""
RUN if [ -n "$AGY_URL" ] || [ -n "$AGY_SHA256" ]; then \
        if [ -z "$AGY_URL" ] || [ -z "$AGY_SHA256" ]; then echo "AGY_URL and AGY_SHA256 must be set together" >&2; exit 1; fi; \
        curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/agy "$AGY_URL" \
        && echo "${AGY_SHA256}  /tmp/agy" | sha256sum -c - \
        || exit 1; \
        case "${AGY_URL%%[?#]*}" in \
            *.tar.gz|*.tgz) \
                mkdir /tmp/agy-unpack \
                && tar -xzf /tmp/agy -C /tmp/agy-unpack --no-same-owner \
                && bin="$(find /tmp/agy-unpack -type f \( -name agy -o -name antigravity \) | head -n 1)" \
                && if [ -z "$bin" ]; then echo "no file named agy or antigravity in the AGY_URL archive" >&2; exit 1; fi \
                && install -m 0755 "$bin" /usr/local/bin/agy \
                && rm -rf /tmp/agy /tmp/agy-unpack ;; \
            *) \
                install -m 0755 /tmp/agy /usr/local/bin/agy \
                && rm -f /tmp/agy ;; \
        esac; \
    else \
        echo "agy skipped: AGY_URL and AGY_SHA256 are empty"; \
    fi

# Setup Python virtual environment (принадлежит root: бот не может подменить свой интерпретатор)
RUN python3 -m venv /opt/venv

# Copy and install bothub core package so `python -m bothub.mcp_server` works
WORKDIR /tmp/bothub-core
COPY core/ /tmp/bothub-core/
RUN /opt/venv/bin/pip install --no-cache-dir ./ \
    && rm -rf /tmp/bothub-core

# bot-guard: root:root 0755, лаунчер ставит его префиксом перед каждой командой под uid 1000, entrypoint тоже.
COPY --from=guard-build /out/usr/local/libexec/bot-guard /usr/local/libexec/bot-guard
RUN chown root:root /usr/local/libexec/bot-guard && chmod 0755 /usr/local/libexec/bot-guard

# Слияние cookies при возврате управления (chromium-supervisor.sh запускает его под uid 1001 через python3): root:root 0644,
# rootfs read-only, подменить скрипт нельзя.
COPY bot-image/cookie_merge.py /usr/local/libexec/cookie-merge.py
RUN chown root:root /usr/local/libexec/cookie-merge.py && chmod 0644 /usr/local/libexec/cookie-merge.py

# Исполнитель шагов процедур (лаунчер запускает его под uid 1001: node /usr/local/libexec/procedure-step.mjs, данные шага в
# stdin). root:root 0644, rootfs read-only. Playwright берётся из глобального @playwright/mcp (playwright-core).
COPY bot-image/procedure-step.mjs /usr/local/libexec/procedure-step.mjs
RUN chown root:root /usr/local/libexec/procedure-step.mjs && chmod 0644 /usr/local/libexec/procedure-step.mjs

# Setup entrypoint
COPY bot-image/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY bot-image/chromium-supervisor.sh /usr/local/bin/chromium-supervisor.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/chromium-supervisor.sh

# Ни одного setuid/setgid файла: no-new-privileges их и так гасит, но образ чистим тоже.
RUN find / -xdev -type f \( -perm -4000 -o -perm -2000 \) -exec chmod a-s {} + 2>/dev/null || true

USER bot
WORKDIR /home/bot
ENV HOME=/home/bot

# Порты не публикуются. Весь entrypoint (bash и его дети: cp, grep, mkdir) идёт под bot-guard: второй seccomp-фильтр
# стоит с первой инструкции, окна без него нет. Лаунчер запускает бота без команды и без --entrypoint
# (docker_args.bot_run_args), поэтому ENTRYPOINT образа действует как есть. Логин-контейнер и копирование тома
# задают свой --entrypoint (sleep, sh) и этот не используют.
ENTRYPOINT ["/usr/local/libexec/bot-guard", "/usr/local/bin/entrypoint.sh"]
