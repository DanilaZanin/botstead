"""Opus round 2 over a35ca57: approval needs the IPs the admin saw, /check shares the probe slots, own subnets by TTL.

Same in-memory tables as test_provider_verify_pure.py; the SQL side is in test_provider_verify_db.py."""
import asyncio
import ipaddress
import uuid

import pytest

from bothub import main
from bothub.gateway import NetworksCache
from test_provider_review_pure import blocked_upstream, settle
from test_provider_verify_pure import ADMIN, GOOD_KEY, env, make_env  # noqa: F401

pytestmark = pytest.mark.pure


async def approve(env, provider, **body):
    return await env.client.patch(f'/api/providers/{provider["id"]}/allow-private', json={'allow': True, **body}, headers=ADMIN)


async def member_provider(env):
    created = await env.create(headers=env.member, base_url='https://llm.lan')
    assert created.status_code == 201 and created.json()['status'] == 'pending_admin', created.text
    return dict(env.db.only())


# ---- 1. одобрение только с ips, которые видел администратор -----------------------------------------------------------

async def test_dns_changed_after_the_list_is_not_approved_and_no_key_goes_out(env):
    provider = await member_provider(env)
    listed = (await env.client.get('/api/admin/provider-requests', headers=ADMIN)).json()
    assert listed[0]['resolved_ips'] == ['192.168.1.20']
    env.hosts['llm.lan'] = '10.0.0.5'  # участник сменил DNS после того, как администратор открыл список
    with_ips = await approve(env, provider, base_url='https://llm.lan', ips=listed[0]['resolved_ips'])
    assert with_ips.status_code == 409 and with_ips.json()['error'] == 'conflict'
    without = await approve(env, provider, base_url='https://llm.lan')
    assert without.status_code == 400
    row = env.db.only()
    assert row['allow_private'] is False and row['allow_private_ips'] == [] and row['status'] == 'pending_admin'
    assert not env.upstream.requests


@pytest.mark.parametrize('ips', [None, [], '192.168.1.20', {'a': 1}, [5], [None], ['junk'], ['192.168.1.20%eth0'], [''],
                                 ['192.168.1.20'] * 65])
async def test_approval_without_a_usable_ips_list_is_a_400_in_the_error_format(env, ips):
    provider = await member_provider(env)
    body = {'base_url': 'https://llm.lan', **({} if ips is None else {'ips': ips})}
    response = await approve(env, provider, **body)
    assert response.status_code == 400, response.text
    assert set(response.json()) == {'error', 'detail'} and response.json()['detail']
    assert env.db.only()['allow_private'] is False and not env.upstream.requests


async def test_approval_ips_must_equal_the_current_private_answers(env):
    env.hosts['llm.lan'] = '192.168.1.20'
    provider = await member_provider(env)
    for ips in (['192.168.1.21'], ['192.168.1.20', '192.168.1.21'], ['8.8.8.8']):
        response = await approve(env, provider, base_url='https://llm.lan', ips=ips)
        assert response.status_code == 409, ips
    assert env.db.only()['allow_private'] is False and not env.upstream.requests
    ok = await approve(env, provider, base_url='https://llm.lan', ips=['192.168.1.20'])
    assert ok.status_code == 200 and ok.json()['allow_private_ips'] == ['192.168.1.20']


async def test_ipv4_mapped_ips_are_reduced_before_the_comparison_and_stored_as_ipv4(env):
    provider = await member_provider(env)
    response = await approve(env, provider, base_url='https://llm.lan', ips=['::ffff:192.168.1.20'])
    assert response.status_code == 200, response.text
    assert response.json()['allow_private_ips'] == ['192.168.1.20']
    assert env.db.only()['allow_private_ips'] == ['192.168.1.20']
    assert env.upstream.requests[-1].url.host == '192.168.1.20'


async def test_ips_with_revocation_stay_a_400(env):
    provider = await member_provider(env)
    response = await env.client.patch(f'/api/providers/{provider["id"]}/allow-private',
                                      json={'allow': False, 'ips': ['192.168.1.20']}, headers=ADMIN)
    assert response.status_code == 400


# ---- 2. /check в тех же слотах проб, что POST и PATCH ---------------------------------------------------------------

