"""Explicit test doubles. Never imported by production package or advertised as LLM quality."""

from __future__ import annotations

import hashlib
from copy import deepcopy

from careerforge.contracts import ModelOutput, ToolCall

CODE = 'def echo(value):\n    """Return a supplied value."""\n    return value\n'
FILES = {
    "src/tools.py": CODE,
    "README.md": "# Fixture Agent\nOffline test fixture, not a real repository.\n",
    "LICENSE": "MIT",
}


class FixtureModel:
    synthetic = True
    supports_embeddings = False
    supports_reranking = False
    profile_version = "fixture-model-v1-not-real-quality"

    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.embedding_dimension = 8

    async def complete(self, request):
        self.calls.append(deepcopy(request))
        role, c = request["role"], request["context"]
        if role in self.overrides:
            value = self.overrides[role](request)
            if isinstance(value, Exception):
                raise value
            return value
        if role in {"analysis", "reviewer"} and not request.get("schema"):
            return ModelOutput(
                synthetic=True,
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish_investigation",
                        arguments={"reason": "evidence_sufficient"},
                    )
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            )
        if role == "analysis":
            reads = c["reading"]["reads"]
            r = next((r for r in reads if r["path"].endswith("tools.py")), reads[0])
            unit = {
                "subject_key": "echo",
                "unit_type": "concept" if c["origin_kind"] == "concept" else "function",
                "title": "返回值示例",
                "applicability": "default",
                "claims": [
                    {
                        "key": "return",
                        "statement": "该函数返回传入的 value。",
                        "conditions": [],
                        "kind": "source_fact",
                        "support": [
                            {
                                "read_id": r["read_id"],
                                "quote": "return value"
                                if "return value" in r["content"]
                                else r["content"].strip(),
                            }
                        ],
                        "counterevidence": [],
                    }
                ],
                "topics": [{"topic_key": "agent.tool_calling", "relation": "illustrates"}],
                "facets": [],
                "dependency_paths": [r["path"]],
                "limitations": ["合成测试材料；未运行参考代码。"],
            }
            value = {"units": [unit], "unanswered_questions": []}
        elif role == "reviewer":
            r = next(
                (r for r in c["independent_bundle"]["reads"] if r["path"].endswith("tools.py")),
                c["independent_bundle"]["reads"][0],
            )
            value = {
                "claim_decisions": [
                    {
                        "claim_id": x["claim_id"],
                        "outcome": "supported_within_scope",
                        "independent_reads": [
                            {
                                "read_id": r["read_id"],
                                "quote": "return value"
                                if "return value" in r["content"]
                                else r["content"].strip(),
                            }
                        ],
                        "counterevidence": [],
                        "boundary": "仅示例固定片段",
                    }
                    for x in c["subject"]["claims"]
                ],
                "accepted_topic_link_ids": [x["id"] for x in c["subject"]["topic_links"]],
                "accepted_facet_ids": [x["id"] for x in c["subject"]["facets"]],
                "blocking_issues": [],
                "decision": "accept_scoped",
            }
            # Explicit synthetic role output for the new scoped protocol. This
            # test double is not semantic/native evidence. Missing-field legacy
            # behavior is covered separately and is never promoted to true.
            if "scope_identity" in c["subject"]:
                value["scope_review"] = {
                    **c["subject"]["scope_identity"],
                    "standalone_claim_ids": [x["claim_id"] for x in c["subject"]["claims"]],
                    "issue_scopes": [],
                    "global_blocking_issues": [],
                }
        elif role == "coverage":
            value = (
                {"source_coverages": []}
                if c.get("assessment_kind") == "independent_task_sufficiency"
                else {
                    "all_facts_bound": True,
                    "conditions_preserved": True,
                    "uncovered_elements": [],
                    "unsupported_titles": [],
                    "rationale_codes": ["fixture_exact_render"],
                }
            )
            if "scope_binding" in c:
                value["scope_audit"] = {
                    **c["scope_binding"],
                    "elements": [
                        {
                            **{k: x[k] for k in ("element_id", "claim_id", "fact_atom_id")},
                            "fact_bound": True,
                            "conditions_preserved": True,
                            "standalone": True,
                            "reason": "Explicit synthetic protocol fixture; not a semantic assessment.",
                        }
                        for x in c["subject"]["scope_elements"]
                    ],
                    "global_blocking_issues": [],
                }
            if "relation_audit" in (request.get("schema") or {}).get("properties", {}):
                value["relation_audit"] = {"confirmed_local_issue_indices": [], "topic_links": [], "facets": []}
        elif role == "extract":
            value = {
                "requirements": [
                    {
                        "key": "tool",
                        "technology": "Tool Calling",
                        "keywords": ["tool", "dispatch"],
                        "depth": "熟悉",
                        "depth_basis": "explicit",
                        "quote": c["jd_text"],
                        "topic_key": "agent.tool_calling",
                    }
                ],
                "limitations": [],
            }
        elif role == "planner":
            value = {
                "queries": [
                    {
                        "query": "tool registry",
                        "question": "哪里有工具注册实现",
                        "topic_keys": ["agent.tool_calling"],
                    },
                    {
                        "query": "agent dispatch",
                        "question": "哪里有调用分发",
                        "topic_keys": ["agent.tool_calling"],
                    },
                ],
                "stop_reason": None,
            }
        elif role == "reflector":
            value = {
                "queries": [
                    {
                        "query": "tool error boundary",
                        "question": "错误返回的相关线索",
                        "topic_keys": ["agent.tool_calling"],
                    }
                ],
                "stop_reason": None,
            }
        elif role == "tutor":
            value = {
                "blocks": [
                    {
                        "kind": "concept_fact"
                        if u["origin_kind"] == "concept"
                        else "repository_fact",
                        "text": f["text"],
                        "fact_atom_ids": [f["fact_atom_id"]],
                        "publication_ids": [u["publication_id"]],
                        "conditions": f["conditions"],
                        "transformation": "exact",
                        "style": "paragraph",
                    }
                    for u in c["evidence_pack"]["knowledge_units"]
                    for f in u["facts"]
                ]
            }
            if c.get("block_contract", {}).get("version") == "atomic-answer-1.6.1":
                count = len(value["blocks"])
                value["blocks"] = value["blocks"][:1]
                value["complete"] = count <= 1
        elif role == "answer_checker":
            if request.get("schema", {}).get("title") in {
                "AnswerCompletionCheck",
                "AnswerAspectCheck",
            }:
                # Explicit protocol fixture, not an independent semantic-quality
                # oracle. Adversarial/partial-question tests override this branch.
                verified = {
                    item["fact"]["fact_atom_id"]: item["fact"] for item in c["verified_facts"]
                }
                accepted = c["accepted_blocks"]
                exact = bool(accepted) and all(
                    block["fact_atom_id"] in verified
                    and block["text"] == verified[block["fact_atom_id"]]["text"]
                    and block["conditions"] == verified[block["fact_atom_id"]]["conditions"]
                    for block in accepted
                )
                return ModelOutput(
                    structured={
                        "status": "complete" if exact else "unknown",
                        "covered_fact_ids": list(
                            dict.fromkeys(block["fact_atom_id"] for block in accepted)
                        ),
                        "missing_aspects": []
                        if exact
                        else ["fixture cannot validate the final phrasing"],
                        "reasons": [
                            "synthetic protocol fixture: exact accepted facts; not semantic quality"
                        ],
                        **(
                            {
                                "answered_aspects": [
                                    {
                                        "requirement": c["original_question"],
                                        "answer_quote": block["text"][:1500],
                                        "fact_atom_id": block["fact_atom_id"],
                                    }
                                    for block in accepted
                                ]
                                if exact
                                else [],
                                "unsupported_aspects": [] if exact else [c["original_question"]],
                                "conclusion_status": "resolved" if exact else "unknown",
                            }
                            if c.get("answer_aspect_contract")
                            else {}
                        ),
                    },
                    synthetic=True,
                )
            value = {
                "supported": False,
                "conditions_preserved": False,
                "quantifiers_preserved": False,
                "no_new_project_facts": False,
                "reasons": ["fixture_reject_free_paraphrase"],
            }
            if "evidence_relevant" in request.get("schema", {}).get("properties", {}):
                exact = c["proposed_block"]["text"] == c["facts"][0]["text"]
                value.update(
                    supported=exact,
                    conditions_preserved=exact,
                    quantifiers_preserved=exact,
                    no_new_project_facts=exact,
                    evidence_relevant=True,
                )
        elif role == "coordinator":
            value = {
                "interpreted_intent": "explain_implementation",
                "topic_keys": ["agent.tool_calling"],
                "project_source_ids": [],
                "reference_resolution": "resolved",
                "confidence": 0.9,
                "proposed_next_action": "retrieve_and_tutor",
                "rationale_codes": ["fixture"],
            }
        elif role == "embedding":
            value = {
                "vectors": [
                    [
                        int(x) / 255
                        for x in hashlib.sha256(t.encode()).digest()[: self.embedding_dimension]
                    ]
                    for t in c["texts"]
                ],
                "model": "fixture-hash-not-semantic",
            }
        elif role == "reranker":
            value = {
                "scores": [{"index": i, "score": 1.0 / (i + 1)} for i in range(len(c["documents"]))]
            }
        elif role == "reflection":
            value = {
                "target": "teaching_skill",
                "content": {
                    "explanation_order": "code_first",
                    "verbosity": "normal",
                    "examples_limit": 2,
                    "check_understanding": False,
                },
                "experience_ids": c["experience_ids"],
                "training_group_ids": c["training_group_ids"],
            }
        elif role == "coding":
            value = {
                "answer": "建议建立示例函数。",
                "citation_ids": [],
                "edits": [
                    {"path": "answer.py", "before": None, "after": "def answer():\n    return 42\n"}
                ],
                "commands": [["python", "-B", "answer.py"]],
            }
        else:
            raise AssertionError("Fixture missing role: " + role)
        return ModelOutput(
            structured=value, synthetic=True, usage={"prompt_tokens": 100, "completion_tokens": 50}
        )


class FixtureRepositories:
    synthetic = True

    def __init__(self):
        self.calls = []

    async def search(self, query, run_id, request_cap, channel="repository"):
        self.calls.append(query)
        return {
            "candidates": [
                {
                    "repository_id": "1",
                    "name": "fixtures/agent",
                    "url": "https://github.com/fixtures/agent",
                    "license": "MIT",
                }
            ],
            "incomplete_results": False,
            "synthetic": True,
        }

    async def snapshot(self, url, run_id, request_cap):
        return {
            "repository_key": url.split("github.com/")[-1],
            "commit_sha": "a" * 40,
            "license_id": "MIT",
            "files": deepcopy(FILES),
            "source_metadata": {"synthetic": True},
        }
