"""Unpublished architecture/Python analysis pairs for explicit user confirmation."""

from typing import Generic, Literal, TypeVar

from pydantic import Field, model_validator

from .contracts import AnalysisOutput, Contract, UnitDraft


class DraftKnowledgePair(Contract):
    architecture_subject_key: str
    implementation_subject_key: str | None
    implementation_language: Literal["python"] | None
    relation: Literal["implementation_example_for"] = "implementation_example_for"


Pair = TypeVar("Pair", bound=DraftKnowledgePair)


class PairedAnalysisBase(AnalysisOutput, Generic[Pair]):
    pairs: list[Pair] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_pairs(self):
        units = {u.subject_key: u for u in self.units}
        if len(units) != len(self.units):
            raise ValueError("知识单元 subject_key 必须唯一")
        if any(c.kind == "runtime_observation" for u in self.units for c in u.claims):
            raise ValueError("静态分析不得生成运行观察")
        covered = set()
        for pair in self.pairs:
            architecture = units.get(pair.architecture_subject_key)
            if architecture is None or architecture.unit_type != "architecture":
                raise ValueError("架构关联必须指向本次 architecture 单元")
            if pair.architecture_subject_key in covered:
                raise ValueError("架构单元不能重复配对")
            covered.add(pair.architecture_subject_key)
            if pair.implementation_subject_key is None:
                if pair.implementation_language is not None:
                    raise ValueError("缺少实现时不得指定实现语言")
            else:
                implementation = units.get(pair.implementation_subject_key)
                if implementation is None or implementation.unit_type != "implementation":
                    raise ValueError("实现关联必须指向本次 implementation 单元")
                if pair.implementation_language != "python":
                    raise ValueError("本次实现知识只允许 Python")
                if pair.implementation_subject_key in covered:
                    raise ValueError("实现单元不能重复配对")
                covered.add(pair.implementation_subject_key)
        if covered != set(units):
            raise ValueError("所有架构与实现单元必须明确关联或标记尚缺实现")
        return self


class LinkedAnalysisOutput(PairedAnalysisBase[DraftKnowledgePair]):
    pass


class PythonKnowledgePair(DraftKnowledgePair):
    implementation_subject_key: str
    implementation_language: Literal["python"]


class FullyPairedAnalysisOutput(PairedAnalysisBase[PythonKnowledgePair]):
    """An explicitly requested Python preview cannot silently omit its implementation."""

    units: list[UnitDraft] = Field(min_length=2, max_length=4)
    pairs: list[PythonKnowledgePair] = Field(min_length=1, max_length=2)
