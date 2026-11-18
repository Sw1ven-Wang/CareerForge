"""Search and ingest multiple projects, without analysis or knowledge publication."""

import re

from sqlalchemy import select

from ..contracts import ImportSourceInput
from ..errors import DomainError
from ..models import ObjectRow, ResearchNode, SearchObservation
from ..util import fingerprint
from .dependencies import check_closure, depend, register, require_object
from .learning_language import require_language_in
from .project_quality import (
    COMMUNITY_CONTRACT,
    COMMUNITY_POLICY,
    QUALITY_CONTRACT,
    QUALITY_POLICY,
    community_gate,
    learning_quality,
    static_quality,
    validate_community_policy,
)
from .project_selection_policy import current_policy_in, record_policy_in
from .research_coverage import read_goal_version
from .runtime import leased


class ProjectPoolService:
    def __init__(self, app):
        self.app, self.db = app, app.db

    def start_in(
        self,
        db,
        actor,
        workflow,
        funding_id,
        key,
        *,
        target_count=3,
        max_snapshots=6,
        repository_urls=None,
        quality_policy=None,
    ):
        if not workflow.goal_version_id or not 1 <= target_count <= max_snapshots <= 8:
            raise DomainError("PROJECT_POOL_SCOPE", "项目检索需要确认目标及有界数量。", 422)
        if repository_urls is not None and (
            not 1 <= len(repository_urls) <= 8
            or len({url.casefold().rstrip("/") for url in repository_urls}) != len(repository_urls)
            or any(
                not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?", url)
                for url in repository_urls
            )
        ):
            raise DomainError("PROJECT_NOMINATION", "请提供去重的公开 GitHub 仓库地址。", 422)
        profile = require_language_in(db, actor, workflow)
        goal = read_goal_version(db, actor, workflow.goal_version_id)
        if goal["namespace_id"] != workflow.namespace_id or workflow.principal_id != actor:
            raise DomainError("PROJECT_POOL_SCOPE", "项目库必须绑定本人的私人目标范围。", 403)
        category = "agent" if profile and profile["ai_agent_track"] else "backend"
        language = (
            (profile["required_core_languages"] or profile["backend_languages"])[0]
            if profile
            else "python"
        )
        policy = validate_community_policy(
            quality_policy
            or current_policy_in(db, actor, workflow.goal_version_id)
            or COMMUNITY_POLICY
        )
        record_policy_in(
            db,
            actor,
            workflow.goal_version_id,
            policy,
            f"项目筛选规则 Star > {policy['stars_exclusive_min']} 且 Fork > {policy['forks_exclusive_min']}；不以运行或 CI 作为入选门槛。",
        )
        manifest = {
            "contract": "project-pool-v4-community",
            "goal_snapshot": goal,
            "goal_version_id": workflow.goal_version_id,
            "language_profile": profile,
            "quality_policy": policy,
            "workflow_id": workflow.id,
            "queries": [
                {"query": category + " framework", "purpose": "architecture"},
                {
                    "query": category + (" memory" if category == "agent" else " service"),
                    "purpose": "architecture",
                },
                {
                    "query": category + " orchestration language:" + language,
                    "purpose": "implementation",
                },
            ],
            "target_count": target_count,
            "max_snapshots": max_snapshots,
            "github_request_cap": 100,
            "no_analysis_or_publication": True,
        }
        if repository_urls is not None:
            manifest.update(
                contract="project-pool-v4-community",
                nominated_repository_urls=repository_urls,
                candidate_origin="user_nomination",
                queries=[],
            )
        for query in manifest["queries"]:
            query["query"] += (
                f" stars:>{policy['stars_exclusive_min']} forks:>{policy['forks_exclusive_min']}"
            )
        return self.app.runtime.create_in(
            db, actor, workflow.namespace_id, funding_id, "project_discovery", manifest, 0, key, 30
        ).id

    async def execute(self, run, epoch, worker):
        from .research_coverage import validate_goal_snapshot

        m = run.input_manifest
        community = m.get("contract") == "project-pool-v4-community"
        if community:
            try:
                validate_community_policy(m.get("quality_policy"))
            except (ValueError, TypeError) as exc:
                raise DomainError("PROJECT_POOL_CONTRACT", "项目库合同不一致。", 409) from exc
        if m.get("contract") not in {
            "project-pool-v1",
            "project-pool-v2-quality-sample",
            "project-pool-v3-nominated",
            "project-pool-v4-community",
        } or (not community and m.get("quality_policy") != QUALITY_POLICY):
            raise DomainError("PROJECT_POOL_CONTRACT", "项目库合同不一致。", 409)
        with self.db.read() as db:
            validate_goal_snapshot(db, run.principal_id, m["goal_snapshot"])
        search_results = []
        for index, query in enumerate(m["queries"]):
            stage = "project.search:" + str(index)
            saved = self.app.runtime.stage(run.id, stage)
            if saved is None:
                observation = await self.app.repositories.search(
                    query["query"],
                    run.id,
                    m["github_request_cap"],
                    channel="repository",
                    access_dependencies=[m["goal_version_id"]],
                )
                with self.db.transaction() as db:
                    active = leased(db, run.id, epoch, worker)
                    node = ResearchNode(
                        run_id=run.id,
                        depth=0,
                        question=query["query"],
                        node_key=fingerprint(query),
                        status="completed",
                        payload=query,
                    )
                    db.add(node)
                    db.flush()
                    row = SearchObservation(
                        node_id=node.id, query_hash=fingerprint(query), payload=observation
                    )
                    db.add(row)
                    db.flush()
                    saved = {"observation_id": row.id, "payload": observation}
                    self.app.runtime.save_stage_in(db, active, stage, saved)
            search_results.append(saved)
        candidates = {
            url: {
                "url": url,
                "name": url.removeprefix("https://github.com/").rstrip("/"),
                "observation_ids": [],
                "candidate_origin": "user_nomination",
            }
            for url in m.get("nominated_repository_urls", [])
        }
        for observation in search_results:
            for item in observation["payload"].get("candidates", []):
                if item.get("url"):
                    entry = candidates.setdefault(item["url"], {**item, "observation_ids": []})
                    entry["observation_ids"].append(observation["observation_id"])
        # Do not rank by stars or require every JD item / a single language.
        ordered = sorted(
            candidates.values(),
            key=lambda c: (
                c.get("archived") is True,
                c.get("license") in (None, "NOASSERTION", "unknown"),
                -len(c["observation_ids"]),
                c.get("name", ""),
            ),
        )
        with self.db.read() as db:
            previous = self.list_in(
                db, run.principal_id, m["goal_version_id"], include_pending=False
            )
        entries = [
            {**p, "reused": True}
            for p in previous
            if not m.get("nominated_repository_urls") or p["url"] in m["nominated_repository_urls"]
        ]
        gaps, priority = [], len(entries)
        for index, candidate in enumerate(ordered[: m["max_snapshots"]]):
            if priority >= m["target_count"]:
                break
            stage = "project.ingest:" + str(index)
            old = self.app.runtime.stage(run.id, stage)
            if any(p["url"].casefold() == candidate["url"].casefold() for p in entries):
                continue
            if old is None:
                try:
                    metadata = None
                    metrics = candidate
                    if community and hasattr(self.app.repositories, "project_metadata"):
                        metadata = await self.app.repositories.project_metadata(
                            candidate["url"], run.id, m["github_request_cap"]
                        )
                        metrics = {
                            "stars": metadata.get("stargazers_count"),
                            "forks": metadata.get("forks_count"),
                            "observed_at": metadata.get("observed_at"),
                        }
                    if community:
                        gate = community_gate(metrics, m["quality_policy"])
                        if gate["status"] != "passed":
                            rejected = {
                                "rejected": True,
                                "repository": candidate.get("name"),
                                "reason": gate["status"],
                                "community_gate": gate,
                            }
                            self.app.runtime.save_stage(run.id, epoch, worker, stage, rejected)
                            gaps.append(rejected)
                            continue
                    collect = (
                        self.app.repositories.quality_snapshot
                        if m["contract"]
                        in {
                            "project-pool-v2-quality-sample",
                            "project-pool-v3-nominated",
                            "project-pool-v4-community",
                        }
                        and hasattr(self.app.repositories, "quality_snapshot")
                        else self.app.repositories.snapshot
                    )
                    kwargs = (
                        {"metadata": metadata, "sampling_mode": "learning_content"}
                        if community
                        and metadata is not None
                        and hasattr(self.app.repositories, "quality_snapshot")
                        else {}
                    )
                    snapshot = await collect(
                        candidate["url"], run.id, m["github_request_cap"], **kwargs
                    )
                    source_id = self.app.sources.import_source(
                        run.principal_id,
                        ImportSourceInput(namespace_id=run.namespace_id, **snapshot),
                        (run.id, epoch, worker),
                    )
                    with self.db.transaction() as db:
                        active = leased(db, run.id, epoch, worker)
                        source = self.app.sources.get(db, run.principal_id, source_id)
                        quality = (
                            learning_quality(source.files, metrics, m["quality_policy"])
                            if community
                            else static_quality(source.files)
                        )
                        quality.update(
                            source_id=source_id,
                            commit=source.commit_sha,
                            source_scope_hash=source.scope_hash,
                            snapshot_skipped=source.metadata_json.get("skipped", {}),
                            partial_snapshot=source.metadata_json.get("partial_snapshot", False),
                            maintenance_observation={
                                "pushed_at": source.metadata_json.get("pushed_at"),
                                "archived": source.metadata_json.get("archived"),
                            },
                        )
                        report = register(
                            db,
                            run.principal_id,
                            run.namespace_id,
                            "project_quality_report",
                            quality,
                        )
                        depend(
                            db, run.principal_id, report.id, source_id, "assesses_project_source"
                        )
                        value = {
                            "contract": "project-pool-entry-v1",
                            "repository": source.repository_key,
                            "url": candidate["url"],
                            "source_id": source_id,
                            "commit": source.commit_sha,
                            "license": source.license_id,
                            "quality_report_id": report.id,
                            "quality_status": quality["status"],
                            "selection_policy_hash": fingerprint(m["quality_policy"]),
                            "community_gate": quality.get("community_gate"),
                            "goal_version_id": m["goal_version_id"],
                            "observation_ids": candidate["observation_ids"],
                            "candidate_origin": candidate.get("candidate_origin", "public_search"),
                            "run_id": run.id,
                            "language": candidate.get("language")
                            or source.metadata_json.get("repository_language"),
                            "stack_match_required": False,
                            "requirements_satisfied": False,
                            "knowledge_publication_created": False,
                            "partial_snapshot": source.metadata_json.get("partial_snapshot", False),
                        }
                        if quality["status"] == "insufficient_implementation":
                            self.app.runtime.save_stage_in(
                                db,
                                active,
                                stage,
                                {
                                    "rejected": True,
                                    "repository": source.repository_key,
                                    "quality_report_id": report.id,
                                    "reason": "insufficient_implementation",
                                },
                            )
                            gaps.append(
                                {
                                    "repository": source.repository_key,
                                    "error": "INSUFFICIENT_IMPLEMENTATION",
                                }
                            )
                            continue
                        obj = register(
                            db, run.principal_id, run.namespace_id, "project_pool_entry", value
                        )
                        depend(db, run.principal_id, obj.id, source_id, "stores_project_source")
                        depend(db, run.principal_id, obj.id, report.id, "uses_project_quality")
                        depend(
                            db, run.principal_id, obj.id, m["goal_version_id"], "binds_goal_version"
                        )
                        old = {"entry_id": obj.id, **value}
                        self.app.runtime.save_stage_in(db, active, stage, old)
                except DomainError as exc:
                    gaps.append({"repository": candidate.get("name"), "error": exc.code})
                    continue
            if old.get("rejected"):
                gaps.append(
                    {
                        "repository": old["repository"],
                        "error": old.get("reason", "INSUFFICIENT_IMPLEMENTATION"),
                        "community_gate": old.get("community_gate"),
                    }
                )
                continue
            entries.append(old)
            priority += old["quality_status"] == "priority_for_source_review"
            if priority >= m["target_count"]:
                break
        self.app.runtime.save_stage(
            run.id,
            epoch,
            worker,
            "result",
            {
                "project_entries": entries,
                "candidate_count": len(candidates),
                "gaps": gaps,
                "priority_count": priority,
                "requested_count": m["target_count"],
                "target_count_reached": priority >= m["target_count"],
                "quality_contract": COMMUNITY_CONTRACT if community else QUALITY_CONTRACT,
                "no_analysis_or_publication": True,
                "calls_consumed": 0,
                "primary_entry_ids": [
                    e["entry_id"]
                    for e in entries
                    if e["quality_status"] == "priority_for_source_review"
                ],
            },
        )
        self.app.runtime.finish(
            run.id,
            epoch,
            worker,
            "succeeded" if priority >= m["target_count"] else "partially_succeeded",
        )

    def reassess_in(self, db, actor, entry_id, *, metadata=None, policy=None):
        old = require_object(db, actor, entry_id, "project_pool_entry")
        check_closure(db, actor, [entry_id])
        read_goal_version(db, actor, old.payload["goal_version_id"])
        source = self.app.sources.get(db, actor, old.payload["source_id"])
        policy = policy or current_policy_in(db, actor, old.payload["goal_version_id"])
        if metadata is None:
            metadata = old.payload.get("community_gate") or source.metadata_json.get(
                "community_metadata", {}
            )
        quality = (
            learning_quality(source.files, metadata or {}, policy)
            if policy
            else static_quality(source.files)
        )
        quality.update(
            source_id=source.id,
            commit=source.commit_sha,
            source_scope_hash=source.scope_hash,
            partial_snapshot=source.metadata_json.get("partial_snapshot", False),
            supersedes_quality_report_id=old.payload["quality_report_id"],
        )
        report = register(db, actor, old.namespace_id, "project_quality_report", quality)
        depend(db, actor, report.id, source.id, "assesses_project_source")
        depend(
            db,
            actor,
            report.id,
            old.payload["quality_report_id"],
            "reassesses_quality_report",
            "provenance_only",
        )
        value = {
            **old.payload,
            "quality_report_id": report.id,
            "quality_status": quality["status"],
            "supersedes_entry_id": entry_id,
        }
        if policy:
            value.update(
                selection_policy_hash=fingerprint(policy), community_gate=quality["community_gate"]
            )
        entry = register(db, actor, old.namespace_id, "project_pool_entry", value)
        depend(db, actor, entry.id, source.id, "stores_project_source")
        depend(db, actor, entry.id, report.id, "uses_project_quality")
        depend(db, actor, entry.id, old.payload["goal_version_id"], "binds_goal_version")
        depend(db, actor, entry.id, entry_id, "reassesses_project_entry", "provenance_only")
        return {"entry_id": entry.id, **value}

    def list_in(self, db, actor, goal_id, *, include_pending=True):
        goal = read_goal_version(db, actor, goal_id)
        policy = current_policy_in(db, actor, goal_id)
        result = []
        seen = set()
        for obj in db.scalars(
            select(ObjectRow)
            .where(
                ObjectRow.kind == "project_pool_entry",
                ObjectRow.namespace_id == goal["namespace_id"],
            )
            .order_by(ObjectRow.created_at.desc(), ObjectRow.id.desc())
        ):
            if obj.payload.get("goal_version_id") != goal["goal_version_id"]:
                continue
            if policy and obj.payload.get("selection_policy_hash") != fingerprint(policy):
                continue
            try:
                check_closure(db, actor, [obj.id])
            except DomainError:
                continue
            if policy:
                repository = obj.payload["repository"].casefold()
                if repository in seen:
                    continue
                seen.add(repository)
            if obj.payload["quality_status"] in {
                "insufficient_implementation",
                "below_community_threshold",
                "community_unverified",
            }:
                continue
            try:
                check_closure(db, actor, [obj.id])
            except DomainError:
                continue
            if include_pending or obj.payload["quality_status"] == "priority_for_source_review":
                result.append({"entry_id": obj.id, **obj.payload})
        chosen = {}
        for row in result:
            key = row["repository"].casefold()
            rank = (
                row["quality_status"] == "priority_for_source_review",
                not row.get("partial_snapshot", False),
            )
            if key not in chosen or rank > chosen[key][0]:
                chosen[key] = (rank, row)
        return sorted(
            [value[1] for value in chosen.values()],
            key=lambda row: (
                row["quality_status"] != "priority_for_source_review",
                row["repository"].casefold(),
            ),
        )
