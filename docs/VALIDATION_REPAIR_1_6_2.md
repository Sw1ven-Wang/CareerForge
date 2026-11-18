# 1.6.2 补丁验证与重现（2026-11-03）

本版是原 1.6.1 盲测后针对真实合同缺陷的独立补丁验证，不替换原盲测、P06 原失败、R08 回归或任何旧评分。机制说明见 `REPAIR_1_6_2.md`，完整结论见根目录 `DELIVERY_REPORT.md`。

## 终态分母

- 全量非生产：1016 收集，989 选中；976 通过、13 跳过、0 失败/错误、27 生产/live/sandbox 排除，900.49 秒。
- 历史独立工程：93 项，92 通过、1 个既有 xfail。该合成模型固定判定相关，不能证明真实相关性，xfail 不算通过。
- 新独立工程：287 项全部通过，37.50 秒。与主套件分别报告，不相加冒充去重覆盖。
- 独立可选运行补测：13/13，11 项真实已安装语法库、2 项 Pi SDK 的确定性模型。新隔离快照，既有依赖只读引用，无安装/付费调用；不抵扣主套件 13 个跳过或 27 个排除。
- Astra 聚焦实现测试 160 通过；新合同、偏好、版本配对和编辑动作家族另有独立审计，审计计数不累加为主套件。
- 静态检查：mypy 76 源文件无诊断，原冻结 Ruff/format 160 活动文件通过；最终测试副本仅版本断言修改后再核对 Ruff/format 的 src/tests/scripts 154 项通过。
- 最终 wheel 离线安装、CLI help、SQLite 初始化通过，77 个运行时/数据负载文件逐字节匹配源码。90 表、30 seed topics、0 publication。SHA-256：`7a64e0c348c6e42a107d6354e40abc22c99e4ec97be64e907e12a9e8d5995a59`。

## 保留的首轮失败与冻结边界

首轮全量为 975 通过、1 失败、13 跳过、27 排除，941.45 秒。唯一失败是 API 版本测试仍硬编码 1.6.1，而产品已为 1.6.2。最终副本只把该断言改为同时验证 pyproject、包版本和 OpenAPI 版本一致；没有删减其他 API 断言。独立 Astra 审核与 3 种错配负例均确认新断言更强。首轮源冻结、原始日志和 native12 输出不改写。

native12 与最终完整回归使用相同 77 个运行时/数据文件；159 项源码/测试/脚本/配置冻结唯一差异为 `tests/test_api.py`。交付工程载荷 176 项匹配最终副本，交付说明随后生成。没有因首轮测试元数据错误重新生成任何模型答案。

## 独立真实模型补丁验证

12 项输入与独立标准在生成前冻结：5 个计划偏好场景、7 个意图路由场景。配置为 6.1 sol/high 的真实文件桥通过实际 ModelGateway，12 请求、12 响应、12 收据；12/19 项目调用预算、0 重试、0 跳过。两类模板分别为计划 5、协调 7；真实 token、费用与后端身份认证未提供，不估造。

fresh 独立评分共 47/48，均分 3.9167/4：计划 3.8（n=5），路由 4（n=7）。P_exercise_only 得 3 分，因为只让学习者提出迁移变体，未给具体变体；其余 11 项为 4 分。12 原始响应通过相应 Schema，156 处引文精确匹配。P06 此次能形成合法提议，但新标准不同，不能把新分数替换旧分数。计划均未采纳或执行。

路由仅测到 Coordinator.decide，未证明下游 Tutor 回答或完整服务执行。R08 的新路线是 fresh 模型选择，原 server 路径不变，不能归因为代码收益。R_missing 保留已授权焦点元数据以防空列表扩大硬范围，仍按 missing/action 澄清；不能把该字段当已解析目标或把本次路由收据当端到端取源门禁实测。

## 重现

准备 Python 3.12 和项目依赖后，从项目根运行：

```bash
python -m pytest -q -m 'not production and not live and not sandbox'
CF_TEST_TREE="$PWD" python -m pytest -q validation/repair_1_6_1/independent_tests
python -m ruff check src tests scripts alembic deploy
python -m ruff format --check src tests scripts alembic deploy
python -m mypy src
python scripts/verify_wheel.py --output-dir validation/local/new-v162-wheel-check
python scripts/verify_integrity.py
```

新增 287 项独立套件含两个真实跨版本探针，不是解压项目后单目录即可重跑。请在一个全新、可丢弃的复现根目录恢复以下布局：`work/CareerForge_v1.6.0/` 放入证据包 frozen_1_6_1 中的实际 1.6.1 冻结工作树（目录名称沿用原测试路径），`work/CareerForge_v1.6.2/` 放入本交付项目，`patch_1_6_2/testing/engineering/` 放入证据包同路径的独立测试与 helper。随后从该根执行 `CF_TEST_TREE="$PWD/work/CareerForge_v1.6.2" python -m pytest -q patch_1_6_2/testing/engineering`。两个跨版本子进程因此读取真正旧版和新版，其余测试用明确 CF_TEST_TREE。请勿设置 CF_EVIDENCE_TAG，以免在冻结证据目录写新摘要；pytest 使用全新临时数据库，勿对生产库运行。其他历史探针可能另需原 1.6.0 副本，见相应原始命令。真实文件桥依赖有权限的模型执行器，不是可独立调用的生产 HTTP 提供商。完整请求/响应/收据、冻结标准、脚本和评分在独立验证证据 ZIP；不得用新的输出覆盖已冻结结果。

## 保留限制

没有验证真实职位导入至计划采纳、练习与进化晋升的完整业务闭环；没有 C 进化对照，没有晋升收益。当前没有可用 PostgreSQL/Redis 服务，Docker 受限且未绕过。未执行生产部署、远程 CI、负载/故障切换或真实学习效果测量。旧 1.6.1 补测曾覆盖前次两份 raw 日志，聚合及依赖证据仍在，未伪造恢复；本版补测输出独立。详细八阶段清单与其余限制见交付报告和 `KNOWN_GAPS.md`。
