from voicedetect.analysis.matcher import Matcher


def evidence(text, hits):
    return [(hit.group, text[hit.start : hit.end], hit.exact) for hit in hits]


def test_exact_hit_points_back_to_original_text():
    text = "这款杯子采用食品级不锈钢，放心用。"
    hits = Matcher({"material": ["食品级不锈钢"]}).scan(text)
    assert evidence(text, hits) == [("material", "食品级不锈钢", True)]


def test_homophone_from_recognition_error_still_matches():
    text = "这是食品及不锈钢"
    hits = Matcher({"material": ["食品级"]}).scan(text)
    assert evidence(text, hits) == [("material", "食品及", False)]


def test_homophones_can_be_disabled():
    assert Matcher({"material": ["食品级"]}, homophones=False).scan("这是食品及不锈钢") == []


def test_single_character_keyword_is_never_matched_by_sound():
    matcher = Matcher({"absolute": ["最"]})
    assert matcher.scan("张嘴") == []
    assert evidence("最好", matcher.scan("最好")) == [("absolute", "最", True)]


def test_numbers_match_across_written_forms():
    matcher = Matcher({"capacity": ["500毫升"], "festival": ["618"]})
    text = "容量五百毫升，六一八大促"
    assert evidence(text, matcher.scan(text)) == [
        ("capacity", "五百毫升", True),
        ("festival", "六一八", True),
    ]
    assert Matcher({"capacity": ["500ml"]}).scan("容量５００ＭＬ")[0].exact


def test_overlapping_keywords_in_one_group_count_once_and_prefer_longest():
    text = "316不锈钢内胆"
    hits = Matcher({"material": ["不锈钢", "316不锈钢"]}).scan(text)
    assert evidence(text, hits) == [("material", "316不锈钢", True)]
    assert hits[0].keyword == "316不锈钢"


def test_different_groups_may_share_text_and_repeats_are_counted():
    text = "赶紧下单，下单立减"
    hits = Matcher({"cta": ["下单"], "urgent": ["赶紧下单"]}).scan(text)
    assert evidence(text, hits) == [
        ("urgent", "赶紧下单", True),
        ("cta", "下单", True),
        ("cta", "下单", True),
    ]
