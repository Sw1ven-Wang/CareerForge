"""Real-relay intent matrix in an isolated DB; no teaching side effects.

Expected labels are never sent to the classifier. This tests recognition,
not execution of every downstream workflow; desk integration is separate.
"""

import asyncio
import json
import os
from pathlib import Path

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.contracts_conversation_actions_v1 import DialogueMessage
from careerforge.services.dialogue_routing import explicit_actions

OUT = Path("private/local-validation/agent-routing-20261116")
CASES = [
    ("chat", "我每天只有半小时，应该怎么安排学习比较好？", ["chat"]),
    ("progress", "后台这次为什么这么久，能告诉我现在卡在哪一步吗？", ["progress"]),
    ("start", "我们开始上课吧", ["start"]),
    ("explain", "请解释这个函数为什么使用倒数排名融合，以及 limit 的作用。", ["explain"]),
    ("reexplain", "刚才那个我还是没听懂，换一种更简单的说法。", ["reexplain"]),
    ("practice", "出一道题检验我对刚才内容的理解。", ["practice"]),
    ("assessment", "请评价我已经提交的答案，指出哪里理解有误。", ["assessment"]),
    ("plan", "请根据已经选择的审核材料安排下一周的学习模块。", ["plan"]),
    ("discover", "帮我找几个适合练习 RAG 的开源项目。", ["discover"]),
    ("change_goal", "我想换一个学习方向，把目标改为 Java 后端。", ["change_goal"]),
    ("confirm_plan", "我接受刚才展示的这份学习计划。", ["confirm_plan"]),
    ("submit_answer", "把我上面写的那段解释作为本题答案交上去。", ["submit_answer"]),
    ("advance", "这个模块我阅读完成了，我要进入下一个模块。", ["advance"]),
    ("pause", "先停一下正在运行的任务。", ["pause"]),
    ("resume", "把暂停的那个任务恢复运行。", ["resume"]),
    ("cancel", "我不要继续这个任务了，请取消它。", ["cancel"]),
    ("unknown", "那个就这样吧。", ["unknown"]),
    ("negated_practice", "不要给我出题，我只想聊聊学习时间怎么分配。", ["chat"]),
    ("negated_exercise_reexplain", "不要出题，刚才没听懂，换个方式讲。", ["reexplain"]),
    ("compound", "刚才那个我还不懂，换个方式讲，然后给我一道题。", ["reexplain", "practice"]),
    (
        "gap",
        "请解释 cognee 的 chunk_id.py 如何生成 ID；已有知识不足就按缺口提取源码、独立审核再回答。",
        ["explain"],
    ),
    ("status_not_code", "解释一下为什么后台任务卡住了。", ["progress"]),
    ("natural_method", "我想学习 cognee 的 RRF 排名融合是怎么实现的。", ["explain"]),
    ("quoted_control", "请解释源码字符串“取消当前任务”的含义，不要取消任务。", ["explain"]),
]


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "recognition.json"
    report = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"status": "running", "cases": []}
    )
    app = Application(
        Settings(
            mode="development",
            database_url="sqlite+pysqlite:///" + str((OUT / "routing.db").resolve()),
            state_dir=OUT,
            model_base_url=os.environ["OPENAI_BASE_URL"],
            model_api_key=os.environ["OPENAI_API_KEY"],
            model_name="gpt-6.1-terra",
            model_timeout=300,
        )
    )

    def save():
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if "actor" not in report:
        owner = app.bootstrap("route-test")
        report.update(actor=owner["principal_id"], namespace=owner["namespace_id"])
        report["conversation"] = app.conversations.create(report["actor"])
        save()
    for name, question, expected in CASES:
        row = next((x for x in report["cases"] if x["name"] == name), None)
        if row and row.get("done"):
            continue
        if row is None:
            row = {
                "name": name,
                "question": question,
                "expected": expected,
                "explicit_route": explicit_actions(
                    DialogueMessage(text=question, expected_focus_revision=1, idempotency_key=name)
                ),
            }
            report["cases"].append(row)
            funding = app.runtime.create_funding(
                report["actor"], report["namespace"], 1, roles=["coordinator"], hours=2
            )
            row["funding"] = funding
            row["run_id"] = app.runtime.create(
                report["actor"],
                report["namespace"],
                funding,
                "desk_intent",
                {
                    "conversation_id": report["conversation"],
                    "model_profile": app.provider.profile_version,
                    "data_classes": ["learning_state", "private_jd"],
                    "access_dependencies": [],
                    "context": {
                        "question": question,
                        "history": [
                            {
                                "question": "请讲解 RRF",
                                "answer": "已讲解排序融合原理，并展示了学习计划。",
                            }
                        ],
                        "teaching_history": [
                            {"question": "请讲解 RRF 的实现和 limit 参数。", "status": "completed"}
                        ],
                        "status_snapshot": {"milestone": "tutoring", "focus_revision": 1},
                        "plan": {"active_version_id": "test-plan-version"},
                    },
                },
                1,
                "routing-matrix:" + name,
            )
            save()
        await app.execute(row["run_id"])
        row["actual"] = app.runtime.stage(row["run_id"], "result")
        row["passed"] = bool(row["actual"] and row["actual"]["intents"] == expected)
        row["done"] = True
        app.runtime.revoke_funding(report["actor"], row["funding"])
        save()
        print(
            name,
            "PASS" if row["passed"] else "FAIL",
            (row["actual"] or {}).get("intents"),
            flush=True,
        )
    report.update(
        status="completed",
        passed=sum(r["passed"] for r in report["cases"]),
        total=len(report["cases"]),
    )
    save()
    app.db.engine.dispose()


asyncio.run(main())
