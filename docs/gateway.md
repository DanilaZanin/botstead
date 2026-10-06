# Model API gateway

Evidence labels: **[verified]** is covered by isolated tests or direct code inspection; **[docs]** links to official CLI documentation; **[assumption]** needs an integration check.

## Bot Hub internal URL

**[verified]** Bot Hub's core mounts the gateway, issues a token for each turn, and passes the gateway URL to the bot. `BOTHUB_INTERNAL_URL` sets the base address used inside the bot network. Its default is `http://core:8080`, where `core` is the internal Docker network alias. This address is not publicly exposed. The Nginx deployment returns 404 for the public `/bots/gateway/` path.

## Wiring

**[verified]** The gateway is an injectable FastAPI router mounted by `main.py`. The application supplies a provider lookup, an active-turn check, and a usage callback.

```python
from bothub.gateway import GatewayProvider, create_gateway_router

async def provider_lookup(bot_id: str, provider_id: str) -> GatewayProvider | None:
    # Verify that this bot owns the provider before returning the binding.
    return GatewayProvider(
        id=provider_id,
        kind="anthropic_api",
        base_url="https://provider.example",
        api_key="provider-secret",
        owner_active=True,
        allowed_models=["claude-example"],
        allowed_anthropic_betas=["claude-code-20250219"],
        owner_id="owner-1",
    )

async def on_usage(bot_id: str, provider_id: str, model: str, input_tokens: int,
                   output_tokens: int, cache_read_tokens: int,
                   cache_write_tokens: int, status: int) -> None:
    # Persist the observation and status.
    ...

gateway = create_gateway_router(
    provider_lookup, on_usage, token_secret,
    max_turn_seconds=1800, token_ttl=1920,
    max_parallel_per_bot=4, max_connections_per_user=4,
)
app.include_router(gateway)  # FastAPI lifespan calls gateway.startup() and gateway.shutdown().
```

**[verified]** The router calls `provider_lookup` on every request and rejects an inactive owner with 403. The lookup implementation must check ownership. The supported kinds are `anthropic_api`, `openai_api`, `openai_compatible`, and `google_api`. `cli_subscription` is not accepted.

**[verified]** Each binding has `allowed_models: list[str]`. POST requests use the JSON string `model`, except Google routes, whose model comes from the path. If a Google body has `model`, it must match the path. Invalid JSON, duplicate keys, case variants of the top-level `model` key, BOM, UTF-16, gzip, and models outside the list return `403 model_disabled`. The gateway sends parsed JSON serialized as UTF-8. A non-boolean `stream` returns 400. `GET /v1/models` needs no request model and returns only listed models.

**[verified]** `allowed_anthropic_betas` controls which `anthropic-beta` values pass upstream. Unknown values are removed from the header. The configurable default currently includes `claude-code-20250219`. **[assumption]** This default has not been verified by a wire capture with `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`; capture is required before labeling the list verified.

## Secrets and token lifetime

**[verified]** Keep the gateway token secret separate from provider encryption keys. `gateway.issue_token(bot_id, provider_id)` binds a token to a bot and provider using the router's `token_ttl`. For a per-bot deadline, set `GatewayProvider.max_turn_seconds` and pass the same value as `max_turn_seconds=bot_deadline` to `gateway.issue_token`. The default `max_turn_seconds` is 1800, so the default token TTL is 1920 seconds. Issue a fresh token for each turn; the router checks provider ownership and `owner_active` even while a token remains valid.

**[verified]** OpenAI callers use `Authorization: Bearer <token>`, Anthropic callers can use `x-api-key`, and Google callers can use `x-goog-api-key`. Conflicting token headers are rejected. The gateway sends the upstream provider key only in the provider's native authentication header.

**[verified]** `BOTHUB_SECRET_KEYS` contains ordered `key_id:base64key` entries. Key IDs 1–255 are unique; ID 0 is reserved. Each decoded AES-256 key is 32 bytes. `encrypt_secret(value: bytes, aad: bytes)` and `decrypt_secret(ciphertext, aad)` bind the plaintext to a nonempty row ID. Include the owner ID in the AAD when row IDs are only unique within an owner. The stored ciphertext is bytes: one-byte key ID, 12-byte nonce, and AES-GCM ciphertext with tag. Encryption uses the first key; rotation decrypts by the stored ID and re-encrypts under the first configured key.