async def forced_providers(env, count):
    ids = []
    for index in range(count):
        created = await env.create(name=f'F{index}', force=True, secret=GOOD_KEY)
        assert created.status_code == 201 and created.json()['status'] == 'unchecked'
        ids.append(created.json()['id'])
    return ids


async def test_parallel_checks_of_forced_providers_use_two_slots_the_rest_get_429(env):
    ids = await forced_providers(env, 20)
    assert not env.upstream.requests
    gate = await blocked_upstream(env)
    tasks = [asyncio.create_task(env.client.post(f'/api/providers/{pid}/check', headers=ADMIN)) for pid in ids]
    await settle()
    assert len(env.upstream.requests) == main.PROVIDER_PROBE_PARALLEL
    done = [t for t in tasks if t.done()]
    assert len(done) == 18 and all(t.result().status_code == 429 for t in done)
    assert done[0].result().json()['error'] == 'rate_limited' and done[0].result().json()['detail']
    assert set(done[0].result().json()) == {'error', 'detail'}
    gate.set()
    results = await asyncio.gather(*tasks)
    assert sorted(r.status_code for r in results).count(200) == 2 and len(env.upstream.requests) == 2
    # отказ по лимиту не портит провайдера: статус и ошибка прежние
    refused = [env.db.providers[uuid.UUID(pid)] for pid, r in zip(ids, results) if r.status_code == 429]
    assert all(row['status'] == 'unchecked' and row['last_error'] is None for row in refused)


async def test_checks_share_the_slots_and_the_rate_with_post_and_patch(env):
    ids = await forced_providers(env, 3)
    gate = await blocked_upstream(env)
    first = asyncio.create_task(env.client.post(f'/api/providers/{ids[0]}/check', headers=ADMIN))
    second = asyncio.create_task(env.create(name='live', secret=GOOD_KEY))
    await settle()
    assert len(env.upstream.requests) == 2  # слот занят проверкой и POST
    over = await env.client.post(f'/api/providers/{ids[1]}/check', headers=ADMIN)
    assert over.status_code == 429 and over.json()['error'] == 'rate_limited'
    patched = await env.patch(env.db.providers[uuid.UUID(ids[2])], secret=GOOD_KEY)
    assert patched.status_code == 429
    gate.set()
    await asyncio.gather(first, second)


async def test_check_rate_is_ten_probes_a_minute_per_user_and_does_not_hit_other_users(env):
    ids = await forced_providers(env, main.PROVIDER_PROBE_RATE + 2)
    for pid in ids[:main.PROVIDER_PROBE_RATE]:
        assert (await env.client.post(f'/api/providers/{pid}/check', headers=ADMIN)).status_code == 200
    sent = len(env.upstream.requests)
    limited = await env.client.post(f'/api/providers/{ids[-1]}/check', headers=ADMIN)
    assert limited.status_code == 429 and limited.json()['error'] == 'rate_limited'
    assert len(env.upstream.requests) == sent
    assert env.db.providers[uuid.UUID(ids[-1])]['status'] == 'unchecked'
    other = await env.create(headers=env.member, name='member')  # у другого пользователя свой счёт
    assert other.status_code == 201


async def test_check_cache_and_shared_task_do_not_spend_probes(env):
    ids = await forced_providers(env, 1)
    gate = await blocked_upstream(env)
    tasks = [asyncio.create_task(env.client.post(f'/api/providers/{ids[0]}/check', headers=ADMIN)) for _ in range(5)]
    await settle()
    assert len(env.upstream.requests) == 1  # пять параллельных /check одного провайдера: одна проба, один слот
    gate.set()
    assert [r.status_code for r in await asyncio.gather(*tasks)] == [200] * 5
    again = await env.client.post(f'/api/providers/{ids[0]}/check', headers=ADMIN)  # кэш 30 с
    assert again.status_code == 200 and len(env.upstream.requests) == 1


