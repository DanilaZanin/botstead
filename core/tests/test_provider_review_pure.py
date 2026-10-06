"""Opus review of 3f233bd: approval bound to IPs, empty base_url, probe limits, approval input checks.

Same in-memory tables as test_provider_verify_pure.py; the SQL side is in test_provider_verify_db.py."""
import asyncio
import uuid

import httpx
import pytest

from bothub import main
from bothub.gateway import GatewayProvider
from bothub.secrets import encrypt_secret
from test_provider_verify_pure import (ADMIN, CANARY, GOOD_KEY, MEMBER_ID, env, make_env, reject)  # noqa: F401

pytestmark = pytest.mark.pure

REAPPROVAL = 'повторное одобрение'


async def approved_member_provider(env, **create):
    """A member's private provider, approved by the administrator at 192.168.1.20."""
    created = await env.create(headers=env.member, base_url='https://llm.lan', **create)
    assert created.status_code == 201, created.text
    provider = dict(env.db.providers[uuid.UUID(created.json()['id'])])
    approved = await env.allow(provider, True)
    assert approved.status_code == 200 and approved.json()['status'] == 'ok', approved.text
    return provider


# ---- 1. DNS rebinding после одобрения -----------------------------------------------------------------------------

async def test_approval_stores_the_resolved_private_ips_and_shows_them_to_the_admin(env):
    env.hosts['llm.lan'] = '192.168.1.20'
    await env.create(headers=env.member, base_url='https://llm.lan')
    provider = dict(env.db.only())
    listed = (await env.client.get('/api/admin/provider-requests', headers=ADMIN)).json()
    assert listed[0]['resolved_ips'] == ['192.168.1.20'] and listed[0]['approved_ips'] == []
    assert listed[0]['reapproval'] is False and listed[0]['allow_private'] is False
    response = await env.allow(provider, True)
    assert response.status_code == 200 and response.json()['allow_private_ips'] == ['192.168.1.20']
    assert env.db.only()['allow_private_ips'] == ['192.168.1.20'] and env.db.only()['allow_private'] is True


async def test_rebinding_to_an_unapproved_address_is_refused_by_check_and_patch_and_flags_the_provider(env):
    provider = await approved_member_provider(env)
    probes = len(env.upstream.requests)
    env.hosts['llm.lan'] = '172.18.0.2'  # сеть Docker с Postgres
    env.db.only().update(last_check_at=None)
    checked = await env.client.post(f'/api/providers/{provider["id"]}/check', headers=env.member)
    assert checked.status_code == 422, checked.text
    assert checked.json()['error'] == 'invalid_base_url' and REAPPROVAL in checked.json()['detail']
    row = env.db.only()
    assert row['status'] == 'pending_admin' and row['allow_private'] is True and REAPPROVAL in row['last_error']
    assert row['allow_private_ips'] == ['192.168.1.20']  # флаг и набор не сняты молча
    again = await env.client.post(f'/api/providers/{provider["id"]}/check', headers=env.member)
    assert again.status_code == 422 and REAPPROVAL in again.json()['detail']
    before = dict(row)
    patched = await env.patch(provider, headers=env.member, secret='sk-good-replacement-ABCD')
    assert patched.status_code == 422 and patched.json()['error'] == 'invalid_base_url'
    assert REAPPROVAL in patched.json()['detail'] and env.db.only() == before
    assert len(env.upstream.requests) == probes  # ни одного запроса на 172.18.0.2
    # админ видит провайдера как требующего повторного одобрения и видит, какие IP одобряет
    listed = (await env.client.get('/api/admin/provider-requests', headers=ADMIN)).json()
    assert len(listed) == 1 and listed[0]['reapproval'] is True and listed[0]['allow_private'] is True
    assert listed[0]['approved_ips'] == ['192.168.1.20'] and listed[0]['resolved_ips'] == ['172.18.0.2']
    assert listed[0]['email'] == 'member@example.com'


async def test_refresh_all_does_not_fail_because_of_one_rebound_provider(env):
    await approved_member_provider(env)
    env.hosts['llm.lan'] = '10.9.9.9'
    env.db.only().update(last_check_at=None)
    refreshed = await env.client.post('/api/models/refresh', headers=env.member)
    assert refreshed.status_code == 200 and refreshed.json()[0]['status'] == 'pending_admin'


