# Botstead Architecture

This document describes the architectural components, trust boundaries, data models, and runtime flows of Botstead.

---

## 1. Components and Trust Boundaries

Botstead is structured into isolated service domains to minimize attack surfaces.

```
+-----------------------------------------------------------------------------------+
|                                 TRUST BOUNDARY 0: WEB                             |
|                                                                                   |
|  [ PWA / Web Client ] <==== HTTPS / WSS ====> [ Reverse Proxy (Nginx) ]           |
+------------------------------------------------------+----------------------------+
                                                       |
                                                       | Internal HTTP / WSS
                                                       v
+-----------------------------------------------------------------------------------+
|                                 TRUST BOUNDARY 1: CORE                            |
|                                                                                   |
|  [ Botstead Core (FastAPI :8080) ]                                                |
|   - Authentication, Sessions & RBAC                                               |
|   - Turn Scheduler, Event Log & Risk Classifier                                   |
|   - Model API Gateway (Token Validation, Secret Decryption & Egress Firewall)     |
|   - NO docker.sock access                                                         |
|                                                                                   |
|   +----------------------------+        +-------------------------------------+   |
|   | PostgreSQL (Schema bothub) |        | Unix Domain Socket                  |   |
|   | Isolated internal db_net   |        | Bearer LAUNCHER_SECRET              |   |
|   +----------------------------+        +------------------+------------------+   |
+------------------------------------------------------------|----------------------+
                                                             |
                                                             v
+-----------------------------------------------------------------------------------+
|                                 TRUST BOUNDARY 2: LAUNCHER                        |
|                                                                                   |
|  [ Botstead Launcher Daemon ]                                                     |
|   - Holds /var/run/docker.sock                                                    |
|   - Runs on Host Network with NET_ADMIN & NET_RAW                                 |
|   - Enforces static configuration (launcher.toml) and validates all API inputs    |
|   - Manages Host iptables rules (DOCKER-USER and INPUT chains)                    |
+------------------------------------------------------------+----------------------+
                                                             |
                                                             | Docker API
                                                             v
+-----------------------------------------------------------------------------------+
|                                 TRUST BOUNDARY 3: CONTAINERS                      |
|                                                                                   |
|  Per-User Bridge Network: bothub-u-<owner> (IPv6 disabled)                        |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  | Bot Container: bot-<id>                                                     |  |
|  |  - Read-only root filesystem, tmpfs on /tmp and /run                         |  |
|  |  - No host volume mounts, memory/CPU/PIDs limits enforced                   |  |
|  |                                                                             |  |
|  |  [ UID 1000: bot ]                                                          |  |
|  |   - Executes CLI runners and bot commands                                   |  |
|  |   - Restricted by bot-guard seccomp filter (no user namespaces)             |  |
|  |   - Cannot access /home/browser (permissions 0700)                          |  |
|  |                                                                             |  |
|  |  [ UID 1001: browser ]                                                      |  |
|  |   - Runs Xvfb, hardened Openbox, and Chromium                               |  |
|  |   - Chromium sandboxed via user namespaces (enabled by seccomp profile)     |  |
|  |   - RFB screen server exposed only via local unix socket                    |  |
|  +-----------------------------------------------------------------------------+  |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  | Login Container: login-<owner>                                              |  |
|  |  - Ephemeral container for subscription CLI OAuth authentication            |  |
|  |  - Mounts dedicated volume bothub-login-<owner> with write access            |  |
|  +-----------------------------------------------------------------------------+  |
+-----------------------------------------------------------------------------------+
```

### Who Holds `docker.sock`

Access to `/var/run/docker.sock` is strictly isolated to the **Launcher** service.

- **Core Application**: Does not mount or access `docker.sock`. The core communicates with the launcher exclusively through a local unix domain socket (`/run/bothub-launcher/launcher.sock`), authenticated with a static bearer secret (`LAUNCHER_SECRET`).
- **Launcher Daemon**: Runs on the host network with `NET_ADMIN` and `NET_RAW` capabilities. It controls Docker container lifecycles, creates per-user bridge networks, and installs iptables filter rules in the host's `DOCKER-USER` and `INPUT` chains.
- **Static Enforcement**: The launcher rejects dynamic runtime changes. Container images, mounts, resource limits, DNS settings, and security profiles are defined strictly in `launcher.toml`. Unexpected parameters in API requests return HTTP 400.

