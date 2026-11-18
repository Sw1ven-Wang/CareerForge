"""Evaluate zero-model action routing against independently specified expectations."""
import json
from pathlib import Path

from careerforge.contracts_conversation_actions_v1 import DialogueMessage
from careerforge.services.dialogue_routing import explicit_actions

cases = [
    ('jd_import', 'JD：熟悉 Python 和 RAG 的 AI 工程师', ['import_jd']),
    ('extract', '确认提取', ['start_extraction']),
    ('build', '确认目标', ['start_build']),
    ('research', '搜项目并入库', ['start_project_discovery']),
    ('topic', '我想学习 RAG', ['set_learning_topic']),
    ('repository', 'https://github.com/topoteretes/cognee', ['set_repository']),
    ('plan', '生成学习计划', ['propose_plan']),
    ('confirm', '确认学习计划', ['accept_plan','start_tutor']),
    ('tutor', '开始讲解', ['start_tutor']),
    ('practice', '开始练习', ['start_practice']),
    ('assessment', '评估我的答案', ['assess_practice']),
    ('feedback', '我没理解', ['record_feedback']),
    ('progress', '查询进度', ['query_progress']),
    ('pause', '暂停当前任务', ['request_pause']),
    ('resume', '/resume', ['resume_workflow']),
    ('cancel', '取消当前任务', ['cancel_workflow']),
    ('retry', '补证重试', ['retry_build']),
    ('module', '选择模块 1', ['select_learning_module']),
    ('method', '我想学习 cognee 的 RRF 排名融合是怎么实现的。', []),
    ('repo_method', '请解释 https://github.com/topoteretes/cognee 中 add 如何实现文档导入', []),
    ('jd_question', '请解释岗位要求中的 RAG 是什么。', []),
    ('jd_product_help', '岗位要求是如何提取成技术需求的？', []),
    ('quoted_jd', '请解释字符串“岗位职责”的含义，不要导入。', []),
    ('negated_pause', '不要暂停当前任务，只告诉我进度。', []),
    ('ambiguous', '那个就这样吧。', []),
]
rows=[]
for name,text,expected in cases:
    actions=explicit_actions(DialogueMessage(text=text,expected_focus_revision=1,idempotency_key=name))
    actual=[a[0] for a in actions or []]
    rows.append(dict(name=name,question=text,expected=expected,actual=actual,passed=actual==expected))
out=Path('private/local-validation/agent-routing-20261116/explicit-routes.json')
out.write_text(json.dumps(dict(cases=rows,total=len(rows),passed=sum(r['passed'] for r in rows)),ensure_ascii=False,indent=2),encoding='utf-8')
print(f"explicit routes: {sum(r['passed'] for r in rows)}/{len(rows)}")
for row in rows:
    if not row['passed']:print(row['name'],row['actual'])