async def test_admin_can_approve_the_new_address_after_a_legitimate_move(env):
    provider = await approved_member_provider(env)
    env.hosts['llm.lan'] = '192.168.1.30'
    env.db.only().update(last_check_at=None)
    assert (await env.client.post(f'/api/providers/{provider["id"]}/check', headers=env.member)).status_code == 422
    # со старым ожиданием админ видит несовпадение и не одобряет вслепую
    stale = await env.allow(provider, True, ips=['192.168.1.20'])
    assert stale.status_code == 409 and env.db.only()['allow_private_ips'] == ['192.168.1.20']
    response = await env.allow(provider, True, ips=['192.168.1.30'])
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'ok' and response.json()['allow_private_ips'] == ['192.168.1.30']
    assert env.db.only()['status'] == 'ok' and env.db.only()['last_error'] is None
    assert env.upstream.requests[-1].url.host == '192.168.1.30'


async def test_approval_refuses_a_name_that_does_not_resolve_and_changes_nothing(env):
    row = env.db.add(owner_id=MEMBER_ID, base_url='https://nowhere.example', status='pending_admin')
    response = await env.allow(row, True, ips=['192.168.1.20'])
    assert response.status_code == 422 and response.json()['error'] == 'unreachable'
    assert env.db.only()['allow_private'] is False and env.db.only()['allow_private_ips'] == []


async def test_a_public_host_has_nothing_to_approve_and_revoking_clears_the_set(env):
    own = env.db.add(base_url='https://api.example', status='new')
    assert (await env.allow(own, True)).status_code == 400  # resolved_ips пуст: одобрять нечего
    wrong = await env.allow(own, True, ips=['8.8.8.8'])  # публичный IP одобрением не является
    assert wrong.status_code == 409
    assert env.db.providers[own['id']]['allow_private'] is False and env.db.providers[own['id']]['allow_private_ips'] == []
    provider = await approved_member_provider(env)
    assert env.db.providers[provider['id']]['allow_private_ips'] == ['192.168.1.20']
    revoked = await env.allow(provider, False)
    assert revoked.status_code == 200 and revoked.json()['allow_private_ips'] == []
    assert env.db.providers[provider['id']]['allow_private'] is False and env.db.providers[provider['id']]['allow_private_ips'] == []


async def test_admin_created_flag_binds_the_set_with_one_dns_answer(env):
    response = await env.create(base_url='https://llm.lan', allow_private=True)
    assert response.status_code == 201, response.text
    assert env.db.only()['allow_private_ips'] == ['192.168.1.20']
    assert env.dns_calls == ['llm.lan']  # проверка адреса, одобрение и пробный запрос на одном ответе DNS
    assert env.upstream.requests[0].url.host == '192.168.1.20'


async def test_admin_patch_with_flag_binds_the_new_set(env):
    own = env.db.add(base_url='https://api.example')
    response = await env.patch(own, secret=GOOD_KEY, base_url='https://llm.lan', allow_private=True)
    assert response.status_code == 200 and response.json()['allow_private'] is True
    assert env.db.only()['allow_private_ips'] == ['192.168.1.20']
    env.hosts['llm.lan'] = '192.168.1.99'
    moved = await env.patch(own, secret=GOOD_KEY)  # адрес в DNS сменился: ключ не уходит на новый IP
    assert moved.status_code == 422 and REAPPROVAL in moved.json()['detail']


async def test_ips_are_not_part_of_the_member_view(env):
    await approved_member_provider(env)
    body = (await env.client.get('/api/providers', headers=env.member)).json()[0]
    assert body['allow_private'] is True and 'allow_private_ips' not in body
    assert '192.168.1.20' not in (await env.client.post(f'/api/providers/{body["id"]}/check', headers=env.member)).text


