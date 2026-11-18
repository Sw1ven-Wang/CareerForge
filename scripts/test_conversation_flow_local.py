"""Exercise only the web conversation ingress; never substitute workflow APIs."""
import json, time, uuid
from pathlib import Path
import httpx

out = Path('private/local-validation/conversation-flow')
out.mkdir(parents=True, exist_ok=True)
client = httpx.Client(base_url='http://127.0.0.1:8001', headers={'X-CareerForge-Desk':'1'}, timeout=60)
def get(path):
    r=client.get('/desk'+path); r.raise_for_status(); return r.json()
def snapshot():
    o=get('/overview'); g=get('/goals'); runs=get('/runs')
    return {'jd_ids':[x['jd_revision_id'] for x in g['documents']], 'goals':g['goals'], 'plans':o['plans'], 'observations':o['observations'], 'publications':[x['publication_id'] for x in o['publications']], 'runs':runs}
steps=[
 ('jd_import','请导入这个岗位 JD 并提取技术要求：AI 应用工程师，要求掌握 Python、异步编程、RAG 检索与 Agent 工具调用，能够阅读开源项目源码。'),
 ('project_research','请为这个学习目标搜索和调研合适的开源项目，优先 topoteretes/cognee，并构建经过审核的学习知识。'),
 ('plan','请基于已有的 topoteretes/cognee 检索知识，为我创建一份学习计划，先学习 RRF 排名融合，再学习候选处理。'),
 ('accept_plan','我确认采用刚才的学习计划，请启用它并开始第一个学习模块。'),
 ('tutor','请根据已发布知识解释 RRF 排名融合的作用，并引用具体项目代码。'),
 ('practice','请为 RRF 排名融合生成一道练习题，并记录为当前学习模块的练习。'),
 ('feedback','这是我的练习答案：RRF 使用各检索列表的名次计算倒数贡献并相加。请评估答案、记录练习反馈并更新学习进度。'),
 ('progress','请查询任务进度。'),
]
report={'status':'running','transport':'same /desk/messages endpoint as web chat','conversation_id':None,'steps':[],'before':snapshot()}
def save():
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
save()
for name,question in steps:
    entry={'stage':name,'question':question,'request_id':'flow-test-'+str(uuid.uuid4()),'status':'submitting'}
    report['steps'].append(entry);save()
    response=client.post('/desk/messages',json={'question':question,'conversation_id':report['conversation_id'],'request_id':entry['request_id'],'use_model':True})
    response.raise_for_status(); job=response.json();entry['job']=job;report['conversation_id']=job['conversation_id'];save()
    print(name,job.get('run_id'),flush=True)
    deadline=time.monotonic()+1500
    while True:
        conv=get('/conversations/'+report['conversation_id'])
        turn=next(t for t in conv['turns'] if t['id']==job['turn_id'])
        entry['status']=turn['status'];entry['turn']=turn;save()
        jobs=get('/overview')['jobs']; own=next((j for j in jobs if j.get('turn_id')==job['turn_id']),{})
        if own.get('status') not in ['running'] and turn['status'] not in ['queued','running']:
            entry['job_final']=own;break
        if time.monotonic()>deadline:
            report['status']='timed_out';save();raise RuntimeError('Run still active: '+str(job.get('run_id')))
        time.sleep(4)
    entry['after']=snapshot();save()
    blocks=[e['payload'].get('content',{}) for e in turn.get('events',[]) if e['type']=='answer.block.validated']
    print(name,entry['status'],[(b.get('kind'),b.get('text','')[:140]) for b in blocks],flush=True)
report['after']=snapshot();report['status']='completed';save()
