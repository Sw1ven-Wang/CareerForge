"""Bundled learning procedures. Data only: no permission grants or external imports."""

from careerforge.domain.json_types import JsonObject as Json

SKILLS: dict[str, Json] = {
    "trace_implementation": {
        "version": "1",
        "title": "沿实现补证据",
        "instructions": [
            "先列当前模块要解释的机制与未知，再检索定义并读取原文。",
            "沿静态相关候选补读被调实现和状态处理；同名不等于确定调用。",
            "需要后部源码时指定 read_code 的行范围；读取上限不变。",
            "每个源码结论引用本轮实际读取的位置；历史回答只是待核对线索。",
        ],
    },
    "failure_boundary": {
        "version": "1",
        "title": "分析失败边界",
        "instructions": [
            "先确定输入、状态与失败条件，查验实现分支与相关测试。",
            "明确哪些行为仅静态可见，哪些需要用户确认命令后才能观察。",
            "区分反例、缺少证据和已确认失败；不能把测试源码当运行成功。",
        ],
    },
    "practice_evidence": {
        "version": "1",
        "title": "围绕练习证据解释",
        "instructions": [
            "补读本模块既往提交与人工观察，保留其练习版本、帮助模式和评阅来源。",
            "未满足或未知条件可用于设计后续问题，不自动诊断用户不会。",
            "允许辅助、本人声明独立和能力掌握分开；同账号自验不是第三方认证。",
        ],
    },
}
