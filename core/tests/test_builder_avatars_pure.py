"""Аватары конструктора ботов: десять персонажей, чистые проверки без БД."""
import pytest

from bothub.builder import BUILDER_SYSTEM_PROMPT, VALID_AVATARS, validate_draft

pytestmark = pytest.mark.pure


@pytest.mark.parametrize('kind', ['fox', 'cat'])
def test_new_avatars_pass_draft_validation(kind):
    draft = validate_draft({'id': 'x', 'name': 'X', 'avatar': kind}, set())
    assert draft['avatar'] == kind
    assert 'avatar' not in draft['rationale']


def test_unknown_avatar_falls_back_to_robot():
    assert validate_draft({'id': 'x', 'name': 'X', 'avatar': 'dragon'}, set())['avatar'] == 'robot'


def test_builder_prompt_lists_every_valid_avatar():
    assert len(VALID_AVATARS) == 10
    for kind in VALID_AVATARS:
        assert f'- {kind} — ' in BUILDER_SYSTEM_PROMPT
    assert 'один из десяти' in BUILDER_SYSTEM_PROMPT
