"""
melody_motifs 动机引擎测试。

覆盖：跨段精确反复、移调反复与移调标注、八度折叠变体、纯节奏型反复、
同音跑动降权、最大重复剪枝（嵌套不重复报告）、非重叠出现选取、节奏变体
标注。
"""

from __future__ import annotations

from melody_motifs import (
    MelodyNote,
    _non_overlapping_starts,
    find_motifs,
)


def note(midi: int, onset: float, dur: float = 1.0, degree_pc=None, measure: int = 1) -> MelodyNote:
    return MelodyNote(
        index=0,
        midi=midi,
        onset_quarters=onset,
        duration_quarters=dur,
        measure=measure,
        position_quarters=onset,
        degree_pc=degree_pc,
    )


def notes_of(midis: list[int], onsets: list[float], durs=None) -> list[MelodyNote]:
    durs = durs or [1.0] * len(midis)
    return [note(m, o, d) for m, o, d in zip(midis, onsets, durs)]


def find(notes: list[MelodyNote], **kwargs) -> list:
    return find_motifs(notes, **kwargs)


def test_cross_segment_exact_repeat():
    """同一旋律出现在两个不同段（乐句），应归并为一个动机。"""
    # 段 A：m1-2 的 5 个音；段 B：m5-6 的同样 5 个音（中间隔 2 个四分音符休止）
    a = notes_of([60, 62, 64, 65, 67], [0.0, 1.0, 2.0, 3.0, 4.0])
    b = notes_of([60, 62, 64, 65, 67], [8.0, 9.0, 10.0, 11.0, 12.0])
    for i, n in enumerate(a + b):
        n.index = i
        n.measure = 1 if i < 5 else 2
        n.phrase_id = 1 if i < 5 else 2
    motifs = find(a + b)
    assert len(motifs) >= 1
    top = motifs[0]
    assert top.length_notes == 5
    assert len(top.occurrences) == 2
    assert top.rep == "exact"
    assert all(o.transposition == 0 for o in top.occurrences)
    assert [o.phrase_id for o in top.occurrences] == [1, 2]


def test_transposed_repeat():
    """移调反复：音程模式相同、绝对音高不同 → interval 表示命中并标移调。"""
    a = notes_of([60, 62, 64, 67], [0.0, 1.0, 2.0, 3.0])
    b = notes_of([65, 67, 69, 72], [8.0, 9.0, 10.0, 11.0])  # +5 移调
    for i, n in enumerate(a + b):
        n.index = i
        n.measure = 1 if i < 4 else 2
        n.phrase_id = 1 if i < 4 else 2
    motifs = find(a + b)
    assert motifs, "移调反复应被发现"
    top = motifs[0]
    assert len(top.occurrences) == 2
    assert top.rep == "interval"
    assert top.occurrences[1].transposition == 5


def test_octave_folded_variant():
    """八度折叠：C-D-C(高八度) 与 C-D-C 视为同一轮廓变体。"""
    # (60,62,72)：间隔 (2,10)，折叠后 (2,-2)；与 (60,62,60) 的 (2,-2) 一致
    a = notes_of([60, 62, 72], [0.0, 1.0, 2.0])
    b = notes_of([60, 62, 60], [8.0, 9.0, 10.0])
    for i, n in enumerate(a + b):
        n.index = i
        n.measure = 1 if i < 3 else 2
        n.phrase_id = 1 if i < 3 else 2
    motifs = find(a + b)
    assert any(m.rep == "octave" for m in motifs), "八度折叠变体应命中 octave 表示"


def test_rhythm_only_repeat():
    """纯节奏反复：音高轮廓不同、节奏相同 → rhythm 动机。"""
    a = notes_of([60, 64, 62], [0.0, 0.5, 1.5], durs=[0.5, 1.0, 1.0])
    b = notes_of([72, 67, 74], [8.0, 8.5, 9.5], durs=[0.5, 1.0, 1.0])
    for i, n in enumerate(a + b):
        n.index = i
        n.measure = 1 if i < 3 else 2
        n.phrase_id = 1 if i < 3 else 2
    motifs = find(a + b)
    rhythm = [m for m in motifs if m.kind == "rhythmic"]
    assert rhythm, "相同节奏应生成 rhythm 动机"


def test_unison_run_ranked_low():
    """同音跑动应被显著性惩罚，不能压过有意义的动机。"""
    unison: list[MelodyNote] = []
    distinctive: list[MelodyNote] = []
    onset = 0.0
    for _ in range(5):
        unison += notes_of([60, 60, 60], [onset, onset + 1, onset + 2])
        onset += 4.0
    onset = 40.0
    for _ in range(2):
        distinctive += notes_of([60, 62, 64, 67, 69], [onset, onset + 1, onset + 2, onset + 3, onset + 4])
        onset += 8.0
    all_notes = unison + distinctive
    for i, n in enumerate(all_notes):
        n.index = i
        n.measure = 1
        n.phrase_id = 1
    motifs = find(all_notes)
    assert motifs
    top = motifs[0]
    # 顶部动机应该是上升五音音阶式图式，而不是同音重复
    assert top.interval_seq != (0, 0)
    assert top.length_notes >= 4


def test_maximal_repeat_single_entry():
    """10 音整块反复只报告一次，不产生嵌套的 3-9 音重复条目。"""
    block = [60, 62, 64, 65, 67, 69, 71, 72, 74, 76]
    a = notes_of(block, list(range(10)))
    b = notes_of(block, [20.0 + i for i in range(10)])
    # 前后缀不同（且与整块隔开休止），保证只有整块可扩展
    prefix = notes_of([48, 50], [17.0, 18.0])
    suffix = notes_of([83, 81], [32.0, 33.0])
    all_notes = sorted(prefix + a + b + suffix, key=lambda n: n.onset_quarters)
    for i, n in enumerate(all_notes):
        n.index = i
        n.measure = 1
        n.phrase_id = 1
    motifs = find(all_notes, min_notes=3, max_notes=20)
    assert len(motifs) == 1, f"应只有一个最大动机，实际 {len(motifs)} 个"
    assert motifs[0].length_notes == 10
    assert len(motifs[0].occurrences) == 2


def test_non_overlapping_occurrences():
    """同 token 相位重叠的起点按贪心取非重叠窗口。"""
    starts = [0, 1, 2, 5, 6, 10]
    assert _non_overlapping_starts(starts, 3) == [0, 5, 10]


def test_rhythm_variant_label():
    """同一音高序列节奏不同时，后一次出现标记为节奏变体。"""
    a = notes_of([60, 62, 64], [0.0, 1.0, 2.0], durs=[1.0, 1.0, 1.0])
    b = notes_of([60, 62, 64], [8.0, 9.0, 10.0], durs=[0.5, 0.5, 0.5])
    for i, n in enumerate(a + b):
        n.index = i
        n.measure = 1 if i < 3 else 2
        n.phrase_id = 1 if i < 3 else 2
    motifs = find(a + b)
    top = motifs[0]
    assert len(top.occurrences) == 2
    variants = [o.rhythm_variant for o in top.occurrences]
    assert True in variants, "节奏不同的出现应标记 rhythm_variant"
