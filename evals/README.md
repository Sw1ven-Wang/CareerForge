# S03 离线评测约定

当前只验证程序行为，不宣称模型效果达标。`runners/requirement_extraction.py` 对独立标注与输出按岗位快照、概念、职责/必须/优先/推断、显式性、可选条件组及 Unicode 支持区间严格匹配，分别报告 precision、recall 和非法引用数。重复输出降低 precision；没有预测或没有 gold 时对应比例保留 null。

标注须阅读完整 JD，保留否定、可选语言组、优先项和缺失字段；不能从规则输出反推 gold。数据按同一岗位发布组分割，避免重复 JD 跨开发/测试集。正式模型评测须双人独立标注、仲裁争议、记录失败类型，再冻结分组测试集和经校准的准确率门槛。当前三个百度 JD 仅是同一公司的开发样本，没有独立双人标注或有效留出集，不能报告真实模型准确率。

运行：`uv run --project backend python evals/runners/requirement_extraction.py fixture.json`。JSON 包含 `predicted`、`gold`、`texts`（快照 ID 到规范正文）。测试 `test_extraction_scoring.py` 用手写合成 gold 验证评分器能识别错类、重复和引用越界。

确定性统计验收见 `test_market_research.py`：输入 6、JD 4、完全重复合并后 3、面经 1、个人材料 1；Python 在两个岗位中出现，故为 2/3，而非按提及次数计数；北京组 2、上海组 1，多地区岗位允许属于多个地区组。这是独立手算的程序回归样本，不是提取质量评测。
