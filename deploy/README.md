# Deploying Bot Hub (your server)

Bot containers are managed by a dedicated `launcher` service: only `launcher` has access to `docker.sock`. The core has no Docker socket.
Isolation model, guarantees, and limitations: `docs/isolation.md`.

## Requirements

- A Linux host with Docker 28+ and Docker Compose 2.33+, with `iptables` enabled in the daemon.
- Docker 28 is needed for `docker network connect --gw-priority`, Compose 2.33 for `gw_priority` in `docker-compose.yml`.
  Together they keep the core's main network as its default gateway when the launcher connects the core to a user network.
  Otherwise a user network can become the gateway and the core's published port (`127.0.0.1:8080`) stops answering.
- On Docker older than 28 the launcher still starts, but it does not pass the flag and writes a warning to its log
  (`docker compose logs launcher`). The priority value is `core_gw_priority` in `launcher.toml` (default `-100`, must be
  below 100).

## Initial Setup

1. **Clone the repository** to your server (Linux, Docker with `iptables` enabled in daemon).
2. **Load `br_netfilter`** (bot network rules only see bridge traffic when this is loaded):
   ```bash
   sudo modprobe br_netfilter
   echo br_netfilter | sudo tee /etc/modules-load.d/br_netfilter.conf
   sudo sysctl -w net.bridge.bridge-nf-call-iptables=1
   ```
3. **Environment**:
   ```bash
   cd deploy
   cp .env.example .env
   cp db.env.example db.env
   ```
   In `.env`, generate secrets (`openssl rand -hex 32`), including `LAUNCHER_SECRET` (at least 32 characters), `BOT_TOKEN_SECRET`, and `BOTHUB_GATEWAY_TOKEN_SECRET`. For `BOTHUB_SECRET_KEYS`, set `1:<base64>` with 32 random bytes generated using `openssl rand -base64 32`. Save this key to restore encrypted provider API keys. In `db.env`, set `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`; the password must match the one in `DATABASE_URL`.
4. **Launcher configuration** is in `deploy/launcher.toml` (image, limits, network exemptions, runtime, bot seccomp profile). By default nothing needs to change: the `deploy/seccomp/bot.json` profile is in the repository and mounted read-only into the launcher container (see the "Seccomp Profile" section below). If `iptables --version` on the host shows `(legacy)`, uncomment `iptables_bin = "iptables-legacy"` and `ip6tables_bin = "ip6tables-legacy"`.
   Before installing on a Linux Docker host, run `./preflight.sh`. On Colima, run the check inside the Linux VM from a checkout with Docker daemon access. The script checks `br_netfilter`, `DOCKER-USER`, iptables backend, IPv6 default bridge, and the Docker version (below 28: a warning, the exit code does not change). On error it prints a remediation command and exits with code 1.
   If your setup uses internal DNS, configure its real IP in `launcher.toml` via `internal_dns`.
   `127.0.0.11` is not suitable here: that address is already used by Docker internal DNS inside the container.
5. **Nginx**: the service runs on the path `https://bots.example.com/bots/` behind an SSO proxy.
   ```bash
   sudo cp nginx/bothub-locations.conf /etc/nginx/snippets/
   # Add: proxy_set_header X-Bothub-Proxy <BOTHUB_PROXY_SECRET>;
   sudo nano /etc/nginx/snippets/bothub-proxy-secret.conf
   sudo chmod 600 /etc/nginx/snippets/bothub-proxy-secret.conf
   ```
   In the `server {}` block of your site, add the line `include /etc/nginx/snippets/bothub-locations.conf;` and reload nginx.
6. **Bot image** (see the "Bot Image" section below), then **start the stack**:
   ```bash
   ./build-bot-image.sh
   docker compose up -d --build
   docker compose logs launcher
   ```
   If launcher did not start, the log will show an explicit reason (missing iptables permissions, missing `br_netfilter`, missing `DOCKER-USER` chain). Without an active network policy, bot containers are not created.
7. **First bot and subscription login**:
   ```bash
   ./bot-create.sh <bot-id> <owner-id>    # UUID from bots.owner_id
   ./login.sh <owner-id>                  # log in to claude, codex, agy
   ./launcherctl.sh recreate <bot-id>     # bot picks up new logins
   ```
