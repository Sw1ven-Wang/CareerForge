import json
import time
import uuid
from pathlib import Path

import httpx

p=Path('private/local-validation/semantic-grounding-live.json')
r=json.loads(p.read_text(encoding='utf-8')) if p.exists() else {'request_id':uuid.uuid4().hex,'status':'running'}
c=httpx.Client(base_url='http://127.0.0.1:8001',headers={'X-CareerForge-Desk':'1'},timeout=90)
cid=json.loads(Path('private/local-validation/semantic-routing-live.json').read_text(encoding='utf-8'))['cid']
def save(): p.write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
save()
if 'job' not in r:
    x=c.post('/desk/messages',json={'question':'刚才那个我还不懂，换个方式讲，然后给我一道题','request_id':r['request_id'],'conversation_id':cid,'use_model':True})
    x.raise_for_status();r['job']=x.json();save()
for _ in range(600):
    j=next(j for j in c.get('/desk/overview').json()['jobs'] if j.get('request_hash')==r['job']['request_hash'])
    if j['status']!='running':break
    time.sleep(3)
else: raise RuntimeError('Still running; resume this script')
r['final']=j;r['detail']=c.get('/desk/conversations/'+cid).json();save()
assert j['status']=='completed',j
assert j['routing']['intents']==['reexplain','practice']
assert [s['status'] for s in j['steps']]==['completed','completed']
turn=r['detail']['turns'][-1]
assert '当前模块的已审核源码' in turn['question']
assert 'repository_fact' in json.dumps(turn['events']), 'Teaching returned no grounded facts'
r['status']='completed';save();print('Context resolved; reexplanation returned source facts; practice exists; completed',flush=True)
