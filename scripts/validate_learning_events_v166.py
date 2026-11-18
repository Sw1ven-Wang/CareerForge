#!/usr/bin/env python3
"""Replay public learning inputs through real services with explicitly fake providers.

Only agent_input/inputs/step06_intent.jsonl and step10_feedback_trajectories.jsonl
are read. Synthetic source/topic fixtures test state plumbing, never real repository
semantics or live model quality. No pass/fail oracle is imported or inferred.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from conftest import env as fixture_environment  # noqa: E402
from conftest import publish_fixture  # noqa: E402
from sqlalchemy import select  # noqa: E402
from test_learning_events_v166 import ack_input, navigation_block  # noqa: E402
from test_source_navigation_v163 import import_navigation_source  # noqa: E402

from careerforge.contracts import ModelOutput, TurnInput  # noqa: E402
from careerforge.contracts_learning_ext import LearningPreferences, ProfileUpdate  # noqa: E402
from careerforge.models import Conversation, ModelReceipt, Topic, Turn  # noqa: E402
from careerforge.services.dependencies import register  # noqa: E402
from careerforge.services.learning_intent import LEARNING_ROUTING_CONTRACT, learning_intent  # noqa: E402
from careerforge.services.orchestration import rule_intent  # noqa: E402
from careerforge.services.policies import ensure_policies  # noqa: E402
from careerforge.services.taxonomy import topic_object_id  # noqa: E402
from careerforge.util import fingerprint  # noqa: E402


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def add_fixture_topic(env, topic):
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        if db.scalar(select(Topic.id).where(Topic.key == topic)):
            return
        db.add(Topic(key=topic, title=topic, aliases=[], facet_schema={}))
        register(
            db,
            actor,
            env["ns"],
            "topic_version",
            {
                "key": topic,
                "title": topic,
                "aliases": [],
                "synthetic_fixture_only": True,
                "provenance": "Public trajectory label, not approved factual repository knowledge",
            },
            topic_object_id(topic),
        )


async def replay(args):
    report = {
        "schema_version": "public-learning-repair-evidence-1.6.6",
        "input_boundary": "agent_input only; no judge, Gold or holdout",
        "test_mode": "real_product_services_with_explicit_synthetic_provider",
        "live_model_calls": 0,
        "semantic_quality_evaluated": False,
        "results": [],
    }
    cases = lines(args.input / "inputs/step06_intent.jsonl")
    trajectories = lines(args.input / "inputs/step10_feedback_trajectories.jsonl")
    with tempfile.TemporaryDirectory(prefix="careerforge-learning-") as directory:
        path = Path(directory)
        generator = fixture_environment.__wrapped__(path)
        env = next(generator)
        app, actor = env["app"], env["actor"]
        source = import_navigation_source(env)
        with app.db.transaction() as db:
            policies = ensure_policies(db, actor, env["ns"])
        for case in cases:
            context = case["context"]
            focus = {
                "project_source_ids": [source] if context["active_repo_id"] else [],
                "code_focus": {"source_id": source, "path": "src/agent.py", "symbol": "Target.run"}
                if context["code_focus"]
                else {},
            }
            result = learning_intent(case["utterance"], focus)
            legacy = rule_intent(case["utterance"], focus)
            actual, decision = await app.coordinator.decide(
                actor,
                case["utterance"],
                focus,
                1,
                policies,
                None,
                routing_contract_version=LEARNING_ROUTING_CONTRACT,
            )
            report["results"].append(
                {
                    "case_id": case["case_id"],
                    "stage": "step06_intent",
                    "status": "exercised",
                    "input_hash": fingerprint(case),
                    "output": result,
                    "actual_coordinator_intent": actual.model_dump(mode="json"),
                    "actual_orchestration": decision.model_dump(mode="json"),
                    "legacy_rule_result": legacy.model_dump(mode="json") if legacy else None,
                    "proof": {
                        "provider_calls": len(env["model"].calls),
                        "fixture_context": focus,
                        "repository_semantics_tested": False,
                    },
                    "limitations": [
                        "The current hard project scope remains restrictive even for comparison; this is route/state testing, not multi-repository answer completeness."
                    ],
                }
            )
        block = await navigation_block(env)
        before = app.learning.navigation_status(actor, block.id)
        ack = app.learning.acknowledge_navigation(actor, ack_input(block))
        report["navigation_proof"] = {
            "synthetic_client_event": True,
            "real_product_block_id": block.id,
            "citation": block.content["citations"][0],
            "before_ack": before,
            "explicit_ack": ack,
            "after_ack": app.learning.navigation_status(actor, block.id),
            "browser_highlight_observed": False,
        }
        try:
            next(generator)
        except StopIteration:
            pass
        for trajectory in trajectories:
            location = path / trajectory["trajectory_id"]
            location.mkdir()
            generator = fixture_environment.__wrapped__(location)
            env = next(generator)
            env["model"].overrides["query_rewriter"] = lambda _: ModelOutput(
                structured={"variants": [], "selected_topic_keys": [], "reason": "no_improvement"},
                synthetic=True,
            )
            app, actor = env["app"], env["actor"]
            source, publication, _ = await publish_fixture(env)
            current_topic = trajectory["current_focus"]
            add_fixture_topic(env, current_topic)
            style = trajectory["initial_preferences"].get("explanation_style", "concept_first")
            style = "concept_first" if style == "general_first" else style
            app.learning.update_profile(
                actor,
                ProfileUpdate(
                    expected_revision=1, preferences=LearningPreferences(preferred_style=style)
                ),
            )
            conv = app.conversations.create(actor)
            with app.db.transaction() as db:
                db.get(Conversation, conv).focus = {
                    "project_source_ids": [source],
                    "topic_hints": [current_topic],
                    "code_focus": {"source_id": source, "path": "src/tools.py", "symbol": "echo"},
                }
            output = {
                "case_id": trajectory["trajectory_id"],
                "stage": "step10_feedback_trajectories",
                "status": "exercised_with_limitations",
                "input_hash": fingerprint(trajectory),
                "turns": [],
                "fixture_setup": {
                    "synthetic_source_id": source,
                    "synthetic_publication_id": publication,
                    "topic_label": current_topic,
                    "published_fact_is_synthetic_echo_not_repository_mechanism": True,
                },
            }
            for item in trajectory["turns"]:
                # Maintain the scenario's explicit focus as soft topic context;
                # it is a test input, not a fabricated evidence/ACL grant.
                with app.db.transaction() as db:
                    conversation = db.get(Conversation, conv)
                    conversation.focus = {
                        **conversation.focus,
                        "topic_keys": [],
                        "retrieval_topic_keys": [],
                        "topic_hints": [current_topic],
                    }
                revision = app.conversations.detail(actor, conv)["revision"]
                before_calls = len(env["model"].calls)
                submitted = app.conversations.submit(
                    actor,
                    conv,
                    TurnInput(
                        question=item["utterance"],
                        expected_revision=revision,
                        funding_id=env["funding"],
                        idempotency_key=item["turn_id"],
                    ),
                )
                execution_error = None
                try:
                    await app.execute(submitted["run_id"])
                except Exception as error:
                    execution_error = {
                        "type": type(error).__name__,
                        "code": getattr(error, "code", "synthetic_fixture_or_product_error"),
                    }
                with app.db.read() as db:
                    turn = db.get(Turn, submitted["turn_id"])
                    receipts = list(
                        db.scalars(
                            select(ModelReceipt).where(ModelReceipt.run_id == submitted["run_id"])
                        )
                    )
                calls = env["model"].calls[before_calls:]
                tutor = [call for call in calls if call["role"] == "tutor"]
                current_events = app.learning.preference_events(actor)
                output["turns"].append(
                    {
                        "input_turn_id": item["turn_id"],
                        "utterance": item["utterance"],
                        "actual_turn_id": turn.id,
                        "status": turn.status,
                        "execution_error": execution_error,
                        "run_id": submitted["run_id"],
                        "frozen_projection": turn.focus["learning_preferences"],
                        "new_event_ids": turn.focus["preference_event_ids"],
                        "events": [
                            event
                            for event in current_events["events"]
                            if event["event_id"] in turn.focus["preference_event_ids"]
                        ],
                        "tutor_calls": [
                            {
                                "preferences": call["context"]["focus"]["preferences"],
                                "teaching_instructions": call["context"]["teaching_instructions"],
                                "synthetic": True,
                            }
                            for call in tutor
                        ],
                        "receipt_ids": [receipt.id for receipt in receipts],
                        "next_turn_projection": app.learning.effective_preferences(
                            actor, conversation_id=conv
                        ),
                    }
                )
            output["final_event_log"] = app.learning.preference_events(actor)
            output["limitations"] = [
                "Fixture provider output is not live semantic model validation.",
                "A feedback utterance without an attempt identifier cannot alter the assistance label of an unspecified exercise.",
                "Named repository correction needs an unambiguous authorized repository identity; no repository alias is invented.",
                "The scenario topic is restored as an explicit soft hint before each input; this runner does not test automatic topic normalization.",
            ]
            report["results"].append(output)
            try:
                next(generator)
            except StopIteration:
                pass
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {"output": str(args.output), "cases": len(report["results"]), "live_model_calls": 0}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(replay(parser.parse_args()))
