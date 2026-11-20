"""Product capabilities and conservative, read-only intake guidance."""

import re

CAPABILITIES = {
    "supported": [
        "保存明确指定的 GitHub 仓库，再确认范围和额度后构建知识",
        "固定源码版本，定向提取、独立审核，通过后发布知识",
        "基于已审核知识生成计划、讲解、练习和评估，提供源码引用",
        "知识不足时按原问题补充；读取实际状态并在后台任务期间聊天",
    ],
    "unsupported": [
        "仅凭聊天中的本地目录或压缩包路径读取用户电脑文件",
        "代替用户修改项目代码、执行任意命令或部署上线",
        "保证整个仓库所有知识一次入库或保证用户已经掌握",
    ],
    "rules": [
        "保存地址不是知识入库；提交任务不是执行完成；仅审核通过并发布的材料可称为入库",
        "材料不足先按缺口补充，不等于项目没有该功能；失败和未查到也不能证明不存在",
        "缺少信息只问必要项，已有信息不要重复索要；未指定目标可建议架构和核心流程",
        "无法执行时说明具体限制，并用：暂无法完成……，请提供……，我会帮你……",
        "只承诺受支持的下一步；不得要求用户发送密码或令牌",
    ],
}

HELP_REPLY = (
    "我可以帮你指定项目、提取并审核知识、制定学习计划、讲解源码、出题和评估，"
    "也可以随时报告后台进度。请提供 GitHub 仓库地址和想学习的内容；"
    "如果没有明确目标，可以先从项目架构和核心流程开始。"
    "保存地址后还需要确认构建范围和额度，审核通过并发布后才算知识入库。"
)


def intake_reply(text, status):
    """Match complete requests, never keywords embedded in a lesson or JD."""
    phrase = text.strip().rstrip("。！!？?")
    if re.fullmatch(
        r"(?:请|帮我)?(?:把)?(?:这个|该)?项目(?:导入|入库)|(?:请|帮我)?(?:导入|入库)(?:这个|该)?项目",
        phrase,
    ):
        if str(status.get("target") or "").startswith("https://github.com/"):
            return (
                f"当前已指定项目：{status['target']}。无需重复提供地址。"
                "请确认学习范围及构建额度，再发送「确认目标」，我会帮你启动源码提取和审核。"
                "保存地址不代表已完成知识入库；如需查看已启动的任务，请发送「查询进度」。"
            )
        return (
            "请提供 GitHub 仓库地址和想学习的模块；没有明确目标时，可以先选项目架构和核心流程。"
            "我会帮你准备知识构建，确认范围和额度后再提取、审核和发布。"
        )
    if re.fullmatch(r"(?:请|帮我)?(?:导入|读取|分析|学习)\s*[A-Za-z]:[\\/][\s\S]+", phrase):
        return (
            "暂无法仅凭聊天中的本地路径读取你的项目。请提供可访问的 GitHub 仓库地址，"
            "我会帮你准备源码提取与知识审核；请不要发送密码或访问令牌。"
        )
    if re.fullmatch(
        r"(?:请|帮我)(?:把这个项目)?(?:部署|上线|执行命令|修改代码|删除文件)[\s\S]*", phrase
    ):
        return (
            "暂无法代你修改文件、执行命令或部署项目。请提供具体问题和相关代码，"
            "我会帮你解释实现、梳理排查步骤或学习方法。"
        )
    return None


def blocked_reply(code, fallback):
    guidance = {
        "WORKFLOW_REQUIRED": ("执行学习操作", "GitHub 仓库地址或学习主题", "建立学习方向"),
        "GOAL_REQUIRED": ("确定本次学习目标", "学习主题或 JD 原文", "整理学习目标"),
        "REPOSITORY_REQUIRED": ("定位指定项目", "明确的 GitHub 仓库地址", "准备项目知识构建"),
    }
    if code not in guidance:
        return fallback
    action, required, next_step = guidance[code]
    return f"暂无法{action}。请提供{required}，我会帮你{next_step}。"
