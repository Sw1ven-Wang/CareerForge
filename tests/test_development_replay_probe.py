from dataclasses import replace

import pytest
from test_evolution_replay_v16 import replay_data

from careerforge.errors import DomainError


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,split", [("production", "development"), ("development", "holdout")])
async def test_development_probe_cannot_enter_production_or_holdout(env, mode, split):
    app = env["app"]
    _, value = await replay_data(env, split="development")
    app.db.settings = replace(app.db.settings, mode=mode)
    with pytest.raises(DomainError) as error:
        app.evolution.register_replay_dataset(
            env["actor"],
            value.model_copy(
                update={
                    "provenance": "development_probe",
                    "split": split,
                    "name": "probe-invalid",
                }
            ),
        )
    assert error.value.code == "DEVELOPMENT_PROBE_SCOPE"


@pytest.mark.asyncio
async def test_development_probe_provenance_is_frozen_and_cannot_be_upgraded(env):
    app = env["app"]
    _, value = await replay_data(env, split="development")
    app.db.settings = replace(app.db.settings, mode="development")
    result = app.evolution.register_replay_dataset(
        env["actor"],
        value.model_copy(
            update={
                "provenance": "development_probe",
                "name": "native-development-probe",
            }
        ),
    )
    with app.db.transaction() as db:
        row = app.evolution._dataset(db, env["actor"], result["dataset_id"])
        assert row.threshold_policy["development_probe_contract"] == "development-replay-probe-v1"
        row.provenance = "human_gold"
    with app.db.read() as db, pytest.raises(DomainError) as error:
        app.evolution._dataset(db, env["actor"], result["dataset_id"])
    assert error.value.code == "DEVELOPMENT_PROBE_SCOPE"
