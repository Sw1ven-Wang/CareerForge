"""Real desk routing of semantic controls; each case uses a new test conversation."""
import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

import httpx

path=Path('private/local-validation/agent-routing-20261116/semantic-gates.json')
report=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'status':'running','cases':[]}
cases=[
    ('discover','帮我找几个适合练习 RAG 的开源项目。'),
    ('change_goal','我想换一个学习方向，把目标改为 Java 后端。'),
    ('confirm_plan','我接受刚才展示的这份学习计划。'),
    ('submit_answer','把我上面写的那段解释作为本题答案交上去。'),
    ('advance','这个模块我阅读完成了，我要进入下一个模块。'),
    ('pause','先停一下正在运行的任务。'),
    ('resume','把暂停的那个任务恢复运行。'),
    ('cancel','我不要继续这个任务了，请取消它。'),
]
client=httpx.Client(base_url='http://127.0.0.1:8001',headers={'X-CareerForge-Desk':'1'},timeout=60)
def save():path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
for intent,question in cases:
    row=next((r for r in report['cases'] if r['expected']==intent),None)
    if row and row.get('done'):continue
    if row is None:
        row={'expected':intent,'question':question,'request_id':uuid4().hex};report['cases'].append(row);save()
    if 'initial' not in row:
        r=client.post('/desk/messages',json={'question':question,'request_id':row['request_id'],'use_model':True});r.raise_for_status();row['initial']=r.json();save()
    for _ in range(240):
        o=client.get('/desk/overview');o.raise_for_status()
        job=next(j for j in o.json()['jobs'] if j.get('request_hash')==row['initial']['request_hash'])
        if job['status']!='running':break
        time.sleep(2)
    else:raise RuntimeError('Pending run retained; resume this script')
    row['final']=job
    with sqlite3.connect('private/local-runtime/development.db') as db:
        row['runs']=db.execute('select workflow,status from agent_runs where funding_id=(select funding_id from agent_runs where id=?)',(row['initial'].get('run_id'),)).fetchall()
    # Fresh chats have no referenced plan/answer/module/task; clarification is valid.
    row['accepted_intents']=[intent] if intent in {'discover','change_goal'} else [intent,'unknown']
    row['recognition_pass']=job.get('routing',{}).get('intents') in [[i] for i in row['accepted_intents']]
    row['control_gate_pass']=bool(job.get('reply')) and row['runs']==[('desk_intent','succeeded')]
    row['done']=True;save();print(intent,row['recognition_pass'],row['control_gate_pass'],flush=True)
report.update(status='completed',passed=sum(r['recognition_pass'] and r['control_gate_pass'] for r in report['cases']),total=len(report['cases']));save()
