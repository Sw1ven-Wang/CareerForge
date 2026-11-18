> 封存的1.6.1验证结果，不是当前1.6.2的替代成绩。当前补丁结果见VALIDATION_REPAIR_1_6_2.md与根报告。

# 1.6.1 验证与重现（2026-11-03）

## 最终工程结果

| 验证 | 分母与结果 | 边界 |
|---|---|---|
| 最终全量非生产用例 | 收集 856，选中 829；816 通过、13 跳过、0 失败/错误；27 排除 | 原环境缺少可选运行依赖；生产标记用例未执行 |
| 独立工程用例 | 93 项：92 通过、1 xfail、0 失败/错误 | xfail 不计通过：合成 FixtureModel 固定判定相关，不能衡量语义相关性 |
| Astra 机制审核 | 54/54；23 个审核文件前后哈希相同 | 属于上述 93 项的子集，不重复累计 |
| 可选真实运行补测 | 独立环境 13/13：11 语法库、2 已安装 Pi SDK | SDK 使用确定性模型，不是付费模型质量测试；不改写全量跳过 |
| 静态检查 | mypy 75 源文件；Ruff/format 157 活动文件；Node 语法 | 默认类型检查，不是 strict 全证明；历史验证脚本有 7 项既有诊断，未改写旧证据 |
| Wheel | 离线安装、CLI help、SQLite 初始化通过；76 个包负载文件与源码逐一一致 | 使用宿主已有依赖；90 张表、30 个 seed topic、0 个 publication |

最终全量耗时 814.99 秒，独立用例 155.00 秒。测试前后 211 个源码/配置/测试/文档文件完全一致。交付报告文档随后生成，交付副本的 173 个非报告文件与最终测试快照逐一核对，运行代码没有再改。当前 wheel SHA-256 为 `784eb7afae62d27f49351be7b6d26ed21d25df3120f059d8375616eeb5e46a1c`。哈希是完整性记录，不是数字签名。

证据位于 `validation/repair_1_6_1/`。真实旧版本创建的 tutor/reflection 收据及完整 tutor/checker 对，在新代码中保持原哈希、原输出且不新增模型调用。旧的不安全未完成上下文及由缓存旧段落衍生的 checker 请求，在新增预约/扣费/外发前拒绝。

## 原始与中间结果

原 1.6.0 ZIP SHA-256：`e4818e2b7a056647ff4f1eca657fb4b8a16675f014b5742c3557500fe3ff0c1c`。原项目及 1106 个原始评测证据文件重新校验，没有变动。

本轮第一次冻结全量为 769 通过、2 失败、13 跳过、27 排除；第二次为 796 通过、1 失败、13 跳过、27 排除。这些失败与修正说明保留在独立证据包，不计为最终通过。第二次的耗时门禁失败来自真实观测比值超过既有阈值，底层耗时异常原因未确定；测试只为缺费用合同采用局部确定性时钟，并另加慢候选控制，没有放宽生产阈值。

补测有一项证据管理缺口：final3 的复制 runner 覆盖了前次 final2 的 raw pytest log 和 JUnit，未找到可恢复副本。前次聚合、源哈希和依赖证据保留，不伪造恢复；最终版补测的 raw/JUnit 单独保存并作为权威依据。该缺口不涉及原基线的 1106 个冻结文件。

## 工程重现

在已准备 Python 3.12 及项目依赖的隔离环境，从项目根执行：

```bash
python -m pytest -q -m 'not production and not live and not sandbox'
CF_TEST_TREE="$PWD" python -m pytest -q validation/repair_1_6_1/independent_tests
python -m ruff check src tests scripts alembic deploy
python -m ruff format --check src tests scripts alembic deploy
python -m mypy src
node --check coding-agent/runner.mjs
python scripts/verify_wheel.py --output-dir validation/local/new-wheel-check
python scripts/verify_integrity.py
```

选择全新输出目录，避免覆盖证据。`astra_legacy_*_probe.py` 跨版本脚本另需原 1.6.0 源码和全新可丢弃数据库；不要使用生产数据库或把测试授权导入真实账号。

可选 SDK 安装应使用包内已修正的 lock。修正仅将不存在的 base64-js 1.6.0 版本/URL 对齐至符合上游范围、且与原 integrity 完全一致的 1.5.1；其余依赖图和 integrity 未改。最终隔离环境中的 120 个 npm 包版本全部匹配锁文件。

## 真实模型质量与进化

结果与分母见根目录 `DELIVERY_REPORT.md` 和独立报告、证据包。新 18 个持出案例及金标先冻结再生成；原 24 个案例已经公开，仅选取 8 个做已知回归，其余 16 个不宣称本轮重跑。A 原版与 B 修复版使用固定配置；C 只允许同代码下真实候选经授权与产品门禁晋升，不允许手工覆盖或挑选最好结果。

推理使用配置为 6.1 sol/high 的原生文件桥进入实际 ModelGateway。请求、响应、回执与评分可审查，但原生后端身份、token 和账单费用未独立提供；不宣称生产 HTTP 集成通过，也不把请求计数降幅当成已测账单节省。文件桥的跨机重现限制随评测源码提供。

## 未验证边界

本轮未执行 PostgreSQL/pgvector、Redis/Celery 生产服务或 Docker 容器。当前工作空间没有可用原生服务，Docker 受限且未绕过；原版本基础设施证据留档，不冒充本轮结果。没有完整 statement coverage、负载/故障切换或真实学习者收益测量。范围与剩余产品差异见 `KNOWN_GAPS.md`。
