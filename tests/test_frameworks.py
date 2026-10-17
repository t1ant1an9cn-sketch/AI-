from pydantic import ValidationError
import pytest

from voicedetect.frameworks import ForbiddenRule, Framework, PaceRules, Point, Stage, builtin_templates


def test_builtin_templates_are_valid_and_have_stable_ids():
    templates = {template.id: template for template in builtin_templates()}
    assert set(templates) == {"tpl-general", "tpl-thermos-cup"}
    thermos = templates["tpl-thermos-cup"]
    assert [stage.name for stage in thermos.stages][:2] == ["开场互动", "产品介绍"]
    assert all(rule.keywords for rule in thermos.forbidden)


def test_keywords_are_trimmed_and_deduplicated():
    point = Point(name="材质", keywords=[" 不锈钢 ", "", "不锈钢", "316"])
    assert point.keywords == ["不锈钢", "316"]


def test_duplicate_ids_are_rejected_with_readable_message():
    with pytest.raises(ValidationError, match="ID 重复：a"):
        Framework(name="x", stages=[Stage(id="a", name="甲"), Stage(id="a", name="乙")])


def test_forbidden_rule_needs_a_keyword():
    with pytest.raises(ValidationError, match="至少需要一个关键词"):
        ForbiddenRule(name="空规则", keywords=["  "])


def test_rate_range_must_be_ordered():
    with pytest.raises(ValidationError, match="下限必须小于上限"):
        PaceRules(min_rate=300, max_rate=200)


def test_json_round_trip_keeps_everything():
    original = builtin_templates()[0]
    assert Framework.model_validate_json(original.model_dump_json()) == original
