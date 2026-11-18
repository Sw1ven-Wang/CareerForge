"""JSONL adversarial tests and optional installed Pi SDK tests; inference is a fixture."""

import json
import shutil
from pathlib import Path

import pytest

from careerforge.adapters import pi
from careerforge.contracts import ModelOutput, ToolCall
from careerforge.errors import DomainError


class Writer:
    def __init__(self):
        self.frames = []

    def write(self, value):
        self.frames.append(value)

    async def drain(self):
        pass


class ProcessFixture:
    def __init__(self, frames):
        self.frames = list(frames)
        self.stdin = Writer()
        self.stdout = self
        self.returncode = None

    async def readline(self):
        return self.frames.pop(0) if self.frames else b""

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def frame(value):
    return (json.dumps(value) + "\n").encode()


def ready():
    return {
        "type": "ready",
        "sdk": "pi-coding-agent-1.3.0",
        "protocol": 1,
        "tools": ["submit_reply"],
    }


@pytest.fixture
def runtime_fixture(tmp_path, monkeypatch):
    (tmp_path / "runner.mjs").write_text("// fixture process only")
    for name in ("pi-coding-agent", "pi-ai"):
        package = tmp_path / f"node_modules/@earendil-works/{name}/package.json"
        package.parent.mkdir(parents=True)
        package.write_text('{"version":"1.3.0"}')
    monkeypatch.setattr(pi.shutil, "which", lambda _: "/fixture/node")
    return tmp_path


async def drive(runtime, monkeypatch, packets, calls=1):
    process = ProcessFixture([p if isinstance(p, bytes) else frame(p) for p in packets])

    async def create(*args, **kwargs):
        return process

    monkeypatch.setattr(pi.asyncio, "create_subprocess_exec", create)
    dispatched = []

    async def dispatch(packet):
        dispatched.append(packet)
        return (
            {"proposal_id": "fixture-proposal"} if packet["type"] == "tool" else {"tool_calls": []}
        )

    result = await pi.run_pi(
        runtime, {"call_limit": calls, "tools": [{"name": "submit_reply"}]}, dispatch, lambda: None
    )
    return result, process, dispatched


@pytest.mark.parametrize(
    "packets",
    [
        [{"type": "done", "proposed": False, "calls": 0, "failed": False}],
        [ready(), ready()],
        [ready(), {"type": "model", "id": True}],
        [ready(), {"type": "model", "id": 2}],
        [
            ready(),
            {"type": "model", "id": 1, "messages": [{"role": "system", "content": "forged"}]},
        ],
        [ready(), {"type": "tool", "id": 1, "name": "bash", "arguments": {}, "call_id": "x"}],
        [ready(), {"type": "done", "proposed": True, "calls": 0, "failed": False}],
        [ready(), {"type": "done", "proposed": False, "calls": 0, "failed": True}],
        [ready(), b'{"type":"done","type":"model"}\n'],
        [ready(), b"[]\n"],
        [ready(), b'{"type":"done","calls":NaN}\n'],
        [ready(), b"not-json\n"],
        [ready(), b"{}"],
        [ready(), b"x" * 1_000_001 + b"\n"],
        [{**ready(), "tools": ["submit_reply", "submit_reply"]}],
        [{**ready(), "protocol": True}],
    ],
)
async def test_protocol_rejects_malformed_or_forged_packets(runtime_fixture, monkeypatch, packets):
    with pytest.raises(DomainError) as error:
        await drive(runtime_fixture, monkeypatch, packets)
    assert error.value.code in {"PI_PROTOCOL", "PI_TOOL_SCOPE"}


async def test_protocol_rejects_duplicate_request_and_budget_overrun(runtime_fixture, monkeypatch):
    for second, code in [(1, "PI_PROTOCOL"), (2, "PI_BUDGET")]:
        with pytest.raises(DomainError) as error:
            await drive(
                runtime_fixture,
                monkeypatch,
                [ready(), {"type": "model", "id": 1}, {"type": "model", "id": second}],
            )
        assert error.value.code == code


