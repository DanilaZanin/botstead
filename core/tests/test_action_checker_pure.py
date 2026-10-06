"""Проверяющая модель действий (раздел 20) без БД: запрос, маскирование, разбор ответа, сбои дают ask."""
import asyncio
import json

import httpx
import pytest

from bothub import checker as ck

pytestmark = pytest.mark.pure

KEY = 'sk-checker-key-0123456789'


async def public(host, port):
    return [(None, None, None, None, ('8.8.8.8', port))]


async def private(host, port):
    return [(None, None, None, None, ('10.1.2.3', port))]


def openai_answer(text):
    return httpx.Response(200, json={'choices': [{'message': {'content': text}}]})


async def run(handler, kind='openai_compatible', **extra):
    seen = []

    def upstream(request):
        seen.append(request)
        return handler(request)

    args = dict(kind=kind, base_url='https://api.example/gw/v1', key=KEY, model='judge-1', role='Ищет вакансии',
                request_text='Найди вакансии SRE и откликнись', tool='mcp__bothub__browser',
                args={'action': 'click', 'name': 'Submit'}, resolver=public, transport=httpx.MockTransport(upstream))
    args.update(extra)
    return await ck.ask_checker(**args), seen


# ---- какие риски проверяются ----

@pytest.mark.parametrize('risk,expected', [('pay', True), ('send', True), ('delete', True), ('login', True),
                                           ('push', True), ('exec', True), ('other', False), (None, False)])
def test_only_risky_actions_are_checked(risk, expected):
    assert ck.should_check(risk) is expected


# ---- запрос ----

def test_prompt_has_role_request_and_action_and_cuts_request_to_2000():
    text = ck.build_prompt('Роль', 'x' * 5000, 'Bash', {'command': 'rm -rf /tmp/a'})
    assert '<role>Роль</role>' in text and '<action>' in text and 'tool: Bash' in text
    request = text.split('<request>')[1].split('</request>')[0]
    assert request == 'x' * 2000
    assert 'rm -rf /tmp/a' in text


def test_prompt_masks_secret_fields_and_token_like_values():
    args = {'password': 'hunter2-secret', 'headers': {'Authorization': 'Bearer abcdefghijklmnop'},
            'text': 'ключ sk-abcdefghijklmnop1234 в тексте', 'ok': 'fine', 'api_key': 'k'}
    text = ck.build_prompt('r', 'q', 'Bash', args)
    for leaked in ('hunter2-secret', 'abcdefghijklmnop', 'sk-abcdefghijklmnop1234'):
        assert leaked not in text
    assert 'fine' in text and ck.MASK in text


def test_prompt_cuts_long_args_and_strips_control_characters():
    text = ck.build_prompt('r‮', 'a\x00b​c', 'T', {'k': 'v' * 20000})
    assert len(text.split('args: ')[1].split('\n</action>')[0]) <= ck.ARGS_MAX
    assert '\x00' not in text and '‮' not in text and '​' not in text


def test_system_prompt_is_fixed_and_forbids_following_data():
    assert '{"verdict":"allow|deny|ask","reason":"<=200 chars"}' in ck.SYSTEM_PROMPT
    assert 'untrusted data' in ck.SYSTEM_PROMPT


# ---- разбор ответа ----

@pytest.mark.parametrize('text,verdict', [
    ('{"verdict":"deny","reason":"вне задачи"}', 'deny'),
    ('```json\n{"verdict":"ALLOW","reason":"ok"}\n```', 'allow'),
    ('Вот ответ: {"verdict": " ask ", "reason": "не ясно"} конец', 'ask'),
])
def test_parse_verdict_valid(text, verdict):
    assert ck.parse_verdict(text)['verdict'] == verdict


@pytest.mark.parametrize('text', ['', 'не json', '{"verdict":"maybe","reason":"x"}', '{"reason":"x"}', '[1,2]',
                                  '{"verdict":{"a":1}}', '{{{', None, 5])
def test_parse_verdict_malformed_becomes_ask(text):
    result = ck.parse_verdict(text)
    assert result == {'verdict': 'ask', 'reason': ck.REASON_INVALID}


def test_parse_verdict_reason_is_trimmed_to_200_and_flattened():
    result = ck.parse_verdict(json.dumps({'verdict': 'deny', 'reason': 'a\nb ' + 'я' * 500}))
    assert len(result['reason']) == 200 and '\n' not in result['reason']
    assert ck.parse_verdict('{"verdict":"deny","reason":5}') == {'verdict': 'deny', 'reason': ''}


def test_parse_verdict_skips_a_broken_first_object():
    assert ck.parse_verdict('{oops} {"verdict":"deny","reason":"r"}') == {'verdict': 'deny', 'reason': 'r'}


