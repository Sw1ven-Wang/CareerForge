# 1.6.2 验证入口

最终结果、重现命令和未执行边界见 [1.6.2 验证报告](VALIDATION_REPAIR_1_6_2.md)、根目录 `DELIVERY_REPORT.md` 和 `RELEASE_STATUS.json`。

- `validation/repair_1_6_2/`：本次跨字段合同和多动作检测补丁验证，包括首轮版本断言失败与仅修正测试后的独立全量重跑。
- `validation/repair_1_6_1/`：原修复版工程证据；[1.6.1 验证报告](VALIDATION_REPAIR_1_6_1.md) 的原 A/B 分数、P06 失败及 R08 回归不改写。
- 其余既有验证目录属于更早版本。原 1.6.0 结果见 [历史验证记录](history/v1.6.0/VALIDATION.md)。本次没有重新执行 PostgreSQL/Redis 或 Docker，不能沿用历史结果宣称本版基础设施通过。

12 项补丁模型重测是已知失败/后盲测验证，不能替代原 18 项持出盲测。报告逐阶段区分真实执行、确定性集成与缺失步骤，未宣称完整业务闭环或真实学习收益。
