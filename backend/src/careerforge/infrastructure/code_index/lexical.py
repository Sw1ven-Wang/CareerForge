"""Deterministic bounded symbol lookup. Alias expansion is explicit, never semantic proof."""

import re
from typing import Any

from careerforge.infrastructure.code_index.ast_index import symbols

ALIASES = {
    "状态恢复": ["get_tuple", "checkpoint"],
    "持久化": ["put", "postgres"],
    "重试": ["retry"],
    "调用": ["invoke"],
}


def rank(files: list[dict[str, str]], query: str, limit: int = 20) -> list[dict[str, Any]]:
    terms = set(re.findall(r"[\w]+", query.casefold()))
    for phrase, names in ALIASES.items():
        if phrase in query:
            terms.update(names)
    matches = []
    for file in files:
        index = symbols(file["content"])
        for symbol in index["symbols"]:
            qualified = symbol["qualified_name"].casefold()
            score = sum(
                4 if term == symbol["name"].casefold() else 2 if term in qualified else 0
                for term in terms
            )
            if score:
                matches.append(
                    {
                        "path": file["path"],
                        **symbol,
                        "score": score,
                        "support_scope": "静态定义定位，不证明调用关系",
                    }
                )
    return sorted(matches, key=lambda r: (-r["score"], r["path"], r["start_line"]))[:limit]