### Dual-User Model in the Bot Container (UID 1000 vs UID 1001)

Bot containers enforce strict separation of duties between two non-root user accounts:

1. **UID 1000 (`bot`)**:
   - Executes bot tooling, CLI runners (Claude Code, OpenAI Codex, Antigravity CLI), and commands requested by the AI model.
   - Home directory: `/home/bot`.
   - **`bot-guard` Syscall Filter**: Every command executed under UID 1000 and the container entrypoint are wrapped with the static `bot-guard` binary. This seccomp filter blocks `CLONE_NEWUSER`, `unshare`, and `setns`, preventing bot code from creating nested user namespaces or escalating privileges.
   - **File Isolation**: UID 1000 cannot read or write to `/home/browser` (file mode 0700, owned by UID 1001).

2. **UID 1001 (`browser`)**:
   - Runs Xvfb (virtual display `:99`), the Openbox window manager, x11vnc, and the Chromium browser.
   - Home directory: `/home/browser` (mounted on a dedicated persistent volume).
   - **User Namespaces Sandbox**: Chromium requires user namespaces (`clone` with `CLONE_NEWUSER`) to build its internal sandbox without `setuid` or `CAP_SYS_ADMIN`. The container's outer seccomp profile (`bot.json`) permits user namespace creation for the container, but `bot-guard` restricts this capability strictly to UID 1001.
   - **Socket Isolation**: The x11vnc server does not bind a TCP port. It listens exclusively on a unix domain socket (`/home/browser/.vnc/rfb.sock`) with mode 0600 inside a mode 0700 directory.

### Model API Gateway and Secret Protection

Botstead ensures that third-party provider API keys never enter the bot container.

- **Encrypted Storage**: Provider API keys are encrypted at rest in PostgreSQL using AES-256-GCM. Keys are managed by an encryption keyring (`BOTHUB_SECRET_KEYS`) with key rotation support. Plaintext keys never leave the core process.
- **Internal Gateway Endpoint**: Bots communicate with upstream LLM APIs through the internal gateway URL (`BOTHUB_INTERNAL_URL = http://core:8080/gateway/<provider_id>/`). Nginx returns HTTP 404 for any public requests to `/bots/gateway/`.
- **Turn-Scoped HMAC Tokens**: For each turn, the core issues a short-lived HMAC token (`gw:<bot_id>:<provider_id>:<turn_id>:<expires>:<hmac>`) signed with `BOTHUB_GATEWAY_TOKEN_SECRET`.
- **Gateway Validation**:
  - Validates the token signature and expiration against the active turn ID and bot ID.
  - Verifies that the provider belongs to the bot's owner and that the owner account is active.
  - Strips incoming auth headers and query parameters (`key`, `api_key`, `access_token`).
  - Validates requested models against the provider's allowed model list.
  - Injects the decrypted provider secret into upstream request headers.
  - Records token usage (`tokens_in`, `tokens_out`, `tokens_cache_read`, `tokens_cache_write`) and enforces daily token budgets.
- **SSRF and DNS Rebinding Prevention**:
  - Resolves upstream hostnames and pins verified IP addresses.
  - Rejects loopback, link-local, cloud metadata (`169.254.169.254`, `fd00:ec2::254`), core network subnets, and prohibited CIDRs.
  - Private IP addresses (RFC 1918, CGNAT, ULA) are blocked unless explicitly approved by an administrator (`allow_private_ips`). If DNS returns a changed private IP, the gateway rejects the request with HTTP 502 and flags the provider for re-approval.

### Browser Control State Machine

Botstead provides safe browser automation and interactive human takeover through a four-state machine: `bot`, `human`, `returning`, and `bot`.

