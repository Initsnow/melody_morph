"""
循环和弦进行检测测试（gp-chords --progressions 的核心）。

覆盖：精确循环、变体重复、非循环、分散重复归族、覆盖选择与重叠过滤、
chord_token 的罗马度数提取。
"""

from __future__ import annotations

from gpchords.annotate import CHORD_TEMPLATES, _detect_progressions
from gpchords.progression import (
    chord_token,
    find_loop_families,
    loop_label,
    token_sim,
)


def tok(degree: str, family: str = "maj"):
    return (degree, family)


def chord(name: str, root: int, quality: str, bass_pc: int | None = None) -> dict:
    return {
        "name": name,
        "root": root,
        "quality": quality,
        "suffix": CHORD_TEMPLATES[quality][1],
        "bass_pc": root if bass_pc is None else bass_pc,
    }


# ---------------------------------------------------------------------------
# token 归一化
# ---------------------------------------------------------------------------


def test_token_sim():
    assert token_sim(tok("I"), tok("I")) == 1.0
    assert token_sim(tok("I", "maj"), tok("I", "sus")) == 0.6  # 同度数容差
    assert token_sim(tok("I"), tok("IV")) == 0.0
    assert token_sim(None, tok("I")) == 0.5


def test_chord_token_roman_degree():
    # B 大调 Bsus2 -> Isus2 -> 度数 I、家族 sus
    assert chord_token(chord("Bsus2", 11, "sus2"), 11, "Major") == ("I", "sus")
    # A 小调按关系大调记：Am -> vi
    assert chord_token(chord("Am", 9, "min"), 9, "Minor") == ("vi", "min")
    # C7/Bb 斜杠：度数取 C（斜杠低音不参与身份）
    assert chord_token(chord("C7/Bb", 0, "7", bass_pc=10), 0, "Major") == ("I", "dom")


# ---------------------------------------------------------------------------
# 循环检测
# ---------------------------------------------------------------------------


def test_exact_loop():
    intro = [tok("I"), tok("V", "dom")] * 4
    fams = find_loop_families(intro)
    assert len(fams) == 1
    f = fams[0]
    assert f.period == 2
    assert f.occurrences == [(1, 8)]
    assert f.copies == 4
    assert f.pattern == ["I", "V"]
    assert loop_label(f) == "P1: I-V"


def test_variant_loop():
    """12 小节进行重复两遍、中间换掉两个和弦，仍归一个 family。"""
    verse1 = [tok("I"), tok("IV"), tok("I"), tok("I"), tok("IV"), tok("V", "dom"),
              tok("I"), tok("I"), tok("IV"), tok("V", "dom"), tok("IV"), tok("I")]
    verse2 = list(verse1)
    verse2[2] = tok("V", "dom")
    verse2[3] = tok("IV")
    fams = find_loop_families(verse1 + verse2)
    assert len(fams) == 1
    f = fams[0]
    assert f.period == 12
    assert f.occurrences == [(1, 24)]
    assert f.copies == 2


def test_non_repeating_sequence():
    seq = [tok("I"), tok("IV"), tok("V"), tok("vi"), tok("II"),
           tok("V"), tok("III"), tok("VI")]
    assert find_loop_families(seq) == []


def test_non_contiguous_same_loop_grouped():
    """分散在曲中两处的同名 2 小节循环（各含 2 遍）归入同一 family。"""
    seq = (
        [tok("I"), tok("V", "dom")] * 2  # 1-4  Intro 循环运行
        + [tok("I"), tok("IV"), tok("V"), tok("vi")]  # 5-8  Verse
        + [tok("II"), tok("V", "dom"), tok("III"), tok("VI")]  # 9-12 Bridge
        + [tok("I"), tok("V", "dom")] * 2  # 13-16 Intro 循环再次出现
        + [tok("IV"), tok("II"), tok("V"), tok("III")]  # 17-20 Outro
    )
    fams = find_loop_families(seq)
    by_id = {f.id: f for f in fams}
    i_v = by_id["P1"]
    assert i_v.period == 2
    assert (1, 4) in i_v.occurrences
    assert (13, 16) in i_v.occurrences
    assert i_v.copies == 4


