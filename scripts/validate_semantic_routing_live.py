"""Resumable real-relay acceptance through the same web message ingress."""

import json
import time
import uuid
from pathlib import Path

import httpx

path = Path("private/local-validation/semantic-routing-live.json")
report = (
    json.loads(path.read_text(encoding="utf-8"))
    if path.exists()
    else {"status": "running", "cid": None, "steps": []}
)
client = httpx.Client(
    base_url="http://127.0.0.1:8001", headers={"X-CareerForge-Desk": "1"}, timeout=90
)


def save():
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def send(name, question, **extra):
    stage = next((s for s in report["steps"] if s["name"] == name), None)
    if stage and stage.get("done"):
        return stage["detail"]
    if not stage:
        stage = {"name": name, "question": question, "request_id": uuid.uuid4().hex}
        report["steps"].append(stage)
    save()
    if "job" not in stage:
        response = client.post(
            "/desk/messages",
            json={
                "question": question,
                "request_id": stage["request_id"],
                "conversation_id": report["cid"],
                "use_model": True,
                **extra,
            },
        )
        response.raise_for_status()
        stage["job"] = response.json()
        report["cid"] = response.json()["conversation_id"]
        save()
    for _ in range(600):
        jobs = client.get("/desk/overview").json()["jobs"]
        current = next(j for j in jobs if j.get("request_hash") == stage["job"]["request_hash"])
        if current["status"] != "running":
            break
        time.sleep(3)
    else:
        raise RuntimeError("Task still running; resume this same stage later")
    stage["final"] = current
    response = client.get("/desk/conversations/" + report["cid"])
    response.raise_for_status()
    stage["detail"] = response.json()
    stage["done"] = True
    save()
    print(
        name,
        current["status"],
        current.get("routing", {}).get("intents"),
        current.get("reply", "")[:120],
        flush=True,
    )
    assert current["status"] == "completed", current.get("error")
    return stage["detail"]


overview = client.get("/desk/overview").json()
pid = next(
    p["publication_id"] for p in overview["publications"] if p["title"] == "候选融合排序与裁剪边界"
)
send("start", "开始学习")
d = send("plan", "使用审核材料生成计划", publication_ids=[pid])
version = d["flow"]["plan"]["versions"][-1]["version_id"]
d = send("accept", "确认学习计划", confirm_version=version)
d = send("compound", "刚才那个我还不懂，换个方式讲，然后给我一道题")
action = d["actions"][-1]
assert action["routing"]["intents"] == ["reexplain", "practice"], action["routing"]
assert [s["status"] for s in action["steps"]] == ["completed", "completed"], action["steps"]
assert d["flow"]["activity"] and d["flow"]["attempt"]
attempt = d["flow"]["attempt"]["attempt_id"]
d = send("progress", "我现在走到哪一步了，接下来该做什么？")
assert d["actions"][-1]["routing"]["intents"] == ["progress"]
d = send("negation", "不要给我出题，我只是想聊聊每天半小时该怎么分配。")
assert d["actions"][-1]["routing"]["intents"] == ["chat"]
assert d["flow"]["attempt"]["attempt_id"] == attempt
d = send("ambiguous", "就这样吧")
assert d["flow"]["attempt"]["attempt_id"] == attempt
assert d["flow"]["attempt"]["state"] != "submitted"
assert not d["flow"].get("module_ack")
report["status"] = "completed"
save()
