"""Check ownership without the test fixture's owner_id column defaults."""

import uuid

import asyncpg
import httpx
import pytest

from bothub.main import create_app


pytestmark = [pytest.mark.asyncio, pytest.mark.empty_users]
OWNER = {"Authorization": "Bearer test-owner"}


async def test_owner_ids_are_explicit_without_column_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("FILES_DIR", str(tmp_path))
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            setup = await client.post("/api/setup", json={"email": "a@example.com", "password": "long-password"}, headers=OWNER)
            assert setup.status_code == 201, setup.text
            owner = uuid.UUID(setup.json()["id"])

            async with app.state.pool.acquire() as con:
                for table in ("bots", "threads", "memory", "schedules", "files", "push_subscriptions"):
                    default = await con.fetchval(
                        "select column_default from information_schema.columns "
                        "where table_schema='bothub' and table_name=$1 and column_name='owner_id'", table,
                    )
                    assert default is None, table

            bot = await client.post("/api/bots", json={"id": "owned-bot", "name": "Owned", "provider": "fake", "model": "fake"}, headers=OWNER)
            assert bot.status_code == 200, bot.text
            thread = await client.post("/api/threads", json={"bot_id": "owned-bot"}, headers=OWNER)
            assert thread.status_code == 200, thread.text
            thread_id = uuid.UUID(thread.json()["id"])
            turn = await client.post(f"/api/threads/{thread_id}/turns", json={"prompt": "hello"}, headers=OWNER)
            assert turn.status_code == 200, turn.text
            memory = await client.post("/api/memory", json={"text": "private"}, headers=OWNER)
            assert memory.status_code == 200, memory.text
            schedule = await client.post("/api/schedules", json={"bot_id": "owned-bot", "name": "Daily", "kind": "cron", "cron": "0 0 * * *", "prompt": "check"}, headers=OWNER)
            assert schedule.status_code == 200, schedule.text
            upload = await client.post("/api/files", data={"thread_id": str(thread_id)}, files={"file": ("owned.txt", b"owned")}, headers=OWNER)
            assert upload.status_code == 200, upload.text
            push = await client.post("/api/push/subscribe", json={"endpoint": "https://push.example/owned", "keys": {}}, headers=OWNER)
            assert push.status_code == 200, push.text

            async with app.state.pool.acquire() as con:
                lookups = (
                    ("bots", "id", "owned-bot"),
                    ("threads", "id", thread_id),
                    ("memory", "id", uuid.UUID(memory.json()["id"])),
                    ("schedules", "id", uuid.UUID(schedule.json()["id"])),
                    ("files", "id", uuid.UUID(upload.json()["id"])),
                    ("push_subscriptions", "endpoint", "https://push.example/owned"),
                )
                for table, key, value in lookups:
                    assert await con.fetchval(f"select owner_id from bothub.{table} where {key}=$1", value) == owner, table
                assert await con.fetchval(
                    "select th.owner_id from bothub.turns t join bothub.threads th on th.id=t.thread_id where t.id=$1",
                    uuid.UUID(turn.json()["id"]),
                ) == owner

                missing_owner_inserts = (
                    ("insert into bothub.bots(id,name,provider,model) values('missing-owner','x','fake','fake')", ()),
                    ("insert into bothub.threads(bot_id) values('owned-bot')", ()),
                    ("insert into bothub.memory(text) values('x')", ()),
                    ("insert into bothub.schedules(bot_id,name,kind,prompt) values('owned-bot','x','hook','x')", ()),
                    ("insert into bothub.files(thread_id,name,origin,size,mime,storage_path) values($1,'x','upload',0,'text/plain','x')", (thread_id,)),
                    ("insert into bothub.push_subscriptions(endpoint,keys) values('https://push.example/missing','{}'::jsonb)", ()),
                )
                for sql, args in missing_owner_inserts:
                    with pytest.raises(asyncpg.NotNullViolationError):
                        async with con.transaction():
                            await con.execute(sql, *args)
