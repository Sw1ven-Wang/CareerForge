import base64
import hashlib
import json

import httpx
import pytest
from sqlalchemy import select
from test_learning_language import choose, imported

from careerforge.adapters.github import GitHubProvider
from careerforge.api import create_app
from careerforge.contracts_conversation_actions_v1 import DialogueMessage
from careerforge.models import AgentRun, ObjectRow, Publication
from careerforge.services.project_quality import static_quality


@pytest.mark.parametrize(
    "stars,forks,status",
    [
        (100, 51, "below_threshold"),
        (101, 50, "below_threshold"),
        (101, 51, "passed"),
        (999999, 1, "below_threshold"),
        (None, 100, "unverified"),
        (101, True, "unverified"),
    ],
)
def test_community_strict_and_threshold_and_missing_counts(stars, forks, status):
    from careerforge.services.project_quality import community_gate

    assert community_gate({"stars": stars, "forks": forks})["status"] == status


def test_learning_content_does_not_require_tests_ci_or_running():
    from careerforge.services.project_quality import learning_quality

    files = {
        "README.md": "Project architecture and usage. " * 10,
        "src/core.py": "def run():\n    return 42",
    }
    report = learning_quality(files, {"stars": 101, "forks": 51})
    assert report["status"] == "priority_for_source_review"
    assert report["runtime_test_result"] == "not_run"
    assert not report["test_ci_gate_required"]
    assert not report["semantic_quality_verified"]


def project_files(language="python", *, test_ci=True, assertions=True):
    files = {"README.md": "# Example\nInstallation and usage instructions. " * 5, "LICENSE": "MIT"}
    if language == "go":
        files.update(
            {
                "src/main.go": "package main\nfunc compute(x int) int { return x + 1 }",
                "go.mod": "module example\ngo 1.22",
                "tests/main_test.go": 'func TestCompute(t *testing.T) { if compute(1) != 2 { t.Error("wrong") } }',
            }
        )
        command = "go test ./..."
    elif language == "typescript":
        files.update(
            {
                "src/main.ts": "export function compute(x: number) { return x + 1; }",
                "package.json": '{"scripts":{"test":"vitest"}}',
                "tests/main.test.ts": 'test("result", () => { expect(compute(1)).toBe(2); });',
            }
        )
        command = "npm test"
    else:
        files.update(
            {
                "src/main.py": "def compute(x):\n    result = x + 1\n    return result\n",
                "pyproject.toml": '[project]\nname="example"',
                "tests/test_main.py": "def test_result():\n    assert compute(1) == 2\n"
                if assertions
                else 'print("demo only")',
            }
        )
        command = "pytest"
    files[".github/workflows/ci.yml"] = (
        "steps:\n  - run: " + command if test_ci else "steps:\n  - uses: actions/deploy-pages@v4"
    )
    return files


@pytest.mark.parametrize("language", ["python", "go", "typescript"])
def test_quality_is_language_independent_and_never_claims_tests_passed(language):
    result = static_quality(project_files(language))
    assert result["status"] == "priority_for_source_review"
    assert result["runtime_test_result"] == "not_run"
    assert result["semantic_quality_verified"] is False


def test_directory_name_and_deployment_workflow_are_not_test_quality_proof():
    result = static_quality(project_files(test_ci=False, assertions=False))
    assert result["status"] == "needs_quality_review"
    assert not result["signals"]["test_assertion_paths"]
    assert not result["signals"]["test_ci_paths"]


def test_vendored_tests_do_not_raise_project_quality():
    files = project_files(assertions=False, test_ci=False)
    files["vendor/tests/test_all.py"] = "def test_ok():\n    assert True"
    assert not static_quality(files)["signals"]["test_assertion_paths"]


