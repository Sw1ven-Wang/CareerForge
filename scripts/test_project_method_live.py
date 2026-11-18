"""Observe current behavior without bypassing conversation admission."""
import json
import time
import uuid
from pathlib import Path

import httpx

p=Path('private/local-validation/project-method-live.json')
r=json.loads(p.read_text(encoding='utf-8')) if p.exists() else {'status':'running','cases':[]}
c=httpx.Client(base_url='http://127.0.0.1:8001',headers={'X-CareerForge-Desk':'1'},timeout=90)
def get(path):
    x=c.get('/desk'+path);x.raise_for_status();return x.json()
def save(): p.write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
cases=[
 ('natural','我想学习 cognee 的 RRF 排名融合是怎么实现的。'),
 ('explicit','请解释 topoteretes/cognee 项目中 rank_chunk_summary_pairs 的 RRF 排名融合实现，给出固定源码引用。'),
 ('missing','请分析 https://github.com/topoteretes/cognee 中 add 和 cognify 如何将文档转换为知识图谱，定位实现方法和调用关系。如果现有知识不足，请提取相关源码知识并审核，再回答原问题。'),
]
for name,question in cases:
    s=next((x for x in r['cases'] if x['name']==name),None)
    if s and s.get('done'):continue
    if s is None:
        s={'name':name,'question':question,'request_id':uuid.uuid4().hex,'before_run_ids':[x['id'] for x in get('/runs')],'before_publication_ids':[x['publication_id'] for x in get('/overview')['publications']]}
        r['cases'].append(s);save()
    if 'job' not in s:
        x=c.post('/desk/messages',json={'question':question,'request_id':s['request_id'],'use_model':True,'agent':'auto'})
        x.raise_for_status();s['job']=x.json();save()
    for _ in range(600):
        j=next(x for x in get('/overview')['jobs'] if x.get('request_hash')==s['job']['request_hash'])
        if j['status']!='running':break
        time.sleep(3)
    else:raise RuntimeError('still running; resume saved request')
    s['final']=j;s['conversation']=get('/conversations/'+j['conversation_id'])
    s['new_runs']=[x for x in get('/runs') if x['id'] not in s['before_run_ids']]
    s['new_publications']=[x['publication_id'] for x in get('/overview')['publications'] if x['publication_id'] not in s['before_publication_ids']]
    s['done']=True;save()
    print(name,j['status'],'routing',j.get('routing',{}).get('intents'),'runs',[(x['workflow'],x['status']) for x in s['new_runs']],'publications',len(s['new_publications']),flush=True)
r['status']='completed';save()
