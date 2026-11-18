"""Run concurrency checks on a new disposable DB in the existing local test server.

Credentials stay in process memory and are never emitted or persisted.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

root=Path('private/local-validation/agent-routing-20261116')
marker=root/'postgres-test.json'
container='cf-dialogue-v2-trial-postgres-1'
name='careerforge_validation_agent_routes_20261116'
info=json.loads(subprocess.check_output(['docker','inspect',container]))[0]
env=dict(item.split('=',1) for item in info['Config']['Env'] if '=' in item)
user=env['POSTGRES_USER']
if not marker.exists():
    subprocess.run(['docker','exec',container,'createdb','-U',user,name],check=True,capture_output=True)
    marker.write_text(json.dumps({'database':name,'container':container,'status':'running'}),encoding='utf-8')
test_env=dict(os.environ)
test_env['CAREERFORGE_DIALOGUE_TEST_DATABASE_URL']=f"postgresql+psycopg://{quote(user,safe='')}:{quote(env['POSTGRES_PASSWORD'],safe='')}@127.0.0.1:55438/{name}"
with (root/'postgres-tests.log').open('w',encoding='utf-8') as log:
    result=subprocess.run([sys.executable,'-m','pytest','tests/test_dialogue_pause.py','--asyncio-mode=auto','-q','--disable-warnings',
        '--junitxml='+str(root/'postgres-tests.xml')],env=test_env,stdout=log,stderr=subprocess.STDOUT)
marker.write_text(json.dumps({'database':name,'container':container,'status':'completed','exit_code':result.returncode}),encoding='utf-8')
print('PostgreSQL concurrency tests exit:',result.returncode)
