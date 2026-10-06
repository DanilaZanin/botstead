"""Database pool and ordered SQL migrations."""

import os
import json
from pathlib import Path

import asyncpg


async def open_pool() -> asyncpg.Pool:
    async def init(con):
        await con.set_type_codec('jsonb', schema='pg_catalog', encoder=lambda value: value if isinstance(value, str) else json.dumps(value), decoder=json.loads)

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], init=init)
    async with pool.acquire() as con:
        await con.execute("create schema if not exists bothub")
        await con.execute("create table if not exists bothub.schema_migrations (name text primary key, applied_at timestamptz not null default now())")
        for path in sorted((Path(__file__).parent / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
            async with con.transaction():
                await con.execute("select pg_advisory_xact_lock(hashtext('bothub_migrations'))")
                if not await con.fetchval("select 1 from bothub.schema_migrations where name=$1", path.name):
                    await con.execute(path.read_text())
                    await con.execute("insert into bothub.schema_migrations(name) values($1)", path.name)
    return pool
