# Contributing

Open an issue or pull request with a concise description of the change. Keep changes focused and include tests when behavior changes. Report vulnerabilities through GitHub Security Advisories as described in [SECURITY.md](SECURITY.md).

Run the Python suites with PostgreSQL 16 available for the core tests:

```sh
docker compose -f deploy/docker-compose.dev.yml up -d db
cd core && uv run pytest
cd ../macagent && uv run pytest
```

Run the browser suite from a separate shell:

```sh
cd e2e
npm ci
npx playwright install chromium
npm test
```

Run Ruff from the repository root with `uvx ruff check .`.