8. **Isolation check** on your server: `./tests/isolation_check.sh` (creates and deletes temporary users).
   Only test flushing the `iptables -F BOTHUB-ISO` chain and restarting Docker on a dedicated test machine: the bot filter disappears temporarily, and the restart affects other containers. Run:
   `ISOCHK_DISRUPTIVE=1 ISOCHK_DOCKER_RESTART_CMD='sudo systemctl restart docker' ./tests/isolation_check.sh`.
   For Colima, use `ISOCHK_DOCKER_RESTART_CMD='colima restart'` when running the script from Mac.
9. **Mac agent (optional)**: on Mac run `cd macagent && ./install.sh`. The SSH tunnel `com.example.botstead-tunnel` forwards `127.0.0.1:18080` to `127.0.0.1:8080`. The agent uses `BOTHUB_URL=http://127.0.0.1:18080` or a priority list:
   ```
   BOTHUB_URLS=https://bots.example.com/bots,http://127.0.0.1:18080
   ```

## Bot Image

The image is built by `./build-bot-image.sh` or `docker compose --profile build build bot-image`. CLI versions are pinned by build arguments `CLAUDE_CODE_VERSION`, `CODEX_VERSION`, and `PLAYWRIGHT_MCP_VERSION` in `bot-image/Dockerfile`.

Antigravity CLI (`agy`) is installed as an optional layer and only with checksum verification. It was previously fetched via `curl | bash`; now a direct binary URL and SHA-256 are required, both set in `deploy/.env`:

```bash
curl -fsSL -o /tmp/agy "<direct URL to agy binary>"
sha256sum /tmp/agy                  # verify checksum from vendor, then write to AGY_SHA256
```

Without `AGY_URL` and `AGY_SHA256`, the layer is skipped, the image builds, and the `gemini` runner remains unavailable.

## Seccomp Profile

Bot containers run with the `deploy/seccomp/bot.json` profile: it opens Chromium user namespaces under `--cap-drop ALL` so the browser works without `--no-sandbox`, and closes dangerous kernel attack surface. Bot code receives user namespaces closed by the second filter, `bot-guard`, located in the image (`/usr/local/libexec/bot-guard`) and placed by launcher before each bot command. Details: `docs/isolation.md`, section "Kernel Protection".

- `bot.json` is generated: `python3 seccomp/build_profile.py` (from `seccomp/moby-default.json`), check without writing: `python3 seccomp/build_profile.py --check`. Do not edit the file manually.
- Compose mounts it into launcher as `/etc/bothub-launcher/seccomp-bot.json`, with the path specified in `launcher.toml` (`[seccomp] profile`). Docker CLI reads the profile inside the launcher container, so the path is specified there. If the file is missing or is not JSON with `defaultAction`, launcher does not start and logs the reason.
- The profile is applied when creating a container. After changing profile or image, recreate bots: `./launcherctl.sh recreate <bot-id>`. Update image and launcher together: launcher places `bot-guard` before commands; the old image lacks it, so `exec` inside it fails.
- Login containers use the standard Docker profile.
- If Chromium does not start, follow the checklist in `docs/isolation.md`, section "If Chromium does not start" (`chroot` is open in the profile for Chromium sandbox; close it with: `python3 seccomp/build_profile.py --no-chroot`).
- Update image, launcher, and profile together and recreate bots: image ENTRYPOINT (entire entrypoint under `bot-guard`), screen over unix socket instead of TCP 5900, Chromium policies, openbox configuration, and tmpfs `/home/browser/Downloads` only arrive with the new image and new container.
- The host needs user namespaces: `sysctl user.max_user_namespaces` greater than zero; on Ubuntu 24.04 check `kernel.apparmor_restrict_unprivileged_userns`.

## Managing Bots

```bash
./launcherctl.sh create <bot-id> <owner-id>
./launcherctl.sh recreate <bot-id>          # new container, home volume is preserved
./launcherctl.sh remove <bot-id> [--purge]  # --purge also removes the home volume
./launcherctl.sh status <bot-id>
./launcherctl.sh list
./launcherctl.sh info
```