def test_short_loop_below_min_coverage():
    """2 小节循环只重复 1 遍（覆盖 2 < 4）不报告。"""
    seq = [tok("I"), tok("V", "dom")] * 2 + [tok("IV"), tok("IV")]
    assert find_loop_families(seq) == []


def test_longer_coverage_wins_over_nested():
    """嵌套长短循环重叠时保留覆盖更多的 family。"""
    verse = [tok("I"), tok("IV"), tok("V"), tok("vi")] * 4  # 4 小节 x4
    fams = find_loop_families(verse)
    assert [f.period for f in fams] == [4]


def test_wildcard_rest_measures():
    """None（无和弦小节）可被弱通配，但纯空小节不构成循环。"""
    seq = [tok("I"), None, tok("I"), None, tok("I"), None, tok("I"), None]
    assert find_loop_families(seq) == []


def test_detect_progressions_pickup_bar_anchor_shift():
    """弱起小节（第 1 小节无和弦）不应产生残缺/写不进去的 P 标注。

    回归：P 标签应从第 1 个有和弦的小节开始（第 2 小节），并且 4 小节
    循环的标注长度必须是 4 个罗马数字，而不是只有 3 个。
    """
    def res(bar, c):
        return {"bar": bar, "chord": c, "key_root": 0, "key_mode": "Major"}

    C = chord("C", 0, "maj")
    F = chord("F", 5, "maj")
    Am = chord("Am", 9, "min")
    Am7 = chord("Am7", 9, "m7")
    # 第 1 小节是休止/无和弦；从第 2 小节起是一个 4 小节循环的变体重复
    seq = [
        (2, F), (3, Am), (4, F), (5, Am7),
        (6, F), (7, Am), (8, F), (9, Am),
        (10, F), (11, Am), (12, F), (13, C),
        (14, F), (15, Am), (16, F), (17, C),
        (18, F), (19, Am), (20, F), (21, C),
        (22, F), (23, Am), (24, F),
    ]
    results = [res(bar, c) for bar, c in seq]
    families, labels, romans, payload = _detect_progressions(results)

    assert len(families) == 1
    assert families[0].occurrences == [(1, 24)]
    # 第 1 小节没有罗马数字，不能作为标注锚点
    assert 1 not in labels
    # 标注顺移到第 2 小节；它描述的仍是第 1-4 小节的原始循环，
    # 因此第 1 小节用 · 占位，且是完整的 4 项进行
    assert labels[2] == "P1: ·-IV-vi-IV"
    assert len(labels[2].split(": ", 1)[1].split("-")) == 4
    assert romans[2] == "IV"
    # 后续循环遍仍按各自的起点标注变体
    assert labels[5] == "P1': vi7-IV-vi-IV"
    assert labels[9] == "P1': vi-IV-vi-IV"
    assert labels[13] == "P1': I-IV-vi-IV"
    # payload 的 region start 也使用实际可写的锚点小节，end 仍是该遍原始范围
    assert [r["start"] for r in payload[0]["regions"]] == [2, 5, 9, 13, 17, 21]
    assert [r["end"] for r in payload[0]["regions"]] == [4, 8, 12, 16, 20, 24]
    assert all(len(r["label"].split(": ", 1)[1].split("-")) == 4 for r in payload[0]["regions"])


