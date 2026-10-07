"""Postgres-backed API test setup."""
import asyncio
import os
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

# Finding 15: тесты и dev делили одну базу (bothub) - прогон тестов чистит данные
# из-под dev-стенда. Тесты по умолчанию используют bothub_test; scripts/dev.sh
# остаётся на bothub (не трогаем).
os.environ.setdefault("DATABASE_URL", "postgresql://bothub:bothub@127.0.0.1:55432/bothub_test")
os.environ.setdefault("OWNER_TOKEN", "test-owner")
os.environ.setdefault("BOT_TOKEN_SECRET", "test-secret")
os.environ.setdefault("MAC_AGENT_TOKEN", "test-mac")
os.environ.setdefault("BOTHUB_RUNNER_EXEC", "local")
os.environ.setdefault("BOTHUB_TEST_LEGACY_IDS", "1")

OWNED_TABLES = ("bots", "threads", "memory", "schedules", "files", "push_subscriptions", "outbox", "mac_status")


async def _create_database_if_missing() -> None:
    url = os.environ["DATABASE_URL"]
    parts = urlsplit(url)
    name = parts.path.lstrip("/")
    admin_url = urlunsplit(parts._replace(path="/postgres"))
    con = await asyncpg.connect(admin_url)
    try:
        exists = await con.fetchval("select 1 from pg_database where datname=$1", name)
        if not exists:
            await con.execute(f'create database "{name}"')
    finally:
        await con.close()


@pytest.fixture(scope="session")
def ensure_test_database():
    asyncio.run(_create_database_if_missing())


async def _truncate() -> None:
    from bothub.db import open_pool

    pool = await open_pool()
    try:
        await _truncate_tables()
    finally:
        await pool.close()


async def _truncate_tables() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute(
            "truncate bothub.events, bothub.approvals, bothub.usage, "
            "bothub.files, bothub.schedules, bothub.memory, bothub.turns, "
            "bothub.slack_handled_events, "
            "bothub.threads, bothub.bots, bothub.outbox, "
            "bothub.push_subscriptions, bothub.mac_status, "
            "bothub.secrets, bothub.procedure_runs, bothub.procedures, bothub.models, bothub.providers, "
            "bothub.sessions, bothub.invites, bothub.users, bothub.settings restart identity cascade"
        )
        for table in OWNED_TABLES:
            await conn.execute(f"alter table bothub.{table} alter column owner_id drop not null")
            await conn.execute(f"alter table bothub.{table} alter column owner_id drop default")
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def clean_db(request, tmp_path, monkeypatch):
    if request.node.get_closest_marker("pure"):
        yield
        return
    request.getfixturevalue("ensure_test_database")
    monkeypatch.setenv("FILES_DIR", str(tmp_path))
    asyncio.run(_truncate())
    if not request.node.get_closest_marker("empty_users"):
        async def seed_owner():
            conn = await asyncpg.connect(os.environ["DATABASE_URL"])
            try:
                owner_id = await conn.fetchval("insert into bothub.users(email,password_hash,role) values('fixture@example.com','fixture','admin') returning id")
                await conn.execute("insert into bothub.settings(key,value) values('setup_user_id',to_jsonb($1::text)),('legacy_auth_enabled','true'::jsonb)",str(owner_id))
                for table in OWNED_TABLES:
                    await conn.execute(f"alter table bothub.{table} alter column owner_id set default '{owner_id}'::uuid")
            finally:
                await conn.close()
        asyncio.run(seed_owner())
    yield
    asyncio.run(_truncate())