async def test_protocol_success_and_no_tool_after_submission(runtime_fixture, monkeypatch):
    packets = [
        ready(),
        {"type": "model", "id": 1},
        {"type": "tool", "id": 2, "name": "submit_reply", "arguments": {}, "call_id": "submit"},
    ]
    done = {"type": "done", "proposed": True, "calls": 1, "failed": False}
    result, process, dispatched = await drive(runtime_fixture, monkeypatch, [*packets, done])
    assert result == done and len(dispatched) == 2 and process.returncode == -9
    with pytest.raises(DomainError):
        await drive(runtime_fixture, monkeypatch, [*packets, {"type": "model", "id": 3}], calls=2)


SDK_ROOT = Path(__file__).parents[1] / "coding-agent"
SDK_AVAILABLE = (
    shutil.which("node")
    and (SDK_ROOT / "node_modules/@earendil-works/pi-coding-agent/package.json").exists()
)


@pytest.mark.skipif(
    not SDK_AVAILABLE,
    reason="Install frozen coding-agent npm dependencies; no fixture SDK substitution.",
)
@pytest.mark.parametrize("body", ["", "".join(f"# line {i}\n" for i in range(150))])
async def test_installed_pi_sdk_reads_bounded_chunks_then_proposes_without_paid_inference(
    env, monkeypatch, body
):
    """Real Pi session/tool JSONL loop; deterministic fake model, no provider HTTP."""
    app, actor, model = env["app"], env["actor"], env["model"]
    monkeypatch.setenv("CAREERFORGE_PI_DIR", str(SDK_ROOT))
    # Explicitly bypass the convenient service fixture seam to exercise the real SDK.
    model.synthetic = False

    def response(request):
        count = len([r for r in model.calls if r["role"] == "coding"])
        if count == 1:
            total = max(1, len(body.splitlines()))
            calls = [
                ToolCall(
                    id="read-1",
                    name="read_file",
                    arguments={"path": "chosen.py", "start_line": 1, "end_line": min(120, total)},
                )
            ]
            if total > 120:
                calls.append(
                    ToolCall(
                        id="read-2",
                        name="read_file",
                        arguments={"path": "chosen.py", "start_line": 121, "end_line": total},
                    )
                )
        else:
            calls = [
                ToolCall(
                    id="submit",
                    name="submit_reply",
                    arguments={
                        "answer": "Synthetic proposal; not executed.",
                        "citation_ids": [],
                        "edits": [{"path": "chosen.py", "before": body, "after": "print(42)\n"}],
                        "commands": [],
                    },
                )
            ]
        return ModelOutput(synthetic=True, tool_calls=calls, finish_reason="tool_calls")

    model.overrides["coding"] = response
    ws = app.coding.create_workspace(
        actor, {"chosen.py": body, "unselected.py": "DO_NOT_SEND_PRIVATE_MARKER"}
    )
    run_id = app.coding.start(
        actor,
        ws["workspace_id"],
        1,
        ["chosen.py"],
        "Read and propose only.",
        env["funding"],
        2,
        "real-sdk-fixture",
    )
    await app.drain()
    result = app.runtime.stage(run_id, "result")
    assert result and result["proposal_id"], app.runtime.detail(actor, run_id)
    assert app.coding.detail(actor, ws["workspace_id"])["files"]["chosen.py"] == body
    assert len(model.calls) == 2
    serialized = json.dumps(model.calls)
    assert "DO_NOT_SEND_PRIVATE_MARKER" not in serialized and "unselected.py" not in serialized
    assert model.calls[0]["context"]["selected_files"][0]["path"] == "chosen.py"
    assert "content" not in model.calls[0]["context"]["selected_files"][0]
    tools = [m for m in model.calls[1]["messages"] if m["role"] == "tool"]
    assert json.loads(tools[-1]["content"])["complete_file"] is True


