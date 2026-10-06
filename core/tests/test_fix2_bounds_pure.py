"""Regression checks for review round two input and migration bounds."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from bothub.main import (ApprovalIn, BotIn, BotPatch, MacCallIn, MemoryIn, PushIn, TurnIn,
                         ScheduleIn, ThreadIn, UsageIn)
from bothub.runner.subprocess import usage_event

pytestmark = pytest.mark.pure


def test_usage_rejects_coercion_negative_huge_and_long_model():
    base = {'thread_id':'00000000-0000-0000-0000-000000000001',
            'turn_id':'00000000-0000-0000-0000-000000000002',
            'provider':'claude', 'model':'sonnet'}
    for field, value in [('tokens_in', '12'), ('tokens_in', -1),
                         ('tokens_in', 10**9+1), ('model', 'x'*129)]:
        with pytest.raises(ValidationError):
            UsageIn.model_validate(base | {field:value})


def test_runner_usage_sanitizes_bad_counts():
    for value in (-2, 'bogus', True, 10**9+1):
        assert usage_event({'input_tokens':value}).payload['tokens_in'] == 0


def test_public_models_enforce_limits():
    bot = {'name':'Scout','provider':'claude','model':'sonnet'}
    for field, limit in (('role',512),('avatar',128),('executor',128)):
        with pytest.raises(ValidationError): BotIn.model_validate(bot | {field:'x'*(limit+1)})
        with pytest.raises(ValidationError): BotPatch.model_validate({field:'x'*(limit+1)})
        BotIn.model_validate(bot | {field:'x'*limit}); BotPatch.model_validate({field:'x'*limit})
    with pytest.raises(ValidationError): BotIn.model_validate(bot | {'instructions':'x'*(64*1024+1)})
    with pytest.raises(ValidationError): ThreadIn.model_validate({'bot_id':'scout','title':'x'*513})
    with pytest.raises(ValidationError): MemoryIn.model_validate({'text':'x'*(16*1024+1)})
    with pytest.raises(ValidationError): MemoryIn.model_validate({'text':'界'*6000})
    with pytest.raises(ValidationError): TurnIn.model_validate({'prompt':'界'*90000})
    with pytest.raises(ValidationError): ScheduleIn.model_validate({'bot_id':'scout','name':'x'*513,'kind':'hook','prompt':'ok'})
    with pytest.raises(ValidationError): ApprovalIn.model_validate({'thread_id':'00000000-0000-0000-0000-000000000001','risk':'other','title':'x'*513,'tool':'test','args':{}})
    with pytest.raises(ValidationError): PushIn.model_validate({'endpoint':'x'*513,'keys':{}})
    with pytest.raises(ValidationError): PushIn.model_validate({'endpoint':'ok','keys':{'p256dh':'x'*513}})
    with pytest.raises(ValidationError): PushIn.model_validate({'endpoint':'ok','keys':{'x'*513:'short'}})
    with pytest.raises(ValidationError): ApprovalIn.model_validate({'thread_id':'00000000-0000-0000-0000-000000000001','risk':'other','title':'ok','tool':'test','args':{'payload':'x'*(64*1024)}})
    with pytest.raises(ValidationError): MacCallIn.model_validate({'thread_id':'00000000-0000-0000-0000-000000000001','turn_id':'00000000-0000-0000-0000-000000000002','tool':'x'*513,'args':{}})


def test_usage_bigint_is_one_alter_statement():
    sql = (Path(__file__).parents[1]/'bothub/migrations/012_usage_bigint.sql').read_text()
    assert sql.lower().count('alter table') == 2
    assert all(name in sql for name in ('budget_daily_tokens','tokens_in','tokens_out','tokens_cache_read','tokens_cache_write'))


def test_registry_bound_migration_exists():
    sql = (Path(__file__).parents[1]/'bothub/migrations/013_registry_bound.sql').read_text()
    assert 'registry_bound' in sql and 'default false' in sql.lower()


def test_builder_draft_fits_bot_creation_limits():
    """Черновик конструктора должен проходить в POST /api/bots: длинная роль и длинный prompt расписания."""
    from bothub.builder import validate_draft
    from bothub.main import BotIn
    draft = validate_draft({'id':'scout','name':'Scout','role':'р'*900,'instructions':'x','provider':'claude','model':'claude-sonnet-5',
                            'schedule':{'name':'n'*900,'cron':'0 9 * * *','timezone':'UTC','prompt':'п'*40000}}, set())
    assert len(draft['role']) <= 512
    assert len(draft['schedule']['name']) <= 512 and len(draft['schedule']['prompt']) <= 16*1024
    BotIn.model_validate({k: draft[k] for k in ('name','role','instructions','provider','model') if k in draft} | {'schedule': draft['schedule']})


def test_usage_provider_length_is_bounded():
    import uuid
    from pydantic import ValidationError
    from bothub.main import UsageIn
    base = {'thread_id': str(uuid.uuid4()), 'turn_id': str(uuid.uuid4()), 'model': 'm'}
    UsageIn.model_validate(base | {'provider': 'x'*64})
    with pytest.raises(ValidationError):
        UsageIn.model_validate(base | {'provider': 'x'*65})
