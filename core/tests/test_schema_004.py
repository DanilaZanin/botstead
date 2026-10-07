"""Миграция 004_users_providers.sql: чистая БД, поверх 001-002, ограничения, mac_status id=1.

Миграционные тесты работают в одноразовых базах (bothub_t004_*), основная тестовая БД не трогается.
Исключение: проверка эндпоинта /api/mac/status идёт через приложение на основной тестовой БД.
"""
import asyncio
import hashlib
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
import pytest_asyncio

import bothub.db
from bothub.main import create_app

MIGRATIONS = Path(bothub.db.__file__).parent / "migrations"

NEW_TABLES = {"users", "invites", "sessions", "providers", "models", "procedures", "procedure_runs", "secrets"}
OWNED_TABLES = ["bots", "threads", "memory", "schedules", "files", "push_subscriptions", "outbox", "mac_status"]


def _db_url(name: str) -> str:
    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{name}"))


@pytest_asyncio.fixture
async def scratch_url():
    """Пустая одноразовая БД. DATABASE_URL указывает на неё, чтобы open_pool() мигрировал именно её.

    Переменную возвращаем вручную, не через monkeypatch: autouse-фикстура clean_db из conftest
    чистит основную БД в своём teardown, и к тому моменту DATABASE_URL уже должен быть прежним.
    """
    name = f"bothub_t004_{uuid.uuid4().hex[:12]}"
    previous = os.environ["DATABASE_URL"]
    admin = await asyncpg.connect(_db_url("postgres"))
    await admin.execute(f'create database "{name}"')
    url = _db_url(name)
    os.environ["DATABASE_URL"] = url
    try:
        yield url
    finally:
        os.environ["DATABASE_URL"] = previous
        await admin.execute(f'drop database if exists "{name}" with (force)')
        await admin.close()


@pytest_asyncio.fixture
async def pool(scratch_url):
    """Чистая БД, прогнанная через настоящий open_pool(): все миграции подряд."""
    pool = await bothub.db.open_pool()
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def con(pool):
    async with pool.acquire() as con:
        yield con


async def apply_old(url: str, *names: str) -> None:
    """Применяет только перечисленные миграции и записывает их в schema_migrations, как open_pool()."""
    con = await asyncpg.connect(url)
    try:
        await con.execute("create schema if not exists bothub")
        await con.execute(
            "create table if not exists bothub.schema_migrations "
            "(name text primary key, applied_at timestamptz not null default now())"
        )
        for name in names:
            await con.execute((MIGRATIONS / name).read_text())
            await con.execute("insert into bothub.schema_migrations(name) values($1)", name)
    finally:
        await con.close()


async def tables(con) -> set[str]:
    rows = await con.fetch("select table_name from information_schema.tables where table_schema='bothub'")
    return {r["table_name"] for r in rows}


async def user(con, email=None, role="member") -> uuid.UUID:
    email = email or f"{uuid.uuid4().hex[:8]}@example.com"
    return await con.fetchval(
        "insert into bothub.users(email,password_hash,role) values($1,'$argon2id$stub',$2) returning id", email, role
    )


async def bot(con, bot_id="scout", **cols) -> str:
    cols = {"name": "Scout", "provider": "fake", "model": "fake"} | cols
    names = ",".join(["id", *cols])
    marks = ",".join(f"${i}" for i in range(1, len(cols) + 2))
    await con.execute(f"insert into bothub.bots({names}) values({marks})", bot_id, *cols.values())
    return bot_id


async def provider(con, owner, name="main", kind="openai_api", **cols) -> uuid.UUID:
    cols = {"kind": kind, "name": name, "owner_id": owner} | cols
    names = ",".join(cols)
    marks = ",".join(f"${i}" for i in range(1, len(cols) + 1))
    return await con.fetchval(f"insert into bothub.providers({names}) values({marks}) returning id", *cols.values())


async def model(con, provider_id, name="gpt-x", **cols) -> uuid.UUID:
    cols = {"provider_id": provider_id, "name": name} | cols
    names = ",".join(cols)
    marks = ",".join(f"${i}" for i in range(1, len(cols) + 1))
    return await con.fetchval(f"insert into bothub.models({names}) values({marks}) returning id", *cols.values())


async def thread(con, bot_id="scout") -> uuid.UUID:
    return await con.fetchval("insert into bothub.threads(bot_id) values($1) returning id", bot_id)


async def rejected(exc, con, sql, *args):
    with pytest.raises(exc):
        await con.execute(sql, *args)


def sha(text: str = "token") -> str:
    """Значение в формате token_hash / id_hash: hex от sha256, 64 символа в нижнем регистре."""
    return hashlib.sha256(text.encode()).hexdigest()


Unique = asyncpg.UniqueViolationError
Fk = asyncpg.ForeignKeyViolationError
Check = asyncpg.CheckViolationError


# ---------------------------------------------------------------------------
# Применение миграции
# ---------------------------------------------------------------------------

async def test_fresh_db_gets_all_tables_and_records_migrations(con):
    assert NEW_TABLES <= await tables(con)
    applied = {r["name"] for r in await con.fetch("select name from bothub.schema_migrations")}
    assert {"001_init.sql", "002_risk_exec.sql", "004_users_providers.sql"} <= applied


async def test_migration_runs_once(scratch_url):
    first = await bothub.db.open_pool()
    await first.close()
    second = await bothub.db.open_pool()
    try:
        count = await second.fetchval("select count(*) from bothub.schema_migrations where name='004_users_providers.sql'")
        assert count == 1
    finally:
        await second.close()


