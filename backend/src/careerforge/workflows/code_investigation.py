"""Replayable bounded read-only decisions; the final review call is reserved by the caller."""

from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from careerforge.application.job_agent_service import strict_schema
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.code_index.investigation import (
    READ_LIMIT,
    TOOL_LIMIT,
    CodeInvestigation,
)
from careerforge.schemas.investigation import FinishArgs, QueryArgs, SpanArgs, TargetArgs, TreeArgs
from careerforge.schemas.model_io import ModelResult

Decide = Callable[[str, Json, list[Json]], Awaitable[ModelResult]]


def tools() -> list[Json]:
    specs: list[tuple[str, str, type[BaseModel]]] = [
        ("list_tree", "查看固定版本文件列表；目录和符号不是已读原文。", TreeArgs),
        ("find_symbol", "查询 Python 符号元数据；按 target_id 再读取才能引用。", QueryArgs),
        ("search_text", "按字面关键词检索正文候选，不生成可引用的源码依据。", QueryArgs),
        ("read_span", "读取已发现目标内至多70行/2600字符；可显式指定后部范围。", SpanArgs),
        ("references_of", "查静态同名调用者/被调实现候选，不证明真实调用关系。", TargetArgs),
        ("find_tests", "查相关测试候选；需 read_span 取得原文，测试不会执行。", TargetArgs),
        ("finish_investigation", "证据已足够或不支持目标时停止；该判断仍是模型提议。", FinishArgs),
    ]
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": strict_schema(schema.model_json_schema()),
            },
        }
        for name, description, schema in specs
    ]


async def investigate(
    files: dict[str, str], requirements: list[Json], call_limit: int, decide: Decide
) -> Json:
    if not 1 <= call_limit <= 3:
        raise ValueError("Review budget must include one final call, and be at most three")
    session = CodeInvestigation(files, requirements)
    session.bootstrap()
    for number in range(call_limit - 1):
        if session.tool_count >= TOOL_LIMIT:
            session.stop("tool_budget")
            break
        if len(session.reads) >= READ_LIMIT:
            session.stop("read_budget")
            break
        before = (len(session.discovered), len(session.reads))
        response = await decide(
            f"investigation:{number}",
            {
                "requirements": requirements,
                "investigation": session.context(),
                "phase": "investigate",
                "instruction": "只选择只读工具，每轮最多3个；需补读实现和测试、寻找反证。"
                "摘要不可引用。"
                "足够时调用finish_investigation；最后一次调用留给审核报告。",
            },
            tools(),
        )
        session.decisions += 1
        if len(response.tool_calls) > 3:
            raise BusinessError(422, "INVESTIGATION_BATCH_LIMIT", "单轮调查工具数超过3个，未执行。")
        if not response.tool_calls:
            session.stop("evidence_sufficient", "模型结束调查；不等于语义支持已核验。")
        for tool in response.tool_calls:
            session.tool(tool.name, tool.arguments)
            if session.stop_reason:
                break
        if session.stop_reason:
            break
        if before == (len(session.discovered), len(session.reads)):
            session.stop("no_new_evidence", "本轮未发现新候选或读取新原文。")
            break
    if not session.stop_reason:
        session.stop(
            "tool_budget"
            if session.tool_count >= TOOL_LIMIT
            else "read_budget"
            if len(session.reads) >= READ_LIMIT
            else "model_budget"
        )
    value = session.result()
    value["investigation"]["model_call_limit"] = call_limit
    value["investigation"]["final_call_reserved"] = True
    return value
