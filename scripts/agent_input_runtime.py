"""Input-only development flow loading and explicit, typed runtime identity bindings."""

import importlib
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

ALLOWED_KEYS = {"case_id", "flow_id", "input", "lineage", "parents", "stage_id"}
FORBIDDEN = {"expected_output", "oracle", "annotation", "gold", "judge_only"}
ID_FIELDS = {
    "namespace_id",
    "funding_id",
    "jd_id",
    "jd_revision_id",
    "goal_version_id",
    "source_id",
    "publication_id",
    "module_id",
    "attempt_id",
    "turn_id",
    "conversation_id",
    "workflow_id",
    "experience_id",
    "rubric_item_id",
    "plan_id",
    "version_id",
    "run_id",
    "read_id",
}
ALIASES = {
    "authorized_namespace_refs": "namespace_id",
    "pinned_publication_ids": "publication_id",
    "project_source_ids": "source_id",
    "source_jd_revision_id": "jd_revision_id",
}


def digest(value):
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class InputFlow:
    def __init__(self, root, flow_id):
        if Path(root).is_symlink():
            raise ValueError("Input root cannot be a symlink")
        self.root = Path(root).resolve()
        if self.root.name != "agent_input" or self.root.is_symlink():
            raise ValueError("Only a separately mounted agent_input directory is accepted")
        folder = self.root / "inputs" / "development"
        self.rows = {}
        frozen = []
        for path in sorted(folder.glob("*.jsonl")):
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise ValueError("Input symlink escapes isolation")
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if set(row) - ALLOWED_KEYS or set(row) & FORBIDDEN:
                    raise ValueError("Only solver input envelopes are accepted")
                if row["flow_id"] != flow_id:
                    continue
                stage = row["stage_id"][:3]
                if stage in self.rows:
                    raise ValueError("Duplicate stage in selected flow")
                ref = row["input"]["contract_ref"]
                if not ref.startswith("careerforge.contracts") or ref.count(":") != 1:
                    raise ValueError("Untrusted native contract import")
                module, name = ref.split(":")
                getattr(importlib.import_module(module), name).model_validate(
                    row["input"]["payload"]
                )
                self.rows[stage] = row
                frozen.append({"case_id": row["case_id"], "row_hash": digest(row)})
        if set(self.rows) != {f"S{i:02}" for i in range(1, 19)}:
            raise ValueError("Selected development flow needs exactly S01 through S18")
        selected = {row["case_id"] for row in self.rows.values()}
        if any(set(row["parents"]) - selected for row in self.rows.values()):
            raise ValueError("Parent is outside the selected development flow")
        self.flow_id, self.fingerprint = flow_id, digest(sorted(frozen, key=lambda r: r["case_id"]))
        from careerforge.contracts_jd_v166 import JDImportInput
        from careerforge.services.jd import import_document

        import_document(JDImportInput.model_validate(self.payload("S01")))
        self.verify_corpus()

    def payload(self, stage):
        return deepcopy(self.rows[stage]["input"]["payload"])

    def verify_corpus(self):
        source = self.payload("S06")
        commit = source["commit_sha"]
        candidates = [
            p for p in (self.root / "corpus" / "development").glob("*/" + commit) if p.is_dir()
        ]
        if len(candidates) != 1:
            raise ValueError("Fixed development corpus must be unambiguous")
        corpus = candidates[0].resolve()
        if not corpus.is_relative_to(self.root):
            raise ValueError("Corpus escapes isolated agent_input")
        for name, content in source["files"].items():
            path = corpus / name
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(corpus)
                or path.read_bytes() != content.encode("utf-8")
            ):
                raise ValueError("Pinned input/source byte mismatch: " + name)


class RuntimeBindings:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []

    def bind(self, field, original, actual, origin):
        if field not in ID_FIELDS or not isinstance(original, str) or not original:
            raise ValueError("Bindings require a typed original identity")
        valid = (isinstance(actual, str) and bool(actual)) or (
            isinstance(actual, list)
            and bool(actual)
            and all(isinstance(v, str) and v for v in actual)
        )
        if not valid:
            raise ValueError("Runtime identity is not materialized yet: " + field)
        old = [r for r in self.rows if (r["field"], r["original"]) == (field, original)]
        value = {"field": field, "original": original, "actual": actual, "origin": origin}
        if old and old[0]["actual"] != actual:
            raise ValueError("Immutable runtime binding conflict: " + field)
        if not old:
            self.rows.append(value)
        return actual

    def resolve(self, payload):
        def walk(value, key=None):
            canonical = ALIASES.get(key, key)
            if canonical and canonical.endswith("_ids"):
                canonical = canonical[:-1]
            if isinstance(value, dict):
                return {k: walk(v, k) for k, v in value.items()}
            if isinstance(value, list):
                result = []
                for item in value:
                    mapped = walk(item, canonical)
                    result.extend(mapped if isinstance(mapped, list) else [mapped])
                return result
            if canonical in ID_FIELDS and isinstance(value, str):
                matches = [
                    r for r in self.rows if r["field"] == canonical and r["original"] == value
                ]
                if matches:
                    return deepcopy(matches[0]["actual"])
                if value.startswith(("fixture-", "local-dataset-read:")):
                    raise ValueError("Unresolved runtime ID: " + canonical + "=" + value)
            return value

        return walk(deepcopy(payload))


class AdmissionJournal:
    """Persist exact idempotent wire input before dispatch, including original CAS revisions."""

    def __init__(self, entries=None):
        self.entries = entries if entries is not None else {}

    def prepare(self, method, path, value):
        if method != "POST" or not isinstance(value, dict) or not value.get("idempotency_key"):
            return deepcopy(value)
        key = digest([method, path, value["idempotency_key"]])
        prior = self.entries.get(key)
        if prior is not None:
            volatile = {
                "expected_revision",
                "expected_focus_revision",
                "expected_workflow_revision",
            }

            def clean(body):
                return {k: v for k, v in body.items() if k not in volatile}

            if clean(prior["body"]) != clean(value):
                raise ValueError("Idempotent admission changed semantic inputs")
            return deepcopy(prior["body"])
        self.entries[key] = {"method": method, "path": path, "body": deepcopy(value)}
        return deepcopy(value)
