# Quick Start Guide

This guide walks through deploying Botstead on a Linux host with Docker.

All commands assume you are working from the repository root or the `deploy/` directory as specified.

## Current capabilities

- **Telegram:** Bots receive messages from allowed chats and send replies back ([contract, section 23](contracts.md#contract-23)).
- **Slack replies:** A completed hook turn can answer in the event's channel and thread when a Bot Token is set ([section 18](contracts.md#contract-18)).
- **Mailgun inbound email:** Signed JSON mail starts a bot turn ([section 18](contracts.md#contract-18)).
- **Multiple Macs:** An owner can register separate Mac agents with their own tokens ([section 5](contracts.md#contract-5)).
- **Template catalog:** Ready-made configurations from `templates/` appear when creating a bot ([section 9](contracts.md#contract-9)).
- **Group chat:** 2–6 bots discuss a message in rounds in one shared thread ([section 21](contracts.md#contract-21)).
- **Model pricing:** The usage summary estimates dollar costs and marks incomplete estimates ([section 2](contracts.md#contract-2)).
- **CSV export:** The owner can download the activity feed for 1, 7, 30, or 90 days ([section 16](contracts.md#contract-16)).
- **Proactive suggestions:** A bot proposes up to three actions for the owner to accept or dismiss ([section 22](contracts.md#contract-22)).
- **Input limits:** The API rejects fields that exceed their length limits ([section 13](contracts.md#contract-13)).
- **Quotas panel:** API-backed bots show their token and estimated dollar usage for the day and month ([section 2](contracts.md#contract-2)).
- **Bot templates:** Export and import bot settings, cron schedules, and procedures without secrets ([section 9](contracts.md#contract-9)).
- **GitHub and Slack triggers:** Signed hook events start bot turns ([section 18](contracts.md#contract-18)).
- **`mcp_allow`:** The owner controls which third-party MCP tools a bot may use ([section 4](contracts.md#contract-4)).
- **Self-wakeup:** A bot schedules a future turn that the owner can view or cancel ([section 17](contracts.md#contract-17)).
- **Delegation:** A bot assigns a task to another bot owned by the same user and reads its result ([section 19](contracts.md#contract-19)).
- **Action checker:** An optional model checks risky actions before owner approval ([section 20](contracts.md#contract-20)).

---

## 1. Prerequisites

Ensure your host meets the following requirements:
- **Operating System**: Linux (Ubuntu 22.04+, Debian 12+, or compatible distribution).
- **Container Engine**: Docker Engine 24+ with `iptables` support enabled in the daemon.
- **Kernel Module**: `br_netfilter` loaded and configured so bridge traffic passes through host iptables.
- **User Namespaces**: Enabled for unprivileged processes (`sysctl user.max_user_namespaces` greater than 0).

### Prepare Host Kernel and Bridge Filtering

Run the following commands on the host:

```bash
sudo modprobe br_netfilter
echo br_netfilter | sudo tee /etc/modules-load.d/br_netfilter.conf
sudo sysctl -w net.bridge.bridge-nf-call-iptables=1
echo "net.bridge.bridge-nf-call-iptables = 1" | sudo tee -a /etc/sysctl.d/99-bothub.conf
```

Verify user namespace support:

```bash
sysctl user.max_user_namespaces
```

If the value is 0, enable it:

```bash
sudo sysctl -w user.max_user_namespaces=28633
echo "user.max_user_namespaces = 28633" | sudo tee -a /etc/sysctl.d/99-bothub.conf
```

---

## 2. Preflight Check

Navigate to the `deploy/` directory and run the preflight verification script:

```bash
cd deploy
./preflight.sh
```

The script verifies:
- `br_netfilter` availability and sysctl configuration.
- The presence of the `DOCKER-USER` iptables chain.
- The matching iptables backend (nftables vs legacy).
- Absence of conflicting IPv6 settings on Docker default bridges.

If any check fails, the script outputs the exact corrective command. Resolve any reported issues before proceeding.

---

## 3. Environment Configuration

From the `deploy/` directory, copy the environment templates:

```bash
cp .env.example .env
cp db.env.example db.env
```

### Configure Secrets (`deploy/.env`)

Generate cryptographically secure random values:

```bash
# Generate 32-byte hex secrets
openssl rand -hex 32   # for LAUNCHER_SECRET
openssl rand -hex 32   # for BOT_TOKEN_SECRET
openssl rand -hex 32   # for BOTHUB_GATEWAY_TOKEN_SECRET

# Generate base64 key for BOTHUB_SECRET_KEYS (provider API key encryption)
openssl rand -base64 32 | tr -d '\n'
```

Edit `deploy/.env` and configure:
- `DATABASE_URL`: PostgreSQL connection string, matching values in `db.env` (e.g., `postgres://bothub:your_db_password@db:5432/bothub`).
- `LAUNCHER_SECRET`: Minimum 32 characters hex string generated above.
- `BOT_TOKEN_SECRET`: Hex secret for HMAC bot tokens.
- `BOTHUB_GATEWAY_TOKEN_SECRET`: Hex secret for model gateway per-turn tokens.
- `BOTHUB_SECRET_KEYS`: `1:<base64_string>` using the base64 key generated above.
- `BOTHUB_INTERNAL_URL`: Internal gateway address, keep as `http://core:8080`.
- `BOTHUB_RUNNER_EXEC`: Set to `docker`.
- `FILES_DIR`: File storage path, defaults to `/data/files`.
- `BOTHUB_PUBLIC_ORIGIN` (optional): Set this to the public origin, such as `https://your-domain.example.com`, for Telegram webhook registration.

### Configure Database (`deploy/db.env`)

Edit `deploy/db.env` and set:
- `POSTGRES_USER`: Database username (e.g., `bothub`).
- `POSTGRES_PASSWORD`: Database password (must match `DATABASE_URL` in `.env`).
- `POSTGRES_DB`: Database name (e.g., `bothub`).

---

## 4. Launcher Configuration

Inspect `deploy/launcher.toml`.

1. **Check iptables backend**:
   Run `iptables --version` on the host. If output contains `(legacy)`, uncomment in `launcher.toml`:
   ```toml
   iptables_bin = "iptables-legacy"
   ip6tables_bin = "ip6tables-legacy"
   ```

2. **Seccomp Profile**:
   The default profile is located at `deploy/seccomp/bot.json` and mounted read-only into the launcher container. To verify or rebuild the profile from source:
   ```bash
   python3 seccomp/build_profile.py --check
   ```

3. **Internal DNS (Optional)**:
   If your bots need to resolve internal private hostnames, specify your upstream DNS resolver in `launcher.toml`:
   ```toml
   internal_dns = "192.168.1.53"
   ```
   Note: Do not use `127.0.0.11`, which is reserved for Docker's embedded container DNS.

---

## 5. Build Bot Image

Build the base bot container image:

```bash
./build-bot-image.sh
```

Alternatively, use Docker Compose:

```bash
docker compose --profile build build bot-image
```

### Optional: Antigravity CLI (`agy`) for Gemini Runner

To enable the `agy` CLI layer in the bot image:
1. Obtain the direct URL of the binary or of a `.tar.gz`/`.tgz` archive and calculate the SHA-256 of the downloaded file (for an archive, the archive itself):
   ```bash
   curl -fsSL -o /tmp/agy "<direct_download_url>"
   sha256sum /tmp/agy
   ```
2. Set `AGY_URL` and `AGY_SHA256` in `deploy/.env`.
3. Re-run `./build-bot-image.sh`.

If these variables are omitted, the image builds without `agy` and the `gemini` CLI runner is disabled.

---

## 6. Reverse Proxy Setup (Nginx)

Botstead serves the core API and static PWA through Nginx.
The snippet mounts the app under `/bots/`. For another prefix, change the paths in the snippet and set the same prefix in `BOTHUB_BASE_PATH` in `.env` (the session cookie path), otherwise login succeeds and every next request gets 401.

1. Copy the Nginx location configuration snippet:
   ```bash
   sudo cp nginx/bothub-locations.conf /etc/nginx/snippets/
   ```

2. Create the proxy secret configuration:
   ```bash
   sudo bash -c 'echo "proxy_set_header X-Bothub-Proxy $(grep BOTHUB_PROXY_SECRET .env | cut -d= -f2);" > /etc/nginx/snippets/bothub-proxy-secret.conf'
   sudo chmod 600 /etc/nginx/snippets/bothub-proxy-secret.conf
   ```

3. Include the snippet in your Nginx server block:
   ```nginx
   server {
       server_name your-domain.example.com;

       include /etc/nginx/snippets/bothub-locations.conf;
   }
   ```

4. Test and reload Nginx:
   ```bash
   sudo nginx -t
   sudo systemctl reload nginx
   ```

---

## 7. Start the Stack

Launch the core services from the `deploy/` directory:

```bash
docker compose up -d --build
```

Check the launcher logs to confirm network rules and startup health:

```bash
docker compose logs launcher
```

If the launcher encountered errors (missing iptables permissions, missing `br_netfilter`, or missing `DOCKER-USER` chain), it terminates with a descriptive error message in the log.

---

## 8. First Administrator Setup

When Botstead boots with an empty database, open registration is blocked and the system enters setup mode.

1. Check the setup status:
   ```bash
   curl -s http://127.0.0.1:8080/api/setup/status
   ```
   The endpoint returns `{"needs_setup": true}`.

2. Retrieve the one-time setup code from the core container log:
   ```bash
   docker compose logs core | grep "Setup code:"
   ```

3. Navigate to `https://your-domain.example.com/bots/` in your browser. The PWA prompts for initial administrator registration.
4. Enter the one-time setup code, your email, and a password (minimum 10 characters).

The setup process creates the primary administrator account, initializes system settings, and locks further setup attempts.

---

## 9. Configure Model Providers

Botstead bots require a configured model provider.

### Option A: API Key Provider

Log in as the administrator in the PWA, navigate to **Providers**, and add a provider:
- **Anthropic API**: Direct Anthropic API key (`sk-ant-...`).
- **OpenAI API**: Direct OpenAI API key (`sk-...`).
- **OpenAI-Compatible**: Custom base URL (e.g. `https://api.together.xyz/v1` or local endpoint) with API key.
- **Google Gemini API**: Direct Gemini API key.

#### Private LAN Endpoints (e.g., Local Ollama / vLLM)
If you configure an endpoint on a private network (RFC 1918, CGNAT, or local host):
1. The provider enters `pending_admin` status.
2. The administrator reviews the resolved IP addresses and approves the request (`allow_private`).
3. The system locks the approved IP addresses to protect against DNS rebinding.

### Option B: Subscription CLI Login

To authenticate developer subscription CLIs (Claude Code, OpenAI Codex, Antigravity CLI):
1. In the PWA, open **Providers** and select **Subscription Login**, or run the CLI helper:
   ```bash
   ./login.sh <owner-id>
   ```
2. Complete the OAuth login flow inside the interactive terminal.
3. Credentials are saved into the owner's dedicated storage volume (`bothub-login-<owner-id>`).

---

## 10. Create and Run a Bot

### Create via Web Interface

1. In the PWA, click **New Bot**.
2. Describe your bot's role, instructions, avatar, and select the model provider.
3. Click **Create**. The core instructs the launcher to provision the bot container and user bridge network.

### Bot Templates

You can export existing bots and import them to create new bots:
- **Export**: Open the bot settings and export the template file. This downloads a `<name>.botstead.json` file containing instructions, avatar, auto-allow rules, MCP allow-list, budget settings, cron schedules, and procedures. It does not contain API keys, secrets, memory, threads, tokens, or container state.
- **Import**: On the new bot screen, choose the option to create from file. Select the `.botstead.json` file (up to 256 KB), select a model provider and model, and click create. The system creates the bot, its schedules, and its procedures in a single transaction.

### Ready-made Template Catalog

On the **New Bot** screen, choose **From catalog**, select a template card, then review its name and role and choose an enabled provider and model before creating the bot. The catalog is served from validated files in `templates/`. Catalog templates do not include hook schedules or their secrets. For example, add a GitHub hook separately if you use the PR Reviewer template ([contract 9](contracts.md#contract-9)).

### Connect GitHub or Slack

You can trigger bot turns from external GitHub or Slack events:

1. In the app, create a schedule of kind `hook` for your bot.
2. Copy the webhook URL and, for GitHub, the token (`hook_token`). Slack does not use `hook_token`.
3. **GitHub**:
   - In your repository or organization settings, open **Webhooks** and click **Add webhook**.
   - Set the Payload URL to `https://<domain>/bots/hooks/<schedule_id>/github`.
   - Set **Content type** to `application/json`.
   - Paste the schedule token into the **Secret** field.
   - Select individual events (such as issues, pull requests, or pushes) and save the webhook.
4. **Slack**:
   - In the Slack app management console, open **Event Subscriptions** and enable events.
   - In the **Request URL** field, enter `https://<domain>/bots/hooks/<schedule_id>/slack`.
   - Copy the app's **Signing Secret** (Basic Information) into the schedule's **Slack signing secret** field in the app (open the schedule in Routines). The field is write-only: after saving, the app only shows that a secret is set.
   - Until the secret is saved, the server answers Slack with 403 and the URL cannot be verified.
   - Slack sends a verification challenge. The server verifies the URL automatically.
   - Under bot events, subscribe to `app_mention` and `message.channels`, then install the app to your workspace.

To send the bot's completed answer back to Slack, grant the Slack app the `chat:write` bot scope and reinstall it in the workspace. In the schedule's Slack settings in **Routines**, save the **Bot User OAuth Token** from Slack's **OAuth & Permissions** page in the **Slack Bot Token** field. The token is write-only. Once configured, completed hook turns reply to the event's channel and thread. A delivery failure is logged and does not change the turn status ([contract 18](contracts.md#contract-18)).

### Receive Email Through Mailgun

Create a `hook` schedule, open it in **Routines**, and select **Mailgun** as its adapter. Copy the JSON route URL shown there and save the **Signing Key** from Mailgun in the schedule's **Mailgun Signing Key** field. Configure the Mailgun route to send JSON requests to that URL. The URL contains a separate route token, and each request must include Mailgun's valid timestamp and signature headers. Requests older than 15 minutes are rejected; the request body limit is 1 MiB and attachments are ignored. When the email signing key is set, the schedule's GitHub and Slack routes are disabled ([contract 18](contracts.md#contract-18)).

### Connect a Telegram Bot

For automatic webhook registration, first set `BOTHUB_PUBLIC_ORIGIN` in `deploy/.env` to the public origin used to reach the Bot Hub deployment. Recreate the core service from `deploy/` before saving the Telegram channel settings:

```bash
docker compose up -d --force-recreate core
```

In the bot's settings, open the **Telegram** section. Create a bot with BotFather and paste its bot token into the settings. Enter the allowed chat IDs as comma-separated integers, enable the channel, and save. The core registers its webhook when the enabled channel is saved. Only text messages from allowed chats start turns, and only those webhook-created turns receive a reply. The channel token is stored encrypted; the PWA displays only its last four characters ([contract 23](contracts.md#contract-23)).

### Create via Script

Alternatively, create a bot from the terminal:

```bash
./bot-create.sh <bot-id> <owner-id>
```

Parameters:
- `bot-id`: Alphanumeric slug (e.g., `scout`, `coder-1`).
- `owner-id`: The UUID of the owner user (from `users.id`).

### Manage Bot Containers

Use `deploy/launcherctl.sh` to manage container lifecycle:

```bash
# Check bot status
./launcherctl.sh status <bot-id>

# Recreate bot container (preserves home data volume)
./launcherctl.sh recreate <bot-id>

# List all managed bots
./launcherctl.sh list

# Inspect system and network policy information
./launcherctl.sh info

# Remove bot container and optionally purge home volume
./launcherctl.sh remove <bot-id> [--purge]
```

---

## 11. Run Verification Suite

To verify isolation policies, seccomp filters, and network boundaries on your deployment:

```bash
cd deploy
./tests/isolation_check.sh
```

The test script:
1. Spawns temporary test users and bot containers.
2. Verifies capability drops and non-root execution.
3. Tests seccomp filters against user namespace creation.
4. Confirms iptables drop rules for host ports, PostgreSQL, and private subnets.
5. Verifies cross-user volume and credential isolation.
6. Cleans up all test resources upon completion.

---

## 12. Optional: Mac Agent Setup

To allow bots to interact with a macOS workstation (read files via `mdfind`, preview files, run local shortcuts, or delegate coding tasks):

1. On your Mac, clone the repository and navigate to `macagent/`:
   ```bash
   cd macagent
   ./install.sh
   ```

2. Establish an SSH tunnel forwarding port 18080 to the server core port 8080:
   ```bash
   ssh -N -L 18080:127.0.0.1:8080 your-server-alias
   ```

3. In `macagent/`, set the environment variable:
   ```bash
   export BOTHUB_URL="http://127.0.0.1:18080"
   export MAC_AGENT_TOKEN="<token_generated_in_pwa>"
   ```

4. Start the LaunchAgent service. The Mac agent registers with the core and reports status (`online`, `locked`, `sleep`, or `offline`).

### Register Multiple Mac Agents

Each Mac needs its own registration and token. In the PWA, open **Settings** and then **Mac Agents**, enter a name, and choose **Add Mac**. Copy the one-time token shown after registration and use it as `MAC_AGENT_TOKEN` on that Mac. The token is shown only once. Repeat these steps for every Mac, then choose the desired Mac in the bot's settings. Removing a Mac revokes its token and clears the Mac assignment from bots that used it ([contract 5](contracts.md#contract-5)).

The LaunchAgent reads `BOTHUB_URL` and `MAC_AGENT_TOKEN` from `~/Library/Application Support/BotHubMac/config.env`. If you install the agent as a LaunchAgent, put the same values in that file; shell exports only apply to processes started from that shell.
