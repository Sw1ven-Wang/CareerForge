from typing import TypedDict


class CareerForgeState(TypedDict):
    """Reference-only checkpoint. Never persist JD text, prompts or model results."""

    workflow_run_id: str
    graph_version: str
    state_schema_version: int
    current_stage: str
    result_stage_key: str