def test_detect_progressions_label_includes_all_chord_changes():
    """P 进行标注应包含小节内的全部和弦变化，而不是每小节只取第一个。

    回归：4 小节循环，每小节 2 个和弦，标注应为
    ``IV-V-vi-I-IV-V-I-V``，与谱面实际和弦一一对应。
    """
    def res(bar, c):
        return {"bar": bar, "chord": c, "key_root": 0, "key_mode": "Major"}

    F = chord("F", 5, "maj")
    G = chord("G", 7, "maj")
    Am = chord("Am", 9, "min")
    C = chord("C", 0, "maj")
    # 每小节两个和弦：IV-V | vi-I | IV-V | I-V，重复两遍
    seq = [
        (1, F), (1, G), (2, Am), (2, C),
        (3, F), (3, G), (4, C), (4, G),
        (5, F), (5, G), (6, Am), (6, C),
        (7, F), (7, G), (8, C), (8, G),
    ]
    results = [res(bar, c) for bar, c in seq]
    families, labels, romans, payload = _detect_progressions(results)

    assert len(families) == 1
    assert labels[1] == "P1: IV-V-vi-I-IV-V-I-V"
    assert labels[5] == "P1: IV-V-vi-I-IV-V-I-V"
    assert romans[1] == "IV"
    assert payload[0]["regions"][0]["label"] == "P1: IV-V-vi-I-IV-V-I-V"


def test_detect_progressions_region_labels():
    """每个循环遍的起点都标该遍实际进行的完整罗马数字（含品质）；
    与第一次出现的那遍完全一致仍标 P1，有变体则标 P1'。"""
    def res(bar, c):
        return {"bar": bar, "chord": c, "key_root": 0, "key_mode": "Major"}

    C = chord("C", 0, "maj")
    F = chord("F", 5, "maj")
    G7 = chord("G7", 7, "7")
    G = chord("G", 7, "maj")
    Am = chord("Am", 9, "min")
    verse1 = (C, F, G7, Am)
    verse2 = (C, F, G, Am)
    filler = (chord("Dm", 2, "min"), chord("Em", 4, "min"), F, chord("Dm", 2, "min"))
    pairs = (
        list(zip(range(1, 9), verse1 * 2))
        + list(zip(range(9, 13), filler))
        + list(zip(range(13, 21), verse2 * 2))
    )
    results = [res(bar, c) for bar, c in pairs]
    families, labels, romans, payload = _detect_progressions(results)

    assert len(families) == 1
    # region 1（1-8，2 遍）：与第一次出现完全一致，都标 P1（不带 '）
    assert labels[1] == "P1: I-IV-V7-vi"
    assert labels[5] == "P1: I-IV-V7-vi"
    # region 2（13-20，2 遍）：相对第一次出现有变体（V7 -> V），标 P1'
    assert labels[13] == "P1': I-IV-V-vi"
    assert labels[17] == "P1': I-IV-V-vi"
    assert romans[1] == "I"
    assert romans[5] == "I"
    assert payload[0]["regions"] == [
        {"start": 1, "end": 4, "label": "P1: I-IV-V7-vi"},
        {"start": 5, "end": 8, "label": "P1: I-IV-V7-vi"},
        {"start": 13, "end": 16, "label": "P1': I-IV-V-vi"},
        {"start": 17, "end": 20, "label": "P1': I-IV-V-vi"},
    ]


def test_periodic_block_reduces_to_shorter_loop():
    """6 小节 V-I-V-I-V-I 是 2 小节循环的重复切片：约简成 2 小节，
    循环起点不再被长周期平移窗口带偏。"""
    seq = [tok("V"), tok("I")] * 8 + [tok("IV"), tok("II"), tok("V"), tok("III")]
    fams = find_loop_families(seq)
    assert len(fams) == 1
    f = fams[0]
    assert f.period == 2
    assert f.occurrences == [(1, 16)]
    assert f.pattern == ["V", "I"]


def test_periodic_with_rest_keeps_longer_period():
    """[V, I, 休, I] 的 4 小节循环：跨小节休止打断短周期重链，
    不能约简成 2 小节（否则会丢失真实的休止结构）。"""
    seq = [tok("V"), tok("I"), None, tok("I")] * 2
    fams = find_loop_families(seq)
    assert len(fams) == 1
    f = fams[0]
    assert f.period == 4
    assert f.occurrences == [(1, 8)]
    assert f.pattern == ["V", "I", "I"]


