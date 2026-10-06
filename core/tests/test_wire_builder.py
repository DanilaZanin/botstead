import json
import pytest

from bothub.builder import launcher_drafter
from bothub.launcher_client import FakeLauncherClient


@pytest.mark.asyncio
async def test_builder_uses_only_owners_bot():
    launcher=FakeLauncherClient()
    await launcher.create_bot('foreign','other')
    await launcher.create_bot('own','owner')
    launcher.script_exec(stdout=[json.dumps({'result':'{"name":"Scout"}'}).encode()])
    assert (await launcher_drafter('description',launcher,'owner'))['name']=='Scout'
    assert launcher.execs[0]['bot_id']=='own'


@pytest.mark.asyncio
async def test_first_bot_builder_uses_temporary_owner_container():
    launcher=FakeLauncherClient()
    await launcher.create_bot('foreign','other')
    launcher.script_exec(stdout=[json.dumps({'result':'{"name":"Scout"}'}).encode()])
    assert (await launcher_drafter('description',launcher,'owner'))['name']=='Scout'
    assert launcher.execs[0]['bot_id'].startswith('draft-')
    assert launcher.execs[0]['bot_id'] not in launcher.bots
    assert 'foreign' in launcher.bots
