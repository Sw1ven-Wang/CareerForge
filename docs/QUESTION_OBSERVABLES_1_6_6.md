# 1.6.6 原题可观察项合同（显式选择）

`POST /api/v1/learning/plan-proposals` 可显式提交 `task_scope: "question_observables"`。
不传或传 `null` 仍走原合同；空值不进入历史请求的幂等指纹。单次会话计划暂不支持此模式。

## 范围

新规划模型合同为 `learning-planner-question-observables-1.6.6`，草稿、任务、活动、评价各有独立版本。
原来的无版本和 1.6.2 规划系统提示/模型 schema，以及原 1.6.6 评价系统提示/schema 保持原字节。
已经排队或已保存的旧计划、活动和模型回执不迁移、不改题、不重新标记结果。

新任务必须引用已授权请求目录中的原问题 `question` 和各项 `observable_identity`。
身份文字是引文数据，不是系统指令；仍经过原有引用、注入、来源和权限检查。
静态 `targets` 只可选本模块焦点中的已审核事实或事实条件。请求文字和来源限制不能替代源码事实。
只支持 diagnostic、understanding_check 和 exercise；相应关闭偏好照常生效。
不新增假设场景或迁移任务，步骤只包含阅读已审核事实。题目/操作选择仍是待用户采纳的计划提议，
精确引用和类型检查本身不证明模型选题及操作映射语义正确。

四种封闭操作：

- `state_condition`：判断原题给定情境对所引条件是否成立。
- `state_result`：判断给定情境在所引源码处的返回值或异常。
- `identify_members`：指出所引集合/类别允许的成员，不能由另一项正确结果代替。
- `verify_runtime_receipt`：仅根据服务端真实执行回执评价运行观察；只允许 exercise。

每个原始 observable 对应一个独立 rubric ID，即使多个项都属于 conditions 也不合并。
维度名不再追加通用训练要求，活动不附加原题未要求的全路径、全边界、调用次数、状态变化或未运行声明。
原题明确要求的解释和源码条件仍然有效；源代码冲突、不足或未知仍需保留未知，不能凭简短答案自动通过。
更宽的能力范围需要新提议，不从窄题结论推断。
创建活动时必须传入与已采纳任务一致的 `kind`，并显式传入该题所选维度的去重 `rubric_keys`；
旧活动请求的默认维度不变，不能把它们自动添加到窄题。多个同维度原项仍各自生成独立 rubric ID。

原题范围会在活动创建和后续读取时，重新对照不可变提议投影核验。后续计划可重排现有模块，
但现有四种通用调整模板（包括会添加完整追踪的 `smaller_steps`）不能应用到选中的窄题模块；
需要另建明确的新提议。改写步骤、原题、逐项检查或显示 rubric 后，即使重新计算内容哈希也会被拒绝。
同一目录中重复的 publication/fact 身份会拒绝处理，不能用后出现的同名项覆盖已有来源。

## 评价与证据

新评价版本 `model-assessment-question-observables-1.6.6` 仍返回逐项 demonstrated / not_yet / unknown。
判断由模型提出，不在产品中硬编码答案、不将 not_yet 重写成 demonstrated。
非 unknown 必须逐字引用当前冻结提交；肯定性静态项必须引用该项 targets 对应的当前事实 ID。
既有引用、ACL、冻结内容、帮助条件和运行回执链路检查保留。
评价额外核验原题 prompt、每项显示 criterion、独立 rubric ID 与具体检查一致，不能借显示层新增要求或合并身份。

窄项 demonstrated 只是该项候选结论：`full_trace_verified=false`、`requires_review=true`、
`semantic_correctness_independently_verified=false`、`competence_verified=false`。
不会生成真人观察、更新掌握状态或发布策略。帮助可伴随静态正确，但 assisted 仍为 assisted，
none_declared 仍仅是本人声明。没有真实执行回执时 runtime 保持 unknown，正确声明没运行也不是运行通过。

## 公开 C09 原题保真复验

`tests/public_exercise_support.py` 保留原映射，并只在明确 opt-in 时使用新操作。
`tests/fixtures/public_c09/` 仅包含用户提供的公开 agent_input v1 中必要的四份文件：
原题八条、练习声明和两个合成源码文件。`PROVENANCE.json` 记录原文件 SHA-256 和字节数，
读取前逐一校验；原问题、原提交、原 help、大小写、observable 身份和源码均未改写。
不包含 Gold、holdout、期望评分答案、原生响应或外部工作目录依赖。

`no_runtime_fabrication` 仍保守映射到运行回执观察，不是积极的诚实评分，缺回执时 unknown。
source/review/plan 是明确标注的合成前置夹具；包内便携测试只构造计划投影、验证保真，
不调用评价模型、不执行合成源码、不生成真人运行回执，不把服务成功计为语义通过。

在源码根目录使用新的输出目录运行：

```sh
python scripts/run_tests.py --output-dir validation/c09-source-fidelity \
  --label portable tests/test_public_question_adapter_v166.py
```

真实原题评价的输入、输出与独立审查在交付验证材料中另行报告，不由上述离线测试重建。
历史失败、unknown 与尚未评测项仍需保留，不能用机械断言数量代替语义正确率。
