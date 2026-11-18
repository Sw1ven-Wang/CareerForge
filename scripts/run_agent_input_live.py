"""Run an input-only development chain with native ownership, receipts and runtime IDs.

The fixture DTOs are scenario inputs, not pre-created database records. Stage
adapters are explicit: producers' real outputs replace prepared downstream
placeholders. No expected answer or judge data is opened by this executor.
"""

import argparse
import asyncio
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

import httpx

# A source-checkout entry point must never silently load an older installed CareerForge.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_input_runtime import AdmissionJournal, InputFlow, RuntimeBindings, digest
from full_loop_business import continue_business
from sqlalchemy import select

from careerforge.api import create_app
from careerforge.application import Application
from careerforge.config import Settings
from careerforge.models import AgentRun, ModelReceipt, SourceRead, StageArtifact
from careerforge.models_dialogue import WorkflowSession
from careerforge.workflows.runtime_adapter import GraphRuntimeAdapter


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class LiveFlow:
    def __init__(self, args):
        self.args = args
        if not (
            8 <= args.build_call_cap <= 100
            and args.build_call_cap <= args.call_cap <= 200
            and 1 <= args.per_arm_call_cap <= 8
        ):
            raise ValueError("Invalid explicitly bounded development call caps")
        self.flow = InputFlow(args.agent_input_root, args.flow_id)
        self.out = Path(args.state_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.file = self.out / "result.json"
        self.report = (
            json.loads(self.file.read_text())
            if self.file.exists()
            else {
                "contract": "agent-input-native-chain-v1",
                "flow_id": args.flow_id,
                "input_fingerprint": self.flow.fingerprint,
                "model": "gpt-6.1-terra",
                "effort": "medium",
                "stages": {},
                "pipeline_stages": {},
                "bindings": [],
                "wire_requests": [],
                "supplements": {
                    "plan_acceptance": "explicit authorized automated development actor",
                    "practice_submission": "AI-assisted integration explanation from reviewed facts; not a real learner",
                    "feedback_consent": "authorized integration actor only; no human observation inferred",
                    "evolution_replay": "development_probe from reviewed facts; not independent reference answers",
                },
                "independent_gold": "NOT_RUN",
                "status": "ready",
                "call_cap": args.call_cap,
            }
        )
        if self.report["input_fingerprint"] != self.flow.fingerprint:
            raise ValueError("Frozen selected input changed; use a new state directory")
        if self.report["call_cap"] != args.call_cap:
            raise ValueError("Do not silently change the original funding cap")
        unmaterialized = [row for row in self.report["bindings"] if not row.get("actual")]
        if unmaterialized:
            self.report.setdefault("binding_recovery_history", []).append(
                {
                    "reason": "Old admission-only placeholders were not materialized domain identities",
                    "old_rows": deepcopy(unmaterialized),
                }
            )
            self.report["bindings"] = [row for row in self.report["bindings"] if row.get("actual")]
        self.bindings = RuntimeBindings(self.report["bindings"])
        self.journal = AdmissionJournal(self.report.setdefault("admissions", {}))
        for request in self.report.get("wire_requests", []):
            self.journal.prepare(request["method"], request["path"], request.get("request"))

    def persist(self):
        write_json(self.file, self.report)

    def bind_field(self, field, actual, origin):
        from agent_input_runtime import ALIASES

        def visit(value, key=None):
            canonical = ALIASES.get(key, key)
            if canonical and canonical.endswith("_ids"):
                canonical = canonical[:-1]
            if isinstance(value, dict):
                for k, v in value.items():
                    visit(v, k)
            elif isinstance(value, list):
                for v in value:
                    visit(v, canonical)
            elif canonical == field and isinstance(value, str) and value.startswith("fixture-"):
                self.bindings.bind(field, value, actual, origin)

        for row in self.flow.rows.values():
            visit(row["input"]["payload"])

    def record(self, stage, resolved, output, adapter):
        row = self.flow.rows[stage]
        self.report["pipeline_stages"][stage] = {
            "case_id": row["case_id"],
            "input_hash": digest(row),
            "original_contract": row["input"]["contract_ref"],
            "original_input": row["input"]["payload"],
            "runtime_input": resolved,
            "actual_output": output,
            "adapter": adapter,
            "actual_output_hash": digest(output),
            "gold_scored": False,
        }

    def save(self, stage, value):
        prior = self.report["stages"].get(stage)
        if prior is not None:
            if prior != value:
                self.report.setdefault("resume_observations", []).append(
                    {"stage": stage, "original_hash": digest(prior), "observed": value}
                )
            value = prior
        else:
            self.report["stages"][stage] = deepcopy(value)
        self.capture(stage, value)
        self.persist()
        print(
            json.dumps(
                {"stage": stage, "status": value.get("status", "recorded")}, ensure_ascii=False
            ),
            flush=True,
        )

    def capture(self, stage, value):
        owner, app = self.owner, self.app
        actor = owner["principal_id"]
        if stage == "jd_import":
            revision = value["action_receipts"][0]["jd_revision_id"]
            self.bind_field("jd_revision_id", revision, "native import_jd action receipt")
            doc = app.jd.read(actor, revision)
            if doc["raw_text"] != self.flow.payload("S01")["raw_text"]:
                raise ValueError("JD byte/text identity changed")
            self.bind_field("jd_id", doc["jd_id"], "native JDRevision")
            self.record(
                "S01",
                {
                    **self.flow.payload("S01"),
                    "namespace_id": owner["namespace_id"],
                    "jd_id": doc["jd_id"],
                },
                doc,
                "Dialogue import projects exact raw text; original source/paragraph metadata stays in original_input",
            )
        if stage == "jd_extracted":
            if not value.get("goal_version_id"):
                raise RuntimeError("Extraction has no GoalVersion")
            runs = self.report["stages"]["jd_import"]["run_refs"]
            self.record(
                "S02",
                self.bindings.resolve(self.flow.payload("S02")),
                {"status": value, "extraction": app.runtime.stage(runs[0], "result")},
                "Native extraction uses the returned JD revision and owned funding",
            )
        if stage == "after_evidence_retry":
            publications = value["publication_count"]
            with app.db.read() as db:
                workflow = db.get(WorkflowSession, value["workflow_id"])
                pids = workflow.target.get("publication_ids", [])
                packs = [app.knowledge.publication_pack(db, actor, pid) for pid in pids]
                builds = list(
                    db.scalars(select(AgentRun).where(AgentRun.workflow == "knowledge_build"))
                )
                artifacts = [
                    {"run_id": a.run_id, "stage_key": a.stage_key, "payload": a.payload}
                    for a in db.scalars(select(StageArtifact))
                    if a.run_id in {r.id for r in db.scalars(select(AgentRun))}
                ]
                run_kinds = {r.id: r.workflow for r in db.scalars(select(AgentRun))}
                planning_receipts = [
                    {"id": r.id, "stage_key": r.stage_key, "response": r.response}
                    for r in db.scalars(select(ModelReceipt))
                    if r.stage_key == "research.initial_plan"
                ]
                plan_runs = [
                    r for r in db.scalars(select(AgentRun)) if r.workflow == "learning_plan"
                ]
            if pids:
                self.bind_field("publication_id", pids, "native reviewed publication packs")
            self.bind_field(
                "workflow_id",
                value["workflow_id"],
                "current learning workflow after explicit retry",
            )
            for number in (3, 4, 5, 7, 8, 9):
                key = f"S{number:02}"
                relevant = [
                    a
                    for a in artifacts
                    if (number == 3 and "context" in a["stage_key"])
                    or (number == 4 and "plan" in a["stage_key"])
                    or (number == 5 and "research" in a["stage_key"])
                    or (
                        number == 7
                        and run_kinds.get(a["run_id"]) == "analysis"
                        and a["stage_key"] in {"analysis_data", "result"}
                    )
                    or (
                        number == 8
                        and run_kinds.get(a["run_id"]) == "review"
                        and a["stage_key"] in {"review_data", "result"}
                    )
                ]
                self.record(
                    key,
                    {
                        "research_question": self.flow.payload("S03")["original_question"],
                        "native_build_ids": [b.id for b in builds],
                    },
                    {
                        "artifacts": relevant,
                        "planner_receipts": planning_receipts if number == 4 else [],
                        "publications": packs if number == 9 else [],
                        "publication_count": publications,
                        "coverage": value["coverage"],
                    },
                    "Chained producer adapter: consumes native upstream artifacts, never inserts prepared review decisions or claims",
                )
            self.record(
                "S11",
                self.bindings.resolve(self.flow.payload("S11")),
                {
                    "status": value,
                    "plan_results": [app.runtime.stage(r.id, "result") for r in plan_runs],
                },
                "Native session planner consumes current GoalVersion and actual reviewed publications; confirmation remains explicit",
            )
        if stage == "plan_accepted":
            profile = app.learning.profile(actor)
            self.bind_field(
                "module_id", profile["active_module_id"], "explicitly accepted plan active module"
            )
            self.record(
                "S10",
                {"question": self.flow.payload("S10")["question"]},
                value,
                "Current dialogue control/start_tutor lane; legacy TurnInput is a scenario question projection",
            )
        if stage == "tutor_initial":
            events = value["events"]
            turn = events[0]["payload"]["turn_id"]
            self.bind_field("turn_id", turn, "native accepted Tutor turn")
            self.record(
                "S12",
                self.bindings.resolve(self.flow.payload("S12")),
                {
                    "turn_id": turn,
                    "validated_blocks": [
                        e for e in events if e["type"] == "answer.block.validated"
                    ],
                },
                "Accepted session retrieves only frozen authorized published facts; inspect answer citations and selection manifests",
            )
            self.record(
                "S13",
                {"question": self.flow.payload("S13")["question"], "turn_id": turn},
                value,
                "Guided Tutor request bound to accepted plan and independent citation guard",
            )
        if stage == "practice_started":
            receipt = value["action_receipts"][0]
            if receipt.get("attempt_id"):
                self.bind_field(
                    "attempt_id",
                    receipt["attempt_id"],
                    "native materialized practice start receipt",
                )
        if stage == "practice_submitted":
            self.bind_field(
                "attempt_id",
                value["action_receipts"][0]["attempt_id"],
                "native materialized submit receipt",
            )
            self.record(
                "S14",
                self.bindings.resolve(self.flow.payload("S14")),
                value,
                "Native Activity and Attempt; missing learner answer is explicitly supplemented by an assisted integration response",
            )
        if stage == "assessment_result":
            self.bind_field("attempt_id", value["attempt_id"], "native assessment detail witness")
            self.record(
                "S15",
                self.bindings.resolve(self.flow.payload("S15")),
                value,
                "Real model assessment of the actual frozen submission; no mastery assertion",
            )
        if stage == "learner_observation":
            self.record(
                "S16",
                {"observation": value["action_receipts"][0]},
                value,
                "Expand placeholder rubric into actual activity rubric IDs; unknown remains unknown",
            )
        if stage == "learner_feedback":
            experience = value["action_receipts"][0]["experience_id"]
            self.bind_field("experience_id", experience, "native explicit feedback experience")
        if stage == "business_cursor":
            self.record(
                "S17",
                self.bindings.resolve(self.flow.payload("S17")),
                value,
                "Actual feedback candidate plus native paired development replay; never human_gold or promotion",
            )

    async def run(self):
        args = self.args
        if not args.execute:
            print(
                json.dumps(
                    {
                        "status": "preflight_passed",
                        "stages": len(self.flow.rows),
                        "input_fingerprint": self.flow.fingerprint,
                        "model_calls": 0,
                    }
                )
            )
            return 0
        async with httpx.AsyncClient(timeout=10) as health_client:
            health_response = await health_client.get(
                os.environ["OPENAI_BASE_URL"].rstrip("/").removesuffix("/v1") + "/health"
            )
            health_response.raise_for_status()
            health = health_response.json()
            if health.get("model") != "gpt-6.1-terra" or health.get("effort") != "medium":
                raise ValueError("Configured subscription bridge is not Terra 6.1 medium")
            self.report["provider_health"] = health
        credentials = Path(args.credential_file).resolve()
        if credentials.is_relative_to(self.out.resolve()) or credentials.is_relative_to(
            self.flow.root
        ):
            raise ValueError(
                "Owner credentials must stay outside solver inputs and public evidence"
            )
        credentials.parent.mkdir(parents=True, exist_ok=True)
        # Fail before account creation if credentials cannot be persisted.
        from tempfile import NamedTemporaryFile

        with NamedTemporaryFile(dir=credentials.parent):
            pass
        if self.file.exists() and not args.resume:
            raise ValueError("Existing state requires --resume")
        self.app = app = Application(
            Settings(
                mode="development",
                database_url="sqlite+pysqlite:///" + str(self.out / "workflow.sqlite"),
                state_dir=self.out,
                workflow_engine="langgraph",
                session_graph_version="knowledge-session-v2",
                learning_graph_version="learning-session-practice-v1",
                evolution_graph_enabled=True,
                graph_checkpoint_url=os.environ["CAREERFORGE_TEST_GRAPH_POSTGRES_DSN"],
                model_base_url=os.environ["OPENAI_BASE_URL"],
                model_api_key=os.environ["OPENAI_API_KEY"],
                model_name="gpt-6.1-terra",
                model_timeout=180,
                github_token=os.getenv("GITHUB_TOKEN", ""),
            )
        )
        app.graph_runtime = GraphRuntimeAdapter(app)
        async with app.graph_runtime.checkpointers.open() as saver:
            await saver.setup()
        if credentials.exists():
            self.owner = json.loads(credentials.read_text())
        else:
            if self.file.exists():
                raise ValueError(
                    "Original owner credential missing; no replacement identity allowed"
                )
            self.owner = app.bootstrap("agent-input-" + args.flow_id)
            self.owner["funding_id"] = app.runtime.create_funding(
                self.owner["principal_id"], self.owner["namespace_id"], args.call_cap, hours=8
            )
            credentials.parent.mkdir(parents=True, exist_ok=True)
            credentials.write_text(json.dumps(self.owner))
            credentials.chmod(0o600)
        owner = self.owner
        for field in ("namespace_id", "funding_id"):
            self.bind_field(field, owner[field], "isolated native bootstrap/explicit funding")
        if self.report.get("error"):
            self.report.setdefault("resume_failures", []).append(deepcopy(self.report["error"]))
        self.report["status"] = "running"
        self.report.pop("error", None)
        self.persist()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(app)),
            base_url="http://local/api/v1",
            headers={"Authorization": "Bearer " + owner["token"]},
        ) as api:

            async def call(method, path, value=None):
                value = self.journal.prepare(method, path, value)
                self.persist()
                response = await api.request(method, path, json=value)
                self.report["wire_requests"].append(
                    {
                        "method": method,
                        "path": path,
                        "request": value,
                        "http_status": response.status_code,
                    }
                )
                if response.is_error:
                    self.persist()
                    raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
                return response.json()

            try:
                if not self.report.get("conversation_id"):
                    conv = await call("POST", "/conversations", {})
                    self.report["conversation_id"] = conv["conversation_id"]
                self.bind_field(
                    "conversation_id",
                    self.report["conversation_id"],
                    "native conversation create receipt",
                )
                path = "/conversations/" + self.report["conversation_id"]
                # Rehydrate only from original native receipts and current owned object witnesses.
                if "source_imported" in self.report["stages"]:
                    self.bind_field(
                        "source_id",
                        self.report["stages"]["source_imported"]["source_id"],
                        "native fixed corpus source import receipt",
                    )
                for recovered_stage in (
                    "jd_import",
                    "jd_extracted",
                    "after_evidence_retry",
                    "plan_accepted",
                    "tutor_initial",
                    "practice_started",
                    "practice_submitted",
                    "assessment_result",
                    "learner_observation",
                    "learner_feedback",
                ):
                    if recovered_stage in self.report["stages"]:
                        self.capture(recovered_stage, self.report["stages"][recovered_stage])
                self.persist()

                async def message(stage, text, **kwargs):
                    conv = await call("GET", path)
                    return await call(
                        "POST",
                        path + "/messages",
                        dict(
                            text=text,
                            expected_focus_revision=conv["revision"],
                            idempotency_key=self.flow.flow_id + "-" + stage,
                            **kwargs,
                        ),
                    )

                if "source_imported" not in self.report["stages"]:
                    source = self.bindings.resolve(self.flow.payload("S06"))
                    self.save("source_imported", await call("POST", "/sources", source))
                sid = self.report["stages"]["source_imported"]["source_id"]
                self.bind_field("source_id", sid, "native fixed corpus source import receipt")
                self.record(
                    "S06",
                    self.bindings.resolve(self.flow.payload("S06")),
                    await call("GET", "/sources/" + sid),
                    "Preload authorized exact source bytes before research; source is not reviewed knowledge",
                )
                if "jd_import" not in self.report["stages"]:
                    self.save(
                        "jd_import",
                        await message(
                            "jd",
                            self.flow.payload("S01")["raw_text"],
                            kind="jd",
                            authorization_ref=owner["funding_id"],
                        ),
                    )
                await app.drain(limit=40)
                view = await call("GET", path + "/workflow-status")
                if "jd_extracted" not in self.report["stages"]:
                    self.save("jd_extracted", view)
                if "research_admission" not in self.report["stages"]:
                    self.save(
                        "research_admission",
                        await message(
                            "build",
                            "确认目标",
                            authorization_ref=owner["funding_id"],
                            research_question=self.flow.payload("S03")["original_question"],
                            build_call_cap=args.build_call_cap,
                            build_wall_time_contract="unlimited-build-time-v1",
                        ),
                    )
                await app.drain(limit=200)
                view = await call("GET", path + "/workflow-status")
                self.save("after_research", view)
                if (
                    not view["publication_count"]
                    and "evidence_retry_admission" not in self.report["stages"]
                ):
                    original = self.report["stages"]["research_admission"]["run_refs"][0]
                    sources = (app.runtime.stage(original, "sources") or {}).get("source_ids", [])
                    if sid not in sources:
                        raise RuntimeError(
                            "Research did not select the dataset's fixed source; no silent source substitution"
                        )
                    self.save(
                        "evidence_retry_admission",
                        await message(
                            "retry",
                            "补证重试",
                            source_id=sid,
                            authorization_ref=owner["funding_id"],
                            auto_evidence_retry=True,
                            build_call_cap=args.build_call_cap,
                            build_wall_time_contract="unlimited-build-time-v1",
                        ),
                    )
                    await app.drain(limit=200)
                view = await call("GET", path + "/workflow-status")
                if (
                    view["goal_version_id"]
                    != self.report["stages"]["jd_extracted"]["goal_version_id"]
                ):
                    raise RuntimeError("GoalVersion changed across runtime binding")
                self.save("after_evidence_retry", view)
                if not view["publication_count"]:
                    raise RuntimeError("No independently reviewed publications")
                scenario = {
                    "tutor_question": self.flow.payload("S13")["question"],
                    "activity": self.flow.payload("S14"),
                    "observed_behavior": self.flow.payload("S16")["observed_behavior"],
                    "evolution_target": self.flow.payload("S17")["intent"]["target"],
                    "per_arm_call_cap": args.per_arm_call_cap,
                }
                final = await continue_business(
                    app, owner, call, path, self.report, self.save, scenario=scenario
                )
                control_view = await call("GET", path)
                progress = self.report["pipeline_stages"].get("S18", {}).get(
                    "actual_output"
                ) or await message("progress", "当前进度")
                control_input = self.bindings.resolve(self.flow.payload("S18"))
                control_input["expected_focus_revision"] = control_view["revision"]
                control_input["idempotency_key"] = self.flow.flow_id + "-progress"
                self.record(
                    "S18",
                    control_input,
                    progress,
                    "V3 query_progress is projected to the current caller dialogue command, with live focus revision",
                )
                self.save("business_cursor", final)
                self.report["status"] = "business_loop_settled_without_promotion"
                assert len(self.report["pipeline_stages"]) == 18
            except Exception as exc:
                self.report["status"] = "blocked"
                self.report["error"] = {"type": type(exc).__name__, "detail": str(exc)}
            finally:
                with app.db.read() as db:
                    self.report["runs"] = [
                        dict(
                            id=r.id,
                            workflow=r.workflow,
                            status=r.status,
                            calls=r.calls_consumed,
                            input_hash=r.input_hash,
                            result=app.runtime.stage(r.id, "result"),
                        )
                        for r in db.scalars(select(AgentRun))
                    ]
                    self.report["model_receipts"] = [
                        dict(
                            id=r.id,
                            run_id=r.run_id,
                            stage=r.stage_key,
                            status=r.status,
                            usage=r.usage,
                        )
                        for r in db.scalars(select(ModelReceipt))
                    ]
                    self.report["source_read_count"] = len(list(db.scalars(select(SourceRead))))
                self.persist()
                app.db.engine.dispose()
        print(
            json.dumps(
                {"status": self.report["status"], "error": self.report.get("error")},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0 if self.report["status"] == "business_loop_settled_without_promotion" else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agent-input-root", required=True)
    p.add_argument("--flow-id", default="CF-D01")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--credential-file", required=True)
    p.add_argument("--call-cap", type=int, default=160)
    p.add_argument("--build-call-cap", type=int, default=48)
    p.add_argument("--per-arm-call-cap", type=int, default=4)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--resume", action="store_true")
    raise SystemExit(asyncio.run(LiveFlow(p.parse_args()).run()))
