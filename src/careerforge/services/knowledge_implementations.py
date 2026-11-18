"""Associate approved architecture knowledge with source-grounded implementations."""

from pathlib import PurePosixPath

from sqlalchemy import select

from ..errors import DomainError
from ..models import Namespace, ObjectRow
from ..util import fingerprint
from .dependencies import check_closure, depend, register, require_object
from .learning_language import implementation_languages
from .security import namespace_access

ARCHITECTURE_TYPES = {"architecture", "concept", "overview", "tradeoff"}
IMPLEMENTATION_TYPES = {"implementation", "function", "testing"}
EXTENSIONS = {
    ".py": "python",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cs": "c#",
    ".kt": "kotlin",
    ".scala": "scala",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
}


def evidence_languages(pack):
    return {
        EXTENSIONS[ext]
        for item in pack.get("citations", [])
        if (ext := PurePosixPath(item.get("path", "")).suffix.lower()) in EXTENSIONS
    }


def material_matches(pack, profile, domain, *, language=None):
    # A Java source can explain architecture to a Python learner. Its code
    # implementation is selected separately and must match that track's language.
    if pack["unit_type"] in ARCHITECTURE_TYPES:
        return True
    if language is not None and language not in implementation_languages(profile, domain):
        return False
    return bool(evidence_languages(pack) & set(implementation_languages(profile, domain)))


def create_link_in(app, db, actor, body):
    namespace = namespace_access(db, actor, body.namespace_id, True)
    architecture = app.knowledge.publication_pack(db, actor, body.architecture_publication_id)
    implementation = app.knowledge.publication_pack(db, actor, body.implementation_publication_id)
    if (
        architecture["unit_type"] not in ARCHITECTURE_TYPES
        or implementation["unit_type"] not in IMPLEMENTATION_TYPES
    ):
        raise DomainError("KNOWLEDGE_PAIR_TYPE", "请关联架构/原理知识与具体实现知识。", 422)
    if body.language not in evidence_languages(implementation):
        raise DomainError(
            "KNOWLEDGE_LANGUAGE_EVIDENCE", "实现语言缺少已审核知识引用的源码路径支持。", 422
        )
    common = {
        t["topic_key"] for t in architecture["topic_links"] if t["relation"] != "mentions"
    } & {t["topic_key"] for t in implementation["topic_links"] if t["relation"] != "mentions"}
    if not common:
        raise DomainError("KNOWLEDGE_PAIR_TOPIC", "两类知识缺少共同的已审核主题。", 422)
    for pack in (architecture, implementation):
        source_namespace = db.get(Namespace, pack["namespace_id"])
        if namespace.kind == "shared_public" and source_namespace.kind != "shared_public":
            raise DomainError("PRIVATE_TO_PUBLIC", "私人知识不得进入公共关联。", 403)
    value = {
        "contract": "knowledge-implementation-link-v1",
        **body.model_dump(mode="json"),
        "topic_keys": sorted(common),
        "association_origin": "user",
        "architecture_language_constraint": None,
    }
    identifier = fingerprint([body.namespace_id, value])[:32]
    old = db.get(ObjectRow, identifier)
    if old:
        require_object(db, actor, identifier, "knowledge_implementation_link")
        check_closure(db, actor, [identifier])
        if old.payload != value:
            raise DomainError("KNOWLEDGE_PAIR_HASH", "知识关联已改变。", 409)
        return {"link_id": identifier, **value}
    obj = register(db, actor, body.namespace_id, "knowledge_implementation_link", value, identifier)
    for pack in (architecture, implementation):
        depend(db, actor, obj.id, pack["publication_id"], "links_knowledge")
    return {"link_id": obj.id, **value}


def linked_materials_in(app, db, actor, profile):
    from .security import accessible_namespaces

    namespaces = [n.id for n in accessible_namespaces(db, actor)]
    result = []
    for obj in db.scalars(
        select(ObjectRow).where(
            ObjectRow.kind == "knowledge_implementation_link",
            ObjectRow.namespace_id.in_(namespaces),
        )
    ):
        try:
            require_object(db, actor, obj.id, "knowledge_implementation_link")
            check_closure(db, actor, [obj.id])
            payload = obj.payload
            architecture = app.knowledge.publication_pack(
                db, actor, payload["architecture_publication_id"]
            )
            implementation = app.knowledge.publication_pack(
                db, actor, payload["implementation_publication_id"]
            )
            result.append(
                {
                    "link_id": obj.id,
                    "architecture": architecture,
                    "implementation": implementation
                    if material_matches(
                        implementation, profile, payload["domain"], language=payload["language"]
                    )
                    else None,
                    "implementation_language": payload["language"],
                    "implementation_selected": material_matches(
                        implementation, profile, payload["domain"], language=payload["language"]
                    ),
                    "semantic_quality_verified": False,
                }
            )
        except DomainError:
            continue
    return result