```
                    takeover (POST /api/bots/{id}/browser/takeover)
              +--------------------------------------------------------+
              |                                                        |
              v                                                        |
       +--------------+                                         +--------------+
       |     bot      |                                         |    human     |
       |  (Default)   |                                         |  (Operator)  |
       +--------------+                                         +--------------+
              ^                                                        |
              |                                                        | return
              | snapshot (POST /api/browser/step)                      | (POST /api/bots/{id}/browser/return)
              |                                                        v
       +--------------+                                         +--------------+
       |     bot      | <------------- [ unfreeze ] <-----------|  returning   |
       |  (Snapshot)  |                                         | (Transition) |
       +--------------+                                         +--------------+
```

1. **State `bot` (Default)**:
   - The AI model controls Chromium via Playwright MCP interacting with Chrome DevTools Protocol (CDP) on `127.0.0.1:9222`.
   - The RFB screen stream is read-only for humans. The WebSocket input proxy drops all `KeyEvent`, `PointerEvent`, and `ClientCutText` messages.

2. **Transition `bot -> human` (Takeover)**:
   - Operator initiates takeover via `POST /api/bots/{id}/browser/takeover`.
   - Core queries the launcher for the URL of the active browser tab (or falls back to the last navigation event). Prohibited URLs are replaced with `about:blank`.
   - Launcher executes `freeze_bot`: terminates active UID 1000 processes and blocks new `docker exec` sessions.
   - Launcher switches browser mode to `human`: stops the bot's Chromium instance and starts a clean Chromium instance without CDP or remote debugging ports in `/home/browser/.config/botstead-browser-human`.
   - Core transitions state to `human`. Active turns are stopped, and browser approvals are expired.
   - The RFB proxy unblocks input events. The operator interacts directly with the clean browser.

3. **Transition `human -> returning` (Return)**:
   - Operator returns control via `POST /api/bots/{id}/browser/return`.
   - Core records state `returning` and immediately closes RFB input processing.
   - Launcher terminates the operator's Chromium instance.
   - Launcher runs `cookie_merge.py` under `python3 -I` with `trusted_schema=OFF` to merge persistent SQLite cookies from the operator profile into the bot profile.
   - Launcher deletes the operator profile directory, clears X11 selection clipboards (`xsel --clear`), and wipes X11 cut buffers (`xprop -root -remove`).
   - Launcher starts the bot's Chromium instance with CDP enabled and executes `unfreeze_bot`.

4. **Transition `returning -> bot` (Re-synchronization)**:
   - The bot resumes and executes `browser_snapshot` on the active turn.
   - Core verifies the snapshot with a single-use authorization token and transitions state back to `bot`.

### Recorded Procedures

Procedures represent structured sequences of browser actions that can be recorded, parameterized, and executed automatically.

- **Step Definitions**: Steps define discrete actions (`navigate`, `click`, `fill`, `press`, `select`, `wait`, `assert`). Targets are identified by semantic role and accessible name, or CSS selectors.
- **Vault Secret References**: Sensitive parameters use `secret_ref: "vault:<secret_name>"`. Secrets are resolved by the core at execution time from encrypted storage and are never saved in procedure definitions, step logs, or prompt contexts.
- **Preconditions and Expectations**: Each step can enforce regex URL patterns and element visibility. URL patterns are checked against ReDoS attacks and evaluated in a separate process with a strict 200 ms timeout.
- **Execution Lifecycle**:
  - `queued` -> `running` -> `done`.
  - Risky actions pause in `waiting_approval`.
  - Missing elements or failed preconditions transition to `waiting_model` for self-healing, or `waiting_human` for operator resolution.

---

## 2. Data Model Overview

Botstead stores all state in PostgreSQL 16 under the `bothub` schema. All user-facing entities are scoped by `owner_id`.

