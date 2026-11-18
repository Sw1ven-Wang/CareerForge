"""Static learning-quality signals from a fixed snapshot, never a test-pass claim."""

import ast
import re
from pathlib import PurePosixPath

from ..util import digest

QUALITY_CONTRACT = "project-learning-quality-v1"
QUALITY_POLICY = {
    "contract": QUALITY_CONTRACT,
    "primary_criteria": [
        "substantive_implementation",
        "learning_documentation",
        "test_evidence",
        "test_ci",
        "reproducibility",
    ],
    "stack_match": "secondary_not_a_repository_filter",
    "single_repository_full_jd_coverage_required": False,
    "stars_are_quality_proof": False,
    "architecture_language_constraint": None,
    "quality_confirmation_requires_further_review": True,
}

COMMUNITY_CONTRACT = "project-community-learning-v2"
COMMUNITY_POLICY = {
    "contract": COMMUNITY_CONTRACT,
    "stars_exclusive_min": 100,
    "forks_exclusive_min": 50,
    "operator": "and",
    "runtime_gate_required": False,
    "test_ci_gate_required": False,
    "required_content": ["implementation", "learning_documentation"],
    "stack_match_required": False,
    "architecture_language_constraint": None,
    "community_adoption_is_correctness_proof": False,
}


def validate_community_policy(policy):
    if (
        not isinstance(policy, dict)
        or policy.get("contract") != COMMUNITY_CONTRACT
        or any(
            type(policy.get(k)) is not int or not 0 <= policy[k] <= 100_000_000
            for k in ("stars_exclusive_min", "forks_exclusive_min")
        )
        or {
            k: v
            for k, v in policy.items()
            if k not in {"stars_exclusive_min", "forks_exclusive_min"}
        }
        != {
            k: v
            for k, v in COMMUNITY_POLICY.items()
            if k not in {"stars_exclusive_min", "forks_exclusive_min"}
        }
    ):
        raise ValueError("无效的项目社区门槛合同")
    return policy


def community_gate(metadata, policy=None):
    policy = validate_community_policy(policy or COMMUNITY_POLICY)
    stars, forks = metadata.get("stars"), metadata.get("forks")
    known = type(stars) is int and stars >= 0 and type(forks) is int and forks >= 0
    reasons = []
    if not known:
        reasons.append("community_metrics_unverified")
    else:
        if stars <= policy["stars_exclusive_min"]:
            reasons.append("star_threshold_not_met")
        if forks <= policy["forks_exclusive_min"]:
            reasons.append("fork_threshold_not_met")
    return {
        "status": "passed" if not reasons else "below_threshold" if known else "unverified",
        "stars": stars,
        "forks": forks,
        "reasons": reasons,
        "observed_at": metadata.get("observed_at"),
        "policy": policy,
    }


def learning_quality(files, metadata, policy=None):
    """Community gate plus study content; runtime/tests/CI are not selection conditions."""
    policy = validate_community_policy(policy or COMMUNITY_POLICY)
    gate = community_gate(metadata, policy)
    code, docs = [], []
    for path, text in files.items():
        parts = PurePosixPath(path).parts
        if any(
            p.lower()
            in {"vendor", "third_party", "node_modules", "tests", "test", "__tests__", "libs"}
            for p in parts
        ):
            continue
        suffix = PurePosixPath(path).suffix.lower()
        if re.search(r"(?:^|/)(?:test_[^/]+|[^/]+(?:_test|\.test|\.spec))\.", path, re.I):
            continue
        if (
            PurePosixPath(path).name.lower().startswith("readme") or path.startswith("docs/")
        ) and len(text.strip()) >= 80:
            docs.append(path)
        if suffix == ".py":
            try:
                if any(
                    isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    for n in ast.walk(ast.parse(text))
                ):
                    code.append(path)
            except (SyntaxError, ValueError):
                pass
        elif suffix in {
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".go",
            ".java",
            ".rs",
            ".cs",
            ".kt",
            ".c",
            ".cpp",
        }:
            if re.search(
                r"\b(?:class|function|func|fn|interface)\s+\w+|(?:const|let)\s+\w+\s*=.*=>|\b\w+\s+\w+\s*\([^;{}]*\)\s*\{",
                text,
            ):
                code.append(path)
    gaps = list(gate["reasons"])
    if not code:
        gaps.append("implementation_not_observed")
    if not docs:
        gaps.append("learning_documentation_not_observed")
    status = (
        "below_community_threshold"
        if gate["status"] == "below_threshold"
        else "community_unverified"
        if gate["status"] == "unverified"
        else "insufficient_implementation"
        if not code
        else "needs_quality_review"
        if not docs
        else "priority_for_source_review"
    )
    return {
        "contract": COMMUNITY_CONTRACT,
        "policy": policy,
        "status": status,
        "community_gate": gate,
        "signals": {"implementation_paths": sorted(code), "documentation_paths": sorted(docs)},
        "gaps": gaps,
        "file_evidence": [
            {"path": p, "file_sha256": digest(files[p])} for p in sorted(set(code[:3] + docs[:2]))
        ],
        "runtime_gate_required": False,
        "test_ci_gate_required": False,
        "runtime_test_result": "not_run",
        "semantic_quality_verified": False,
        "community_adoption_is_correctness_proof": False,
    }