async def test_refresh_all_returns_unprobed_providers_as_they_are_when_slots_are_taken(env):
    ids = await forced_providers(env, 4)
    gate = await blocked_upstream(env)
    task = asyncio.create_task(env.client.post('/api/models/refresh', headers=ADMIN))
    await settle()
    assert len(env.upstream.requests) == main.PROVIDER_PROBE_PARALLEL
    gate.set()
    response = await task
    assert response.status_code == 200, response.text
    assert sorted(item['status'] for item in response.json()) == ['ok', 'ok', 'unchecked', 'unchecked']
    assert len(env.upstream.requests) == 2 and {item['id'] for item in response.json()} == set(ids)


async def test_approval_is_kept_when_the_owner_has_no_free_probe_slot(env):
    provider = await member_provider(env)
    gate = await blocked_upstream(env)
    busy = [asyncio.create_task(env.create(headers=env.member, name=f'M{i}', secret=GOOD_KEY)) for i in range(2)]
    await settle()
    assert len(env.upstream.requests) == 2  # оба слота участника заняты
    response = await approve(env, provider, base_url='https://llm.lan', ips=['192.168.1.20'])
    assert response.status_code == 200, response.text  # одобрение записано, пробу сделает следующий /check
    assert response.json()['status'] == 'new' and response.json()['allow_private_ips'] == ['192.168.1.20']
    assert env.db.providers[provider['id']]['allow_private'] is True and len(env.upstream.requests) == 2
    gate.set()
    await asyncio.gather(*busy)


# ---- 3. собственные сети ядра: пересчёт по TTL ----------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_networks_cache_recomputes_after_the_ttl_only():
    clock, found, calls = Clock(), [], []

    def source():
        calls.append(clock.now)
        return tuple(found)

    cache = NetworksCache(source, ttl=30, clock=clock)
    assert cache() == () and len(calls) == 1
    found.append(ipaddress.ip_network('172.30.0.0/16'))
    clock.now += 29.9
    assert cache() == () and len(calls) == 1  # кэш держится до TTL
    clock.now += 0.2
    assert cache() == (ipaddress.ip_network('172.30.0.0/16'),) and len(calls) == 2
    assert cache() == cache() and len(calls) == 2


def test_networks_cache_keeps_the_last_good_value_when_the_source_breaks():
    clock, state = Clock(), {'broken': False}

    def source():
        if state['broken']:
            raise OSError('no /proc')
        return (ipaddress.ip_network('172.30.0.0/16'),)

    cache = NetworksCache(source, ttl=30, clock=clock)
    assert cache() == (ipaddress.ip_network('172.30.0.0/16'),)
    state['broken'], clock.now = True, clock.now + 31
    assert cache() == (ipaddress.ip_network('172.30.0.0/16'),)


async def test_a_bot_network_that_appears_after_start_is_blocked_after_the_ttl(monkeypatch):
    networks = []
    async with make_env(monkeypatch) as env:
        monkeypatch.setattr('bothub.main.own_networks', lambda: tuple(networks))
        clock = Clock()
        env.app.state.own_networks.clock = clock
        env.app.state.own_networks.reset()
        env.hosts['bot.lan'] = '172.30.0.5'
        assert (await env.create(base_url='https://bot.lan', allow_private=True, name='before')).status_code == 201
        networks.append(ipaddress.ip_network('172.30.0.0/16'))  # лаунчер подключил ядро к сети бота bothub-u-*
        clock.now += 29
        assert (await env.create(base_url='https://bot.lan', allow_private=True, name='cached')).status_code == 201
        clock.now += 2
        sent = len(env.upstream.requests)
        refused = await env.create(base_url='https://bot.lan', allow_private=True, name='after')
        assert refused.status_code == 422 and refused.json()['error'] == 'invalid_base_url'
        member = await env.create(headers=env.member, base_url='https://bot.lan', name='member')
        assert member.status_code == 422  # и участнику не заявка, а отказ
        assert len(env.upstream.requests) == sent
        # уже одобренный провайдер в новой сети тоже закрыт: /check и PATCH не отправляют ключ
        stored = next(iter(env.db.providers.values()))
        stored.update(last_check_at=None)
        checked = await env.client.post(f'/api/providers/{stored["id"]}/check', headers=ADMIN)
        assert len(env.upstream.requests) == sent
        assert checked.status_code == 200 and checked.json()['status'] == 'error'