```
                            +-------------------+
                            |       users       |
                            +---------+---------+
                                      |
         +----------------------------+----------------------------+
         | 1:N                        | 1:N                        | 1:N
         v                            v                            v
+-----------------+          +-----------------+          +-----------------+
|      bots       |          |    providers    |          |   procedures    |
+--------+--------+          +--------+--------+          +--------+--------+
         |                            |                            |
         | 1:N                        | 1:N                        | 1:N
         v                            v                            v
+-----------------+          +-----------------+          +-----------------+
|     threads     |          |     models      |          | procedure_runs  |
+--------+--------+          +-----------------+          +-----------------+
         |
         | 1:N
         v
+-----------------+
|      turns      |
+--------+--------+
         |
         +----------------------------+
         | 1:N                        | 1:N
         v                            v
+-----------------+          +-----------------+
|     events      |          |    approvals    |
+-----------------+          +-----------------+
```

### Table Summary

| Table | Purpose |
|---|---|
| `users` | User accounts with email (lowercase), Argon2id password hash, role (`admin`, `member`), and status (`active`, `disabled`). |
| `invites` | Cryptographic invite tokens (`token_hash = sha256(token)`), creator reference, expiration, and redemption tracking. |
| `sessions` | Cookie sessions (`id_hash = sha256(cookie)`), user reference, sliding expiration, and revocation status. |
| `mac_tokens` | Authentication tokens for macOS agents tied to specific users. |
| `providers` | Configured AI model providers (Anthropic, OpenAI, OpenAI-compatible, Google, CLI subscription), encrypted secrets, and status. |
| `models` | Models discovered or registered under specific providers, context window limits, and manual disable flags. |
| `secrets` | Encrypted key-value credentials for procedures and browser automation (`vault:<name>`), scoped to user or bot. |
| `bots` | Bot definitions (name, role, instructions, avatar, provider binding, model binding, auto_allow rules, tool modes, daily budget). |
| `threads` | Conversation threads belonging to a bot and owner, tracking session IDs, dry-run mode, and sequential event numbering. |
| `turns` | Individual execution turns within a thread, tracking turn status (`queued`, `running`, `waiting_approval`, `done`, `error`), lease deadlines, and error messages. |
| `events` | Append-only audit log of turn activity (`user_msg`, `assistant_msg`, `tool_call`, `tool_result`, `browser_step`, `approval_req`, `usage`). |
| `approvals` | Action approval requests (`pay`, `send`, `delete`, `login`, `push`, `other`), canonical argument hashes, decisions, and expiration. |
| `memory` | Persistent facts and memory items with status (`proposed`, `active`, `archived`), versioning, and per-bot or shared visibility. |
| `usage` | Bigint token accounting records (`tokens_in`, `tokens_out`, `tokens_cache_read`, `tokens_cache_write`) linked to bots, providers, and turns. |
| `schedules` | Scheduled bot jobs triggered by cron expressions, webhook tokens, or local Mac folder watches. |
| `files` | File metadata and storage paths for uploaded, generated, or retrieved thread attachments. |
| `outbox` | Reliable notification dispatch queue for Web Push and Telegram messages with exponential backoff delivery. |
| `push_subscriptions`| Web Push VAPID subscription endpoints and crypto keys per user device. |
| `mac_status` | Connection state (`online`, `sleep`, `offline`, `locked`, `needs_permission`) and telemetry for linked Mac agents. |
| `procedures` | Reusable browser procedure definitions, step arrays, input parameters, and versioning. |
| `procedure_runs` | Individual executions of browser procedures, step snapshot copies, execution logs, and recovery states. |

### Integrity and Composite Foreign Keys

Cross-user data contamination is blocked at the database constraint level:
- Composite foreign key `bots(provider_id, owner_id) REFERENCES providers(id, owner_id)` ensures a bot can only bind to a provider owned by the same user.
- Composite foreign key `bots(model_id, provider_id) REFERENCES models(id, provider_id)` ensures a bot's model belongs strictly to its bound provider.
- All primary user-owned tables enforce `owner_id UUID NOT NULL REFERENCES users(id)`.

---

## 3. Request Flow of One Bot Turn

The following trace details the end-to-end lifecycle of a single bot execution turn.

