# 公开岗位采集与可复用路径

2026-10-12，版本 job-collection-1.0 / public-jobs-1.0。真实正文写入本机演示账号，个人材料 joblearn 未外传。

## 页面与执行

/sources → 查询 / 采集岗位。关键词、渠道、详情链接、岗位分类和数量可手动填写。手动模式有详情链接时优先读取这些链接；否则查询所选渠道，再读取发现的候选详情。Agent 模式加载内置 Skill，通过 search_jobs/read_job 选择渠道、查询词和下一步详情，不是把固定关键词脚本伪装成 Agent。可在页面查看 Skill。

Agent 使用本地模型配置，low、每次最多 2048 输出 token，每轮最多 3 次；默认不开启，页面要求本轮明确勾选。网络请求默认 30 次（最多 50），岗位最多 12，计数跨恢复持久化。模型不自动重试，网络超时不自动重试。Token/次数限制不是金额限额。

跨站关键词发现需要 CAREERFORGE_TAVILY_API_KEY 和本轮搜索开关；当前没有配置，未做真实付费搜索。搜索摘要仅用于发现链接，不进入 JD 正文或统计分母。没有搜索服务时，腾讯可直接查关键词，其他渠道给出浏览器入口，用户查询后粘贴详情链接。Agent 能力受已实现的工具和当前访问条件约束，不能凭 Skill 自动拥有登录或动态浏览器能力。

## 已观察并复用的路径

| 渠道 | 路径 / 解析方法 | 当前边界 |
| --- | --- | --- |
| BOSS直聘 | /web/geek/job?query=…；正常浏览器入口 | robots 禁止自动查询路径；未绕过验证，未取得正文 |
| 牛客 | /jobs/detail/{id}；window.__INITIAL_STATE__ 的 store.jobDetail.detail，ext.infos/requirements | 已实采 3 份；关键词列表是动态页，未声称有通用公开查询 API |
| 猎聘 | /job/{id}.shtml；JobPosting JSON-LD | 已实采 1 份；另一链接无有效 JSON-LD，返回浏览器协助；查询参数路径受 robots 限制 |
| 腾讯 | /tencentcareer/api/post/Query?keyword=… → jobdesc.html?postId=… → /tencentcareer/api/post/ByPostId | 已实采 2 份；支持公开关键词查询 |
| 百度 | /jobs/detail/{类型}/{UUID}；SSR 工作职责 / 职责要求 | 已实采 2 份；不执行页面脚本，不猜测隐藏接口 |
| 字节、阿里 | 官网导航 → 候选单岗位链接 → 通用 JSON-LD 解析 | 本轮首页/列表为动态壳，无可验证正文；需浏览器协助或后续适配 |
| 美团、京东、网易、快手 | 已登记招聘导航域名；使用官网 Skill 指引发现路径 | 尚未验证单岗位自动采集，不能将导航登记算作适配完成 |

初次实采运行 6eed3b14-3ccd-462b-aa44-cc9f300734aa：12 条输入，19 次匿名 HTTP 请求，8 份完整正文入库，4 条明确限制，状态 partially_succeeded，模型调用 0。牛客部分原页显示已结束，保留 closed_observed，不能宣称 8 份均正在招聘。随后 11b283ca-4fa1-4573-8b1f-089221271f1c 对百度两份原文保存更新版本（3 次 HTTP、0 模型）；45b705b5-1977-4e3d-b7ff-977fa3b3a621 实际查询 AgentRuntime 并采集一份资源运营岗位（4 次 HTTP、0 模型），已另放入“Agent 生态 / 运营”，不混同开发岗。共 9 份不同 URL 的真实正文；后续查询/更新收据保存在 Git 忽略的 private/collection/。

## 模块结构与扩展

- infrastructure/collection/portals.py：明确登记渠道、HTTPS 域名与浏览器入口。新增域名先核对官方归属。
- network.py：匿名请求、robots、公开地址检查、同源重定向检查、1MB 响应限制、每主机间隔、401/403/429 停止及持久化请求预留。
- extractors.py：各站结构解析，未获得正文就返回限制；绝不执行网页脚本。
- tools.py：手动与 Agent 共用 search_jobs/read_job。腾讯直接查询，其他渠道可选 Tavily 域名限定发现。
- workflows/job_collection.py：Python 显式工具循环、模型调用收据、阶段产物、取消/租约及重复投递防护；只读取用户给出或工具发现的链接。
- application/collection_service.py：原始响应与标准化数据同事务保存，按岗位 → 来源 → 具体岗位归档。已有 URL 可选择复用（明确标记本轮未重新获取）或新版本采集。
- data/skills/official-recruitment/SKILL.md：官网域名、路径发现、方向辨别和停止条件；应用运行时实际加载并冻结 Skill 哈希。

新增渠道时先保存匿名公开响应样本，确认正文、来源及限制，再扩展精确域名和解析器。页面结构变化时升级适配器并留存旧原文。登录或验证码交给用户正常浏览后手动导入。

## 证据与标准化

保存原始 HTML/JSON 字节、SHA256、来源链接、有效详情地址、采集时间、适配器版本、逐岗位请求路径、HTTP 状态与观察到的结束状态；无时区更新值保留为原始元数据，不冒充发布时间。正文进入现有 V1.1 JobPostingSnapshot、要求候选及引用链路，要求仍须人工确认。采集不会自动采纳计划或宣称个人能力满足岗位。

接口：GET /api/v1/collection/config、GET /skill、POST /runs、GET /runs、GET /runs/{id}（均以 /api/v1/collection 为前缀）。创建需要登录、CSRF 和 Idempotency-Key；用户只能读取自己的运行和来源。

## 必要验证

新增离线采集测试 3 项和既有标准化契约 2 项均通过；真实模型使用 MockTransport 替代，真实付费模型/跨站搜索均未再次调用。前端类型检查和构建通过，Skill 格式校验通过。已通过 Linux Celery 实际执行上述匿名公开采集。本轮未进行全量回归。