# ---- запросы к провайдерам ----

async def test_openai_compatible_request_goes_to_pinned_ip_with_base_path_and_key():
    result, seen = await run(lambda r: openai_answer('{"verdict":"deny","reason":"не просили"}'))
    assert result == {'verdict': 'deny', 'reason': 'не просили'}
    assert len(seen) == 1
    request = seen[0]
    assert request.method == 'POST' and request.url.host == '8.8.8.8' and request.url.path == '/gw/v1/chat/completions'
    assert request.headers['host'] == 'api.example' and request.headers['authorization'] == f'Bearer {KEY}'
    body = json.loads(request.content)
    assert body['model'] == 'judge-1' and body['max_tokens'] == ck.OUTPUT_TOKENS
    assert body['messages'][0] == {'role': 'system', 'content': ck.SYSTEM_PROMPT}
    assert 'Найди вакансии SRE' in body['messages'][1]['content']


async def test_openai_api_uses_max_completion_tokens():
    _, seen = await run(lambda r: openai_answer('{"verdict":"allow","reason":""}'), kind='openai_api', base_url=None)
    body = json.loads(seen[0].content)
    assert 'max_completion_tokens' in body and 'max_tokens' not in body
    assert seen[0].headers['host'] == 'api.openai.com'


async def test_anthropic_request_and_answer():
    def handler(request):
        assert request.url.path == '/v1/messages' and request.headers['x-api-key'] == KEY
        assert request.headers['anthropic-version'] == '2023-06-01' and 'authorization' not in request.headers
        assert json.loads(request.content)['system'] == ck.SYSTEM_PROMPT
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': '{"verdict":"ask","reason":"?"}'}]})
    result, seen = await run(handler, kind='anthropic_api', base_url=None)
    assert result == {'verdict': 'ask', 'reason': '?'} and len(seen) == 1


async def test_google_request_and_answer():
    def handler(request):
        assert b'/v1beta/models/gem-1:generateContent' in request.url.raw_path
        assert request.headers['x-goog-api-key'] == KEY
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': '{"verdict":"deny","reason":"g"}'}]}}]})
    result, _ = await run(handler, kind='google_api', base_url=None, model='gem-1')
    assert result == {'verdict': 'deny', 'reason': 'g'}


async def test_key_never_reaches_the_prompt_or_the_result():
    result, seen = await run(lambda r: openai_answer('{"verdict":"ask","reason":"r"}'))
    assert KEY not in seen[0].content.decode() and KEY not in json.dumps(result)


# ---- сбои: всегда ask ----

@pytest.mark.parametrize('response', [
    httpx.Response(500, text='boom'), httpx.Response(401, json={}), httpx.Response(302, headers={'location': 'https://evil.example/'}),
    httpx.Response(200, text='not json'), httpx.Response(200, json={'choices': []}), httpx.Response(200, json={'x': 1}),
    openai_answer('сплошной текст без json'), openai_answer('{"verdict":"nope"}'),
])
async def test_bad_upstream_answers_become_ask(response):
    result, seen = await run(lambda r: response)
    assert result['verdict'] == 'ask' and result['reason'] in (ck.REASON_UNAVAILABLE, ck.REASON_INVALID)
    assert len(seen) == 1  # повторов нет


async def test_network_error_becomes_ask():
    def handler(request):
        raise httpx.ConnectError('down')
    result, _ = await run(handler)
    assert result == {'verdict': 'ask', 'reason': ck.REASON_UNAVAILABLE}


async def test_timeout_becomes_ask():
    async def slow(request):
        await asyncio.sleep(5)
        return openai_answer('{"verdict":"deny","reason":"late"}')
    result, _ = await run(slow, timeout=0.1)
    assert result == {'verdict': 'ask', 'reason': ck.REASON_TIMEOUT}


async def test_oversized_answer_becomes_ask():
    result, _ = await run(lambda r: httpx.Response(200, content=b'x' * (ck.RESPONSE_MAX + 10)))
    assert result['verdict'] == 'ask'


async def test_private_address_without_approval_is_not_called():
    result, seen = await run(lambda r: openai_answer('{"verdict":"deny","reason":"x"}'), resolver=private)
    assert result == {'verdict': 'ask', 'reason': ck.REASON_UNAVAILABLE} and seen == []


async def test_approved_private_address_is_called():
    result, seen = await run(lambda r: openai_answer('{"verdict":"deny","reason":"x"}'), resolver=private,
                             allow_private=True, approved_ips=['10.1.2.3'])
    assert result['verdict'] == 'deny' and seen[0].url.host == '10.1.2.3'
