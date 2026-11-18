"""Original privacy generations for explicitly versioned M6 paid Runs.

Only trusted frozen object references are roots. No prompt/body scanning, current
value backfill, legacy enrollment, or permission caching is permitted here.
"""
from __future__ import annotations

from sqlalchemy import select

from ..errors import DomainError
from ..models import Namespace, ObjectRow
from ..models_dialogue import WorkflowEvolutionRequest
from ..util import fingerprint
from .dependencies import _reachable, check_closure
from .privacy import assert_personal_access


def _fail():
    raise DomainError("EVOLUTION_GRAPH_BINDING", "原始隐私代次见证缺失或与冻结运行不一致。", 409)


def dependency_owners_in(db, actor, roots):
    # Preserve each original operation's 1024-root limit. Never concatenate all
    # cases/packs into a new unbounded authorization query.
    roots = sorted(set(roots))
    check_closure(db, actor, roots)
    if not roots:
        return set()
    closure = _reachable(roots, blocking_only=True)
    return set(db.scalars(
        select(Namespace.owner_id)
        .join(ObjectRow, ObjectRow.namespace_id == Namespace.id)
        .join(closure, ObjectRow.id == closure.c.node_id)
        .where(Namespace.kind != "shared_public")
        .distinct()
    ))


def _base_owners(db, actor, namespace_id):
    namespace = db.get(Namespace, namespace_id, populate_existing=True)
    if not namespace:
        _fail()
    return {actor} | ({namespace.owner_id} if namespace.kind != "shared_public" else set())


def freeze_generations_in(app, db, actor, namespace_id, manifest):
    """Called once before recording the original admission_manifest_hash."""
    from .knowledge import KnowledgeService
    from .taxonomy import require_topic, resolve_topics, topic_object_id

    owners = _base_owners(db, actor, namespace_id)

    def include(roots):
        owners.update(dependency_owners_in(db, actor, roots))

    if "reflection_input" in manifest:
        include(manifest["reflection_input"]["access_dependencies"])
    else:
        include(manifest["policy_dependencies"])
        for case in manifest["cases"]:
            for pack in case.get("frozen_units", []):
                include(KnowledgeService.pack_dependencies(pack))
            # These are frozen, server-validated catalog/focus references, not
            # inferred IDs from questions, output prose, or evaluation labels.
            include([entry["source_id"] for entry in case.get("typed_snapshot", {}).get("catalog", [])])
            include(case.get("focus", {}).get("project_source_ids", []))
            keys = set(case.get("topic_keys", [])) | set(case.get("focus", {}).get("topic_keys", []))
            if manifest["target"] == "routing_policy":
                keys.update(resolve_topics(app.db, case["question"], actor=actor))
            for key in sorted(keys):
                topic = require_topic(db, actor, key)
                include([topic_object_id(key, topic.version)])
        # The new replay selection is created after Runtime.create_in in the
        # same transaction. Its namespace owner is already in _base_owners;
        # actual selection/arm/model dependencies are checked again when used.
    return {owner: assert_personal_access(db, owner) for owner in sorted(owners)}


def validate_manifest_generations_in(db, actor, namespace_id, manifest, *, dependencies=None):
    marker = manifest.get("evolution_graph")
    generations = marker.get("privacy_generations") if isinstance(marker, dict) else None
    if (
        not isinstance(generations, dict)
        or not generations
        or any(not isinstance(owner, str) or not owner or type(value) is not int or value < 0
               for owner, value in generations.items())
        or not _base_owners(db, actor, namespace_id) <= set(generations)
    ):
        _fail()
    request = db.get(WorkflowEvolutionRequest, marker.get("request_id"))
    if (
        not request
        or request.principal_id != actor
        or request.namespace_id != namespace_id
        or request.admission_manifest_hash != fingerprint(manifest)
    ):
        _fail()
    for owner, generation in sorted(generations.items()):
        assert_personal_access(db, owner, expected_generation=generation)
    if dependencies is not None:
        if not dependency_owners_in(db, actor, dependencies) <= set(generations):
            _fail()
    return generations


def validate_run_generations_in(db, run, *, dependencies=None):
    marked = run.input_manifest.get("evolution_graph")
    if not marked and run.graph_version not in {"evolution-candidate-v1", "evolution-replay-v1"}:
        return
    if run.input_hash != fingerprint(run.input_manifest):
        _fail()
    return validate_manifest_generations_in(
        db, run.principal_id, run.namespace_id, run.input_manifest, dependencies=dependencies
    )
