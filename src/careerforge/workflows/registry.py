from ..errors import DomainError

GRAPH_VERSION = "jd-extract-v1"
STATE_SCHEMA_VERSION = 1
GRAPH_BINDINGS = {
    ("evolution", "evolution-candidate-v1", 1),
    ("evolution_replay", "evolution-replay-v1", 1),
    ("jd_extract", GRAPH_VERSION, STATE_SCHEMA_VERSION),
    ("knowledge_build", "knowledge-build-v1", 1),
    ("chat", "learning-tutor-v1", 1),
    ("learning_assessment", "learning-assessment-v1", 1),
}
DEFAULT_GRAPH_VERSIONS = {"jd_extract": GRAPH_VERSION, "knowledge_build": "knowledge-build-v1"}


def validate_binding(run) -> None:
    if (
        run.workflow_engine != "langgraph"
        or (run.workflow, run.graph_version, run.state_schema_version) not in GRAPH_BINDINGS
    ):
        raise DomainError("GRAPH_VERSION", "冻结的 Graph 或 State Schema 版本未注册。", 409)


SESSION_GRAPH_BINDINGS = {
    ("evolution_session", "langgraph", "evolution-session-v1", 1),
    ("learning_session", "langgraph", "learning-session-v1", 1),
    ("learning_session", "langgraph", "learning-session-practice-v1", 1),
    ("knowledge_session", "langgraph", "knowledge-session-v1", 1),
    ("knowledge_session", "langgraph", "knowledge-session-v2", 1),
}


def validate_session_binding(execution) -> None:
    if (
        execution.workflow_type,
        execution.workflow_engine,
        execution.graph_version,
        execution.state_schema_version,
    ) not in SESSION_GRAPH_BINDINGS:
        raise DomainError("GRAPH_VERSION", "冻结的会话 Graph 或 State Schema 版本未注册。", 409)