```
PWA User           Core API           Database          Runner (Launcher)       Gateway            LLM API
   |                  |                  |                      |                  |                  |
   | 1. POST Turn     |                  |                      |                  |                  |
   |----------------->|                  |                      |                  |                  |
   |                  | 2. Insert Turn   |                      |                  |                  |
   |                  |    & user_msg    |                      |                  |                  |
   |                  |----------------->|                      |                  |                  |
   | 201 Turn queued  |                  |                      |                  |                  |
   |<-----------------|                  |                      |                  |                  |
   |                  |                  |                      |                  |                  |
   |                  | 3. Worker claims |                      |                  |                  |
   |                  |    turn & lease  |                      |                  |                  |
   |                  |----------------->|                      |                  |                  |
   |                  |                  |                      |                  |                  |
   |                  | 4. Issue Turn Token                     |                  |                  |
   |                  |    Start Runner Stream                  |                  |                  |
   |                  |---------------------------------------->|                  |                  |
   |                  |                  |                      |                  |                  |
   |                  |                  |                      | 5. POST Request  |                  |
   |                  |                  |                      |    with Token    |                  |
   |                  |                  |                      |----------------->|                  |
   |                  |                  |                      |                  | 6. Validate Token|
   |                  |                  |                      |                  |    Inject Secret |
   |                  |                  |                      |                  |----------------->|
   |                  |                  |                      |                  | 7. Stream Resp   |
   |                  |                  |                      |                  |<-----------------|
   |                  |                  |                      |<-----------------|                  |
   |                  | 8. Tool Call Event                      |                  |                  |
   |                  |<----------------------------------------|                  |                  |
   |                  | 9. Classify Risk |                      |                  |                  |
   |                  |    (Auto-allow / Approval)              |                  |                  |
   | 10. WS Event     |                  |                      |                  |                  |
   |<-----------------|                  |                      |                  |                  |
   |                  |                  |                      |                  |                  |
   |                  | 11. Record Final |                      |                  |                  |
   |                  |     Usage & Done |                      |                  |                  |
   |                  |----------------->|                      |                  |                  |
   | 12. Turn Done    |                  |                      |                  |                  |
   |<-----------------|                  |                      |                  |                  |
```

### Turn Execution Steps

1. **Submission**: The user submits a prompt via `POST /api/threads/{id}/turns`.
2. **Turn Creation**: The core logs an initial `user_msg` event in `events` and creates a `turns` record with `status = 'queued'`.
3. **Turn Claiming**: The background worker claims the turn, verifies that the bot's daily token budget is not exhausted, sets `status = 'running'`, and acquires a 60-second lease with periodic renewals.
4. **Context Preparation & Launch**:
   - The core compiles instructions, relevant active memories, and tool configurations.
   - It generates a signed gateway HMAC token for the turn.
   - It invokes the runner via the launcher's streaming execution API (`POST /v1/bots/{id}/exec`).
5. **Model Query**: The CLI runner inside the bot container sends LLM requests to `BOTHUB_INTERNAL_URL` using the signed gateway token.
6. **Gateway Processing**: The internal gateway authenticates the token, verifies active turn status, checks target IP rules, decrypts the upstream API key, and forwards the payload to the LLM provider.
7. **Streaming Response**: The upstream provider streams chunks back through the gateway to the runner.
8. **Tool Invocations & Risk Assessment**:
   - When the model emits a tool call, the runner requests execution.
   - The core risk classifier evaluates the tool name and arguments.
   - **Safe Tools** (reading files, workspace grep, web search) execute immediately if permitted by policy.
   - **High-Risk Actions** (`pay`, `delete`, `login`, `send`, `push`) or write actions without matching `auto_allow` rules pause the turn in `waiting_approval` and emit an `approval_req` event.
   - The operator approves or rejects the action in the PWA. Upon approval, the turn resumes in `running`.
9. **Event Streaming**: As the runner emits messages, plan steps, and tool outputs, the core persists each event in PostgreSQL and broadcasts it to connected clients over WebSocket (`/api/ws`).
10. **Completion**: When the model finishes its response, the core records final token usage in `usage`, releases the lease, marks the turn `status = 'done'`, and enqueues push notifications in `outbox`.
