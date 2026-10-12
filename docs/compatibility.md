# S00 兼容性记录

2026-10-12。实测 Python 3.12.14、Node 24.12.0、uv 0.12.5、Docker 29.8.1。精确 Python 依赖见 backend/uv.lock，环境清单见 progress/S00_ENVIRONMENT.json。

| 组合 | 实际结果 |
| --- | --- |
| PostgreSQL 17.11 + psycopg 3.3.6 + SQLAlchemy 2.1.4 | 实际连接与 SELECT 通过 |
| Redis 7 容器 + redis-py 6.4.0 | PING/SET/GET/DELETE 通过 |
| LangGraph 1.2.14 + PostgreSQL saver 3.1.2 | setup、图运行、关闭连接后重新读 checkpoint 通过 |
| 模型契约 | 26 项离线测试通过；真实供应商待验，按用户要求无模型调用 |
| Celery 5.6.3 | 已锁定安装；Linux Worker 联通在 S02 验证 |
| Alembic 1.20.0 | 已安装；实际业务迁移从 S01 验证 |

Windows localhost PostgreSQL 连接在一次测试中等待过久并被中断；使用 127.0.0.1 和 connect_timeout=5 后通过。所有开发文档已采用 IPv4。原生 Windows Celery 不作为验收环境。
