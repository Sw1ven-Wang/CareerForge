# 来源与兼容说明

## 1. 实现来源

本包以用户提供的两份 CareerForge v1.5 文档及其引用的 v1.2.1 技术合同为目标。它是本次生成的独立实现，不是完整原仓库检出后的修改集。

通过已连接的 GitHub 工具核对过的历史基线为：

`Sw1ven-Wang/CareerForge@90e32f187ba44f9273cd3e2c7c12af2aeaba5e93`（2026-10-19）。

本环境不能直接取得完整 Git checkout；GitHub 工具的 UTF-8 文件读取不提供完整仓库 ZIP。原 420 个文件及原测试套件未复制，原 Vue/Tauri 界面也未包含。不能将此包当作原仓库的完整历史或增量补丁。

## 2. 保留的第三方/原项目来源

`coding-agent/runner.mjs` 基于上述固定提交中的 Pi JSONL 运行器调整；原 blob SHA 为 `f3b5d2d8e72d68bd1d90a042cdca061e2baa0f38`。保留原项目 MIT License（Copyright 2026 Sw1ven-Wang）。本次调整连接新的 Python 模型网关、受限工具合同和计量接口。

Pi SDK 通过 `@earendil-works/pi-coding-agent` 和 `@earendil-works/pi-ai` 的 1.3.0 声明依赖使用，SDK 源码及 node_modules 未打入本包。Docker、PostgreSQL、Redis、Tree-sitter 等也未捆绑二进制。

Research 的问题树、独立 Review、评测后晋升是本包实现的机制；没有将 GPT Researcher、DeerFlow、DeepWiki-Open、ACE、GEPA 或 SkillOpt 安装后冒称已直接集成。

## 3. 不能原地升级

必须使用新虚拟环境、新数据库 `careerforge_v15` 和独立数据目录。不要将本包覆盖在原项目或把 `CAREERFORGE_DATABASE_URL` 指向原数据库。

Alembic 与 bootstrap 均检查 `users / study_tracks / study_artifacts` 等原库标记，遇到即拒绝。该保护不是已经完成旧库迁移；旧知识不能自动获得新 Review/FactAtom 资格。

## 4. 版本含义

Python 包号 `1.5.0` 表示本次对应的规格目标，不表示产品全部需求已完成或 production ready。权威交付状态见 `RELEASE_STATUS.json`、`DELIVERY_REPORT.md` 和 `docs/KNOWN_GAPS.md`。
