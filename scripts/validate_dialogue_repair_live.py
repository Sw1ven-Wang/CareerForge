"""Real relay acceptance; resume by saved steps, no duplicate paid messages."""

import json
import time
import uuid
from pathlib import Path

import httpx

p = Path("private/local-validation/dialogue-repair-live.json")
r = (
    json.loads(p.read_text(encoding="utf-8"))
    if p.exists()
    else {"status": "running", "steps": [], "cid": None}
)
c = httpx.Client(base_url="http://127.0.0.1:8001", headers={"X-CareerForge-Desk": "1"}, timeout=90)


def save():
    p.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")


def get(path):
    x = c.get("/desk" + path)
    x.raise_for_status()
    return x.json()


def send(name, text, **extra):
    old = next((s for s in r["steps"] if s["name"] == name), None)
    if old and old.get("done"):
        return old["detail"]
    s = old or {"name": name, "request_id": uuid.uuid4().hex, "question": text}
    if not old:
        r["steps"].append(s)
    save()
    if "job" not in s:
        x = c.post(
            "/desk/messages",
            json={
                "question": text,
                "request_id": s["request_id"],
                "conversation_id": r["cid"],
                "use_model": True,
                **extra,
            },
        )
        x.raise_for_status()
        s["job"] = x.json()
        r["cid"] = s["job"]["conversation_id"]
        save()
    for _ in range(300):
        jobs = get("/overview")["jobs"]
        j = next(j for j in jobs if j.get("request_hash") == s["job"]["request_hash"])
        if j["status"] != "running":
            break
        time.sleep(3)
    else:
        raise RuntimeError("still running")
    s["job_final"] = j
    s["detail"] = get("/conversations/" + r["cid"])
    s["done"] = True
    save()
    print(
        name,
        j["status"],
        s["detail"].get("actions", [])[-1].get("reply", "")[:100]
        if s["detail"].get("actions")
        else "",
        flush=True,
    )
    return s["detail"]


over = get("/overview")
pid = next(
    p["publication_id"] for p in over["publications"] if p["title"] == "候选融合排序与裁剪边界"
)
d = send("jd", "JD：AI 应用工程师，熟悉 Python，理解 RAG 检索与排名融合，能够阅读开源项目源码。")
d = send("plan_retry", "使用已审核检索材料生成学习计划", publication_ids=[pid])
plan = d.get("flow", {}).get("plan")
if not plan:
    raise RuntimeError("No plan: " + str(d.get("flow"))[:1200])
d = send("accept", "确认学习计划", confirm_version=plan["versions"][-1]["version_id"])
d = send("practice", "开始练习")
d = send(
    "answer",
    "提交答案：rank_chunk_summary_pairs 在 limit 不大于零时返回空列表；chunk 为 None 或没有排名则跳过。对有效候选的 vector_rank 和 summary_rank 计算 1/(rrf_k+rank+1) 后相加，启用重要性权重时再调整分数。HybridCandidates._first 将选中的 ID 对应候选放在其余候选之前；不能据此证明已经做了模型重排。",
)
d = send("assessment", "评估我的答案")
teaching = d.get("flow", {}).get("teaching")
if teaching and teaching.get("can_acknowledge_module"):
    d = send("advance", "接受材料缺口并确认阅读完成，进入下一模块")
d = send("progress", "查询学习进度")
r["status"] = "completed"
save()