def test_node_assert_strict_tests_are_recognized_without_executing_them():
    files = project_files("typescript")
    files["tests/main.test.ts"] = (
        'import assert from "node:assert/strict";\nassert.equal(compute(1), 2);\nassert.deepEqual([1], [1]);'
    )
    report = static_quality(files)
    assert report["signals"]["assertion_count"] == 2
    assert report["status"] == "priority_for_source_review"
    assert report["runtime_test_result"] == "not_run"


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True])
@pytest.mark.parametrize("sampling_mode", ["quality_evidence", "learning_content"])
async def test_quality_snapshot_is_bounded_pinned_and_does_not_download_archives(
    corrupt, sampling_mode
):
    provider = object.__new__(GitHubProvider)
    files = project_files("go")
    blobs = {
        hashlib.sha1(
            b"blob " + str(len(text.encode())).encode() + b"\0" + text.encode()
        ).hexdigest(): (path, text)
        for path, text in files.items()
    }
    operations = []

    async def get(url, run_id, cap, operation, **kwargs):
        operations.append(operation)
        if operation == "metadata":
            value = {
                "id": 1,
                "private": False,
                "fork": False,
                "archived": False,
                "license": {"spdx_id": "MIT"},
                "default_branch": "main",
                "language": "Go",
            }
        elif operation == "pin":
            value = {"sha": "a" * 40}
        elif operation == "quality:tree":
            value = {
                "tree": [
                    {
                        "type": "blob",
                        "mode": "100644",
                        "size": len(text.encode()),
                        "path": path,
                        "sha": sha,
                    }
                    for sha, (path, text) in blobs.items()
                ],
                "truncated": False,
            }
        else:
            sha = url.rsplit("/", 1)[-1]
            path, text = blobs[sha]
            if corrupt and path == "src/main.go":
                text = text.replace("return", "returX")
            value = {
                "sha": sha,
                "encoding": "base64",
                "content": base64.b64encode(text.encode()).decode(),
            }
        return json.dumps(value).encode()

    provider._get = get
    snapshot = await provider.quality_snapshot(
        "https://github.com/example/project", "run", 30, sampling_mode=sampling_mode
    )
    if sampling_mode == "learning_content":
        assert not any(p.startswith(("tests/", ".github/")) for p in snapshot["files"])
    assert "archive" not in operations
    assert snapshot["commit_sha"] == "a" * 40
    assert snapshot["source_metadata"]["partial_snapshot"]
    assert snapshot["source_metadata"]["source_scope"] == "selected_fixed_files_not_full_repository"
    assert len(snapshot["files"]) <= 9
    if corrupt:
        assert "src/main.go" not in snapshot["files"]
        assert "src/main.go" in snapshot["source_metadata"]["unreadable_paths"]
    else:
        assert snapshot["files"]["src/main.go"] == files["src/main.go"]


class Projects:
    synthetic = True

    def __init__(self):
        self.calls = []

    async def search(self, query, run_id, request_cap, channel="repository", **kwargs):
        self.calls.append(query)
        return {
            "candidates": [
                {
                    "name": "fixtures/" + name,
                    "url": "https://github.com/fixtures/" + name,
                    "language": language,
                    "stars": stars,
                    "forks": 1 if name == "a-poor" else 51,
                    "license": "MIT",
                    "archived": False,
                }
                for name, language, stars in [
                    ("a-poor", "Python", 999999),
                    ("b-go", "Go", 101),
                    ("c-python", "Python", 102),
                    ("d-ts", "TypeScript", 103),
                ]
            ],
            "synthetic": True,
        }

    async def snapshot(self, url, run_id, request_cap):
        name = url.rsplit("/", 1)[-1]
        language = "go" if name == "b-go" else "typescript" if name == "d-ts" else "python"
        return {
            "repository_key": "fixtures/" + name,
            "commit_sha": "a" * 40,
            "license_id": "MIT",
            "files": project_files(language, test_ci=name != "a-poor", assertions=name != "a-poor"),
            "source_metadata": {"synthetic": True},
        }

    async def project_metadata(self, url, run_id, request_cap):
        return {
            "stars": None,
            "stargazers_count": 1000,
            "forks_count": 1 if url.endswith("a-poor") else 51,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["搜项目并入库", "确认目标"])
async def test_search_ingests_multiple_projects_prioritizes_quality_and_does_not_analyze(
    env, message
):
    app, actor = env["app"], env["actor"]
    cid, wid = await imported(env)
    choose(env, cid, "/language Python")
    app.repositories = Projects()
    before = len(env["model"].calls)
    response = app.dialogue.dispatch(
        actor,
        cid,
        DialogueMessage(
            text=message,
            expected_focus_revision=app.conversations.detail(actor, cid)["revision"],
            idempotency_key="projects",
            authorization_ref=env["funding"],
        ),
    )
    assert response["run_refs"], response
    await app.drain()
    result = app.runtime.stage(response["run_refs"][0], "result")
    assert result["priority_count"] == 3
    assert len(result["project_entries"]) == 3
    assert result["gaps"][0]["community_gate"]["reasons"] == ["fork_threshold_not_met"]
    assert result["target_count_reached"]
    assert len(env["model"].calls) == before
    assert result["no_analysis_or_publication"]
    with app.db.read() as db:
        assert not list(db.scalars(select(Publication)))
        assert all(
            r.workflow in {"jd_extract", "project_discovery"} for r in db.scalars(select(AgentRun))
        )
        assert (
            len(list(db.scalars(select(ObjectRow).where(ObjectRow.kind == "project_pool_entry"))))
            == 3
        )
    view = app.workflows.status(actor, cid)
    assert view["milestone"] == "awaiting_project_selection"
    assert view["project_pool"]["priority_count"] == 3
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as api:
        response = await api.get(f"/api/v1/workflows/{wid}/projects?include_pending=false")
        assert response.status_code == 200
        projects = response.json()["projects"]
        assert {p["language"] for p in projects} == {"Python", "Go", "TypeScript"}
        assert all(p["repository"] != "fixtures/a-poor" for p in projects)
        default = await api.get(f"/api/v1/workflows/{wid}/projects")
        assert {p["entry_id"] for p in default.json()["projects"]} == {
            p["entry_id"] for p in projects
        }
        for project in projects:
            quality = await api.get("/api/v1/projects/" + project["entry_id"] + "/quality")
            assert quality.status_code == 200
            assert quality.json()["runtime_test_result"] == "not_run"
    from careerforge.services.security import create_principal

    with app.db.transaction() as db:
        _, _, token = create_principal(db, "project-outsider")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + token},
    ) as api:
        assert (await api.get(f"/api/v1/workflows/{wid}/projects")).status_code == 404
        assert (
            await api.get("/api/v1/projects/" + projects[0]["entry_id"] + "/quality")
        ).status_code == 404


