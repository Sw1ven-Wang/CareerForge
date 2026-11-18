import pytest
from support import FILES, FixtureModel, FixtureRepositories

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput


@pytest.fixture
def env(tmp_path):
    model = FixtureModel()
    repos = FixtureRepositories()
    settings = Settings(
        mode="test",
        database_url="sqlite+pysqlite:///" + str(tmp_path / "test.db"),
        allow_fixture_publication=True,
        embedding_dimensions=8,
        sandbox_image="sha256:" + "1" * 64,
        state_dir=tmp_path,
    )
    app = Application(settings, model, repos)
    owner = app.bootstrap()
    funding = app.runtime.create_funding(owner["principal_id"], owner["namespace_id"], 1000)
    value = {
        "app": app,
        "owner": owner,
        "actor": owner["principal_id"],
        "ns": owner["namespace_id"],
        "funding": funding,
        "model": model,
        "repos": repos,
    }
    yield value
    app.db.engine.dispose()


async def publish_fixture(
    env, key="build", repository="fixtures/agent", commit="a" * 40, namespace=None
):
    app, actor = env["app"], env["actor"]
    ns = namespace or env["ns"]
    funding = env["funding"] if ns == env["ns"] else app.runtime.create_funding(actor, ns, 100)
    source = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=ns,
            repository_key=repository,
            commit_sha=commit,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )
    run = app.build.start(
        actor,
        KnowledgeBuildInput(
            namespace_id=ns,
            funding_id=funding,
            question="Tool Calling 返回值示例",
            source_id=source,
            topic_keys=["agent.tool_calling"],
            call_cap=10,
            max_units_per_project=1,
            idempotency_key=key,
        ),
    )
    await app.drain()
    result = app.runtime.stage(run, "result")
    assert result and result["publication_ids"], result
    return source, result["publication_ids"][0], run
