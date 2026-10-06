# Security Model and Isolation

This document outlines the security guarantees, boundaries, residual risks, and sandboxing options for Botstead.

It summarizes the implementation details from the isolation specification (`docs/isolation.md`) and module contracts (`docs/contracts.md`, section 13).

---

## 1. Threat Model

Botstead operates on the assumption that AI bots execute arbitrary code and interact with untrusted third-party web content.

### What We Protect
- **Multi-Tenant Isolation**: Data, persistent memory, and volumes of other users.
- **System Credentials**: PostgreSQL database and host environment secrets.
- **Host Infrastructure**: The host server, internal services, private networks, and cloud metadata endpoints.
- **Container Daemon**: Protection of `docker.sock` against unauthorized container execution or privilege escalation.

### What is Out of Scope
- A compromised host server or malicious server administrator.
- Hardware-level side-channel attacks (e.g., Spectre, Meltdown).

---

## 2. Security Guarantees

Botstead implements defense in depth across execution, system calls, networking, credentials, and browser controls.

### Process and Container Privileges
- **Non-Root Execution**: Bot commands execute strictly under UID 1000 (`bot`). Browser services run under UID 1001 (`browser`). Root UID 0 is prohibited in launcher configurations.
- **Dropped Capabilities**: Bot containers run with `--cap-drop ALL` and `no-new-privileges: true`.
- **Read-Only Root Filesystem**: The container root filesystem is mounted read-only. Writable paths (`/tmp`, `/run`) are backed by temporary in-memory filesystems (`tmpfs`).
- **No Host Volume Mounts**: Containers mount only named Docker volumes. Direct host directory mounts and access to `docker.sock` are disallowed.
- **Resource Constraints**: Default constraints limit each bot to 2 GB RAM (no swap), 2 CPUs, 512 PIDs, and 1024 open file descriptors.

### Kernel and System Call Filtering
- **Outer Seccomp Profile (`bot.json`)**:
  - Restricts system call surface for the entire container.
  - Removes access to `bpf`, `io_uring_*`, `userfaultfd`, `kexec_*`, `mount`, and `pivot_root`.
  - Blocks dangerous `socket` families, allowing only `AF_UNIX`, `AF_INET`, `AF_INET6`, `NETLINK_ROUTE`, and `NETLINK_KOBJECT_UEVENT`.
  - Blocks netfilter socket options on `SOL_IP` (0) and `SOL_IPV6` (41) levels (`setsockopt` and `getsockopt` optnames associated with legacy table manipulations such as CVE-2021-22555).
  - Allows `clone` and `unshare` for user namespaces to support Chromium sandboxing without `--no-sandbox` or `setuid`.
- **Inner Seccomp Filter (`bot-guard`)**:
  - A static C binary installed at `/usr/local/libexec/bot-guard`.
  - Wraps the container entrypoint (PID 1) and every command executed by the launcher under UID 1000.
  - Blocks `CLONE_NEWUSER` (user namespaces via `clone`), `unshare`, and `setns`.
  - Terminates any process attempting 32-bit (i386) or x32 ABI system calls.
  - Result: Chromium (UID 1001) retains user namespace creation for its internal sandbox, while bot tools and model code (UID 1000) are prevented from creating user namespaces.

### Network Isolation
- **Dedicated Per-User Bridges**: Each user receives an isolated Docker bridge network (`bothub-u-<owner>`) with IPv6 disabled.
- **Host iptables Enforcement**: The launcher daemon manages dedicated chains in the host's `DOCKER-USER` and `INPUT` tables.
- **Default Drop Rules**:
  - Drops packets from bot bridges to host ports (`INPUT` chain).
  - Drops packets destined for the PostgreSQL internal network (`db_net`).
  - Drops packets destined for other users' bot containers or bridges.
  - Drops traffic targeting private subnets: RFC 1918 (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), CGNAT (`100.64.0.0/10`), loopback (`127.0.0.0/8`), link-local and cloud metadata (`169.254.0.0/16`, `fd00:ec2::/32`), multicast (`224.0.0.0/4`), and IPv6 ULA (`fc00::/7`).
- **Permitted Traffic**: Bots can communicate only with the core API port (8080), the designated DNS resolver (port 53), and public internet IP addresses via Docker NAT.

### Model API Gateway and Secrets
- **Credential Storage**: Provider API keys are stored encrypted at rest with AES-256-GCM. Plaintext keys are never stored on disk or injected into bot containers.
- **Per-Turn HMAC Tokens**: Bots receive short-lived, turn-scoped HMAC tokens. The internal gateway validates the token, verifies active turn status, injects the real provider secret, and strips sensitive client headers.
- **SSRF and Rebinding Guard**: The gateway resolves upstream hostnames, pins verified IP addresses, and rejects private IP ranges unless explicitly approved by an administrator (`allow_private_ips`). If DNS returns a changed private IP, requests are blocked with HTTP 502 until re-approved.

