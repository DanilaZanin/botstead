"""Every registered route has an A/B access probe; Postgres is required."""
import hashlib
import hmac
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from starlette.routing import Mount

from bothub.main import create_app
from bothub.launcher_client import FakeLauncherClient


pytestmark = pytest.mark.empty_users
OWNER = {'Authorization':'Bearer test-owner'}

# A new app route fails collection until it has an explicit access probe.
CASES = {
    ('GET','/openapi.json'):'public', ('GET','/docs'):'public',
    ('GET','/docs/oauth2-redirect'):'public', ('GET','/redoc'):'public',
    ('MOUNT','/'):'public', ('GET','/api/health'):'public',
    ('GET','/api/setup/status'):'public',
    ('POST','/api/setup'):'setup', ('POST','/api/auth/login'):'login',
    ('GET','/api/auth/me'):'me', ('POST','/api/auth/logout'):'logout',
    ('POST','/api/auth/password'):'password',
    ('POST','/api/invites'):'admin', ('GET','/api/invites'):'admin',
    ('DELETE','/api/invites/{id}'):'admin', ('GET','/api/invites/check'):'check',
    ('POST','/api/invites/accept'):'accept', ('GET','/api/users'):'admin',
    ('PATCH','/api/users/{id}'):'admin', ('GET','/api/sessions'):'sessions',
    ('DELETE','/api/sessions/{id}'):'session',
    ('POST','/api/mac/token'):'own_create', ('DELETE','/api/mac/token'):'own_delete',
    ('GET','/api/providers'):'provider_list', ('POST','/api/providers'):'own_create',
    ('PATCH','/api/providers/{id}'):'resource', ('DELETE','/api/providers/{id}'):'resource',
    ('POST','/api/providers/{id}/check'):'resource',
    ('PATCH','/api/providers/{id}/allow-private'):'admin', ('GET','/api/admin/provider-requests'):'admin',
    ('GET','/api/models'):'model_list', ('PATCH','/api/models/{id}'):'resource',
    ('POST','/api/models/refresh'):'own_create',
    ('WS','/api/providers/{id}/login'):'ws_provider',
    ('GET','/api/bots'):'list', ('POST','/api/bots/draft'):'draft',
    ('POST','/api/bots'):'own_create', ('PATCH','/api/bots/{id}'):'resource',
    ('DELETE','/api/bots/{id}'):'resource', ('POST','/api/bots/{id}/recreate'):'resource',
    ('GET','/api/bots/{id}/browser'):'resource',
    ('POST','/api/bots/{id}/browser/takeover'):'resource',
    ('POST','/api/bots/{id}/browser/return'):'resource',
    ('POST','/api/bots/{id}/browser/secret-input'):'resource',
    ('WS','/api/bots/{id}/screen'):'ws_screen',
    ('POST','/api/browser/authorize'):'bot_resource',
    ('POST','/api/browser/step'):'bot_resource',
    ('GET','/api/threads'):'list', ('POST','/api/threads'):'resource',
    ('PATCH','/api/threads/{id}'):'resource', ('POST','/api/threads/{id}/turns'):'resource',
    ('GET','/api/threads/{id}'):'resource', ('POST','/api/threads/{id}/compact'):'resource',
    ('POST','/api/turns/{id}/stop'):'resource', ('GET','/api/threads/{id}/events'):'resource',
    ('WS','/api/ws'):'ws_resource',
    ('GET','/api/approvals'):'list', ('POST','/api/approvals'):'bot_resource',
    ('POST','/api/approvals/{id}/decide'):'resource',
    ('GET','/api/approvals/{id}/wait'):'bot_resource',
    ('GET','/api/memory'):'list', ('POST','/api/memory'):'resource',
    ('PATCH','/api/memory/{id}'):'resource', ('DELETE','/api/memory/{id}'):'resource',
    ('GET','/api/usage/summary'):'usage_summary',
    ('POST','/api/usage'):'bot_resource',
    ('GET','/api/schedules'):'list', ('POST','/api/schedules'):'resource',
    ('PATCH','/api/schedules/{id}'):'resource', ('POST','/api/schedules/{id}/run'):'resource',
    ('POST','/hooks/{id}'):'hook', ('POST','/api/files'):'file_upload',
    ('GET','/api/files/{id}'):'resource', ('GET','/api/mac/status'):'mac_status',
    ('WS','/agent/mac'):'ws_mac', ('POST','/api/mac/call'):'bot_resource',
    ('POST','/api/push/subscribe'):'own_create',
    ('GET','/api/procedures'):'procedure_list', ('POST','/api/procedures'):'own_create',
    ('POST','/api/procedures/from-turn'):'resource', ('POST','/api/procedures/import'):'own_create',
    ('GET','/api/procedures/{id}'):'resource', ('PATCH','/api/procedures/{id}'):'resource',
    ('DELETE','/api/procedures/{id}'):'resource', ('GET','/api/procedures/{id}/export'):'resource',
    ('GET','/api/procedures/{id}/runs'):'resource', ('POST','/api/procedures/{id}/run'):'resource',
    ('GET','/api/procedure-runs/{id}'):'resource', ('POST','/api/procedure-runs/{id}/stop'):'resource',
    ('POST','/api/procedure-runs/{id}/decide'):'resource', ('GET','/api/secrets'):'secret_list',
    ('GET','/api/activity'):'activity', ('POST','/api/bots/{id}/pause'):'resource', ('POST','/api/bots/{id}/resume'):'resource',
    ('POST','/api/bots/pause-all'):'pause_all', ('POST','/api/bots/resume-all'):'pause_all',
    **{(method,'/gateway/{provider_id}/{path:path}'):'gateway' for method in ('GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS','TRACE')},
}


