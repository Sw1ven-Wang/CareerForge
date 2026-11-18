import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_input_runtime import InputFlow, RuntimeBindings


def test_runtime_ids_are_typed_preserve_prose_and_fail_on_missing_bindings():
    bindings = RuntimeBindings()
    bindings.bind("namespace_id", "fixture-ns", "actual-ns", "bootstrap")
    bindings.bind("publication_id", "fixture-pub", ["pub-a", "pub-b"], "native publications")
    result = bindings.resolve(
        {
            "authorized_namespace_refs": ["fixture-ns"],
            "pinned_publication_ids": ["fixture-pub"],
            "raw_text": "fixture-ns must remain in source text",
        }
    )
    assert result == {
        "authorized_namespace_refs": ["actual-ns"],
        "pinned_publication_ids": ["pub-a", "pub-b"],
        "raw_text": "fixture-ns must remain in source text",
    }
    with pytest.raises(ValueError, match="Unresolved runtime ID"):
        bindings.resolve({"attempt_id": "fixture-old-attempt"})
    with pytest.raises(ValueError, match="Immutable"):
        bindings.bind("namespace_id", "fixture-ns", "other-user-ns", "later")
    assert len(bindings.rows) == 2


def test_root_must_be_solver_input_and_never_accepts_casebook_or_judge(tmp_path):
    for name in ["casebook", "judge_only"]:
        folder = tmp_path / name
        folder.mkdir()
        with pytest.raises(ValueError, match="separately mounted agent_input"):
            InputFlow(folder, "CF-D01")


def test_judge_fields_are_rejected_even_if_selected_flow_would_ignore_the_row(tmp_path):
    folder = tmp_path / "agent_input/inputs/development"
    folder.mkdir(parents=True)
    (folder / "stage.jsonl").write_text(
        json.dumps({"flow_id": "different", "expected_output": {"answer": "secret"}})
    )
    with pytest.raises(ValueError, match="Only solver input"):
        InputFlow(tmp_path / "agent_input", "CF-D01")


def test_duplicate_or_incomplete_flow_cannot_start_paid_execution(tmp_path):
    folder = tmp_path / "agent_input/inputs/development"
    folder.mkdir(parents=True)
    with pytest.raises(ValueError, match="exactly S01"):
        InputFlow(tmp_path / "agent_input", "CF-D01")


def test_paid_admission_resume_replays_original_cas_body_and_rejects_scope_change():
    from agent_input_runtime import AdmissionJournal

    entries = {}
    journal = AdmissionJournal(entries)
    original = {
        "idempotency_key": "same",
        "expected_focus_revision": 1,
        "authorization_ref": "owned-funding",
        "intent": {"type": "request_assessment"},
    }
    sent = journal.prepare("POST", "/messages", original)
    original["intent"]["type"] = "mutated-by-caller"
    assert sent["intent"]["type"] == "request_assessment"
    resumed = AdmissionJournal(entries)
    replay = resumed.prepare("POST", "/messages", {**sent, "expected_focus_revision": 9})
    assert replay == sent
    with pytest.raises(ValueError, match="semantic"):
        resumed.prepare("POST", "/messages", {**sent, "authorization_ref": "other-funding"})


def test_solver_root_symlink_is_rejected_before_loading_any_files(tmp_path):
    target = tmp_path / "real/agent_input"
    target.mkdir(parents=True)
    alias = tmp_path / "agent_input"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        InputFlow(alias, "CF-D01")


def test_admission_journal_does_not_merge_different_workflows_with_same_client_key():
    from agent_input_runtime import AdmissionJournal

    journal = AdmissionJournal()
    for path in ["/workflow/a/messages", "/workflow/b/messages"]:
        journal.prepare("POST", path, {"idempotency_key": "same", "workflow_id": path})
    assert len(journal.entries) == 2


def test_resume_does_not_rewrite_original_stage_receipts():
    from run_agent_input_live import LiveFlow

    flow = object.__new__(LiveFlow)
    original = {"milestone": "awaiting_plan_acceptance", "revision": 3}
    flow.report = {"stages": {"after_research": original.copy()}}
    captures = []
    flow.capture = lambda stage, value: captures.append(value)
    flow.persist = lambda: None
    flow.save("after_research", {"milestone": "learning", "revision": 9})
    assert flow.report["stages"]["after_research"] == original
    assert captures == [original]
    assert flow.report["resume_observations"][0]["observed"]["revision"] == 9


def test_first_binding_updates_persisted_empty_ledger_and_survives_json_reload():
    report = {"bindings": []}
    first = RuntimeBindings(report["bindings"])
    first.bind("attempt_id", "fixture-attempt", "native-attempt", "native practice receipt")
    assert report["bindings"] == first.rows and len(report["bindings"]) == 1
    restored = json.loads(json.dumps(report))
    resumed = RuntimeBindings(restored["bindings"])
    assert resumed.resolve({"attempt_id": "fixture-attempt"}) == {"attempt_id": "native-attempt"}
    resumed.bind("funding_id", "fixture-funding", "owned-grant", "original owner funding")
    assert len(restored["bindings"]) == 2


@pytest.mark.parametrize("unready", [None, "", []])
def test_unmaterialized_domain_ids_cannot_be_registered_as_completed_bindings(unready):
    rows = []
    bindings = RuntimeBindings(rows)
    with pytest.raises(ValueError, match="not materialized"):
        bindings.bind("attempt_id", "fixture-attempt", unready, "queued admission")
    assert rows == []
    bindings.bind("attempt_id", "fixture-attempt", "native-created-attempt", "submit receipt")
    assert (
        bindings.resolve({"attempt_id": "fixture-attempt"})["attempt_id"]
        == "native-created-attempt"
    )