async def test_own_subnets_are_refused_even_with_the_flag(monkeypatch):
    async with make_env(monkeypatch, own=['172.18.0.0/16']) as env:
        env.hosts['dock.lan'] = '172.18.0.2'
        refused = await env.create(base_url='https://dock.lan', allow_private=True)
        assert refused.status_code == 422 and refused.json()['error'] == 'invalid_base_url'
        member = await env.create(headers=env.member, base_url='https://dock.lan')  # и участнику не заявка, а отказ
        assert member.status_code == 422 and member.json()['error'] == 'invalid_base_url'
        assert not env.db.providers and not env.upstream.requests
        await env.create(headers=env.member, base_url='https://llm.lan')
        env.hosts['llm.lan'] = '172.18.0.9'
        approval = await env.allow(env.db.only(), True)
        assert approval.status_code == 422 and env.db.only()['allow_private'] is False
        env.hosts['llm.lan'] = '192.168.1.20'
        assert (await env.allow(env.db.only(), True)).json()['status'] == 'ok'


async def test_legacy_env_allowance_does_not_open_own_subnets(monkeypatch):
    async with make_env(monkeypatch, own=['172.18.0.0/16']) as env:
        monkeypatch.setenv('PROVIDER_PRIVATE_ALLOW', 'llm.lan,dock.lan')
        env.hosts['dock.lan'] = '172.18.0.2'
        assert (await env.create(headers=env.member, base_url='https://dock.lan')).status_code == 422
        legacy = await env.create(headers=env.member, base_url='https://llm.lan')
        assert legacy.status_code == 201 and legacy.json()['status'] == 'ok'


async def test_forbidden_cidrs_from_env_are_refused(monkeypatch):
    async with make_env(monkeypatch) as plain:
        assert (await plain.create(base_url='https://llm.lan', allow_private=True)).status_code == 201
    async with make_env(monkeypatch, forbidden_cidrs='10.0.0.0/8, 192.168.1.0/24') as env:
        response = await env.create(base_url='https://llm.lan', allow_private=True)
        assert response.status_code == 422 and response.json()['error'] == 'invalid_base_url'
        assert not env.db.providers and not env.upstream.requests
        env.hosts['other.lan'] = '172.20.0.5'
        assert (await env.create(base_url='https://other.lan', allow_private=True)).status_code == 201


async def test_a_typo_in_forbidden_cidrs_stops_the_core(monkeypatch):
    monkeypatch.setenv('PROVIDER_FORBIDDEN_CIDRS', '10.0.0.0/99')
    with pytest.raises(ValueError, match='PROVIDER_FORBIDDEN_CIDRS'):
        main.create_app()


async def test_gateway_report_hook_flags_the_provider_for_reapproval(env):
    provider = env.db.add(base_url='https://llm.lan', allow_private=True, allow_private_ips=['192.168.1.20'], status='ok')
    report = env.app.state.provider_address_changed
    await report(GatewayProvider(str(provider['id']), 'openai_compatible', 'https://llm.lan', 'k', True, [],
                                 allow_private=True, allow_private_ips=('192.168.1.20',)))
    row = env.db.only()
    assert row['status'] == 'pending_admin' and row['allow_private'] is True and REAPPROVAL in row['last_error']
    plain = env.db.add(base_url='https://api.example', status='ok')  # без флага отчёт ничего не меняет
    await report(GatewayProvider(str(plain['id']), 'openai_compatible', 'https://api.example', 'k', True, []))
    assert env.db.providers[plain['id']]['status'] == 'ok'


# ---- 2. PATCH с пустым base_url ------------------------------------------------------------------------------------

@pytest.mark.parametrize('kind', ['openai_compatible', 'openai_api', 'anthropic_api', 'google_api'])
@pytest.mark.parametrize('empty', ['', '   '])
@pytest.mark.parametrize('force', [False, True])
async def test_empty_base_url_is_a_400_invalid_base_url_for_every_kind(env, kind, empty, force):
    provider = env.db.add(kind=kind, base_url='https://api.example' if kind == 'openai_compatible' else None)
    before = dict(provider)
    body = {'secret': GOOD_KEY, 'base_url': empty, **({'force': True} if force else {})}
    response = await env.patch(provider, **body)
    assert response.status_code == 400, response.text
    assert response.json()['error'] == 'invalid_base_url' and response.json()['detail']
    assert env.db.only() == before and not env.upstream.requests