def _registered():
    result=set()
    def walk(routes):
        for route in routes:
            if isinstance(route, Mount):
                result.add(('MOUNT',route.path))
                walk(route.routes)
            elif hasattr(route,'original_router'):
                walk(route.original_router.routes)
            elif type(route).__name__ == 'APIWebSocketRoute':
                result.add(('WS',route.path))
            else:
                for method in route.methods or ():
                    if method not in ('HEAD','OPTIONS') or route.path.startswith('/gateway/'):
                        result.add((method,route.path))
    walk(create_app().routes)
    return result


@pytest.mark.pure
def test_route_matrix_covers_all_registered_routes():
    registered = _registered()
    assert set(CASES)==registered, f'route matrix drift: missing={registered-set(CASES)}, stale={set(CASES)-registered}'


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path,kind',[(method,path,kind) for (method,path),kind in CASES.items()],ids=[f'{m} {p}' for m,p in CASES])
async def test_route_a_to_b_access_matrix(method,path,kind,monkeypatch,tmp_path):
    monkeypatch.setenv('BOTHUB_BASE_PATH','/')
    async def fake_draft(description):
        raise RuntimeError('test drafter unavailable')
    app=create_app(drafter=fake_draft)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://testserver') as client:
            a=(await client.post('/api/setup',json={'email':'a@example.com','password':'long-password'},headers=OWNER)).json()
            invite=(await client.post('/api/invites',json={},headers=OWNER)).json()
            b=(await client.post('/api/invites/accept',json={'token':invite['token'],'email':'b@example.com','password':'long-password'})).json()
            a_bot=(await client.post('/api/bots',json={'id':'alpha','name':'Alpha','provider':'fake','model':'fake'},headers=OWNER)).json()
            a_thread=(await client.post('/api/threads',json={'bot_id':a_bot['id']},headers=OWNER)).json()
            a_memory=(await client.post('/api/memory',json={'text':'private'},headers=OWNER)).json()
            a_schedule=(await client.post('/api/schedules',json={'bot_id':a_bot['id'],'name':'A','kind':'hook','prompt':'hello'},headers=OWNER)).json()
            async with app.state.pool.acquire() as con:
                a_turn=await con.fetchval("insert into bothub.turns(thread_id,prompt,status) values($1,$2,'done') returning id",uuid.UUID(a_thread['id']),'hello')
                a_approval=await con.fetchval("insert into bothub.approvals(thread_id,turn_id,bot_id,risk,title,tool,args,args_hash,expires_at) values($1,$2,$3,'other','A','Read','{}','hash',now()+interval '1 day') returning id",uuid.UUID(a_thread['id']),a_turn,a_bot['id'])
                a_file=await con.fetchval("insert into bothub.files(thread_id,name,origin,size,mime,storage_path,owner_id) values($1,'a','upload',0,'text/plain',$2,$3) returning id",uuid.UUID(a_thread['id']),str(tmp_path/'absent'),uuid.UUID(a['id']))
                a_session=await con.fetchval("insert into bothub.sessions(id_hash,user_id,expires_at) values($1,$2,now()+interval '30 days') returning id_hash",'a'*64,uuid.UUID(a['id']))
                a_provider=await con.fetchval("insert into bothub.providers(owner_id,kind,cli,name) values($1,'cli_subscription','claude','A CLI') returning id",uuid.UUID(a['id']))
                a_model=await con.fetchval("insert into bothub.models(provider_id,name) values($1,'claude-test') returning id",a_provider)
                a_procedure=await con.fetchval("insert into bothub.procedures(owner_id,name) values($1,'A procedure') returning id",uuid.UUID(a['id']))
                a_run=await con.fetchval("insert into bothub.procedure_runs(procedure_id,procedure_version) values($1,1) returning id",a_procedure)
                await con.execute("insert into bothub.secrets(owner_id,bot_id,name,value_encrypted) values($1,null,'a-private-secret',$2)",uuid.UUID(a['id']),b'ciphertext')
            login=await client.post('/api/auth/login',json={'email':'b@example.com','password':'long-password'})
            csrf=login.json()['csrf_token']
            b_headers={'X-CSRF':csrf,'Origin':'https://testserver'}
            async with app.state.pool.acquire() as con:
                b_provider=await con.fetchval("insert into bothub.providers(owner_id,kind,cli,name,status) values($1,'cli_subscription','claude','B Bound CLI','ok') returning id",uuid.UUID(b['id']))
                b_model=await con.fetchval("insert into bothub.models(provider_id,name) values($1,'claude-test') returning id",b_provider)
            foreign_create=await client.post('/api/bots',json={'id':'foreign-binding','name':'Foreign','provider':'claude','model':'claude-test','provider_id':str(a_provider),'model_id':str(a_model)},headers=b_headers)
            assert foreign_create.status_code==400,foreign_create.text
            b_bot_response=await client.post('/api/bots',json={'id':'beta','name':'Beta','provider':'claude','model':'claude-test','provider_id':str(b_provider),'model_id':str(b_model)},headers=b_headers)
            assert b_bot_response.status_code==200,b_bot_response.text
            b_bot=b_bot_response.json()
            foreign_patch=await client.patch('/api/bots/'+b_bot['id'],json={'provider_id':str(a_provider),'model_id':str(a_model)},headers=b_headers)
            assert foreign_patch.status_code==400,foreign_patch.text
            digest=hmac.new(b'test-secret',b_bot['id'].encode(),hashlib.sha256).hexdigest()
            bot_headers={'Authorization':f"Bearer bot:{b_bot['id']}:{digest}"}
            ids={'bot':a_bot['id'],'thread':a_thread['id'],'turn':str(a_turn),'approval':str(a_approval),'memory':a_memory['id'],'schedule':a_schedule['id'],'file':str(a_file),'session':a_session,'user':a['id'],'invite':invite['token_hash'],'provider':str(a_provider),'model':str(a_model),'procedure':str(a_procedure),'run':str(a_run)}
            if kind.startswith('ws_'):
                url='/api/ws?thread_id='+ids['thread'] if kind=='ws_resource' else '/api/providers/'+ids['provider']+'/login' if kind=='ws_provider' else '/api/bots/'+ids['bot']+'/screen' if kind=='ws_screen' else '/agent/mac?token=bad'
                with TestClient(create_app(launcher=FakeLauncherClient()),base_url='https://testserver') as websocket_client:
                    with pytest.raises(WebSocketDisconnect) as failure:
                        with websocket_client.websocket_connect(url,headers={'Cookie':f"bothub_session={login.cookies['bothub_session']}",'Origin':'https://testserver'}) as ws:
                            ws.receive_json()
                assert failure.value.code == (4404 if kind in ('ws_resource','ws_provider','ws_screen') else 4401)
                return
            if kind=='gateway':
                b_thread=(await client.post('/api/threads',json={'bot_id':b_bot['id']},headers=b_headers)).json()
                async with app.state.pool.acquire() as con:
                    b_turn=await con.fetchval("insert into bothub.turns(thread_id,prompt,status,lease_until) values($1,'gateway probe','running',now()+interval '1 hour') returning id",uuid.UUID(b_thread['id']))
                own_token=app.state.gateway.issue_token(b_bot['id'],str(b_provider),turn_id=str(b_turn))
                own=await client.get(f'/gateway/{b_provider}/v1/models',headers={'Authorization':f'Bearer {own_token}'})
                assert own.status_code==403 and own.json()['detail']=='provider unavailable to bot',own.text
                url='/gateway/'+ids['provider']+'/v1/models'
                foreign_token=app.state.gateway.issue_token(b_bot['id'],ids['provider'],turn_id=str(b_turn))
                response=await client.request(method,url,headers={'Authorization':f'Bearer {foreign_token}'})
                assert response.status_code==401 and (method=='HEAD' or response.json()['detail']=='turn is not active'),(method,response.text)  # у HEAD нет тела
                return
            if method=='MOUNT':
                response=await client.get('/')
            elif path=='/api/setup': response=await client.post(path,json={'email':'c@example.com','password':'long-password'},headers=OWNER)
            elif path=='/api/auth/login': response=await client.post(path,json={'email':'a@example.com','password':'wrong-password'})
            elif path=='/api/auth/password': response=await client.post(path,json={'old_password':'wrong-password','new_password':'new-long-password'},headers=b_headers)
            elif path=='/api/invites/check': response=await client.get(path,params={'token':'invalid'})
            elif path=='/api/invites/accept': response=await client.post(path,json={'token':'invalid','email':'c@example.com','password':'long-password'})
            elif path=='/api/auth/logout': response=await client.post(path,headers=b_headers)
            elif path=='/api/threads' and method=='POST': response=await client.post(path,json={'bot_id':ids['bot']},headers=b_headers)
            elif path=='/api/approvals': response=await client.post(path,json={'thread_id':ids['thread'],'risk':'other','title':'x','tool':'Read','args':{}},headers=bot_headers) if method=='POST' else await client.get(path)
            elif path=='/api/usage': response=await client.post(path,json={'thread_id':ids['thread'],'turn_id':ids['turn'],'provider':'fake','model':'fake','tokens_in':1,'tokens_out':1},headers=bot_headers)
            elif path=='/api/approvals/{id}/wait': response=await client.get('/api/approvals/'+ids['approval']+'/wait',params={'timeout':0},headers=bot_headers)
            elif path=='/api/mac/call': response=await client.post(path,json={'thread_id':ids['thread'],'turn_id':ids['turn'],'tool':'read_file','args':{}},headers=bot_headers)
            elif path in ('/api/browser/authorize','/api/browser/step'): response=await client.post(path,json={'thread_id':ids['thread'],'turn_id':ids['turn'],'action':'snapshot'},headers=bot_headers)
            elif path=='/api/bots/{id}/browser/secret-input': response=await client.post('/api/bots/'+ids['bot']+'/browser/secret-input',json={'value':'private'},headers=b_headers)
            elif path=='/api/files' and method=='POST': response=await client.post(path,data={'thread_id':ids['thread']},files={'file':('x.txt',b'x')},headers=b_headers)
            elif path=='/api/memory' and method=='POST': response=await client.post(path,json={'text':'x','bot_id':ids['bot']},headers=b_headers)
            elif path=='/api/schedules' and method=='POST': response=await client.post(path,json={'bot_id':ids['bot'],'name':'B','kind':'hook','prompt':'x'},headers=b_headers)
            elif path=='/api/bots' and method=='POST': response=await client.post(path,json={'name':'Own','provider':'fake','model':'fake'},headers=b_headers)
            elif path=='/api/bots/draft': response=await client.post(path,json={'description':'valid draft text'},headers=b_headers)
            elif path=='/api/providers' and method=='POST': response=await client.post(path,json={'kind':'cli_subscription','cli':'claude','name':'B CLI'},headers=b_headers)
            elif path=='/api/models/refresh': response=await client.post(path,headers=b_headers)
            elif path=='/api/procedures' and method=='POST': response=await client.post(path,json={'name':'B procedure','params':[],'steps':[]},headers=b_headers)
            elif path=='/api/procedures/import': response=await client.post(path,json={'name':'B imported'},headers=b_headers)
            elif path=='/api/procedures/from-turn': response=await client.post(path,json={'thread_id':ids['thread'],'name':'B recorded'},headers=b_headers)
            elif path=='/api/push/subscribe': response=await client.post(path,json={'endpoint':'https://push-b.example','keys':{},'device':'b'},headers=b_headers)
            elif path=='/hooks/{id}': response=await client.post('/hooks/'+ids['schedule'],params={'token':'wrong'},json={})
            elif path in ('/api/bots/{id}/pause','/api/bots/{id}/resume'): response=await client.post('/api/bots/'+ids['bot']+'/'+path.rsplit('/',1)[1],json={},headers=b_headers)
            elif path in ('/api/bots/pause-all','/api/bots/resume-all'):
                if path.endswith('resume-all'):
                    async with app.state.pool.acquire() as con: await con.execute('update bothub.bots set paused=true where id=$1',ids['bot'])
                response=await client.post(path,json={},headers=b_headers)
            else:
                url=path.format(id=ids['procedure'] if path.startswith('/api/procedures/') else ids['run'] if path.startswith('/api/procedure-runs/') else ids['provider'] if path.startswith('/api/providers/') else ids['model'] if path.startswith('/api/models/') else ids['user'] if path.startswith('/api/users/') else ids['session'] if path.startswith('/api/sessions/') else ids['invite'] if path.startswith('/api/invites/') else ids['bot'] if path.startswith('/api/bots/') else ids['turn'] if path.startswith('/api/turns/') else ids['approval'] if path.startswith('/api/approvals/') else ids['memory'] if path.startswith('/api/memory/') else ids['schedule'] if path.startswith('/api/schedules/') else ids['file'] if path.startswith('/api/files/') else ids['thread'])
                body={'name':'taken'} if path.startswith('/api/bots/') or path.startswith('/api/providers/') or path=='/api/procedures/{id}' else {'action':'retry'} if path=='/api/procedure-runs/{id}/decide' else {} if path.startswith('/api/procedure') else {'enabled':False} if path.startswith('/api/models/') else {'status':'archived'} if path.startswith('/api/threads/') and method=='PATCH' else {'prompt':'x'} if path.endswith('/turns') else {'decision':'reject'} if path.endswith('/decide') else {'text':'x'} if path.startswith('/api/memory/') else {'enabled':False} if path.startswith('/api/schedules/') else {} if path.endswith('/compact') else {'disabled':True}
                if path=='/api/auth/me' or path=='/api/sessions' or path=='/api/mac/status' or path=='/api/usage/summary' or method=='GET': response=await client.get(url,headers=b_headers)
                elif method=='PATCH': response=await client.patch(url,json=body,headers=b_headers)
                elif method=='DELETE': response=await client.delete(url,headers=b_headers)
                else: response=await client.post(url,json=body,headers=bot_headers if path.endswith('/wait') else b_headers)
            if kind=='resource' or kind=='bot_resource' or kind=='session' or kind=='file_upload': assert response.status_code==404,response.text
            elif kind=='admin': assert response.status_code==403,response.text
            elif kind=='list':
                assert response.status_code==200,response.text
                assert ids['bot'] not in response.text and ids['thread'] not in response.text and ids['memory'] not in response.text and ids['schedule'] not in response.text
            elif kind=='provider_list': assert response.status_code==200 and ids['provider'] not in response.text
            elif kind=='activity': assert response.status_code==200 and response.json()=={'items':[],'next':None},response.text  # у A в ленте есть записи, у B пусто
            elif kind=='pause_all':
                assert response.status_code==200 and ids['bot'] not in response.text,response.text
                async with app.state.pool.acquire() as con: assert await con.fetchval('select paused from bothub.bots where id=$1',ids['bot']) is path.endswith('resume-all')  # бот A не тронут
            elif kind=='secret_list': assert response.status_code==200 and 'a-private-secret' not in response.text and response.json()==[]
            elif kind=='procedure_list': assert response.status_code==200 and ids['procedure'] not in response.text and 'A procedure' not in response.text
            elif kind=='model_list': assert response.status_code==200 and ids['model'] not in response.text
            elif kind=='usage_summary': assert ids['bot'] not in response.text
            elif kind=='setup': assert response.status_code==409
            elif kind=='login': assert response.status_code==401
            elif kind=='password': assert response.status_code==401
            elif kind=='check': assert response.json()=={'valid':False}
            elif kind=='accept': assert response.status_code==410
            elif kind=='me': assert response.json()['id']==b['id']
            elif kind=='sessions': assert a_session not in response.text
            elif kind=='hook': assert response.status_code==403  # расписание существует, токен неверный: 403 по контракту раздела 8
            elif kind=='mac_status': assert response.json()['state']=='offline'
            elif kind=='own_create': assert response.status_code in (200,201),response.text
            elif kind=='own_delete' or kind=='logout': assert response.status_code==200,response.text
            elif kind=='draft': assert response.status_code==502,response.text
            elif kind=='public': assert response.status_code==200,response.text
            if path=='/api/models/refresh':
                async with app.state.pool.acquire() as con:
                    assert await con.fetchval('select last_check_at from bothub.providers where id=$1',a_provider) is None