async def test_migration_010_adds_only_manual_disable_after_004_through_009(scratch_url):
    await apply_old(
        scratch_url,
        "001_init.sql", "002_risk_exec.sql", "004_users_providers.sql",
        "005_reliability.sql", "006_auto_allow_cleanup.sql",
        "007_outbox_sending_approval_used.sql", "008_owner_not_null.sql",
        "009_instance_settings.sql",
    )
    before = await asyncpg.connect(scratch_url)
    try:
        owner_id = await user(before, role="admin")
        provider_id = await provider(before, owner_id)
        enabled_id = await model(before, provider_id, "enabled")
        disabled_id = await model(before, provider_id, "disabled", enabled=False)
        columns_before = {
            (row["table_name"], row["column_name"])
            for row in await before.fetch(
                "select table_name,column_name from information_schema.columns "
                "where table_schema='bothub'"
            )
        }
        assert ("models", "enabled") in columns_before
        assert ("models", "manually_disabled") not in columns_before
    finally:
        await before.close()

    await apply_old(scratch_url, "010_model_manual_disable.sql")
    migrated = await asyncpg.connect(scratch_url)
    try:
        columns_after = {
            (row["table_name"], row["column_name"])
            for row in await migrated.fetch(
                "select table_name,column_name from information_schema.columns "
                "where table_schema='bothub'"
            )
        }
        assert columns_after - columns_before == {("models", "manually_disabled")}
        assert columns_before - columns_after == set()
        rows = await migrated.fetch(
            "select id,enabled,manually_disabled from bothub.models order by name"
        )
        assert {row["id"]: (row["enabled"], row["manually_disabled"]) for row in rows} == {
            enabled_id: (True, False), disabled_id: (False, True),
        }
        assert await migrated.fetchval(
            "select count(*) from bothub.schema_migrations "
            "where name='010_model_manual_disable.sql'"
        ) == 1
    finally:
        await migrated.close()


async def test_migration_011_adds_only_need_restart_after_010(scratch_url):
    await apply_old(
        scratch_url,
        "001_init.sql", "002_risk_exec.sql", "004_users_providers.sql",
        "005_reliability.sql", "006_auto_allow_cleanup.sql",
        "007_outbox_sending_approval_used.sql", "008_owner_not_null.sql",
        "009_instance_settings.sql", "010_model_manual_disable.sql",
    )
    before = await asyncpg.connect(scratch_url)
    try:
        owner_id = await user(before, role="admin")
        await bot(before, "existing-bot", owner_id=owner_id)
        columns_before = {
            (row["table_name"], row["column_name"])
            for row in await before.fetch(
                "select table_name,column_name from information_schema.columns "
                "where table_schema='bothub'"
            )
        }
        assert ("bots", "need_restart") not in columns_before
    finally:
        await before.close()

    await apply_old(scratch_url, "011_bot_need_restart.sql")
    migrated = await asyncpg.connect(scratch_url)
    try:
        columns_after = {
            (row["table_name"], row["column_name"])
            for row in await migrated.fetch(
                "select table_name,column_name from information_schema.columns "
                "where table_schema='bothub'"
            )
        }
        assert columns_after - columns_before == {("bots", "need_restart")}
        assert columns_before - columns_after == set()
        assert await migrated.fetchval(
            "select need_restart from bothub.bots where id='existing-bot'"
        ) is False
        assert await migrated.fetchval(
            "select count(*) from bothub.schema_migrations "
            "where name='011_bot_need_restart.sql'"
        ) == 1
    finally:
        await migrated.close()


async def test_migration_012_widens_populated_usage_and_budget(scratch_url):
    await apply_old(
        scratch_url,
        "001_init.sql", "002_risk_exec.sql", "004_users_providers.sql",
        "005_reliability.sql", "006_auto_allow_cleanup.sql",
        "007_outbox_sending_approval_used.sql", "008_owner_not_null.sql",
        "009_instance_settings.sql", "010_model_manual_disable.sql",
        "011_bot_need_restart.sql",
    )
    old = await asyncpg.connect(scratch_url)
    try:
        owner_id = await user(old, role="admin")
        await bot(old, "wide-bot", owner_id=owner_id, budget_daily_tokens=2_000_000_000)
        usage_id = await old.fetchval(
            "insert into bothub.usage(bot_id,provider,model,tokens_in,tokens_out,tokens_cache_read,tokens_cache_write) "
            "values('wide-bot','fake','fake',500000000,500000000,500000000,500000000) returning id"
        )
    finally:
        await old.close()

    migrated_pool = await bothub.db.open_pool()
    try:
        async with migrated_pool.acquire() as migrated:
            rows = await migrated.fetch(
                "select table_name,column_name,data_type from information_schema.columns "
                "where table_schema='bothub' and "
                "((table_name='bots' and column_name='budget_daily_tokens') or "
                "(table_name='usage' and column_name=any($1::text[])))",
                ['tokens_in', 'tokens_out', 'tokens_cache_read', 'tokens_cache_write'],
            )
            assert len(rows) == 5 and all(row['data_type'] == 'bigint' for row in rows)
            assert await migrated.fetchval("select budget_daily_tokens from bothub.bots where id='wide-bot'") == 2_000_000_000
            assert tuple(await migrated.fetchrow(
                "select tokens_in,tokens_out,tokens_cache_read,tokens_cache_write from bothub.usage where id=$1", usage_id
            )) == (500_000_000,) * 4
            await migrated.execute("update bothub.bots set budget_daily_tokens=$1 where id='wide-bot'", 10**12)
            await migrated.execute("update bothub.usage set tokens_in=$2,tokens_out=$2,tokens_cache_read=$2,tokens_cache_write=$2 where id=$1", usage_id, 10**9)
            assert await migrated.fetchval(
                "select tokens_in::bigint+tokens_out::bigint+tokens_cache_read::bigint+tokens_cache_write::bigint "
                "from bothub.usage where id=$1", usage_id
            ) == 4 * 10**9
    finally:
        await migrated_pool.close()

    again = await bothub.db.open_pool()
    try:
        async with again.acquire() as migrated:
            assert await migrated.fetchval(
                "select count(*) from bothub.schema_migrations where name='012_usage_bigint.sql'"
            ) == 1
            assert await migrated.fetchval("select budget_daily_tokens from bothub.bots where id='wide-bot'") == 10**12
    finally:
        await again.close()