async def test_base_url_without_secret_and_non_string_base_url_stay_400(env):
    provider = env.db.add()
    assert (await env.patch(provider, base_url='')).status_code == 400
    assert (await env.patch(provider, secret=GOOD_KEY, base_url=None)).status_code == 400
    assert (await env.patch(provider, secret=GOOD_KEY, base_url=5)).status_code == 400
    assert not env.upstream.requests


# ---- 3. пробные запросы: лимит и параллельность -------------------------------------------------------------------

async def blocked_upstream(env):
    gate = asyncio.Event()

    async def behaviour(request):
        await gate.wait()
        return httpx.Response(200, json={'data': [{'id': 'm1'}]})

    env.upstream.behaviour = behaviour
    return gate


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)


async def test_only_two_probes_per_user_run_at_once_the_rest_get_429(env):
    gate = await blocked_upstream(env)
    tasks = [asyncio.create_task(env.create(name=f'P{i}', secret=f'sk-good-key-{i:012d}')) for i in range(30)]
    await settle()
    assert len(env.upstream.requests) == 2  # остальные 28 не дошли до провайдера
    done = [t for t in tasks if t.done()]
    assert len(done) == 28 and all(t.result().status_code == 429 for t in done)
    assert done[0].result().json()['error'] == 'rate_limited' and done[0].result().json()['detail']
    gate.set()
    results = await asyncio.gather(*tasks)
    assert sorted(r.status_code for r in results).count(201) == 2 and len(env.upstream.requests) == 2
    assert len(env.db.providers) == 2


async def test_patches_with_different_keys_are_limited_the_same_way(env):
    provider = env.db.add()
    gate = await blocked_upstream(env)
    tasks = [asyncio.create_task(env.patch(provider, secret=f'sk-good-key-{i:012d}')) for i in range(30)]
    await settle()
    assert len(env.upstream.requests) == 2
    assert sum(t.done() and t.result().status_code == 429 for t in tasks) == 28
    gate.set()
    await asyncio.gather(*tasks)
    assert len(env.upstream.requests) == 2


async def test_slots_are_per_user(env):
    gate = await blocked_upstream(env)
    admin = [asyncio.create_task(env.create(name=f'A{i}')) for i in range(3)]
    member = [asyncio.create_task(env.create(headers=env.member, name=f'M{i}')) for i in range(3)]
    await settle()
    assert len(env.upstream.requests) == 4  # по двое на пользователя
    assert sum(t.done() and t.result().status_code == 429 for t in admin + member) == 2
    gate.set()
    await asyncio.gather(*admin, *member)


async def test_slot_is_released_after_a_failed_probe(env):
    env.upstream.behaviour = reject(401)
    for index in range(4):
        assert (await env.create(name=f'bad{index}', secret=CANARY)).status_code == 422
    env.upstream.behaviour = None
    assert (await env.create(name='good')).status_code == 201


async def test_probe_rate_is_limited_per_user_and_window(env):
    for index in range(main.PROVIDER_PROBE_RATE):
        assert (await env.create(name=f'P{index}')).status_code == 201, index
    sent = len(env.upstream.requests)
    limited = await env.create(name='over')
    assert limited.status_code == 429 and limited.json()['error'] == 'rate_limited'
    assert len(env.upstream.requests) == sent and len(env.db.providers) == main.PROVIDER_PROBE_RATE
    assert (await env.create(headers=env.member, name='member')).status_code == 201  # у другого пользователя свой счёт
    patched = await env.patch(next(iter(env.db.providers.values())), secret=GOOD_KEY)
    assert patched.status_code == 429


async def test_requests_without_a_probe_do_not_use_the_budget(env):
    for index in range(main.PROVIDER_PROBE_RATE + 3):
        forced = await env.create(name=f'F{index}', force=True, secret=CANARY)
        assert forced.status_code == 201 and forced.json()['status'] == 'unchecked'
    assert not env.upstream.requests
    assert (await env.create(name='first real probe')).status_code == 201
    renamed = await env.patch(env.db.add(), name='Renamed')
    assert renamed.status_code == 200


# ---- 4. approval: base_url обязателен, 409 при несовпадении ---------------------------------------------------