### Browser Protection and Human Takeover
- **Process Freezing**: When an operator initiates takeover (`POST /api/bots/{id}/browser/takeover`), the launcher terminates UID 1000 bot processes and blocks new execution requests.
- **Clean Human Browser Instance**: The operator interacts with a dedicated Chromium process started without Chrome DevTools Protocol (CDP) and without remote debugging ports, using a clean temporary profile directory (`/home/browser/.config/botstead-browser-human`).
- **Sanitized Cookie Return**: Upon returning control to the bot, persistent SQLite cookies are merged into the bot profile via a restricted Python script running with `trusted_schema=OFF`. The temporary human profile is wiped, and X11 clipboards and cut buffers are cleared.
- **Input and URL Masking**:
  - Passwords, tokens, OTP codes, and credit card numbers are masked from turn logs, events, approvals, and DOM snapshots (`[скрыто]`).
  - Navigation is restricted to `http`, `https`, and `about:blank`. Prohibited schemes (`file:`, `chrome:`, `javascript:`, `data:`) and internal network addresses are blocked at the container tool, core API, and approval layers.
- **Mandatory Approvals**: Destructive actions (payments, data deletion, login form submissions) always require explicit operator confirmation and cannot be bypassed by automated rules.

---

## 3. What Isolation Does NOT Guarantee (Residual Risks)

Operators must understand the inherent limitations of container-based isolation without hypervisor virtualization.

### Shared Host Kernel
Standard Docker containers share the Linux host kernel. A vulnerability in the host kernel or the container runtime (`runc`) can allow a container escape. Standard container isolation reduces the attack surface but does not provide a hard security boundary against zero-day kernel exploits. For untrusted users or untrusted bot workloads, gVisor (`runsc`) is strongly recommended.

### Unrestricted Public Internet Access
Bot containers have outbound internet access via the host IP address by default. A bot running untrusted instructions could perform outbound port scanning, spamming, or abuse from your server IP. Domain-level egress whitelisting is not enforced by default.

### Storage Volume Quotas
Named Docker volumes (`home` and `login` volumes) do not have enforced disk quotas. A compromised or misbehaving bot can write large files and exhaust host disk space.

### Shared User Network
All bots belonging to the same user share that user's bridge network (`bothub-u-<owner>`). Bots of the same user can communicate with one another over the local bridge.

### Subscription Tokens Visible to Bot Code
Subscription CLI OAuth credentials (e.g. Claude Code or Codex tokens) are mounted read-only into `/home/bot/.auth` and copied into the bot workspace. A bot running under a user account can read that user's subscription credentials and send them outbound.

### Unauthenticated Local CDP Port
During automated `bot` mode, Chromium listens for CDP connections on `127.0.0.1:9222` without authentication. Bot processes (UID 1000) can connect to this port and alter the bot profile or install persistent service workers. While these modifications do not affect the human takeover session (which runs in an isolated profile without CDP), they will execute when control returns to the bot.

### Text-Based URL Validation
Browser navigation filters evaluate URL strings rather than resolved IP addresses. A public domain that resolves to a private IP or redirects to internal services bypasses the string check and relies entirely on host iptables packet drops.

### Semantic DOM Disguises
Heuristic risk classification identifies payment and login actions from element roles, text, and page labels. A malicious web page that deliberately uses benign text (such as "Next" instead of "Pay") may evade heuristic payment classification, although origin-based approval rules limit automatic actions.

---

## 4. Enabling gVisor (`runsc`)

gVisor provides an application kernel written in Go (`runsc`) that intercepts and executes system calls in user space, creating a strong virtualization boundary between the container and the host Linux kernel.

### Prerequisites and Host Setup

1. **Install `runsc`**:
   Follow official gVisor documentation to install the `runsc` binary on the host.

2. **Register Docker Runtime**:
   ```bash
   sudo runsc install
   sudo systemctl restart docker
   ```
   Verify that `/etc/docker/daemon.json` contains the `runsc` runtime definition.

3. **Test Runtime**:
   ```bash
   docker run --rm --runtime runsc bothub-bot echo ok
   ```

### Configure Botstead for gVisor

1. Open `deploy/launcher.toml` and uncomment the runtime setting:
   ```toml
   runtime = "runsc"
   ```

2. Restart the launcher daemon:
   ```bash
   cd deploy
   docker compose up -d launcher
   ```

3. Recreate existing bot containers:
   ```bash
   ./launcherctl.sh recreate <bot-id>
   ```

4. Verify the active runtime:
   ```bash
   ./launcherctl.sh info
   ```
   The `runtime` field reports `runsc`.

### On-Site Verification Considerations Under gVisor

When deploying with gVisor, verify the following operational aspects:
- **Chromium Sandbox**: Confirm that user namespace emulation in `runsc` supports Chromium startup without `--no-sandbox`.
- **Seccomp Layering**: gVisor intercepts system calls directly and handles inner seccomp filters (`bot-guard`) in user space.
- **Bridge Filtering**: Ensure iptables rules on host bridge interfaces continue to filter traffic from gVisor sandbox endpoints.
- **Cgroup Limits**: Verify that memory and CPU limits function as expected with your installed version of `runsc`.