async def test_r4_legacy(scratch_url, monkeypatch):
    """A populated pre-registry bot keeps turns and schedules after every migration."""
    await apply_old(scratch_url, '001_init.sql', '002_risk_exec.sql', '004_users_providers.sql')
    old = await asyncpg.connect(scratch_url)
    try:
        owner_id = await user(old, role='admin')
        await old.execute("insert into bothub.bots(id,name,provider,model,owner_id) values('legacy-claude','Legacy','claude','sonnet',$1)",owner_id)
        thread_id = await old.fetchval("insert into bothub.threads(bot_id,owner_id) values('legacy-claude',$1) returning id",owner_id)
        schedule_id = await old.fetchval("insert into bothub.schedules(bot_id,name,kind,cron,timezone,prompt,owner_id) values('legacy-claude','daily','cron','* * * * *','UTC','ping',$1) returning id",owner_id)
    finally:
        await old.close()
    migrated_pool = await bothub.db.open_pool()
    # таблица настроек появляется в 009: признак первичной настройки пишется уже после миграций, как при обновлении
    async with migrated_pool.acquire() as con:
        await con.execute("insert into bothub.settings(key,value) values('setup_user_id',to_jsonb($1::text)),('legacy_auth_enabled','true'::jsonb) on conflict(key) do update set value=excluded.value",str(owner_id))
    await migrated_pool.close()
    monkeypatch.setenv('BOTHUB_LEGACY_AUTH','true')
    app = create_app()
    async def idle():
        await asyncio.Event().wait()
    app.state.worker = app.state.scheduler = app.state.outbox_sender = idle
    async with app.router.lifespan_context(app):
        async with app.state.pool.acquire() as con:
            assert await con.fetchval("select registry_bound from bothub.bots where id='legacy-claude'") is False
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response = await client.post(f'/api/threads/{thread_id}/turns',headers={'Authorization':'Bearer test-owner'},json={'prompt':'hello'})
            assert response.status_code == 200, response.text
            response = await client.post(f'/api/schedules/{schedule_id}/run',headers={'Authorization':'Bearer test-owner'})
            assert response.status_code == 200, response.text


async def test_migration_on_top_of_001_002_keeps_data(scratch_url):
    await apply_old(scratch_url, "001_init.sql", "002_risk_exec.sql")
    old = await asyncpg.connect(scratch_url)
    try:
        await old.execute("insert into bothub.bots(id,name,provider,model) values('scout','Scout','claude','claude-sonnet-5')")
        thread_id = await old.fetchval("insert into bothub.threads(bot_id,title) values('scout','old') returning id")
        await old.execute("insert into bothub.memory(text) values('помню')")
        await old.execute("insert into bothub.schedules(bot_id,name,kind,prompt) values('scout','daily','cron','go')")
        await old.execute(
            "insert into bothub.files(thread_id,name,origin,size,mime,storage_path) values($1,'a.txt','upload',1,'text/plain','/f/a')",
            thread_id,
        )
        await old.execute("insert into bothub.push_subscriptions(endpoint,keys) values('https://push/1','{}')")
        await old.execute("insert into bothub.outbox(kind,payload) values('push','{}')")
        await old.execute("insert into bothub.mac_status(id,state,last_seen,info) values(1,'online',now(),'{\"host\":\"mac\"}')")
        await old.execute("insert into bothub.usage(bot_id,provider,model,tokens_in,tokens_out) values('scout','claude','claude-sonnet-5',5,6)")
    finally:
        await old.close()

    pool = await bothub.db.open_pool()
    try:
        async with pool.acquire() as con:
            assert NEW_TABLES <= await tables(con)
            assert await con.fetchval("select count(*) from bothub.bots") == 1
            row = await con.fetchrow("select * from bothub.bots where id='scout'")
            assert (row["provider"], row["model"]) == ("claude", "claude-sonnet-5")
            assert row["provider_id"] is None and row["model_id"] is None and row["tool_modes"] == {}
            usage = await con.fetchrow("select * from bothub.usage")
            assert (usage["tokens_in"], usage["tokens_out"]) == (5, 6)
            assert usage["provider_id"] is None and usage["model_id"] is None
            assert (usage["tokens_cache_read"], usage["tokens_cache_write"]) == (0, 0)
            for table in OWNED_TABLES:
                assert await con.fetchval(f"select count(*) from bothub.{table} where owner_id is not null") == 0, table
                assert await con.fetchval(f"select count(*) from bothub.{table}") == 1, table
            mac = await con.fetchrow("select * from bothub.mac_status where id=1")
            assert mac["state"] == "online" and mac["info"] == {"host": "mac"}
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# users, invites, sessions
# ---------------------------------------------------------------------------

async def test_users_email_unique_and_lowercase(con):
    await user(con, "owner@example.com", role="admin")
    await rejected(Unique, con, "insert into bothub.users(email,password_hash) values('owner@example.com','x')")
    await rejected(Check, con, "insert into bothub.users(email,password_hash) values('Other@Example.com','x')")


async def test_users_role_and_status_checked(con):
    await rejected(Check, con, "insert into bothub.users(email,password_hash,role) values('a@x.io','x','root')")
    await rejected(Check, con, "insert into bothub.users(email,password_hash,status) values('b@x.io','x','banned')")
    row = await con.fetchrow("insert into bothub.users(email,password_hash) values('c@x.io','x') returning role,status,created_at")
    assert (row["role"], row["status"]) == ("member", "active") and row["created_at"] is not None


async def test_invites_constraints(con):
    admin = await user(con, role="admin")
    expires = "now() + interval '1 day'"
    h1, h2, h3 = sha("1"), sha("2"), sha("3")
    await con.execute(f"insert into bothub.invites(token_hash,created_by,expires_at) values($1,$2,{expires})", h1, admin)
    await rejected(Unique, con, f"insert into bothub.invites(token_hash,created_by,expires_at) values($1,$2,{expires})", h1, admin)
    await rejected(Fk, con, f"insert into bothub.invites(token_hash,created_by,expires_at) values($1,$2,{expires})", h2, uuid.uuid4())
    await rejected(Check, con, f"insert into bothub.invites(token_hash,created_by,role,expires_at) values($1,$2,'owner',{expires})", h3, admin)
    # used_by без used_at бессмыслен
    invitee = await user(con)
    await rejected(Check, con, "update bothub.invites set used_by=$1 where token_hash=$2", invitee, h1)
    await con.execute("update bothub.invites set used_by=$1, used_at=now() where token_hash=$2", invitee, h1)