def static_quality(files):
    own = {
        p: t
        for p, t in files.items()
        if not any(
            part in {"vendor", "vendored", "node_modules", "third_party", "libs"}
            for part in PurePosixPath(p).parts
        )
    }
    implementation, tests, docs, manifests, locks, ci = [], [], [], [], [], []
    assertions = 0
    for path, text in own.items():
        name = PurePosixPath(path).name.lower()
        suffix = PurePosixPath(path).suffix.lower()
        is_test = bool(
            re.search(
                r"(?:^|/)(?:tests?|__tests__)(?:/|$)|(?:^|/)(?:test_[^/]+|[^/]+(?:_test|\.test|\.spec))\.",
                path,
                re.I,
            )
        )
        if name.startswith(("readme", "contributing")) or path.startswith(("docs/", "examples/")):
            if (
                suffix in {".md", ".rst", ".py", ".ts", ".js", ".go", ".java"}
                and len(text.strip()) >= 80
            ):
                docs.append(path)
        if name in {
            "pyproject.toml",
            "requirements.txt",
            "environment.yml",
            "package.json",
            "go.mod",
            "cargo.toml",
            "pom.xml",
            "build.gradle",
            "setup.py",
        }:
            manifests.append(path)
        if name in {
            "uv.lock",
            "poetry.lock",
            "pdm.lock",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "go.sum",
            "cargo.lock",
            "gradle.lockfile",
        }:
            locks.append(path)
        if path.startswith((".github/workflows/", ".gitlab-ci")) or name in {
            "jenkinsfile",
            "azure-pipelines.yml",
        }:
            lines = "\n".join(s for s in text.splitlines() if not s.lstrip().startswith("#"))
            if re.search(
                r"\b(?:pytest|unittest|tox|nox)\b|\b(?:go|cargo|mvn|gradle|dotnet)\s+test\b|\b(?:npm|pnpm|yarn|bun)(?:\s+run)?\s+test\b",
                lines,
            ):
                ci.append(path)
        count = 0
        code = False
        if suffix == ".py":
            try:
                tree = ast.parse(text)
                nodes = list(ast.walk(tree))
                code = any(
                    isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    for n in nodes
                )
                if is_test:
                    count = sum(isinstance(n, ast.Assert) for n in nodes)
                    count += len(
                        re.findall(
                            r"\b(?:assertRaises|assertEqual|assertTrue|assertFalse|pytest\.raises)\s*\(",
                            text,
                        )
                    )
            except (SyntaxError, ValueError):
                pass
        elif suffix in {
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".go",
            ".java",
            ".rs",
            ".kt",
            ".cs",
            ".c",
            ".cpp",
            ".cc",
        }:
            code = bool(
                re.search(
                    r"\b(?:class|function|func|fn|interface)\s+\w+|(?:const|let)\s+\w+\s*=.*=>|\b\w+\s+\w+\s*\([^;{}]*\)\s*\{",
                    text,
                )
            )
            if is_test or "@Test" in text or "#[test]" in text:
                count = len(
                    re.findall(
                        r"\b(?:expect|assert\w*|Assert\w*)\s*[!(]|\b(?:assert|Assert)\.\w+\s*\(|\bt\.(?:Error|Fatal)",
                        text,
                    )
                )
        if count:
            tests.append(path)
            assertions += count
        if code and not is_test and not path.startswith(("docs/", "examples/", "configs/")):
            implementation.append(path)
    signals = {
        "implementation_paths": sorted(implementation),
        "documentation_paths": sorted(docs),
        "test_assertion_paths": sorted(tests),
        "assertion_count": assertions,
        "test_ci_paths": sorted(ci),
        "dependency_manifest_paths": sorted(manifests),
        "lockfile_paths": sorted(locks),
    }
    gaps = [
        label
        for label, found in [
            ("test_ci_not_observed", ci),
            ("executable_assertions_not_observed", tests),
            ("dependency_lock_not_observed", locks),
            ("setup_manifest_not_observed", manifests),
            ("learning_documentation_not_observed", docs),
        ]
        if not found
    ]
    strong = bool(implementation and docs and tests and ci and manifests)
    paths = sorted(
        set(implementation[:3] + tests[:3] + docs[:2] + ci[:3] + manifests[:2] + locks[:2])
    )
    return {
        "contract": QUALITY_CONTRACT,
        "detector_version": "static-quality-v1.1-node-assert",
        "policy": QUALITY_POLICY,
        "status": "priority_for_source_review"
        if strong
        else "needs_quality_review"
        if implementation
        else "insufficient_implementation",
        "signals": signals,
        "gaps": gaps,
        "file_evidence": [{"path": p, "file_sha256": digest(files[p])} for p in paths],
        "runtime_test_result": "not_run",
        "maintenance_status": "not_assessed",
        "semantic_quality_verified": False,
        "production_readiness_verified": False,
    }