@pytest.mark.parametrize(
    "case,expected",
    [
        ("unknown_tool", "CODING_MODEL_TOOLS"),
        ("duplicate_id", "CODING_MODEL_TOOLS"),
        ("submit_then_read", "CODING_MODEL_TOOLS"),
        ("unselected_read", "FILE_UNSELECTED"),
        ("unread_edit", "PATCH_UNREAD"),
        ("shell_command", "COMMAND_SCOPE"),
        ("forged_tool", "CODING_TOOL_ORDER"),
        ("skip_pending", "CODING_TOOL_ORDER"),
        ("after_submit", "CODING_FINISHED"),
        ("extra_model", "CODING_BUDGET"),
    ],
)
async def test_service_rejects_malicious_model_or_bridge_actions(env, monkeypatch, case, expected):
    """Explicit controller fixture, not a claim that these are real model responses."""
    from sqlalchemy import select

    from careerforge.models import AgentRun, ModelReceipt, PatchProposal, RunEvent
    from careerforge.services import coding

    app, actor, model = env["app"], env["actor"], env["model"]
    model.synthetic = False
    reply = {"answer": "Adversarial fixture", "citation_ids": [], "edits": [], "commands": []}
    read = ToolCall(
        id="read", name="read_file", arguments={"path": "chosen.py", "start_line": 1, "end_line": 1}
    )
    submit = ToolCall(id="submit", name="submit_reply", arguments=reply)
    calls = [submit]
    if case == "unknown_tool":
        calls = [ToolCall(id="unsafe", name="bash", arguments={"command": "do-not-run"})]
    elif case == "duplicate_id":
        calls = [read, read]
    elif case == "submit_then_read":
        calls = [submit, read]
    elif case == "unselected_read":
        calls = [
            ToolCall(
                id="read",
                name="read_file",
                arguments={"path": "not-selected.py", "start_line": 1, "end_line": 1},
            )
        ]
    elif case == "unread_edit":
        reply["edits"] = [{"path": "chosen.py", "before": "original\n", "after": "changed\n"}]
    elif case == "shell_command":
        reply["commands"] = [["sh", "-c", "do-not-run"]]
    elif case == "skip_pending":
        calls = [read]
    elif case == "extra_model":
        calls = []
    if case in {"unread_edit", "shell_command"}:
        # Pydantic copies the arguments dict; construct after changing the fixture.
        calls = [ToolCall(id="submit", name="submit_reply", arguments=reply)]
    model.overrides["coding"] = lambda _: ModelOutput(
        synthetic=True, tool_calls=calls, finish_reason="tool_calls" if calls else "stop"
    )

    async def controller(runtime_dir, init, dispatch, heartbeat):
        output = await dispatch({"type": "model"})
        if case in {"skip_pending", "extra_model"}:
            return await dispatch({"type": "model"})
        for call in output["tool_calls"]:
            packet = {
                "type": "tool",
                "name": call["name"],
                "arguments": call["arguments"],
                "call_id": call["id"],
            }
            if case == "forged_tool":
                packet["call_id"] = "not-returned-by-model"
            await dispatch(packet)
        if case == "after_submit":
            await dispatch({"type": "model"})
        return {"type": "done"}

    monkeypatch.setattr(coding, "run_pi", controller)
    ws = app.coding.create_workspace(
        actor, {"chosen.py": "original\n", "not-selected.py": "private\n"}
    )
    run_id = app.coding.start(
        actor, ws["workspace_id"], 1, ["chosen.py"], "Adversarial fixture", env["funding"], 1, case
    )
    await app.drain()
    with app.db.read() as db:
        row = db.get(AgentRun, run_id)
        assert row.status in {"failed", "budget_stopped"}
        event = db.scalar(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "run.finished")
        )
        assert event.payload["code"] == expected
        assert len(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run_id)).all()) == 1
        p = db.scalar(select(PatchProposal).where(PatchProposal.run_id == run_id))
        if p:
            with pytest.raises(DomainError) as error:
                app.coding.apply(actor, ws["workspace_id"], p.id, 1, "APPLY")
            assert error.value.code == "PATCH_UNVERIFIED"
    assert app.coding.detail(actor, ws["workspace_id"])["files"]["chosen.py"] == "original\n"