@pytest.mark.parametrize("bad", ["h1", "", sha()[:63], sha() + "0", sha().upper(), "g" * 64, " " + sha()[1:]])
async def test_invite_token_hash_must_be_sha256_hex(con, bad):
    admin = await user(con, role="admin")
    await rejected(Check, con, "insert into bothub.invites(token_hash,created_by,expires_at) values($1,$2,now()+interval '1 day')", bad, admin)


async def test_user_with_invites_cannot_be_deleted(con):
    admin = await user(con, role="admin")
    await con.execute("insert into bothub.invites(token_hash,created_by,expires_at) values($1,$2,now()+interval '1 day')", sha("1"), admin)
    await rejected(Fk, con, "delete from bothub.users where id=$1", admin)


async def test_sessions_fk_and_cascade(con):
    owner = await user(con)
    s1, s2 = sha("s1"), sha("s2")
    await con.execute("insert into bothub.sessions(id_hash,user_id,expires_at) values($1,$2,now()+interval '1 day')", s1, owner)
    await rejected(Unique, con, "insert into bothub.sessions(id_hash,user_id,expires_at) values($1,$2,now()+interval '1 day')", s1, owner)
    await rejected(Fk, con, "insert into bothub.sessions(id_hash,user_id,expires_at) values($1,$2,now()+interval '1 day')", s2, uuid.uuid4())
    row = await con.fetchrow("select user_agent, revoked_at from bothub.sessions where id_hash=$1", s1)
    assert row["user_agent"] == "" and row["revoked_at"] is None
    await con.execute("delete from bothub.users where id=$1", owner)
    assert await con.fetchval("select count(*) from bothub.sessions") == 0


@pytest.mark.parametrize("bad", ["s1", "", sha()[:63], sha() + "0", sha().upper(), "z" * 64])
async def test_session_id_hash_must_be_sha256_hex(con, bad):
    owner = await user(con)
    await rejected(Check, con, "insert into bothub.sessions(id_hash,user_id,expires_at) values($1,$2,now()+interval '1 day')", bad, owner)


# ---------------------------------------------------------------------------
# providers, models
# ---------------------------------------------------------------------------

async def test_provider_name_unique_per_owner(con):
    a, b = await user(con), await user(con)
    await provider(con, a, "main")
    await rejected(Unique, con, "insert into bothub.providers(owner_id,kind,name) values($1,'openai_api','main')", a)
    await provider(con, b, "main")  # у другого владельца то же имя допустимо


async def test_provider_requires_owner_and_known_kind(con):
    owner = await user(con)
    await rejected(Check, con, "insert into bothub.providers(owner_id,kind,name) values($1,'azure','x')", owner)
    await rejected(Fk, con, "insert into bothub.providers(owner_id,kind,name) values($1,'openai_api','x')", uuid.uuid4())
    with pytest.raises(asyncpg.NotNullViolationError):
        await con.execute("insert into bothub.providers(kind,name) values('openai_api','x')")


async def test_provider_kind_shape(con):
    owner = await user(con)
    await rejected(Check, con, "insert into bothub.providers(owner_id,kind,name) values($1,'cli_subscription','c')", owner)
    await rejected(Check, con, "insert into bothub.providers(owner_id,kind,cli,name) values($1,'openai_api','codex','c')", owner)
    await rejected(Check, con, "insert into bothub.providers(owner_id,kind,cli,name) values($1,'cli_subscription','bash','c')", owner)
    await rejected(Check, con, "insert into bothub.providers(owner_id,kind,name) values($1,'openai_compatible','local')", owner)
    await rejected(
        Check, con,
        "insert into bothub.providers(owner_id,kind,cli,name,secret_encrypted) values($1,'cli_subscription','claude','c',$2)",
        owner, b"\x01",
    )
    ok = await con.fetchrow(
        "insert into bothub.providers(owner_id,kind,cli,name) values($1,'cli_subscription','claude','sub') returning status,last_check_at",
        owner,
    )
    assert ok["status"] == "new" and ok["last_check_at"] is None
    await provider(con, owner, "local", kind="openai_compatible", base_url="https://llm.example.com/v1")


async def test_provider_secret_is_bytea_and_status_checked(con):
    owner = await user(con)
    secret = b"\x01" + os.urandom(40)
    pid = await provider(con, owner, "k", secret_encrypted=secret)
    assert await con.fetchval("select secret_encrypted from bothub.providers where id=$1", pid) == secret
    await rejected(Check, con, "update bothub.providers set status='broken' where id=$1", pid)
    await con.execute("update bothub.providers set status='error', last_error='401', last_check_at=now() where id=$1", pid)


async def test_models_unique_per_provider_and_cascade(con):
    owner = await user(con)
    p1, p2 = await provider(con, owner, "p1"), await provider(con, owner, "p2")
    await model(con, p1, "gpt-x")
    await model(con, p2, "gpt-x")  # то же имя у другого провайдера допустимо
    await rejected(Unique, con, "insert into bothub.models(provider_id,name) values($1,'gpt-x')", p1)
    await rejected(Fk, con, "insert into bothub.models(provider_id,name) values($1,'m')", uuid.uuid4())
    await rejected(Check, con, "insert into bothub.models(provider_id,name,context_window) values($1,'big',0)", p1)
    row = await con.fetchrow("select enabled, display_name, context_window from bothub.models where provider_id=$1", p1)
    assert row["enabled"] is True and row["display_name"] == "" and row["context_window"] is None
    await con.execute("delete from bothub.providers where id=$1", p1)
    assert await con.fetchval("select count(*) from bothub.models") == 1


async def test_bot_provider_and_model_are_optional_and_checked(con):
    owner = await user(con)
    p = await provider(con, owner)
    m = await model(con, p)
    await bot(con, "plain")
    row = await con.fetchrow("select provider_id, model_id from bothub.bots where id='plain'")
    assert row["provider_id"] is None and row["model_id"] is None
    await bot(con, "ok", owner_id=owner, provider_id=p, model_id=m)
    await bot(con, "only-provider", owner_id=owner, provider_id=p)
    await bot(con, "owned-no-registry", owner_id=owner)
    with pytest.raises(Fk):
        await bot(con, "ghost-provider", owner_id=owner, provider_id=uuid.uuid4())
    with pytest.raises(Fk):
        await bot(con, "ghost-model", owner_id=owner, provider_id=p, model_id=uuid.uuid4())


