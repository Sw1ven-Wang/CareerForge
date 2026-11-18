"""Disputes require fresh independent reviews; diff previews never inherit semantics."""

from __future__ import annotations

from sqlalchemy import select

from ..db import aware, locked
from ..errors import DomainError
from ..models import (
    AgentRun,
    ArtifactDependency,
    Dispute,
    DisputeResolution,
    Namespace,
    ObjectRow,
    Outbox,
    Publication,
    ReviewDecision,
    UnitVersion,
)
from .dependencies import _reaches, check_closure, invalidate, require_object
from .security import namespace_admin
from .sources import SourceService


class LifecycleService:
    def __init__(self, app):
        self.app, self.db = app, app.db

    def recheck(self, actor, dispute_id, publication_id, funding_id, key):
        with self.db.transaction() as db:
            d = locked(db, Dispute, dispute_id)
            if not d:
                raise DomainError("NOT_FOUND", "质疑不存在。", 404)
            pub = self._related(db, actor, d, publication_id)
            namespace_admin(db, actor, pub.namespace_id)
            if d.status in {"resolved", "dismissed", "withdrawn"}:
                raise DomainError("DISPUTE_CLOSED", "质疑已结束。")
            unit = self.app.knowledge._validated_version(db, actor, pub.unit_version_id)
            from ..models import TopicLink

            versions = {}
            taxonomy_classes = set()
            for link in db.scalars(select(TopicLink).where(TopicLink.unit_version_id == unit.id)):
                frozen = self.app.knowledge._topic_link_view(db, actor, link)
                versions[frozen["topic_key"]] = frozen["topic_version"]
                topic_obj = require_object(db, actor, frozen["topic_version_id"], "topic_version")
                ns = db.get(Namespace, topic_obj.namespace_id)
                if ns is None:
                    raise DomainError("TAXONOMY_SCOPE", "历史主题范围不可验证。")
                taxonomy_classes.add(
                    "public_source" if ns.kind == "shared_public" else "private_source"
                )
            # Re-evaluate the ORIGINAL claims, not a newly invented easier question.
            m = {
                "unit_version_ids": [unit.id],
                "question": "独立复核冻结知识中的原有命题，主动寻找反证。",
                "model_profile": self.app.provider.profile_version,
                "dispute_id": d.id,
                "taxonomy_versions": versions,
                "taxonomy_data_classes": sorted(taxonomy_classes),
            }
            run = self.app.runtime.create_in(
                db, actor, pub.namespace_id, funding_id, "review", m, 3, key
            )
            d.status = "investigating"
            return {"run_id": run.id, "publication_unchanged": True}

    def _related(self, db, actor, d, publication_id):
        require_object(db, actor, publication_id, "publication", active=False)
        require_object(db, actor, d.target_id, active=False)
        if (
            d.target_id != publication_id
            and not _reaches(db, publication_id, d.target_id)
            and not _reaches(db, d.target_id, publication_id)
        ):
            raise DomainError("DISPUTE_TARGET", "发布版本与原质疑没有证据依赖关系。", 422)
        return db.get(Publication, publication_id)

    def resolve(
        self, actor, dispute_id, publication_id, outcome, review_id, reason, replacement_id=None
    ):
        if (
            outcome not in {"uphold", "revoke", "correct"}
            or not reason.strip()
            or len(reason) > 3000
        ):
            raise DomainError("RESOLUTION", "复核结论或说明无效。", 422)
        with self.db.transaction() as db:
            d = locked(db, Dispute, dispute_id)
            if not d:
                raise DomainError("NOT_FOUND", "质疑不存在。", 404)
            pub = self._related(db, actor, d, publication_id)
            namespace_admin(db, actor, pub.namespace_id)
            if db.scalar(select(DisputeResolution.id).where(DisputeResolution.dispute_id == d.id)):
                raise DomainError("DISPUTE_IMMUTABLE", "已保存的复核结论不可覆盖。")
            if d.status in {"resolved", "dismissed", "withdrawn"}:
                raise DomainError("DISPUTE_CLOSED", "质疑已结束。")
            review = db.get(ReviewDecision, review_id)
            if not review or aware(db.get(ObjectRow, review.id).created_at) <= aware(d.created_at):
                raise DomainError("FRESH_REVIEW_REQUIRED", "必须提交质疑之后的新独立审核。", 422)
            require_object(db, actor, review.id, "review_decision")
            target = self.app.knowledge._validated_version(db, actor, pub.unit_version_id)
            review_version = self.app.knowledge._validated_version(
                db, actor, review.unit_version_id
            )
            self.app.knowledge.validated_review(db, actor, review.id, review_version)
            reviewer_run = db.get(AgentRun, review.reviewer_run_id)
            if reviewer_run.workflow != "review" or reviewer_run.status != "succeeded":
                raise DomainError("FRESH_REVIEW_REQUIRED", "独立复核任务必须成功完成。", 422)
            check_closure(db, actor, [review.id])
            if outcome in {"uphold", "revoke"}:
                run = db.get(AgentRun, review.reviewer_run_id)
                if (
                    review.unit_version_id != target.id
                    or run.input_manifest.get("dispute_id") != d.id
                ):
                    raise DomainError("REVIEW_TARGET", "复核必须针对本次质疑的原始知识版本。", 422)
                required = "accept_scoped" if outcome == "uphold" else "reject"
                if review.decision != required:
                    raise DomainError("REVIEW_OUTCOME", "证据不足不能强制判定正确或错误。", 422)
            else:
                replacement = db.get(Publication, replacement_id) if replacement_id else None
                if (
                    not replacement
                    or replacement.id == pub.id
                    or replacement.namespace_id != pub.namespace_id
                    or replacement.review_id != review.id
                ):
                    raise DomainError("REPLACEMENT", "修正需要同域、独立审核的新发布版本。", 422)
                rv = db.get(UnitVersion, replacement.unit_version_id)
                if (
                    rv.unit_id != target.unit_id
                    or rv.source_id != target.source_id
                    or review.decision != "accept_scoped"
                ):
                    raise DomainError(
                        "REPLACEMENT_SCOPE",
                        "新Commit的结论不能作为旧Commit事实纠错；应修订同一来源和知识身份。",
                        422,
                    )
                self.app.knowledge.publication_pack(db, actor, replacement.id)
            if outcome in {"revoke", "correct"}:
                invalidate(db, actor, pub.id, "revoked", "dispute:" + d.id)
                if outcome == "correct":
                    from ..models import SelectionHead

                    heads = db.scalars(
                        select(SelectionHead).where(
                            SelectionHead.selection_namespace_id == pub.namespace_id,
                            SelectionHead.publication_id == pub.id,
                        )
                    ).all()
                    for head in heads:
                        self.app.knowledge._set_head(
                            db, actor, pub.namespace_id, replacement_id, head.revision
                        )
                    # Private overrides are not silently rewritten; invalidation blocks their old use.
            else:
                obj = require_object(db, actor, pub.id, "publication", active=False)
                if obj.state == "revoked":
                    raise DomainError("REVOKED_IMMUTABLE", "已撤销对象不能恢复原记录。")
                inputs = db.scalars(
                    select(ArtifactDependency.source_id).where(
                        ArtifactDependency.dependent_id == pub.id,
                        ArtifactDependency.enforcement == "blocking",
                    )
                ).all()
                check_closure(db, actor, list(inputs))
                if obj.state == "suspended":
                    obj.state = "active"
                    obj.revision += 1
                    locked(db, Namespace, pub.namespace_id).revision += 1
            resolution = DisputeResolution(
                dispute_id=d.id,
                actor_id=actor,
                outcome=outcome,
                fresh_review_id=review.id,
                replacement_publication_id=replacement_id,
                reason=reason,
            )
            db.add(resolution)
            db.flush()
            d.status = "resolved"
            db.add(
                Outbox(
                    kind="dispute.audit",
                    aggregate_id=d.id,
                    payload={"outcome": outcome, "resolution_id": resolution.id},
                )
            )
            return {
                "resolution_id": resolution.id,
                "outcome": outcome,
                "historical_answers_rewritten": False,
            }

    def preview_update(self, actor, old_source_id, new_source_id):
        with self.db.read() as db:
            old = SourceService.get(db, actor, old_source_id)
            new = SourceService.get(db, actor, new_source_id)
            if old.repository_key != new.repository_key or old.origin_kind != new.origin_kind:
                raise DomainError(
                    "UPDATE_IDENTITY", "更新预览必须属于同一项目或概念来源身份。", 422
                )
            changed = sorted(
                p for p in set(old.files) | set(new.files) if old.files.get(p) != new.files.get(p)
            )
            results = []
            for unit in db.scalars(select(UnitVersion).where(UnitVersion.source_id == old.id)):
                try:
                    require_object(db, actor, unit.id, "knowledge_unit_version")
                except DomainError:
                    # A readable shared source does not disclose private consumers.
                    continue
                deps = unit.dependency_manifest
                paths = (
                    set(deps.get("paths", deps.get("dependency_paths", [])))
                    if isinstance(deps, dict)
                    else set()
                )
                impacted = sorted(paths & set(changed))
                results.append(
                    {
                        "unit_version_id": unit.id,
                        "changed_declared_dependencies": impacted,
                        "route": "reuse_exact_source"
                        if old.id == new.id
                        else "independent_review_required",
                        "automatic_semantic_carry_forward": False,
                        "reason": "same_source_identity"
                        if old.id == new.id
                        else "dependency_closure_not_proven",
                    }
                )
            return {
                "old_source_id": old.id,
                "new_source_id": new.id,
                "changed_files": changed,
                "units": results,
                "source_read_execution": False,
                "old_publications_revoked": False,
            }

    def schedule_gap(self, actor, gap_id, funding_id, call_cap, key, **limits):
        from .gap_scheduling import schedule_gap, schedule_legacy_gap

        if not limits:
            legacy = schedule_legacy_gap(self, actor, gap_id, funding_id, call_cap)
            if legacy is not None:
                return legacy
        return schedule_gap(self, actor, gap_id, funding_id, call_cap, key, **limits)

    def reconcile_gap(self, actor, gap_id, answer_turn_id=None):
        from .gap_scheduling import reconcile_gap

        return reconcile_gap(self, actor, gap_id, answer_turn_id)

    def reanswer_gap(self, actor, gap_id, funding_id):
        from .gap_scheduling import reanswer_gap

        return reanswer_gap(self, actor, gap_id, funding_id)

    def create_update_plan(self, actor, body):
        """Freeze an exact-source diff and conservative per-unit maintenance routes."""
        from ..models_governance import SourceUpdatePlan
        from ..util import fingerprint
        from .security import namespace_access

        with self.db.transaction() as db:
            namespace_access(db, actor, body.namespace_id, True)
            manifest = self._update_manifest(
                db, actor, body.old_source_id, body.new_source_id, body.namespace_id
            )
            row = SourceUpdatePlan(
                namespace_id=body.namespace_id,
                principal_id=actor,
                old_source_id=body.old_source_id,
                new_source_id=body.new_source_id,
                manifest=manifest,
                content_hash=fingerprint(manifest),
            )
            db.add(row)
            db.flush()
            return {
                "plan_id": row.id,
                "revision": row.revision,
                "state": row.state,
                "manifest": manifest,
                "model_calls": 0,
                "old_publications_revoked": False,
            }

    def _update_manifest(self, db, actor, old_source_id, new_source_id, namespace_id=None):
        import ast
        import difflib
        import platform

        from ..models import Claim, CodeSymbol
        from ..util import digest, fingerprint
        from .answers import HIGH_RISK

        old = SourceService.get(db, actor, old_source_id)
        if namespace_id and db.get(Namespace, namespace_id).kind == "shared_public":
            for sid in [old_source_id, new_source_id]:
                obj = require_object(db, actor, sid)
                if db.get(Namespace, obj.namespace_id).kind != "shared_public":
                    raise DomainError("PRIVATE_TO_PUBLIC", "私人来源不能进入公共维护计划。", 403)
        new = SourceService.get(db, actor, new_source_id)
        if (old.repository_key, old.origin_kind) != (new.repository_key, new.origin_kind):
            raise DomainError("UPDATE_IDENTITY", "更新必须属于同一来源身份。", 422)
        changes = []
        for path in sorted(set(old.files) | set(new.files)):
            before, after = old.files.get(path), new.files.get(path)
            if before == after:
                continue
            diff = list(
                difflib.unified_diff(
                    (before or "").splitlines(),
                    (after or "").splitlines(),
                    fromfile="old/" + path,
                    tofile="new/" + path,
                    lineterm="",
                )
            )
            changes.append(
                {
                    "path": path,
                    "kind": "added"
                    if before is None
                    else "deleted"
                    if after is None
                    else "modified",
                    "before_hash": digest(before) if before is not None else None,
                    "after_hash": digest(after) if after is not None else None,
                    "unified_diff": diff[:200],
                    "diff_truncated": len(diff) > 200,
                }
            )
        changed = {c["path"] for c in changes}
        results = []
        for version in db.scalars(
            select(UnitVersion).where(UnitVersion.source_id == old.id).order_by(UnitVersion.id)
        ):
            try:
                self.app.knowledge._validated_version(db, actor, version.id)
                version_obj = require_object(db, actor, version.id)
                if (
                    namespace_id
                    and db.get(Namespace, namespace_id).kind == "shared_public"
                    and db.get(Namespace, version_obj.namespace_id).kind != "shared_public"
                ):
                    continue
                publication = db.scalar(
                    select(Publication).where(Publication.unit_version_id == version.id)
                )
                if not publication:
                    continue
                pack = self.app.knowledge.publication_pack(db, actor, publication.id)
            except DomainError:
                continue
            claims = list(db.scalars(select(Claim).where(Claim.unit_version_id == version.id)))
            partial = pack.get("publication_scope", {}).get("status") == "partial"
            if partial:
                retained = {fact["claim_id"] for fact in pack["facts"]}
                claims = [claim for claim in claims if claim.id in retained]
            paths = set(version.dependency_manifest.get("paths", {}))
            structural = []
            for claim in claims:
                # This whitelist describes syntax only, not Python execution/behavior/purity.
                for symbol in db.scalars(select(CodeSymbol).where(CodeSymbol.source_id == old.id)):
                    template = f"结构声明：{symbol.path}::{symbol.name} [{symbol.kind}]"
                    if (
                        claim.statement != template
                        or claim.conditions
                        or symbol.parser != "python-ast"
                    ):
                        continue
                    if (
                        symbol.path not in new.files
                        or old.files.get(symbol.path) != new.files[symbol.path]
                    ):
                        continue
                    try:
                        tree = ast.parse(new.files[symbol.path])
                    except (SyntaxError, ValueError, RecursionError):
                        continue
                    matches = [
                        n
                        for n in ast.walk(tree)
                        if type(n).__name__ == symbol.kind
                        and getattr(n, "name", None) == symbol.name
                        and getattr(n, "lineno", None) == symbol.start_line
                    ]
                    if len(matches) != 1:
                        continue
                    structural.append(
                        {
                            "old_claim_id": claim.id,
                            "old_review_id": publication.review_id,
                            "old_unit_version_id": version.id,
                            "rule_version": "python-declaration-exists-1.6.0",
                            "path": symbol.path,
                            "symbol": symbol.name,
                            "kind": symbol.kind,
                            "statement": template,
                            "new_source_id": new.id,
                            "new_commit": new.commit_sha,
                            "file_hash": digest(new.files[symbol.path]),
                            "parser_version": "python-ast:" + platform.python_version(),
                            "closure_grade": "complete_for_structural_claim",
                            "reviewer_reread": False,
                            "runtime_observed": False,
                            "qualifies_as_publication": False,
                        }
                    )
            config_changed = any(
                p.endswith(("lock", "lock.json", ".toml", ".yaml", ".yml", ".ini", ".cfg"))
                or p.rsplit("/", 1)[-1].upper().startswith(("LICENSE", "COPYING", "NOTICE"))
                for p in changed
            )
            high_risk = any(
                HIGH_RISK.search(c.statement) or c.kind == "runtime_observation" for c in claims
            )
            local_static = (
                version.dependency_manifest.get("closure") == "partial" and 0 < len(paths) <= 3
            )
            if local_static:
                for path in paths:
                    if not path.endswith(".py") or path not in old.files or path not in new.files:
                        local_static = False
                        break
                    try:
                        trees = [ast.parse(old.files[path]), ast.parse(new.files[path])]
                    except (SyntaxError, ValueError, RecursionError):
                        local_static = False
                        break
                    risky_nodes = (
                        ast.Call,
                        ast.Attribute,
                        ast.Import,
                        ast.ImportFrom,
                        ast.Global,
                        ast.Nonlocal,
                        ast.Await,
                        ast.Yield,
                        ast.YieldFrom,
                    )
                    signatures = []
                    for tree in trees:
                        if any(
                            isinstance(node, risky_nodes)
                            or isinstance(
                                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                            )
                            and bool(node.decorator_list)
                            for node in ast.walk(tree)
                        ):
                            local_static = False
                        signatures.append(
                            sorted(
                                (node.name, ast.dump(node.args, include_attributes=False))
                                for node in ast.walk(tree)
                                if isinstance(node, ast.FunctionDef)
                            )
                        )
                    if not signatures[0] or signatures[0] != signatures[1]:
                        local_static = False
            route = (
                "reuse_exact_source"
                if old.id == new.id
                else "A_structural_carry_forward"
                if claims and len(structural) == len(claims) and not config_changed
                else "C_reinvestigate"
                if high_risk or not local_static or config_changed
                else "B_targeted_review"
            )
            if partial:
                route = "reuse_exact_source" if old.id == new.id else "C_reinvestigate"
            results.append(
                {
                    **({"publication_scope": pack["publication_scope"]} if partial else {}),
                    "unit_version_id": version.id,
                    "publication_id": publication.id,
                    "claim_ids": [c.id for c in claims],
                    "old_review_id": publication.review_id,
                    "subject_key": require_object(db, actor, version.id).payload["draft"][
                        "subject_key"
                    ],
                    "unit_type": require_object(db, actor, version.id).payload["draft"][
                        "unit_type"
                    ],
                    "claims_to_recheck": [
                        {
                            "claim_id": c.id,
                            "statement": c.statement,
                            "conditions": c.conditions,
                            "kind": c.kind,
                        }
                        for c in claims
                    ],
                    "old_evidence_ids": list(
                        dict.fromkeys(
                            e
                            for c in claims
                            for e in require_object(db, actor, c.id).payload.get("support_refs", [])
                        )
                    ),
                    "route": route,
                    "changed_declared_dependencies": sorted(paths & changed),
                    "structural_bindings": structural
                    if route == "A_structural_carry_forward"
                    else [],
                    "requires_new_publication_review": old.id != new.id,
                    "automatic_semantic_carry_forward": False,
                    "dependency_closure": version.dependency_manifest.get("closure", "unknown"),
                    "reason": "exact_structural_whitelist"
                    if route == "A_structural_carry_forward"
                    else "bounded_static_partial_scope_with_fresh_review"
                    if route == "B_targeted_review"
                    else "unchanged_fixed_source"
                    if route == "reuse_exact_source"
                    else "unknown_closure_or_high_risk_requires_fresh_investigation",
                }
            )
        return {
            "old_source_id": old.id,
            "new_source_id": new.id,
            "old_scope_hash": old.scope_hash,
            "new_scope_hash": new.scope_hash,
            "repository": old.repository_key,
            "old_commit": old.commit_sha,
            "new_commit": new.commit_sha,
            "changes": changes,
            "units": results,
            "diff_hash": fingerprint(changes),
            "old_publications_revoked": False,
            "new_head_adopted": False,
            "source_read_execution": False,
        }

    def update_plan(self, actor, plan_id):
        from ..models_governance import SourceUpdatePlan
        from ..util import fingerprint
        from .security import namespace_access

        with self.db.read() as db:
            plan = db.get(SourceUpdatePlan, plan_id)
            if plan is None:
                raise DomainError("NOT_FOUND", "维护计划不存在。", 404)
            namespace_access(db, actor, plan.namespace_id)
            SourceService.get(db, actor, plan.old_source_id)
            SourceService.get(db, actor, plan.new_source_id)
            if plan.content_hash != fingerprint(plan.manifest):
                raise DomainError("UPDATE_HASH", "维护计划完整性失败。")
            return {
                "plan_id": plan.id,
                "revision": plan.revision,
                "state": plan.state,
                "manifest": plan.manifest,
                "build_run_id": plan.build_run_id,
            }

    def _schedule_update_in(self, db, actor, plan, funding_id, call_cap):
        from ..contracts import KnowledgeBuildInput
        from ..util import fingerprint
        from .security import namespace_access

        namespace_access(db, actor, plan.namespace_id, True)
        if plan.content_hash != fingerprint(plan.manifest):
            raise DomainError("UPDATE_HASH", "维护计划完整性失败。")
        current = self._update_manifest(
            db, actor, plan.old_source_id, plan.new_source_id, plan.namespace_id
        )
        if fingerprint(current) != plan.content_hash:
            raise DomainError("UPDATE_CHANGED", "来源或证据资格改变，请重新预览。")
        if plan.old_source_id == plan.new_source_id:
            raise DomainError("UPDATE_UNCHANGED", "相同固定来源不启动付费重审。")
        body = KnowledgeBuildInput(
            namespace_id=plan.namespace_id,
            funding_id=funding_id,
            source_id=plan.new_source_id,
            question="更新复核（不继承旧语义批准）："
            + ", ".join(u["unit_version_id"] + ":" + u["route"] for u in plan.manifest["units"])[
                :5000
            ],
            call_cap=call_cap,
            research_scope="focused",
            max_units_per_project=min(
                5,
                max(1, len(plan.manifest["units"])),
                max(1, (call_cap - 4) // (4 + int(self.app.retrieval._available()[0]))),
            ),
            idempotency_key="maintenance:" + plan.id,
        )
        manifest = {
            **self.app.build.prepare_in(db, actor, body),
            "update_plan_id": plan.id,
            "update_scope": {
                "plan_id": plan.id,
                "plan_hash": plan.content_hash,
                "old_source_id": plan.old_source_id,
                "new_source_id": plan.new_source_id,
                "changes": plan.manifest["changes"],
                "units": plan.manifest["units"][: body.max_units_per_project],
                "prior_approval_is_not_new_evidence": True,
            },
        }
        from ..models import FundingAgreement
        from .taxonomy import topic_object_id

        funding = locked(db, FundingAgreement, funding_id)
        if funding is None:
            raise DomainError("FUNDING_UNAVAILABLE", "维护费用授权不存在。", 403)
        missing_roles = {"analysis", "reviewer", "coverage"} - set(funding.allowed_roles)
        if missing_roles:
            raise DomainError(
                "MAINTENANCE_ROLE_AUTHORIZATION",
                "missing_roles=" + ",".join(sorted(missing_roles)),
                403,
            )
        if funding.allowed_source_ids and plan.new_source_id not in funding.allowed_source_ids:
            raise DomainError(
                "MAINTENANCE_SOURCE_AUTHORIZATION", "new_snapshot_not_in_funding_scope", 403
            )
        source_obj = require_object(db, actor, plan.new_source_id, "source_set")
        data_classes = {
            "public_source"
            if db.get(Namespace, source_obj.namespace_id).kind == "shared_public"
            else "private_source"
        }
        for key, number in manifest["taxonomy_versions"].items():
            topic_obj = require_object(db, actor, topic_object_id(key, number), "topic_version")
            if db.get(Namespace, topic_obj.namespace_id).kind != "shared_public":
                data_classes.add("private_source")
        missing_classes = data_classes - set(funding.allowed_data_classes)
        if missing_classes:
            raise DomainError(
                "MAINTENANCE_DATA_AUTHORIZATION",
                "missing_data_classes=" + ",".join(sorted(missing_classes)),
                403,
            )
        manifest["maintenance_authorization"] = {
            "required_roles": ["analysis", "coverage", "reviewer"],
            "data_classes": sorted(data_classes),
            "source_id": plan.new_source_id,
            "funding_id": funding.id,
            "authorization_expanded": False,
        }
        run = self.app.runtime.create_in(
            db,
            actor,
            plan.namespace_id,
            funding_id,
            "knowledge_build",
            manifest,
            call_cap,
            body.idempotency_key,
            body.duration_minutes,
        )
        plan.build_run_id = run.id
        plan.state = "scheduled"
        plan.revision += 1
        return {
            "plan_id": plan.id,
            "run_id": run.id,
            "new_review_required": True,
            "head_changed": False,
            "unprocessed_unit_ids": [
                u["unit_version_id"] for u in plan.manifest["units"][body.max_units_per_project :]
            ],
        }

    def execute_update(self, actor, plan_id, body):
        from ..models_governance import SourceUpdatePlan
        from .security import namespace_access

        with self.db.transaction() as db:
            plan = locked(db, SourceUpdatePlan, plan_id)
            if not plan:
                raise DomainError("NOT_FOUND", "维护计划不存在。", 404)
            namespace_access(db, actor, plan.namespace_id, True)
            if plan.revision != body.expected_revision or plan.state != "proposed":
                raise DomainError("UPDATE_CONFLICT", "维护计划版本或状态已改变。")
            return self._schedule_update_in(db, actor, plan, body.funding_id, body.call_cap)

    def create_maintenance(self, actor, body):
        from datetime import timedelta

        from ..db import clock
        from ..models_governance import MaintenanceSchedule

        with self.db.transaction() as db:
            namespace_admin(db, actor, body.namespace_id)
            source = SourceService.get(db, actor, body.source_id)
            obj = require_object(db, actor, source.id)
            if obj.namespace_id != body.namespace_id:
                raise DomainError("MAINTENANCE_SCOPE", "维护任务必须属于来源域。", 403)
            if body.allow_model_calls and not body.funding_id:
                raise DomainError("FUNDING_REQUIRED", "自动重审需要明确的费用授权。", 422)
            row = MaintenanceSchedule(
                namespace_id=body.namespace_id,
                principal_id=actor,
                source_id=source.id,
                interval_seconds=body.interval_seconds,
                allow_model_calls=body.allow_model_calls,
                funding_id=body.funding_id,
                call_cap=body.call_cap,
                enabled=False,
                next_due_at=clock(db) + timedelta(seconds=body.interval_seconds),
            )
            db.add(row)
            db.flush()
            return {
                "schedule_id": row.id,
                "revision": row.revision,
                "enabled": False,
                "external_fetch": False,
            }

    def set_maintenance(self, actor, schedule_id, body):
        from ..db import clock
        from ..models import FundingAgreement
        from ..models_governance import MaintenanceSchedule

        with self.db.transaction() as db:
            row = locked(db, MaintenanceSchedule, schedule_id)
            if row is None:
                raise DomainError("NOT_FOUND", "维护计划不存在。", 404)
            namespace_admin(db, actor, row.namespace_id)
            if row.revision != body.expected_revision:
                raise DomainError("REVISION_CONFLICT", "维护授权已变化。")
            SourceService.get(db, actor, row.source_id)
            if body.enabled and actor != row.principal_id:
                raise DomainError("MAINTENANCE_ACTOR", "启用维护需要原主体明确授权。", 403)
            if body.enabled and row.allow_model_calls:
                funding = db.get(FundingAgreement, row.funding_id)
                if (
                    funding is None
                    or funding.payer_id != actor
                    or funding.namespace_id != row.namespace_id
                    or funding.status != "active"
                    or aware(funding.expires_at) <= clock(db)
                ):
                    raise DomainError("FUNDING_UNAVAILABLE", "维护费用授权无效。", 403)
            row.enabled = body.enabled
            row.revision += 1
            return {
                "schedule_id": row.id,
                "revision": row.revision,
                "enabled": row.enabled,
                "allow_model_calls": row.allow_model_calls,
                "next_due_at": row.next_due_at.isoformat(),
            }

    def tick_maintenance(self, actor, schedule_id):
        """Authorized local poll: examines already imported snapshots; never downloads code."""
        from datetime import timedelta

        from ..db import clock
        from ..models import SourceSet
        from ..models_governance import MaintenanceOccurrence, MaintenanceSchedule, SourceUpdatePlan
        from ..util import fingerprint

        with self.db.transaction() as db:
            row = locked(db, MaintenanceSchedule, schedule_id)
            if row is None:
                raise DomainError("NOT_FOUND", "维护计划不存在。", 404)
            namespace_admin(db, actor, row.namespace_id)
            if actor != row.principal_id:
                raise DomainError("MAINTENANCE_ACTOR", "维护必须由原授权主体执行。", 403)
            if not row.enabled:
                return {"status": "disabled", "model_calls": 0}
            instant = clock(db)
            if aware(row.next_due_at) > instant:
                return {"status": "not_due", "model_calls": 0}
            old = SourceService.get(db, actor, row.source_id)
            candidates = db.scalars(
                select(SourceSet)
                .join(ObjectRow, ObjectRow.id == SourceSet.id)
                .where(
                    ObjectRow.namespace_id == row.namespace_id,
                    SourceSet.repository_key == old.repository_key,
                    SourceSet.origin_kind == old.origin_kind,
                    ObjectRow.created_at > require_object(db, actor, old.id).created_at,
                )
                .order_by(ObjectRow.created_at.desc(), SourceSet.id)
            ).all()
            newest = next(
                (
                    s
                    for s in candidates
                    if s.id != old.id and db.get(ObjectRow, s.id).state == "active"
                ),
                None,
            )
            occurrence = MaintenanceOccurrence(
                schedule_id=row.id,
                due_at=row.next_due_at,
                status="unchanged",
                reason="no_new_imported_snapshot",
            )
            db.add(occurrence)
            result = {"status": "unchanged", "model_calls": 0}
            if newest is not None:
                manifest = self._update_manifest(db, actor, old.id, newest.id, row.namespace_id)
                plan = SourceUpdatePlan(
                    namespace_id=row.namespace_id,
                    principal_id=actor,
                    old_source_id=old.id,
                    new_source_id=newest.id,
                    manifest=manifest,
                    content_hash=fingerprint(manifest),
                )
                db.add(plan)
                db.flush()
                occurrence.plan_id = plan.id
                if row.allow_model_calls:
                    result = self._schedule_update_in(db, actor, plan, row.funding_id, row.call_cap)
                    occurrence.status = "scheduled"
                    occurrence.reason = "explicit_funding_and_schedule"
                else:
                    result = {"status": "planned", "plan_id": plan.id, "model_calls": 0}
                    occurrence.status = "planned"
                    occurrence.reason = "no_model_authorization"
                # Track discovery only; never move published source-selection heads.
                row.source_id = newest.id
            row.next_due_at = instant + timedelta(seconds=row.interval_seconds)
            row.revision += 1
            db.flush()
            result["occurrence_id"] = occurrence.id
            return result

    def run_due_maintenance(self, limit: int = 20) -> list[dict]:
        """Worker-local bounded dispatcher; only explicitly enabled schedules qualify.

        The original principal's permissions, source access, funding and cost ceiling
        are checked again by tick_maintenance. Authorization failure disables the
        occurrence rather than retrying or silently switching to a service account.
        """
        from ..db import clock
        from ..models_governance import MaintenanceOccurrence, MaintenanceSchedule

        if not 1 <= limit <= 100:
            raise ValueError("maintenance batch must be between 1 and 100")
        with self.db.read() as db:
            due = [
                (row.id, row.principal_id, row.revision)
                for row in db.scalars(
                    select(MaintenanceSchedule)
                    .where(
                        MaintenanceSchedule.enabled.is_(True),
                        MaintenanceSchedule.next_due_at <= clock(db),
                    )
                    .order_by(MaintenanceSchedule.next_due_at, MaintenanceSchedule.id)
                    .limit(limit)
                )
            ]
        results = []
        for schedule_id, actor, revision in due:
            try:
                results.append(
                    {"schedule_id": schedule_id, **self.tick_maintenance(actor, schedule_id)}
                )
            except DomainError as exc:
                with self.db.transaction() as db:
                    row = locked(db, MaintenanceSchedule, schedule_id)
                    if row is None or row.revision != revision or not row.enabled:
                        continue
                    occurrence = db.scalar(
                        select(MaintenanceOccurrence).where(
                            MaintenanceOccurrence.schedule_id == row.id,
                            MaintenanceOccurrence.due_at == row.next_due_at,
                        )
                    )
                    if occurrence is None:
                        occurrence = MaintenanceOccurrence(
                            schedule_id=row.id,
                            due_at=row.next_due_at,
                            status="blocked",
                            reason=(exc.code + ":" + exc.message)[:100],
                        )
                        db.add(occurrence)
                    row.enabled = False
                    row.revision += 1
                    results.append(
                        {
                            "schedule_id": row.id,
                            "status": "blocked",
                            "reason": exc.code,
                            "authorization_detail": exc.message,
                            "model_calls": 0,
                        }
                    )
        return results