@pytest.mark.asyncio
async def test_project_search_checks_private_goal_egress_before_admission(env):
    app, actor = env["app"], env["actor"]
    cid, _ = await imported(env)
    choose(env, cid, "/language Python")
    funding = app.runtime.create_funding(
        actor, env["ns"], 1, data_classes=["public_source"], roles=[]
    )
    result = app.dialogue.dispatch(
        actor,
        cid,
        DialogueMessage(
            text="搜项目并入库",
            expected_focus_revision=app.conversations.detail(actor, cid)["revision"],
            idempotency_key="no-private-scope",
            authorization_ref=funding,
        ),
    )
    assert result["action_receipts"][0]["failure_code"] == "GITHUB_EGRESS"
    assert not result["run_refs"]


@pytest.mark.asyncio
async def test_user_named_projects_bypass_generic_search_but_keep_quality_gate(env):
    from careerforge.models_dialogue import WorkflowSession
    from careerforge.services.project_pool import ProjectPoolService

    app, actor = env["app"], env["actor"]
    cid, wid = await imported(env)
    choose(env, cid, "/language Python")
    repos = Projects()
    app.repositories = repos
    before = len(env["model"].calls)
    with app.db.transaction() as db:
        workflow = db.get(WorkflowSession, wid)
        identifier = ProjectPoolService(app).start_in(
            db,
            actor,
            workflow,
            env["funding"],
            "nominated",
            target_count=2,
            max_snapshots=2,
            repository_urls=[
                "https://github.com/fixtures/a-poor",
                "https://github.com/fixtures/b-go",
            ],
        )
    await app.execute(identifier)
    result = app.runtime.stage(identifier, "result")
    assert not repos.calls
    assert len(result["project_entries"]) == 1
    assert result["priority_count"] == 1
    assert not result["target_count_reached"]
    assert all(p["candidate_origin"] == "user_nomination" for p in result["project_entries"])
    assert len(env["model"].calls) == before
    from copy import deepcopy

    with app.db.transaction() as db:
        entry = next(p for p in result["project_entries"] if p["repository"] == "fixtures/b-go")
        original = deepcopy(db.get(ObjectRow, entry["quality_report_id"]).payload)
        revised = ProjectPoolService(app).reassess_in(db, actor, entry["entry_id"])
        assert revised["entry_id"] != entry["entry_id"]
        assert revised["quality_report_id"] != entry["quality_report_id"]
        assert db.get(ObjectRow, entry["quality_report_id"]).payload == original
        assert revised["supersedes_entry_id"] == entry["entry_id"]
        from careerforge.services.project_quality import COMMUNITY_POLICY

        rejected = ProjectPoolService(app).reassess_in(
            db,
            actor,
            revised["entry_id"],
            metadata={"stars": 100, "forks": 51},
            policy=COMMUNITY_POLICY,
        )
        assert rejected["quality_status"] == "below_community_threshold"
        assert not ProjectPoolService(app).list_in(db, actor, entry["goal_version_id"])
        # Activate a changed policy and rescreen: historical eligibility cannot leak back.
        from careerforge.services.project_selection_policy import record_policy_in

        policy = {**COMMUNITY_POLICY, "stars_exclusive_min": 1001}
        record_policy_in(db, actor, entry["goal_version_id"], policy, "测试变更门槛")
        ProjectPoolService(app).reassess_in(
            db, actor, entry["entry_id"], metadata={"stars": 1000, "forks": 51}, policy=policy
        )
        assert not ProjectPoolService(app).list_in(db, actor, entry["goal_version_id"])
        assert db.get(ObjectRow, entry["quality_report_id"]).payload == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "urls", [["https://evil.example/a/b"], ["https://github.com/a/b", "https://github.com/A/B"]]
)
async def test_project_nomination_rejects_foreign_hosts_and_duplicate_aliases(env, urls):
    from careerforge.errors import DomainError
    from careerforge.models_dialogue import WorkflowSession
    from careerforge.services.project_pool import ProjectPoolService

    cid, wid = await imported(env)
    choose(env, cid, "/language Python")
    with env["app"].db.transaction() as db:
        with pytest.raises(DomainError, match="GitHub"):
            ProjectPoolService(env["app"]).start_in(
                db,
                env["actor"],
                db.get(WorkflowSession, wid),
                env["funding"],
                "invalid-nomination",
                target_count=1,
                max_snapshots=2,
                repository_urls=urls,
            )