async def test_bot_cannot_use_provider_of_another_owner(con):
    a, b = await user(con), await user(con)
    pb = await provider(con, b, "theirs")
    mb = await model(con, pb)
    # бот A + провайдер B: составной FK (provider_id, owner_id) -> providers(id, owner_id)
    with pytest.raises(Fk):
        await bot(con, "a-bot", owner_id=a, provider_id=pb)
    with pytest.raises(Fk):
        await bot(con, "a-bot", owner_id=a, provider_id=pb, model_id=mb)
    # то же через UPDATE существующего бота и через смену владельца
    await bot(con, "a-bot", owner_id=a)
    await rejected(Fk, con, "update bothub.bots set provider_id=$1 where id='a-bot'", pb)
    await bot(con, "b-bot", owner_id=b, provider_id=pb, model_id=mb)
    await rejected(Fk, con, "update bothub.bots set owner_id=$1 where id='b-bot'", a)
    assert await con.fetchval("select count(*) from bothub.bots where provider_id is not null") == 1


async def test_bot_cannot_use_model_of_another_provider(con):
    owner = await user(con)
    p1, p2 = await provider(con, owner, "p1"), await provider(con, owner, "p2")
    m1, m2 = await model(con, p1), await model(con, p2)
    # оба провайдера свои, но модель m2 принадлежит p2: составной FK (model_id, provider_id) -> models(id, provider_id)
    with pytest.raises(Fk):
        await bot(con, "mixed", owner_id=owner, provider_id=p1, model_id=m2)
    await bot(con, "scout", owner_id=owner, provider_id=p1, model_id=m1)
    await rejected(Fk, con, "update bothub.bots set model_id=$1 where id='scout'", m2)
    await rejected(Fk, con, "update bothub.bots set provider_id=$1 where id='scout'", p2)  # модель m1 осталась бы чужой
    await con.execute("update bothub.bots set provider_id=$1, model_id=$2 where id='scout'", p2, m2)
    row = await con.fetchrow("select provider_id, model_id from bothub.bots where id='scout'")
    assert (row["provider_id"], row["model_id"]) == (p2, m2)


async def test_bot_registry_columns_need_owner_and_provider(con):
    owner = await user(con)
    p = await provider(con, owner)
    m = await model(con, p)
    # FK с null в паре не проверяется (MATCH SIMPLE): дыры закрывают CHECK
    with pytest.raises(Check):
        await bot(con, "no-owner", provider_id=p)
    with pytest.raises(Check):
        await bot(con, "no-provider", owner_id=owner, model_id=m)
    await bot(con, "scout", owner_id=owner, provider_id=p, model_id=m)
    await rejected(Check, con, "update bothub.bots set owner_id=null where id='scout'")
    # снятие провайдера снимает и модель (триггер), иначе удаление провайдера упиралось бы в CHECK
    await con.execute("update bothub.bots set provider_id=null where id='scout'")
    assert await con.fetchval("select model_id from bothub.bots where id='scout'") is None


async def test_deleting_model_keeps_bot_and_provider(con):
    owner = await user(con)
    p = await provider(con, owner)
    m = await model(con, p)
    await bot(con, "scout", owner_id=owner, provider_id=p, model_id=m)
    await con.execute("delete from bothub.models where id=$1", m)
    row = await con.fetchrow("select owner_id, provider_id, model_id from bothub.bots where id='scout'")
    assert (row["owner_id"], row["provider_id"], row["model_id"]) == (owner, p, None)


async def test_deleting_provider_detaches_bots(con):
    owner = await user(con)
    p, other = await provider(con, owner), await provider(con, owner, "other")
    m, other_m = await model(con, p), await model(con, other)
    await bot(con, "scout", owner_id=owner, provider_id=p, model_id=m)
    await bot(con, "only-provider", owner_id=owner, provider_id=p)
    await bot(con, "keeps", owner_id=owner, provider_id=other, model_id=other_m)
    await con.execute("delete from bothub.providers where id=$1", p)
    for bot_id in ("scout", "only-provider"):
        row = await con.fetchrow("select owner_id, provider, model, provider_id, model_id from bothub.bots where id=$1", bot_id)
        assert row["provider_id"] is None and row["model_id"] is None, bot_id
        assert (row["owner_id"], row["provider"], row["model"]) == (owner, "fake", "fake"), bot_id  # владелец и текстовые поля целы
    row = await con.fetchrow("select provider_id, model_id from bothub.bots where id='keeps'")
    assert (row["provider_id"], row["model_id"]) == (other, other_m)


async def test_deleting_provider_nulls_model_of_every_bot(con):
    # каскад удаления models и обнуление provider_id идут в одном DELETE: model_id не должен остаться висеть
    owner = await user(con)
    p = await provider(con, owner)
    for i in range(3):
        await bot(con, f"bot-{i}", owner_id=owner, provider_id=p, model_id=await model(con, p, f"m{i}"))
    await con.execute("delete from bothub.providers where id=$1", p)
    assert await con.fetchval("select count(*) from bothub.models") == 0
    assert await con.fetchval("select count(*) from bothub.bots where provider_id is null and model_id is null") == 3


async def test_provider_and_model_have_composite_fk_targets(con):
    rows = await con.fetch(
        """
        select conrelid::regclass::text as tbl, pg_get_constraintdef(oid) as def
        from pg_constraint
        where connamespace = 'bothub'::regnamespace and contype = 'u'
          and conrelid in ('bothub.providers'::regclass, 'bothub.models'::regclass)
        """
    )
    defs = {(r["tbl"].split(".")[-1], r["def"]) for r in rows}
    assert ("providers", "UNIQUE (id, owner_id)") in defs
    assert ("models", "UNIQUE (id, provider_id)") in defs


# ---------------------------------------------------------------------------
# bots.tool_modes, id и имя бота
# ---------------------------------------------------------------------------

