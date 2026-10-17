"""Validate model claims against frozen original text, not model-generated identifiers."""

from pathlib import PurePosixPath

CODE_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".vue",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".cpp",
    ".h",
    ".cs",
    ".sql",
}


def normalize_depths(output, sources):
    """Repair only depth metadata supported by the already validated literal text."""
    from careerforge.schemas.study import ExtractOutput

    allowed = {s["source_snapshot_id"]: s["content"] for s in sources}
    value = output.model_dump()
    repairs = []
    for item in value["requirements"]:
        quotes = [
            e["quote"]
            for e in item["evidence"]
            if allowed.get(e["source_snapshot_id"], "").count(e["quote"]) == 1
        ]
        mentioned = {
            level for level in ("了解", "熟悉", "掌握") if any(level in quote for quote in quotes)
        }
        old_depth, old_basis = item["depth"], item["depth_basis"]
        if item["depth_basis"] == "explicit" and item["depth"] not in mentioned:
            if len(mentioned) == 1:
                item["depth"] = next(iter(mentioned))
                reason = "原文有唯一明确等级，保留原文等级；加分项不等于降低深度。"
            else:
                item["depth_basis"] = "inferred"
                reason = "摘录未明确使用该等级词，目标深度属于模型推断。"
            repairs.append(
                {
                    "key": item["key"],
                    "original_depth": old_depth,
                    "original_basis": old_basis,
                    "depth": item["depth"],
                    "depth_basis": item["depth_basis"],
                    "reason": reason,
                    "original_rationale": item["rationale"],
                }
            )
            item["rationale"] = (
                reason
                if item["depth"] != old_depth
                else reason + " 原模型说明：" + item["rationale"][:450]
            )
    return ExtractOutput.model_validate(value), repairs


def extracted(output, sources):
    allowed = {s["source_snapshot_id"]: s for s in sources}
    keys = [r.key for r in output.requirements]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate requirement key")
    requirements = []
    for item in output.requirements:
        evidence = []
        for point in item.evidence:
            source = allowed.get(point.source_snapshot_id)
            if not source or source["content"].count(point.quote) != 1:
                raise ValueError("invalid requirement quote")
            offset = source["content"].index(point.quote)
            evidence.append(
                {
                    **point.model_dump(),
                    "content_hash": source["content_hash"],
                    "source_title": source["title"],
                    "quote_start": offset,
                    "quote_end": offset + len(point.quote),
                }
            )
        # Explicit depth needs original wording. Inferred depth remains labelled.
        if item.depth_basis == "explicit" and not any(item.depth in e["quote"] for e in evidence):
            raise ValueError("explicit depth missing in evidence")
        requirements.append({**item.model_dump(), "evidence": evidence})
    return requirements


def partial_extraction(output, sources):
    """Keep individually grounded requirements; unresolved claims cannot drive downstream work."""
    from collections import Counter

    from careerforge.schemas.study import ExtractOutput

    normalized, repairs = normalize_depths(output, sources)
    counts = Counter(item.key for item in normalized.requirements)
    valid, unresolved = [], []
    for item in normalized.requirements:
        try:
            if counts[item.key] != 1:
                raise ValueError("duplicate key")
            valid.extend(extracted(ExtractOutput(requirements=[item], limitations=[]), sources))
        except ValueError:
            unresolved.append(
                {
                    "key": item.key,
                    "technology": item.technology,
                    "reason": "原文引用缺失、不匹配或不能唯一定位；未纳入有效需求。",
                }
            )
    if not valid:
        raise ValueError("no grounded requirements")
    kept = {item["key"] for item in valid}
    return {
        "requirements": valid,
        "unresolved": unresolved,
        "normalization_repairs": [r for r in repairs if r["key"] in kept],
        "coverage": {
            "complete": not unresolved,
            "validated": len(valid),
            "unresolved": len(unresolved),
        },
        "limitations": [
            *output.limitations,
            *(
                [f"{len(unresolved)} 项未通过原文校验，已排除后续调研和计划。"]
                if unresolved
                else []
            ),
        ],
    }


def excerpts(files, requirements):
    terms = list(
        dict.fromkeys(
            k.lower() for r in requirements for k in [r["technology"], *r["keywords"]] if k.strip()
        )
    )
    ranked = []
    for path, content in files.items():
        if PurePosixPath(path).suffix.lower() not in CODE_SUFFIXES:
            continue
        lines = content.splitlines()
        scores = [
            (sum(t in (path + " " + line).lower() for t in terms), i)
            for i, line in enumerate(lines)
        ]
        score, line = max(scores, default=(0, 0))
        if not score:
            continue
        start = max(0, line - 8)
        quote, end = "", start
        for i in range(start, min(len(lines), start + 70)):
            value = lines[i] + "\n"
            if len(quote) + len(value) > 2600:
                break
            quote += value
            end = i + 1
        if quote:
            ranked.append(
                (
                    score,
                    path,
                    {
                        "path": path,
                        "start_line": start + 1,
                        "end_line": end,
                        "content": quote.rstrip("\n"),
                    },
                )
            )
    ranked.sort(key=lambda item: (-item[0], item[1]))
    docs = [
        {"path": p, "content": c[:5000], "truncated": len(c) > 5000}
        for p, c in sorted(files.items())
        if PurePosixPath(p).name.lower().startswith("readme")
    ][:2]
    return {
        "code": [r[2] for r in ranked[:6]],
        "documents": docs,
        "coverage": "关键词选取至多6个静态源码窗口；不是完整项目审计",
    }


