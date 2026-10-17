"""话术框架：主播应该讲什么、不能说什么，以及讲话节奏的标准。

框架由若干环节（开场、产品介绍、价格优惠、促单……）组成，每个环节下有要点。
要点靠关键词判断有没有讲到；描述写给大模型做语义判断。另含违规表达、口头禅和节奏阈值。
"""

from __future__ import annotations

from importlib.resources import files
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

Severity = Literal["high", "medium", "low"]

DEFAULT_FILLERS = ["嗯", "呃", "额", "那个那个", "然后然后", "就是说"]


def new_id() -> str:
    return uuid4().hex[:8]


def clean_words(words: list[str]) -> list[str]:
    """去掉首尾空白、空项和重复项，保持原顺序。"""
    result: list[str] = []
    for word in words:
        word = word.strip()
        if word and word not in result:
            result.append(word)
    return result


class Point(BaseModel):
    """一个要点，例如“材质”。说到任一关键词算提及一次。"""

    id: str = Field(default_factory=new_id, min_length=1)
    name: str = Field(min_length=1, max_length=50)
    keywords: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=500)
    required: bool = True
    min_mentions: int = Field(default=1, ge=1, le=20)

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, value: list[str]) -> list[str]:
        return clean_words(value)


class Stage(BaseModel):
    """话术环节，例如“产品介绍”。"""

    id: str = Field(default_factory=new_id, min_length=1)
    name: str = Field(min_length=1, max_length=50)
    points: list[Point] = Field(default_factory=list)


class ForbiddenRule(BaseModel):
    """一类不能说的话，例如“绝对化用语”。"""

    id: str = Field(default_factory=new_id, min_length=1)
    name: str = Field(min_length=1, max_length=50)
    keywords: list[str]
    severity: Severity = "high"
    suggestion: str = Field(default="", max_length=200)

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, value: list[str]) -> list[str]:
        value = clean_words(value)
        if not value:
            raise ValueError("违规表达至少需要一个关键词")
        return value


class PaceRules(BaseModel):
    """讲话节奏的标准，超出即提示。语速只按说话时间计算，不含停顿。"""

    min_rate: float = Field(default=150, ge=30, le=600, description="语速下限，字/分钟")
    max_rate: float = Field(default=330, ge=30, le=800, description="语速上限，字/分钟")
    max_pause_seconds: float = Field(default=8, ge=1, le=120, description="停顿超过多少秒算冷场")
    min_speech_ratio: float = Field(default=0.5, ge=0, le=1, description="窗口内说话时间占比下限")
    max_fillers_per_minute: float = Field(default=8, ge=0, le=100, description="每分钟口头禅次数上限")

    @model_validator(mode="after")
    def _check_range(self) -> PaceRules:
        if self.min_rate >= self.max_rate:
            raise ValueError("语速下限必须小于上限")
        return self


class Framework(BaseModel):
    """一套完整的话术框架。"""

    id: str = Field(default_factory=new_id, min_length=1)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    stages: list[Stage] = Field(default_factory=list)
    forbidden: list[ForbiddenRule] = Field(default_factory=list)
    fillers: list[str] = Field(default_factory=lambda: list(DEFAULT_FILLERS))
    pace: PaceRules = Field(default_factory=PaceRules)
    check_order: bool = Field(default=False, description="是否检查环节先后顺序")
    llm_instructions: str = Field(default="", max_length=2000, description="给大模型的额外审核要求")

    @field_validator("fillers")
    @classmethod
    def _clean_fillers(cls, value: list[str]) -> list[str]:
        return clean_words(value)

    @model_validator(mode="after")
    def _check_unique_ids(self) -> Framework:
        ids = [stage.id for stage in self.stages]
        ids += [point.id for _, point in self.points()]
        ids += [rule.id for rule in self.forbidden]
        duplicated = sorted({item for item in ids if ids.count(item) > 1})
        if duplicated:
            raise ValueError(f"环节、要点或违规规则的 ID 重复：{'、'.join(duplicated)}")
        return self

    def points(self) -> list[tuple[Stage, Point]]:
        return [(stage, point) for stage in self.stages for point in stage.points]


def builtin_templates() -> list[Framework]:
    """随程序附带的框架模板（assets/frameworks/*.json）。"""
    folder = files("voicedetect") / "assets" / "frameworks"
    paths = sorted((p for p in folder.iterdir() if p.name.endswith(".json")), key=lambda p: p.name)
    return [Framework.model_validate_json(path.read_text(encoding="utf-8")) for path in paths]