async def test_bot_tool_modes_default_and_shape(con):
    await bot(con, "plain")
    assert await con.fetchval("select tool_modes from bothub.bots where id='plain'") == {}
    modes = '{"mcp__bothub__mac_find_files":"allow","Bash":"deny"}'
    await con.execute("update bothub.bots set tool_modes=$1::jsonb where id='plain'", modes)
    assert await con.fetchval("select tool_modes from bothub.bots where id='plain'") == {"mcp__bothub__mac_find_files": "allow", "Bash": "deny"}
    await rejected(Check, con, "update bothub.bots set tool_modes='[]'::jsonb where id='plain'")
    await rejected(Check, con, "update bothub.bots set tool_modes='\"allow\"'::jsonb where id='plain'")
    with pytest.raises(asyncpg.NotNullViolationError):
        await con.execute("update bothub.bots set tool_modes=null where id='plain'")


async def test_bot_id_slug_plus_random_suffix_and_visible_name(con):
    # раздел 10: новые боты id = <slug>-<4 случайных [a-z0-9]>, видимое имя живёт отдельно в name; старые id не трогаем
    fresh = {"tax-helper-a3f9": "Налоговик", "vakansii-0k2z": "Вакансии & отчёты", "bot-zzzz": "Бот №1"}
    for bot_id, name in fresh.items():
        await bot(con, bot_id, name=name)
    for legacy in ("scout", "mac", "sre"):
        await bot(con, legacy, name=legacy.title())
    rows = {r["id"]: r["name"] for r in await con.fetch("select id, name from bothub.bots")}
    for bot_id, name in fresh.items():
        assert re.fullmatch(r"[a-z0-9-]{1,27}-[a-z0-9]{4}", bot_id), bot_id  # часть 1: формат id
        assert re.fullmatch(r"[a-z0-9-]{1,32}", bot_id), bot_id               # и он проходит проверку раздела 9
        assert rows[bot_id] == name                                            # часть 2: имя отдельно, не выводится из id
    assert {rows[k] for k in ("scout", "mac", "sre")} == {"Scout", "Mac", "Sre"}
    await rejected(Unique, con, "insert into bothub.bots(id,name,provider,model) values('tax-helper-a3f9','Дубль','fake','fake')")
    # два бота с одним видимым именем различаются суффиксом id
    await bot(con, "tax-helper-b7c1", name="Налоговик")
    assert await con.fetchval("select count(*) from bothub.bots where name='Налоговик'") == 2


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------

async def secret(con, owner, name="bank-pass", bot_id=None, value=b"\x01blob") -> uuid.UUID:
    return await con.fetchval(
        "insert into bothub.secrets(owner_id,bot_id,name,value_encrypted) values($1,$2,$3,$4) returning id", owner, bot_id, name, value
    )


async def test_secrets_shape_defaults_and_bytea(con):
    owner = await user(con)
    await bot(con, "scout")
    value = b"\x01" + os.urandom(12) + os.urandom(20)
    sid = await secret(con, owner, "bank", "scout", value)
    row = await con.fetchrow("select * from bothub.secrets where id=$1", sid)
    assert (row["owner_id"], row["bot_id"], row["name"], row["value_encrypted"]) == (owner, "scout", "bank", value)
    assert row["created_at"] is not None
    with pytest.raises(asyncpg.NotNullViolationError):
        await con.execute("insert into bothub.secrets(bot_id,name,value_encrypted) values('scout','x','\\x01')")
    with pytest.raises(asyncpg.NotNullViolationError):
        await con.execute("insert into bothub.secrets(owner_id,name) values($1,'x')", owner)
    await rejected(Fk, con, "insert into bothub.secrets(owner_id,name,value_encrypted) values($1,'x','\\x01')", uuid.uuid4())
    await rejected(Fk, con, "insert into bothub.secrets(owner_id,bot_id,name,value_encrypted) values($1,'nobody','x','\\x01')", owner)


async def test_secrets_unique_per_owner_bot_and_name(con):
    a, b = await user(con), await user(con)
    await bot(con, "scout")
    await bot(con, "mac")
    await secret(con, a, "pass", "scout")
    await rejected(Unique, con, "insert into bothub.secrets(owner_id,bot_id,name,value_encrypted) values($1,'scout','pass','\\x01')", a)
    await secret(con, a, "pass", "mac")      # то же имя у другого бота
    await secret(con, a, "other", "scout")   # другое имя у того же бота
    await secret(con, b, "pass", "scout")    # то же имя у другого владельца
    # общие секреты (bot_id null): null считается равным null, дубль не проходит
    await secret(con, a, "pass")
    await rejected(Unique, con, "insert into bothub.secrets(owner_id,name,value_encrypted) values($1,'pass','\\x01')", a)
    await secret(con, b, "pass")


async def test_deleting_bot_cascades_its_secrets_only(con):
    owner = await user(con)
    await bot(con, "scout")
    await bot(con, "mac")
    await secret(con, owner, "a", "scout")
    await secret(con, owner, "b", "mac")
    shared = await secret(con, owner, "shared")
    await con.execute("delete from bothub.bots where id='scout'")
    names = {r["name"] for r in await con.fetch("select name from bothub.secrets")}
    assert names == {"b", "shared"}
    assert await con.fetchval("select id from bothub.secrets where name='shared'") == shared


async def test_user_with_secrets_cannot_be_deleted(con):
    owner = await user(con)
    await secret(con, owner)
    await rejected(Fk, con, "delete from bothub.users where id=$1", owner)


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

async def test_usage_registry_columns_defaults_and_set_null(con):
    owner = await user(con)
    p = await provider(con, owner)
    m = await model(con, p)
    await bot(con, "scout", owner_id=owner, provider_id=p, model_id=m)
    plain = await con.fetchrow(
        "insert into bothub.usage(bot_id,provider,model) values('scout','claude','sonnet') "
        "returning provider_id, model_id, tokens_in, tokens_out, tokens_cache_read, tokens_cache_write"
    )
    assert plain["provider_id"] is None and plain["model_id"] is None
    assert (plain["tokens_in"], plain["tokens_out"], plain["tokens_cache_read"], plain["tokens_cache_write"]) == (0, 0, 0, 0)
    uid = await con.fetchval(
        "insert into bothub.usage(bot_id,provider,model,provider_id,model_id,tokens_in,tokens_out,tokens_cache_read,tokens_cache_write) "
        "values('scout','openai','gpt-x',$1,$2,10,20,300,40) returning id", p, m,
    )
    row = await con.fetchrow("select * from bothub.usage where id=$1", uid)
    assert (row["provider_id"], row["model_id"]) == (p, m)
    assert (row["tokens_cache_read"], row["tokens_cache_write"]) == (300, 40)
    await rejected(Fk, con, "insert into bothub.usage(bot_id,provider,model,provider_id) values('scout','x','x',$1)", uuid.uuid4())
    await rejected(Fk, con, "insert into bothub.usage(bot_id,provider,model,model_id) values('scout','x','x',$1)", uuid.uuid4())
    # модель исчезла: расход остаётся, ссылка на неё обнуляется
    await con.execute("delete from bothub.models where id=$1", m)
    row = await con.fetchrow("select provider_id, model_id from bothub.usage where id=$1", uid)
    assert (row["provider_id"], row["model_id"]) == (p, None)
    # провайдер удалён: история расхода сохраняется
    await con.execute("delete from bothub.providers where id=$1", p)
    assert await con.fetchval("select count(*) from bothub.usage where provider_id is null and model_id is null") == 2