## Routes and resources

**[verified]** Routes start with `/gateway/{provider_id}/`. Anthropic accepts `POST /v1/messages` and `/v1/messages/count_tokens`; OpenAI accepts `POST /v1/chat/completions`, `/v1/responses`, `/v1/embeddings`, and `GET /v1/models`; Google accepts `POST /v1beta/models/{model}:generateContent`, `:streamGenerateContent`, and `:countTokens`. Other routes return 404.

**[verified]** The gateway strips caller credentials, cookies, and query parameters named `key`, `api_key`, or `access_token`. Google streaming uses `alt=sse`. OpenAI chat streaming requests include `stream_options.include_usage=true`.

**[verified]** A bot may have at most four simultaneous requests by default. `max_turn_seconds` bounds body reads, upstream connection, and streaming after provider lookup. The router creates one HTTPX `AsyncClient` in its lifespan and refuses upstream redirects. `gateway.startup()` and `gateway.shutdown()` are available for hosts that manage lifecycle explicitly. `max_connections_per_user` limits simultaneous requests across bots with the same `GatewayProvider.owner_id`; provide a stable owner ID in each binding.

## Accounting and errors

**[verified]** After an active provider binding is found, `on_usage` receives `(bot_id, provider_id, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, status)` once per request, including upstream errors and zero usage. Authentication failures, unsupported routes or methods, and concurrency or connection-limit rejections (401/404/405/429) are counted in memory and logged once per minute, without calling `on_usage`. SSE processing preserves usage from events larger than 1 MiB, up to a 10 MiB event limit. Disconnects can leave partial counts. An upstream stream read failure records status 502.

**[assumption]** Usage records reflect provider responses, not guaranteed billable totals. A provider may omit usage or accept work before a connection fails. Reconcile records against provider data; zero usage does not prove a free request.

**[verified]** Upstream 4xx and 5xx retain their status and expose only JSON `error.type`, `error.code`, and `error.message`; fields are truncated to 2 KiB and the provider key is redacted. The provider key is replaced with `***` in successful buffered and SSE bodies, including a key split across chunks, and in forwarded `content-type` and `retry-after` headers. DEBUG log-capture tests check that connection errors do not expose the key.

**[assumption]** Deployment logging must also avoid request headers and upstream bodies outside this router.

## Network controls

**[verified]** The URL validator resolves names asynchronously and checks every answer. Outbound requests target a checked IP while retaining the original hostname in Host and the TLS SNI extension. Public hosts require HTTPS. Private addresses (RFC1918, IPv6 ULA, CGNAT `100.64.0.0/10`, which includes Tailscale) are allowed only for a provider with `allow_private` set by an administrator, and then only the IPs stored in `allow_private_ips` at approval time; a name that later resolves to another private IP is refused with 502 (`invalid provider address`) until the administrator approves again, and the router reports it through `on_address_changed` so the provider becomes `pending_admin`. A host listed in the deprecated `PROVIDER_PRIVATE_ALLOW` is not bound to a set. Blocked in every case: loopback, link-local, metadata (`fd00:ec2::254`, `100.100.100.200`), multicast, unspecified, `64:ff9b::/96`, IPv4-compatible `::/96`, the subnets of the core's own network interfaces (re-read at every address check and cached for 30 seconds, so bot networks attached after start are covered; only interface subnets count: IPv4 prefixes shorter than /8 and IPv6 prefixes shorter than /16, such as a default route or VPN `0.0.0.0/1` + `128.0.0.0/1`, are not taken) and `PROVIDER_FORBIDDEN_CIDRS` (passed to the router as `forbidden_networks`, a sequence or a callable returning one); IPv4-mapped `::ffff:0:0/96` is reduced to IPv4 first. The client uses `trust_env=False` and does not follow redirects.

**[verified]** A test with the system resolver rejects `localhost`. **[assumption]** The pinned request tests use an injected transport; they do not open a real TLS socket or drive a controlled DNS rebind. An integration test must confirm SNI and certificate validation with the pinned address.

**[assumption]** Keep an egress firewall as a separate network boundary. Set `allowed_private_hosts` from administrator configuration, never from a bot request.

## CLI setup

**[verified]** `gateway.issue_token` creates a short-lived token for one bot and provider. Bot Hub core mounts the router and supplies each bot with a per-turn token and its internal gateway URL.