def review_matches(output, manifest, reading):
    keys = {r["key"] for r in manifest["requirements"]}
    matched = []
    for point in output.matches:
        windows = [r for r in reading["code"] if r["path"] == point.path]
        exact = [w for w in windows if w["content"].count(point.quote) == 1]
        if point.requirement_key not in keys or len(exact) != 1:
            raise ValueError("review needs a unique literal quote in a read source window")
        window = exact[0]
        offset = window["content"].index(point.quote)
        start = window["start_line"] + window["content"][:offset].count("\n")
        end = start + point.quote.count("\n")
        match = {**point.model_dump(), "start_line": start, "end_line": end}
        if (start, end) != (point.start_line, point.end_line):
            match["line_normalization"] = {
                "reported_start_line": point.start_line,
                "reported_end_line": point.end_line,
                "method": "unique_literal_quote_in_read_window",
            }
        matched.append(match)
    if output.verdict == "strong" and not matched:
        raise ValueError("strong relevance requires actual code")
    return matched


def partial_review(output, manifest, reading):
    """Exclude unmatched claims; only grounded matches may support admission and plans."""
    valid, unresolved = [], []
    for point in output.matches:
        try:
            valid.extend(
                review_matches(output.model_copy(update={"matches": [point]}), manifest, reading)
            )
        except ValueError:
            unresolved.append(
                {
                    "requirement_key": point.requirement_key,
                    "path": point.path,
                    "reason": "摘录不在已读源码中或不能唯一定位；未用于入库及计划。",
                }
            )
    if output.verdict == "strong" and not valid:
        raise ValueError("strong relevance requires grounded matches")
    metadata = {"validated": len(valid), "unresolved": unresolved}
    if unresolved:
        metadata["unverified_model_summary"] = output.summary
        output = output.model_copy(
            update={
                "summary": f"审核结论：{output.verdict}；{len(valid)} 项源码匹配已核验，"
                f"{len(unresolved)} 项引用未通过校验，已排除。覆盖范围以代码依据为准。",
                "limitations": [
                    *output.limitations[:9],
                    "未通过原文校验的技术匹配："
                    + "、".join(p["requirement_key"] for p in unresolved),
                ],
                "matches": [
                    p
                    for p in output.matches
                    if not any(
                        u["requirement_key"] == p.requirement_key and u["path"] == p.path
                        for u in unresolved
                    )
                ],
            }
        )
    return output, valid, metadata


def plan_modules(output, manifest):
    modules = {m.key: m for m in output.modules}
    if len(modules) != len(output.modules):
        raise ValueError("duplicate module key")
    allowed_requirements = set(manifest["intent"]["requirement_keys"])
    projects = {p["id"]: p for p in manifest["projects"]}
    highlights = {
        h["citation_id"]: (p["id"], h["requirement_key"])
        for p in projects.values()
        for h in p["highlights"]
    }
    covered = set()

    def visit(key, chain, relation):
        if key in chain or key not in modules:
            raise ValueError("invalid module graph")
        item = modules[key]
        parents = (
            [item.parent_key]
            if relation == "parent" and item.parent_key
            else (item.prerequisite_keys if relation == "prerequisite" else [])
        )
        for parent in parents:
            visit(parent, chain + [key], relation)
        if relation == "parent" and len(chain) > 2:
            raise ValueError("module hierarchy deeper than 3")

    for item in modules.values():
        if (
            not set(item.requirement_keys) <= allowed_requirements
            or not set(item.project_review_ids) <= projects.keys()
        ):
            raise ValueError("plan scope mismatch")
        for identifier in item.highlight_ids:
            match = highlights.get(identifier)
            if (
                not match
                or match[0] not in item.project_review_ids
                or match[1] not in item.requirement_keys
            ):
                raise ValueError("unknown highlight")
        covered.update(item.requirement_keys)
        visit(item.key, [], "parent")
        visit(item.key, [], "prerequisite")
    if manifest["intent"]["kind"] == "function":
        focus_ids = {h["citation_id"] for h in manifest.get("focus_highlights", [])}
        if not focus_ids.intersection(
            h
            for m in modules.values()
            if not any(c.parent_key == m.key for c in modules.values())
            for h in m.highlight_ids
        ):
            raise ValueError("function plan omitted user-selected implementation")
    if covered != allowed_requirements:
        raise ValueError("selected requirements omitted from plan")
    if len(modules) > 1 and not any(m.parent_key for m in modules.values()):
        raise ValueError("plan must be modular and hierarchical")
    # Require at least one navigable code highlight in every leaf.
    parents = {m.parent_key for m in modules.values()}
    if any(not m.highlight_ids for m in modules.values() if m.key not in parents):
        raise ValueError("leaf module needs code highlight")
    return [m.model_dump() for m in output.modules]


def tutor_points(output, sources):
    allowed = {s["citation_id"]: s for s in sources}
    points = []
    for point in output.points:
        source = allowed.get(point.citation_id)
        if not source or source["quote"].count(point.quote) != 1:
            raise ValueError("tutor cited unread code")
        offset = source["quote"].index(point.quote)
        start = source["start_line"] + source["quote"][:offset].count("\n")
        points.append(
            {
                **point.model_dump(),
                "path": source["path"],
                "commit": source["commit"],
                "quote_start_line": start,
                "quote_end_line": start + point.quote.count("\n"),
            }
        )
    if output.status == "grounded" and not points:
        raise ValueError("grounded tutoring needs code evidence")
    if output.focus_citation_id and output.focus_citation_id not in {
        p["citation_id"] for p in points
    }:
        raise ValueError("focus must refer to a grounded point")
    return points