# ---------------------------------------------------------------------------
# owner_id
# ---------------------------------------------------------------------------

async def test_owner_id_is_nullable_fk_to_users_everywhere(con):
    rows = await con.fetch(
        """
        select c.relname as tbl, a.attnotnull as not_null, rc.relname as ref
        from pg_attribute a
        join pg_class c on c.oid = a.attrelid
        join pg_namespace n on n.oid = c.relnamespace
        join pg_constraint k on k.conrelid = c.oid and k.contype = 'f' and k.conkey = array[a.attnum]
        join pg_class rc on rc.oid = k.confrelid
        where n.nspname = 'bothub' and a.attname = 'owner_id'
        """
    )
    found = {r["tbl"]: (r["not_null"], r["ref"]) for r in rows}
    for table in OWNED_TABLES:
        assert found.get(table) == (False, "users"), table
    # в новых таблицах владелец обязателен
    assert found["providers"] == (True, "users") and found["procedures"] == (True, "users")


def _insert_owned(table: str):
    """SQL вставки строки в таблицу, $1 = owner_id."""
    return {
        "bots": "insert into bothub.bots(id,name,provider,model,owner_id) values('b-'||gen_random_uuid()::text,'n','fake','fake',$1)",
        "threads": "insert into bothub.threads(bot_id,owner_id) values('scout',$1)",
        "memory": "insert into bothub.memory(text,owner_id) values('t',$1)",
        "schedules": "insert into bothub.schedules(bot_id,name,kind,prompt,owner_id) values('scout','s','cron','p',$1)",
        "files": "insert into bothub.files(thread_id,name,origin,size,mime,storage_path,owner_id) "
                 "select id,'f','upload',1,'text/plain','/f',$1::uuid from bothub.threads limit 1",
        "push_subscriptions": "insert into bothub.push_subscriptions(endpoint,keys,owner_id) values('e-'||gen_random_uuid()::text,'{}',$1::uuid)",
        "outbox": "insert into bothub.outbox(kind,payload,owner_id) values('push','{}',$1)",
        "mac_status": "insert into bothub.mac_status(owner_id) values($1)",
    }[table]


@pytest.mark.parametrize("table", OWNED_TABLES)
async def test_owner_id_fk_enforced_and_null_allowed(con, table):
    await bot(con, "scout")
    await thread(con)
    owner = await user(con)
    sql = _insert_owned(table)
    await con.execute(sql, owner)
    await con.execute(sql, None)
    await rejected(Fk, con, sql, uuid.uuid4())
    assert await con.fetchval(f"select count(*) from bothub.{table} where owner_id=$1", owner) == 1
    # владельца с данными удалить нельзя: пользователей отключают, а не удаляют
    await rejected(Fk, con, "delete from bothub.users where id=$1", owner)


# ---------------------------------------------------------------------------
# procedures
# ---------------------------------------------------------------------------

async def test_procedures_constraints(con):
    owner = await user(con)
    await bot(con, "scout")
    pid = await con.fetchval(
        "insert into bothub.procedures(owner_id,bot_id,name,steps) values($1,'scout','login-bank',$2::jsonb) returning id",
        owner, '[{"action":"navigate","target":{"url":"https://example.com"}}]',
    )
    row = await con.fetchrow("select version, source, status, params from bothub.procedures where id=$1", pid)
    assert (row["version"], row["source"], row["status"], row["params"]) == (1, "bot", "active", [])
    await rejected(Unique, con, "insert into bothub.procedures(owner_id,name) values($1,'login-bank')", owner)
    await rejected(Check, con, "insert into bothub.procedures(owner_id,name,steps) values($1,'x','{}'::jsonb)", owner)
    await rejected(Check, con, "insert into bothub.procedures(owner_id,name,source) values($1,'y','robot')", owner)
    await rejected(Fk, con, "insert into bothub.procedures(owner_id,name) values($1,'z')", uuid.uuid4())
    await rejected(Fk, con, "insert into bothub.procedures(owner_id,bot_id,name) values($1,'nobody','w')", owner)
    # то же имя у другого пользователя допустимо
    await con.execute("insert into bothub.procedures(owner_id,name) values($1,'login-bank')", await user(con))
    # удаление бота оставляет процедуру
    await con.execute("delete from bothub.bots where id='scout'")
    assert await con.fetchval("select bot_id from bothub.procedures where id=$1", pid) is None


async def test_procedure_runs_constraints_and_cascade(con):
    owner = await user(con)
    pid = await con.fetchval("insert into bothub.procedures(owner_id,name) values($1,'p') returning id", owner)
    run = await con.fetchrow(
        "insert into bothub.procedure_runs(procedure_id,procedure_version) values($1,1) returning id,status,next_step,step_log,params", pid
    )
    assert (run["status"], run["next_step"], run["step_log"], run["params"]) == ("queued", 0, [], {})
    await rejected(Check, con, "update bothub.procedure_runs set status='paused' where id=$1", run["id"])
    await rejected(Check, con, "update bothub.procedure_runs set next_step=-1 where id=$1", run["id"])
    await rejected(Fk, con, "insert into bothub.procedure_runs(procedure_id,procedure_version) values($1,1)", uuid.uuid4())
    for status in ("running", "waiting_approval", "waiting_model", "waiting_human", "done", "failed", "stopped"):
        await con.execute("update bothub.procedure_runs set status=$2 where id=$1", run["id"], status)
    await con.execute("delete from bothub.procedures where id=$1", pid)
    assert await con.fetchval("select count(*) from bothub.procedure_runs") == 0