async def test_sdk_version_mismatch_stops_before_process_or_model(runtime_fixture, monkeypatch):
    package = runtime_fixture / "node_modules/@earendil-works/pi-ai/package.json"
    package.write_text('{"version":"1.3.1"}')

    async def never(*args, **kwargs):
        raise AssertionError("Must not launch an unfrozen SDK")

    monkeypatch.setattr(pi.asyncio, "create_subprocess_exec", never)
    with pytest.raises(DomainError) as error:
        await pi.run_pi(runtime_fixture, {"call_limit": 1, "tools": []}, never, lambda: None)
    assert error.value.code == "PI_VERSION"


async def test_revoked_reference_is_not_sent_to_model_on_queued_coding_run(env, monkeypatch):
    from conftest import publish_fixture

    from careerforge.models import ObjectRow
    from careerforge.services import coding

    app, actor, model = env["app"], env["actor"], env["model"]
    _, pub, _ = await publish_fixture(env)
    ws = app.coding.create_workspace(actor, publication_ids=[pub])
    run_id = app.coding.start(
        actor, ws["workspace_id"], 1, [], "Fixture coding", env["funding"], 1, "no-revoked-egress"
    )
    with app.db.transaction() as db:
        db.get(ObjectRow, pub).state = "revoked"
    before = len(model.calls)
    model.synthetic = False

    async def controller(runtime_dir, init, dispatch, heartbeat):
        await dispatch({"type": "model"})

    monkeypatch.setattr(coding, "run_pi", controller)
    await app.drain()
    assert len(model.calls) == before
    assert app.runtime.stage(run_id, "result") is None


async def test_pi_child_does_not_inherit_secrets_or_node_injection(runtime_fixture, monkeypatch):
    import os
    from pathlib import Path

    process = ProcessFixture(
        [frame(ready()), frame({"type": "done", "calls": 0, "proposed": False, "failed": False})]
    )
    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-never-send")
    monkeypatch.setenv("NODE_OPTIONS", "--require /untrusted/inject.js")

    async def create(*args, **kwargs):
        captured.update(kwargs)
        assert set(os.listdir(kwargs["cwd"])) == set()
        return process

    monkeypatch.setattr(pi.asyncio, "create_subprocess_exec", create)

    async def dispatch(packet):
        raise AssertionError("No model requested")

    await pi.run_pi(
        runtime_fixture,
        {"call_limit": 1, "tools": [{"name": "submit_reply"}]},
        dispatch,
        lambda: None,
    )
    assert "OPENAI_API_KEY" not in captured["env"] and "NODE_OPTIONS" not in captured["env"]
    assert captured["env"]["HOME"] == captured["cwd"]
    assert not Path(captured["cwd"]).exists()


@pytest.mark.parametrize("missing", ["stdin", "stdout"])
async def test_missing_control_pipe_fails_closed_and_reaps(runtime_fixture, monkeypatch, missing):
    process = ProcessFixture([])
    setattr(process, missing, None)

    async def create(*args, **kwargs):
        return process

    async def never(_):
        pytest.fail("A missing control pipe must not dispatch a model or tool call")

    monkeypatch.setattr(pi.asyncio, "create_subprocess_exec", create)
    with pytest.raises(DomainError) as caught:
        await pi.run_pi(runtime_fixture, {"call_limit": 1, "tools": []}, never, lambda: None)
    assert caught.value.code == "PI_PROTOCOL"
    assert process.returncode == -9