def test_per_occurrence_conflict_keeps_intro_region():
    """intro 的 2 小节 V-I 循环与副歌区 6 小节 family 撞车时，
    只丢冲突的副歌区，intro 区保留——整个 family 被误杀会导致
    intro 起点没有进行标注。"""
    intro = [tok("V"), tok("I")] * 4                        # 1-8
    filler = [tok("IV"), tok("II"), tok("V"), tok("III")]   # 9-12
    verse_block = (
        [tok("V"), tok("V"), tok("V"), tok("I"), tok("V"), tok("I")]
        + [tok("V"), tok("I")] * 9                          # 13-36
    )
    verse2 = (
        [tok("V"), tok("V"), tok("V"), tok("I"), tok("V"), tok("I")]
        + [tok("V"), tok("I")] * 9                          # 37-60
    )
    pickup_only = [tok("V"), tok("V"), tok("V"), tok("I"), tok("V"), tok("I")] * 3  # 61-78
    seq = intro + filler + verse_block + verse2 + pickup_only
    fams = find_loop_families(seq)
    by_period = {f.period: f for f in fams}
    assert by_period[2].occurrences == [(1, 8)]
    assert by_period[6].occurrences == [(13, 78)]


def test_variant_16bar_phrase_not_clumped():
    """两个 8 小节变体句链成 16 小节时，选族保留 8 小节循环，
    不再聚成一坨 16 小节模式；主歌/副歌同进行归到同一 family，
    起点取更靠后的循环（第 3 小节，而不是吞进 intro 多余 I 的第 2 小节）。"""
    verse = (
        [tok("I"), tok("I"), tok("V"), tok("V"), tok("vi"), tok("vi"), tok("IV"), tok("IV")]
        + [tok("I"), tok("I"), tok("V"), tok("V"), tok("VI"), tok("vi"), tok("IV"), tok("IV")]
        + [tok("I"), tok("I"), tok("V"), tok("V"), tok("vi"), tok("vi"), tok("IV"), tok("iv")]
        + [tok("I"), tok("I"), tok("V"), tok("V"), tok("VI"), tok("VI"), tok("IV"), tok("IV")]
    )
    transition = [tok("VI"), tok("IV"), tok("I"), tok("V")] * 2
    chorus = [tok("I"), tok("I"), tok("V"), tok("V"), tok("VI"), tok("VI"), tok("IV"), tok("IV")] * 7
    seq = [tok("I"), tok("I")] + verse + transition + [tok("V"), tok("V")] + chorus
    fams = find_loop_families(seq)
    by_period: dict[int, list] = {}
    for f in fams:
        by_period.setdefault(f.period, []).append(f)
    assert 16 not in by_period  # 变体句不再合并成 16 小节
    assert len(by_period[8]) == 1  # 主歌与副歌同一进行，归一个 family
    main = by_period[8][0]
    assert main.occurrences == [(3, 34), (45, 100)]
    assert fams[0].id == "P1"  # 按出现顺序：主歌是 P1


def test_static_patterns_not_reported():
    """持续音/踏板（模式只有一种度数）不是和弦进行，不报告。"""
    seq = [tok("V"), tok("V")] * 2 + [tok("I"), tok("I"), tok("I")] * 2
    assert find_loop_families(seq) == []


def test_ids_numbered_by_appearance():
    """P 编号按首次出现顺序（谱面上先遇到的循环是 P1），不按覆盖量。"""
    seq = (
        [tok("I"), tok("V", "dom")] * 4  # 1-8 小循环
        + [tok("I"), tok("IV"), tok("V"), tok("vi")] * 4  # 9-24 大循环
    )
    fams = find_loop_families(seq)
    assert [f.id for f in fams] == ["P1", "P2"]
    assert fams[0].period == 2
    assert fams[1].period == 4