# ---------------------------------------------------------------------------
# mac_status
# ---------------------------------------------------------------------------

async def test_mac_status_id_1_queries_from_core_still_work(con):
    # дословно запросы из bothub/main.py (/api/mac/status, /agent/mac)
    assert await con.fetchrow("select * from bothub.mac_status where id=1") is None
    await con.execute(
        "insert into bothub.mac_status(id,state,last_seen,info) values(1,'online',now(),$1::jsonb) "
        "on conflict(id) do update set state='online',last_seen=now(),info=$1::jsonb", '{"host":"mac"}',
    )
    await con.execute(
        "insert into bothub.mac_status(id,state,last_seen,info) values(1,$1,now(),$2::jsonb) "
        "on conflict(id) do update set state=$1,last_seen=now(),info=bothub.mac_status.info || $2::jsonb",
        "locked", '{"locked":true}',
    )
    row = await con.fetchrow("select * from bothub.mac_status where id=1")
    assert row["state"] == "locked" and row["info"] == {"host": "mac", "locked": True}
    assert await con.fetchval("select state from bothub.mac_status where id=1 and last_seen>now()-interval '90 seconds'") == "locked"
    await con.execute("update bothub.mac_status set state='offline' where id=1")
    assert await con.fetchval("select state from bothub.mac_status where id=1") == "offline"
    assert await con.fetchval("select count(*) from bothub.mac_status") == 1


async def test_mac_status_no_longer_pinned_to_id_1(con):
    owner = await user(con)
    await con.execute("insert into bothub.mac_status(id,state) values(1,'online')")
    # раньше любая строка кроме id=1 нарушала check; теперь id выдаётся последовательностью со 2
    a = await con.fetchval("insert into bothub.mac_status(owner_id,state) values($1,'sleep') returning id", owner)
    b = await con.fetchval("insert into bothub.mac_status(owner_id) values($1) returning id", owner)
    assert a >= 2 and b > a
    await con.execute("insert into bothub.mac_status(id) values(42)")
    # старый путь чтения по-прежнему видит ровно свою строку
    row = await con.fetchrow("select * from bothub.mac_status where id=1")
    assert row["state"] == "online" and row["owner_id"] is None
    assert await con.fetchval("select state from bothub.mac_status where id=$1", a) == "sleep"
    await rejected(Check, con, "update bothub.mac_status set state='broken' where id=1")


async def test_api_mac_status_reads_first_registered_mac_for_owner():
    app = create_app(lambda provider: None)
    async with app.router.lifespan_context(app):
        try:
            async with app.state.pool.acquire() as con:
                owner = await con.fetchval("select (value #>> '{}')::uuid from bothub.settings where key='setup_user_id'")
                other_owner = await user(con)
                await con.execute("insert into bothub.mac_status(id,state,last_seen,info) values(1,'online',now(),'{\"host\":\"legacy\"}')")
                first = await con.fetchval(
                    "insert into bothub.macs(owner_id,name,created_at,last_seen_at) "
                    "values($1,'First',now()-interval '2 minutes',now()) returning id", owner
                )
                second = await con.fetchval(
                    "insert into bothub.macs(owner_id,name,created_at,last_seen_at) "
                    "values($1,'Second',now()-interval '1 minute',now()) returning id", owner
                )
                outsider = await con.fetchval(
                    "insert into bothub.macs(owner_id,name,created_at,last_seen_at) "
                    "values($1,'Other',now()-interval '3 minutes',now()) returning id", other_owner
                )
                for mac_id, host, mac_owner in ((first, 'first', owner), (second, 'second', owner), (outsider, 'other', other_owner)):
                    await con.execute(
                        "insert into bothub.mac_status(owner_id,mac_id,state,last_seen,info) "
                        "values($1,$2,'online',now(),jsonb_build_object('host',$3::text))",
                        mac_owner, mac_id, host,
                    )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/mac/status", headers={"Authorization": "Bearer test-owner"})
            assert response.status_code == 200
            body = response.json()
            assert set(body) == {"state", "last_seen", "info"}
            assert body["state"] == "offline"  # no live WebSocket
            assert body["info"] == {"host": "first"}
        finally:
            async with app.state.pool.acquire() as con:
                await con.execute("truncate bothub.mac_status, bothub.users cascade")


async def test_conftest_truncate_clears_every_table():
    # основная тестовая БД: после прогона conftest._truncate_tables не остаётся строк ни в одной таблице схемы
    import conftest

    app = create_app(lambda provider: None)
    async with app.router.lifespan_context(app):
        async with app.state.pool.acquire() as con:
            owner = await user(con)
            await bot(con, "scout", owner_id=owner)
            p = await provider(con, owner)
            m = await model(con, p)
            await con.execute("update bothub.bots set provider_id=$1, model_id=$2 where id='scout'", p, m)
            await con.execute("insert into bothub.invites(token_hash,created_by,expires_at) values($1,$2,now()+interval '1 day')", sha("i"), owner)
            await con.execute("insert into bothub.sessions(id_hash,user_id,expires_at) values($1,$2,now()+interval '1 day')", sha("s"), owner)
            await con.execute("insert into bothub.secrets(owner_id,bot_id,name,value_encrypted) values($1,'scout','pw','\\x01')", owner)
            proc = await con.fetchval("insert into bothub.procedures(owner_id,name) values($1,'p') returning id", owner)
            await con.execute("insert into bothub.procedure_runs(procedure_id,procedure_version) values($1,1)", proc)
            await con.execute("insert into bothub.usage(bot_id,provider,model,provider_id,model_id) values('scout','x','x',$1,$2)", p, m)
            await conftest._truncate_tables()
            names = [r["table_name"] for r in await con.fetch(
                "select table_name from information_schema.tables where table_schema='bothub' and table_type='BASE TABLE'"
            )]
            for name in names:
                if name == "schema_migrations":
                    continue
                assert await con.fetchval(f"select count(*) from bothub.{name}") == 0, name