async def test_approval_requires_the_address_the_admin_saw(env):
    await env.create(headers=env.member, base_url='https://llm.lan')
    provider = dict(env.db.only())
    path = f'/api/providers/{provider["id"]}/allow-private'
    missing = await env.client.patch(path, json={'allow': True}, headers=ADMIN)
    assert missing.status_code == 400 and missing.json()['error'] == 'invalid' and 'base_url' in missing.json()['detail']
    wrong = await env.client.patch(path, json={'allow': True, 'base_url': 'https://llm2.lan', 'ips': ['192.168.1.20']}, headers=ADMIN)
    assert wrong.status_code == 409 and wrong.json()['error'] == 'conflict'
    assert env.db.only()['allow_private'] is False and not env.upstream.requests
    ok = await env.client.patch(path, json={'allow': True, 'base_url': 'https://llm.lan', 'ips': ['192.168.1.20']}, headers=ADMIN)
    assert ok.status_code == 200 and ok.json()['allow_private'] is True
    # снять одобрение можно без адреса
    assert (await env.client.patch(path, json={'allow': False}, headers=ADMIN)).status_code == 200


# ---- 5. заявки администратору ------------------------------------------------------------------------------------

async def test_a_member_cannot_flood_the_admin_with_requests(env):
    for index in range(main.PROVIDER_ADMIN_REQUESTS_PER_HOUR):
        response = await env.create(headers=env.member, base_url='https://llm.lan', name=f'R{index}')
        assert response.status_code == 201 and response.json()['status'] == 'pending_admin', index
    over = await env.create(headers=env.member, base_url='https://llm.lan', name='over')
    assert over.status_code == 429 and over.json()['error'] == 'rate_limited'
    assert len(env.db.providers) == main.PROVIDER_ADMIN_REQUESTS_PER_HOUR
    moved = await env.patch(next(iter(env.db.providers.values())), headers=env.member, secret=GOOD_KEY, base_url='https://llm2.lan')
    assert moved.status_code == 429
    # публичные адреса и ошибки адреса заявку не создают и лимит не тратят
    assert (await env.create(headers=env.member, name='public')).status_code == 201
    assert (await env.create(headers=env.member, base_url='https://loop.lan')).status_code == 422
    assert (await env.create(headers=env.member, base_url='https://nowhere.example')).status_code == 422
    # администратор со своим одобрением не упирается в этот лимит
    assert (await env.create(base_url='https://llm.lan', allow_private=True, name='admin')).status_code == 201


# ---- 6. DNS внутри таймаута, один раз -----------------------------------------------------------------------------

async def test_dns_is_resolved_once_per_create_and_patch(env):
    assert (await env.create(base_url='https://api.example')).status_code == 201
    assert env.dns_calls == ['api.example']
    env.dns_calls.clear()
    assert (await env.patch(env.db.only(), secret=GOOD_KEY, base_url='https://api.example')).status_code == 200
    assert env.dns_calls == ['api.example']
    env.dns_calls.clear()
    member = await env.create(headers=env.member, base_url='https://llm.lan', name='private')
    assert member.json()['status'] == 'pending_admin' and env.dns_calls == ['llm.lan']


async def test_slow_dns_is_part_of_the_provider_timeout(env, monkeypatch):
    monkeypatch.setattr(main, 'PROVIDER_CHECK_TIMEOUT', .05)
    created = await asyncio.wait_for(env.create(base_url='https://slow.example'), 3)
    assert created.status_code == 422 and created.json()['error'] == 'unreachable' and not env.db.providers
    provider_id = uuid.uuid4()
    stored = env.db.add(id=provider_id, base_url='https://slow.example', status='new',
                        secret_encrypted=encrypt_secret(GOOD_KEY.encode(), provider_id.bytes))
    checked = await asyncio.wait_for(env.client.post(f'/api/providers/{stored["id"]}/check', headers=ADMIN), 3)
    assert checked.status_code == 200 and checked.json()['status'] == 'error'
    assert checked.json()['last_error'] == 'provider unreachable'  # таймаут, а не ошибка расшифровки
    patched = await asyncio.wait_for(env.patch(env.db.add(), secret=GOOD_KEY, base_url='https://slow.example'), 3)
    assert patched.status_code == 422 and patched.json()['error'] == 'unreachable'