The core creates and runs bot containers through launcher. In `BOTHUB_RUNNER_EXEC=docker` mode, core checks launcher availability at startup.

Named Docker volumes for home and logins have no disk quotas. Monitor free disk space on the host; `--memory` and `--pids-limit` do not limit volume space.

## Updates

1. `git pull origin main`
2. `docker compose up -d --build` (core and launcher).
3. If the bot image changed: `./build-bot-image.sh`, then `./launcherctl.sh recreate <bot-id>` for each bot.
   Home volumes are preserved. To switch to the seccomp profile and `bot-guard`, the order is: build the image first (`./build-bot-image.sh`: compiles `bot-guard`), then `docker compose up -d --build launcher`, then `recreate` each bot. Bots created earlier run on the default profile without `bot-guard` in the image; Chromium will not start in them.

Recreating every running bot is required to switch to the separate browser uid and volume. The old container lacks the `/home/browser` volume and keeps its original PID 1: freezing processes and launching the browser inside it do not provide the new isolation model. Launcher rejects exec and takeover for it until recreated. First build the new image, then recreate containers with the command above and verify `./tests/isolation_check.sh` on a dedicated test machine.

After this update, recreate already labeled bots even without an image change: only the new container receives `--restart no`, disabled IPv6, user-accessible `/run`, and the new `--ulimit`. Before the first reboot, check restart policies of all old containers via `docker inspect`.

If an earlier created network `bothub-u-<owner>` has `EnableIPv6=true`, launcher restores rules and aborts bot startup with an error. Check the network with `docker network inspect -f '{{.EnableIPv6}}' bothub-u-<owner>`. Save volumes and network composition, then stop and remove connected bot/login containers without `-v`, disconnect `bothub-core` from the network, and delete the network. Restart launcher and create containers again: the network receives `--ipv6=false`, and named volumes keep their data. Before deleting the network, verify home backups.

## Migrating from Legacy Scheme

Old `bot-<id>` containers without a label are not touched by launcher. The original volume `bot-<id>-home` remains untouched during migration; the new volume is named `bot-<id>-home-adopted` with an owner label.

1. Save a backup of each old home volume. Before rebooting the host, disable restart on old bots:
   `docker update --restart=no bot-<id>`. Then stop and remove old containers with
   `docker rm -f bot-<id>` without `-v`: the original volume must remain.
2. Build the new image. In `deploy/launcher.toml`, temporarily set `adopt_legacy_volumes = true`, then run
   `docker compose up -d --build launcher`. Wait for successful startup and network rule setup.
3. For each bot, run `./bot-create.sh <id> <owner-id>`, where `owner-id` is the UUID from `bots.owner_id`.
   Launcher copies data into the labeled volume `bot-<id>-home-adopted`, sets ownership to 1000:1000, and excludes
   directories `.claude`, `.codex`, `.gemini`, `.auth`. Verify data in home and the new volume label with
   `docker volume inspect bot-<id>-home-adopted`. After a successful copy, a separate volume
   `bot-<id>-home-adopted-complete` appears with the owner label; it is not mounted into the bot.
4. Set `adopt_legacy_volumes = false` back and restart launcher. Verify with `./launcherctl.sh recreate <id>`:
   the bot should start with the same home. Do not delete the old volume yet.
5. Log into subscriptions again: `./login.sh <owner-id>`. The shared directory `/data/bots/logins`, the variable
   `CLAUDE_CODE_OAUTH_TOKEN`, and the `claude-token.sh` script are no longer used.

To roll back, stop the new bot and revert to the previous stack version that mounts the saved
`bot-<id>-home`. The new volume can be kept until rollback verification is complete. If migration was interrupted, repeat it with
`adopt_legacy_volumes = true`: without the marker volume, an incomplete copy is not accepted after reverting to `false`.

Before the next reboot, check `docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' bot-<id>`:
`no` is expected. The updated launcher changes restart policy for already labeled bots after applying rules, but old
containers without a label must be updated manually in step 1. Core integration in `core/bothub/main.py` with `launcher_client`
must be deployed together with the transition to the new compose: under `BOTHUB_RUNNER_EXEC=docker`, core still calls Docker
directly, and without access to Docker CLI or daemon its Docker operations fail.
