"""CLI interruption/replay and terminal/config boundaries; no external providers."""

import json
import stat
from contextlib import contextmanager
from io import StringIO

import httpx
import pytest
import typer
from rich.console import Console

from careerforge.cli import main
from careerforge.cli.stream import StreamProtocolError, parse_events


def frame(sequence, event, value):
    return [f"id: {sequence}", f"event: {event}", "data: " + json.dumps(value), ""]


def mock_streams(monkeypatch, streams):
    requested = []
    saved = []
    output = StringIO()
    monkeypatch.setattr(main, "console", Console(file=output, color_system=None, width=160))
    monkeypatch.setattr(main, "save_config", lambda state: saved.append(dict(state)))
    monkeypatch.setattr(main.time, "sleep", lambda _: None)

    class Response:
        is_success = True

        def __init__(self, lines):
            self.lines = lines

        def iter_lines(self):
            for line in self.lines:
                if isinstance(line, Exception):
                    raise line
                yield line

    class Client:
        @contextmanager
        def stream(self, method, path, *, params, timeout):
            requested.append((method, path, dict(params)))
            yield Response(streams[len(requested) - 1])

    @contextmanager
    def client():
        yield Client()

    monkeypatch.setattr(main, "client", client)
    return requested, saved, output


def test_eof_and_replay_do_not_duplicate_or_acknowledge_partial_delta(monkeypatch):
    streams = [
        frame(1, "answer.delta", {"text": "A"}) + frame(2, "answer.delta", {"text": "B"})[:-1],
        frame(1, "answer.delta", {"text": "A"})
        + frame(2, "answer.delta", {"text": "B"})
        + frame(3, "answer.completed", {"turn_id": "t"}),
    ]
    requests, saved, output = mock_streams(monkeypatch, streams)
    main.stream_turn("t", {})
    assert output.getvalue() == "AB\n"
    assert [x[2]["after"] for x in requests] == [0, 1]
    assert all(x[0] == "GET" for x in requests)
    assert [x["last_event_id"] for x in saved] == [1, 2, 3]
    assert saved[-1]["last_turn_status"] == "completed"


def test_network_disconnect_reconnects_same_turn_only(monkeypatch):
    requests, saved, output = mock_streams(
        monkeypatch,
        [
            frame(7, "answer.delta", {"text": "restored"}) + [httpx.ReadError("lost")],
            frame(8, "answer.completed", {"turn_id": "t"}),
        ],
    )
    main.stream_turn("t", {}, start=6)
    assert [x[2]["after"] for x in requests] == [6, 7]
    assert "restored" in output.getvalue()


def test_truncated_stream_is_failure_not_success(monkeypatch):
    requests, saved, output = mock_streams(monkeypatch, [[], [], []])
    with pytest.raises(typer.Exit) as raised:
        main.stream_turn("t", {})
    assert raised.value.exit_code == 1
    assert len(requests) == 3 and not saved
    assert "resume" in output.getvalue()


def test_interrupted_answer_is_nonzero_and_persists_cursor(monkeypatch):
    requests, saved, _ = mock_streams(
        monkeypatch, [frame(2, "answer.interrupted", {"code": "CANCELLED"})]
    )
    with pytest.raises(typer.Exit) as raised:
        main.stream_turn("t", {})
    assert raised.value.exit_code == 1 and len(requests) == 1
    assert saved[-1]["last_turn_status"] == "interrupted"


@pytest.mark.parametrize(
    "lines",
    [
        ["id: -1", "data: {}", ""],
        ["id: 0", "data: {}", ""],
        ["id: １", "data: {}", ""],
        ["data: {}", ""],
        ["id: 1", "data: []", ""],
        ["id: 1", "data: {", ""],
    ],
)
def test_invalid_event_contract_fails_closed(lines):
    with pytest.raises(StreamProtocolError):
        list(parse_events(lines))


def test_bounded_frame_and_multiline_json():
    with pytest.raises(StreamProtocolError):
        list(parse_events(["data: " + "x" * 20], max_characters=10))
    events = list(
        parse_events(
            [": heartbeat", "", "id: 1", "event: status", "data: {", 'data: "ok": true}', ""]
        )
    )
    assert events[0].payload == {"ok": True}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ok\x1b]52;c;YQ==\x07end", "okend"),
        ("ok\x9d52;c;YQ==\x9cend", "okend"),
        ("\x1b[31mred\x1b[0m", "red"),
        ("\x9b31mred\x9b0m", "red"),
        ("safe\u202eevil", "safeevil"),
        ("中文\n\tOK\rBAD", "中文\n\tOKBAD"),
        ("\x1bPcontrol\x1b\\text", "text"),
    ],
)
def test_terminal_controls_are_not_rendered(text, expected):
    assert main.clean(text) == expected


def test_config_atomic_write_does_not_follow_predictable_temp_symlink(tmp_path, monkeypatch):
    path = tmp_path / "client.json"
    target = tmp_path / "unrelated"
    target.write_text("preserved")
    (tmp_path / "client.tmp").symlink_to(target)
    monkeypatch.setattr(main, "config_path", lambda: path)
    main.save_config({"token": "private-token"})
    assert target.read_text() == "preserved"
    assert json.loads(path.read_text()) == {"token": "private-token"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".client-*.tmp"))


def test_resume_uses_saved_cursor_without_posting(monkeypatch):
    requests, _, _ = mock_streams(monkeypatch, [frame(5, "answer.completed", {"turn_id": "t"})])
    monkeypatch.setattr(main, "config", lambda: {"last_turn_id": "t", "last_event_id": 4})
    main.resume()
    assert requests == [("GET", "/api/v1/turns/t/events", {"after": 4})]