The examples below use `https://gateway.example` as the URL of a separate gateway deployment. Configure that deployment to serve the documented routes and expose its own public URL before using these client settings. Bot Hub bots use the internal URL described above.

**[docs]** Claude Code supports `ANTHROPIC_BASE_URL` for an Anthropic-compatible gateway and `ANTHROPIC_AUTH_TOKEN` for a bearer token. Set the base URL to `https://gateway.example/gateway/<provider_id>` and the auth token to the issued gateway token. Select a model in `allowed_models`. Sources: [gateway setup](https://code.claude.com/docs/en/llm-gateway), [environment variables](https://code.claude.com/docs/en/env-vars).

```sh
export ANTHROPIC_BASE_URL="https://gateway.example/gateway/<provider_id>"
export ANTHROPIC_AUTH_TOKEN="<issued_gateway_token>"
export ANTHROPIC_MODEL="<allowed_model>"
```

**[assumption]** Check the beta header against a wire capture from the installed Claude Code version with `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`. Set `default_anthropic_betas` on the gateway or `allowed_anthropic_betas` per binding. Unknown values are stripped.

**[docs]** Codex custom providers use `model_provider`, `model_providers.<id>.base_url`, and `model_providers.<id>.env_key` in the user-level `~/.codex/config.toml`. The supported custom wire API is `responses`. Source: [OpenAI Codex configuration reference](https://developers.openai.com/codex/config-reference).

```toml
model_provider = "bothub"
model = "<allowed_model>"

[model_providers.bothub]
name = "Bot Hub"
base_url = "https://gateway.example/gateway/<provider_id>/v1"
env_key = "BOTHUB_GATEWAY_TOKEN"
wire_api = "responses"
supports_websockets = false
```

```sh
export BOTHUB_GATEWAY_TOKEN="<issued_gateway_token>"
```

**[docs]** Antigravity CLI (`agy`) uses `GOOGLE_GEMINI_BASE_URL` for a Gemini-compatible endpoint when `modelProvider` is `gemini` in `~/.gemini/antigravity-cli/settings.json`; it reads `GEMINI_API_KEY`. Set the base URL to `https://gateway.example/gateway/<provider_id>` and the API key to the issued gateway token. Source: [Antigravity CLI installation and auth](https://www.antigravity.google/docs/cli/install/).

```json
{"modelProvider": "gemini", "model": "gemini-3.1-flash-lite-preview"}
```

```sh
export GOOGLE_GEMINI_BASE_URL="https://gateway.example/gateway/<provider_id>"
export GEMINI_API_KEY="<issued_gateway_token>"
```

**[docs]** Gemini CLI supports `GOOGLE_GEMINI_BASE_URL` under Gemini API key authentication and reads `GEMINI_API_KEY`. Set `security.auth.selectedType` to `gemini-api-key` in `~/.gemini/settings.json`, and export `GEMINI_CLI_TRUST_WORKSPACE=true` for a trusted local workspace. Use the same endpoint and token environment variables as above. Source: [Gemini CLI configuration](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md).

**[assumption]** Check each CLI version against the route list before use. A client may call unsupported endpoints or retain a token beyond one turn.

**[verified]** The default token expires after 1920 seconds. Use `gateway.issue_token` so issuance reads the router's `token_ttl`. For a per-bot turn, pass its `max_turn_seconds` when issuing and set `GatewayProvider.max_turn_seconds` in the binding. Verification checks the signed expiration. **[assumption]** The static environment variable examples suit a single turn or a short local check. A longer CLI session needs a fresh token before expiry; confirm how that CLI reloads credentials before relying on a long-running process.

## Spending limits

**[verified]** `max_turn_seconds` and `max_parallel_per_bot` bound request lifetime and concurrency. `allowed_models` blocks unlisted models. In the core integration, each token includes a signed turn ID; the gateway rejects requests after that turn stops and records usage against the exact turn. The core checks a daily token budget after recording the provider response.

**[assumption]** Upstream work can be billed when a request times out, disconnects, or returns an error with no usage. Set hard spending limits at the provider or application layer and reconcile gateway usage with provider billing data.

## Isolated tests

**[verified]** Run from `core/`: `UV_CACHE_DIR=/tmp/bothub-uv-cache uv run pytest tests/test_secrets.py tests/test_gateway.py --noconftest -v`. These tests do not load the Postgres fixtures in `tests/conftest.py`.
