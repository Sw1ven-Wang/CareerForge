"""Question-scoped soft hints, never an evidence or authorization decision."""

from __future__ import annotations

import re

LEGACY_RETRIEVAL_CONTEXT_CONTRACT = "question-focused-retrieval-1.6.6"
WHOLE_PACK_RETRIEVAL_CONTEXT_CONTRACT = "question-focused-retrieval-1.6.6-r2"
LEGACY_FACT_RETRIEVAL_CONTEXT_CONTRACT = "question-focused-retrieval-1.6.6-r3"
RETRIEVAL_CONTEXT_CONTRACT = "question-focused-retrieval-1.6.6-r4"

# A pronoun or the word "continue" somewhere in a substantive question is not
# permission to reuse every topic from earlier turns. Only a whole, bare
# continuation needs the previous soft topic to supply its missing subject.
_TOPIC_CONTINUATION = re.compile(
    r"\s*(?:(?:please\s+)?(?:continue(?:\s+(?:explaining|the explanation|with\s+(?:this|that|it)))?|"
    r"go on|tell me more|explain\s+(?:this|that|it|more)|more detail)|"
    r"(?:请)?(?:继续(?:讲解|解释|展开|详细说明|说说|说下)?|再(?:讲解|解释|展开|详细说明|说说|说下)|"
    r"(?:解释|讲解)(?:一下)?(?:这个|那个|它)(?:实现|内容|部分)?))\s*[.!?。！？]*\s*",
    re.I,
)


def is_topic_continuation(question: str) -> bool:
    return _TOPIC_CONTINUATION.fullmatch(question) is not None
