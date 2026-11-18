import pytest

from careerforge.contracts import TaskType
from careerforge.services.learning_intent import SCOPED_QUESTION_ROUTING_CONTRACT
from careerforge.services.orchestration import ROUTING_CONTRACT_VERSION, rule_intent


@pytest.mark.parametrize(
    "question",
    [
        "根据已发布知识解释：工具 guardrail 的 reject_content 为什么不等于继续执行工具？",
        "根据已发布知识解释：PII 脱敏能保证不存在敏感信息外发吗？",
        "解释 owned/repo 的过滤条件为什么不等于鉴权？",
    ],
)
def test_factual_negation_in_an_explanation_routes_to_retrieval(question):
    decision = rule_intent(question, {}, routing_contract_version=SCOPED_QUESTION_ROUTING_CONTRACT)
    assert decision.interpreted_intent == TaskType.IMPLEMENTATION
    assert decision.proposed_next_action == "retrieve_and_tutor"
    assert rule_intent(question, {}, routing_contract_version=ROUTING_CONTRACT_VERSION) is None


@pytest.mark.parametrize(
    "question",
    [
        "不要解释 PII 脱敏",
        "解释 PII 脱敏，然后发布项目",
        "解释 PII 脱敏，并删除数据库",
        "explain guardrails; then edit the code",
    ],
)
def test_explanation_does_not_hide_a_negated_or_additional_action(question):
    decision = rule_intent(question, {}, routing_contract_version=SCOPED_QUESTION_ROUTING_CONTRACT)
    assert decision is None or decision.proposed_next_action != "retrieve_and_tutor"
