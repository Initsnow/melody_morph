"""
循环和弦进行检测测试（gp-chords --progressions 的核心）。

覆盖：精确循环、变体重复、非循环、分散重复归族、覆盖选择与重叠过滤、
chord_token 的罗马度数提取。
"""

from __future__ import annotations

from gpchords.annotate import CHORD_TEMPLATES
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
