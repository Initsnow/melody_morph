"""
全曲动机发现引擎
================

在整条旋律（而非单个乐句内部）上寻找重复的音乐素材，输出按显著性排序、
去冗余的动机家族。输入是"发声音符"序列（见 :class:`MelodyNote`），与
调性/和弦标注解耦，可复用于任何音轨。

匹配表示（从严格到宽松）:

- ``exact``    绝对音高序列        —— 逐字反复（如副歌 4 小节块原样重现）
- ``interval`` 有符号半音音程序列   —— 移调反复（序列）
- ``octave``   八度折叠的音程序列   —— 八度移位变体
- ``degree``   调内音级序列        —— 调内转位变体（需音符带 ``degree_pc``）
- ``contour``  上下行方向序列      —— 容忍节奏变化的轮廓反复
- ``rhythm``   时值符号序列        —— 纯节奏型反复

算法:

1. 按休止间隔把旋律切成"段"；匹配窗口不跨段，但**分组跨段进行**——
   同一个窗口 token 在不同段/乐句/段落里的出现会被归为一组。
2. 对每种表示做全局 n-gram 哈希分组，用"最大重复"剪枝去掉嵌套冗余。
3. 每组按 onset 贪心取非重叠出现；出现数不足 ``min_occurrences`` 则丢弃。
4. 跨表示合并：更严格/更长的候选优先选中；时间跨度重叠 ≥70% 的候选
   视为同一素材的另一种表示而丢弃（抑制相位噪声与重复计数）。
5. 显著性打分 = 出现次数 + 长度 + 音程/方向/节奏多样性 + 小节对齐奖励
   - 同音/单调反复/邻音振荡/节奏单调等惩罚。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import log2
from typing import Optional

EPS = 1e-6

# 表示特异性：数值越小越严格
_REP_ORDER = {
    "exact": 0,
    "interval": 1,
    "octave": 2,
    "degree": 3,
    "contour": 4,
    "rhythm": 5,
}

_RHYTHM_NAMES = (
    (4.0, "whole"),
    (3.0, "dotted half"),
    (2.0, "half"),
    (1.5, "dotted quarter"),
    (1.0, "quarter"),
    (0.75, "dotted eighth"),
    (0.5, "eighth"),
    (0.375, "dotted sixteenth"),
    (0.25, "sixteenth"),
    (0.125, "thirty-second"),
)


@dataclass
class MelodyNote:
    """旋律中的一个发声音符（动机引擎的输入单元）。"""

    index: int                      # 输入列表中的位置（0 起）
    midi: int
    onset_quarters: float
    duration_quarters: float
    measure: int
    position_quarters: float
    section: Optional[str] = None
    phrase_id: Optional[int] = None
    degree_pc: Optional[int] = None  # 调内音级偏移（主音=0，属音=4）；非调内音为 None


@dataclass
class Occurrence:
    """动机的一次出现。"""

    note_start: int                 # 闭区间（MelodyNote 位置）
    note_end: int
    onset_quarters: float
    end_quarters: float
    measure: int
    phrase_id: Optional[int] = None
    section: Optional[str] = None
    transposition: int = 0          # 相对代表出现的移调半音数
    variant: str = "exact"          # 命中的表示：exact/interval/octave/degree/contour/rhythm
    rhythm_variant: bool = False    # 时值模式与代表出现不同（节奏变体）
    function: str = "发展"
    position_in_phrase: str = "中段"


@dataclass
class Motif:
    """一个去冗余后的动机家族。"""

    motif_id: str
    kind: str                       # melodic | rhythmic
    length_notes: int
    length_quarters: float
    interval_seq: tuple             # 代表形态的有符号音程序列（rhythm 动机为空）
    rhythm_seq: tuple               # 代表形态的时值符号序列
    representative: Occurrence
    occurrences: list[Occurrence]
    score: float
    rep: str                        # 命中的表示名
    abc: str = ""


@dataclass
class _Candidate:
    rep: str
    length_notes: int
    occ: list[tuple[int, int, float, float]]  # (gstart, gend, onset, end)
    iv_seq: tuple
    rh_seq: tuple
    aligned_fraction: float
    score: float


def find_motifs(
    notes: list[MelodyNote],
    min_notes: int = 3,
    max_notes: int = 24,
    min_occurrences: int = 2,
    segment_gap: float = 1.0,
    max_results: int = 80,
) -> list[Motif]:
    """在整条旋律上发现重复动机。

    :param notes: 按 onset 排序的发声音符列表。
    :param min_notes: 最短动机长度（音符数）。
    :param max_notes: 最长动机长度（音符数）。
    :param min_occurrences: 非重叠出现次数下限。
    :param segment_gap: 超过该四分音符长度的休止视为旋律段边界。
    :param max_results: 最多保留的动机数量（0 = 不限制）。
    """
    if not notes or min_notes < 2:
        return []
    seg_of = _segment_ids(notes, segment_gap)
    candidates: list[_Candidate] = []
    for rep in ("exact", "interval", "octave", "degree", "contour"):
        _collect_pitch_candidates(
            candidates, notes, seg_of, rep, min_notes, max_notes, min_occurrences
        )
    _collect_rhythm_candidates(
        candidates, notes, seg_of, min_notes, max_notes, min_occurrences
    )
    selected = _select_candidates(candidates, max_results)
    return [_finalize_motif(c, index, notes) for index, c in enumerate(selected, 1)]


# ---------------------------------------------------------------------------
# 分段与表示
# ---------------------------------------------------------------------------


def _segment_ids(notes: list[MelodyNote], segment_gap: float) -> list[int]:
    """每个音符所属旋律段编号（相邻休止 < segment_gap 才同段）。"""
    seg_of: list[int] = []
    current = 0
    for i, note in enumerate(notes):
        if i > 0:
            prev = notes[i - 1]
            gap = note.onset_quarters - (prev.onset_quarters + prev.duration_quarters)
            if gap >= segment_gap - EPS:
                current += 1
        seg_of.append(current)
    return seg_of


def _signed_intervals(midis: list[int]) -> tuple[int, ...]:
    return tuple(midis[i + 1] - midis[i] for i in range(len(midis) - 1))


def _fold_octave(interval: int) -> int:
    """八度折叠：±12 视为 0；其余折叠到最小绝对值（方向尽量保留）。"""
    r = interval % 12
    if r > 6:
        r -= 12
    return r


def _contour(interval: int) -> int:
    return 1 if interval > 0 else -1 if interval < 0 else 0


def _rhythm_symbol(duration_quarters: float) -> str:
    if duration_quarters <= 0:
        return "zero"
    for value, name in _RHYTHM_NAMES:
        if abs(duration_quarters - value) < 0.001:
            return name
    return f"{duration_quarters:g}q"


# ---------------------------------------------------------------------------
# 候选收集（全局窗口 + 跨段分组）
# ---------------------------------------------------------------------------


def _collect_pitch_candidates(
    candidates: list[_Candidate],
    notes: list[MelodyNote],
    seg_of: list[int],
    rep: str,
    min_notes: int,
    max_notes: int,
    min_occurrences: int,
) -> None:
    midis = [n.midi for n in notes]
    if rep == "exact":
        seq = tuple(midis)
        interval_repr = False
    elif rep == "interval":
        seq = _signed_intervals(midis)
        interval_repr = True
    elif rep == "octave":
        seq = tuple(_fold_octave(d) for d in _signed_intervals(midis))
        interval_repr = True
    elif rep == "degree":
        if any(n.degree_pc is None for n in notes):
            return
        seq = tuple(n.degree_pc for n in notes)  # type: ignore[union-attr]
        interval_repr = False
    elif rep == "contour":
        seq = tuple(_contour(d) for d in _signed_intervals(midis))
        interval_repr = True
    else:
        return
    _collect_windows(
        candidates,
        notes,
        seg_of,
        rep,
        seq,
        interval_repr,
        min_notes,
        max_notes,
        min_occurrences,
    )


def _collect_windows(
    candidates: list[_Candidate],
    notes: list[MelodyNote],
    seg_of: list[int],
    rep: str,
    seq: tuple,
    interval_repr: bool,
    min_notes: int,
    max_notes: int,
    min_occurrences: int,
) -> None:
    total = len(notes)
    upper = min(max_notes, total)
    for length in range(min_notes, upper + 1):
        width = length - 1 if interval_repr else length
        if width <= 0 or width > len(seq):
            continue
        buckets: dict[tuple, list[int]] = defaultdict(list)
        for start in range(total - length + 1):
            if seg_of[start] != seg_of[start + length - 1]:
                continue  # 窗口跨段，不参与匹配
            buckets[seq[start : start + width]].append(start)
        for token, starts in buckets.items():
            if len(starts) < 2:
                continue
            if not _is_maximal(seq, starts, width, seg_of, length):
                continue
            kept = _non_overlapping_starts(starts, length)
            if len(kept) < min_occurrences:
                continue
            occ = [
                (
                    s,
                    s + length - 1,
                    notes[s].onset_quarters,
                    notes[s + length - 1].onset_quarters
                    + notes[s + length - 1].duration_quarters,
                )
                for s in kept
            ]
            aligned = sum(
                1 for s in kept if notes[s].position_quarters < EPS
            ) / len(kept)
            iv_seq = _signed_intervals(
                [n.midi for n in notes[kept[0] : kept[0] + length]]
            )
            rh_seq = tuple(
                _rhythm_symbol(n.duration_quarters)
                for n in notes[kept[0] : kept[0] + length]
            )
            candidates.append(
                _Candidate(
                    rep=rep,
                    length_notes=length,
                    occ=occ,
                    iv_seq=iv_seq,
                    rh_seq=rh_seq,
                    aligned_fraction=aligned,
                    score=_salience(length, len(occ), iv_seq, rh_seq, aligned),
                )
            )


def _collect_rhythm_candidates(
    candidates: list[_Candidate],
    notes: list[MelodyNote],
    seg_of: list[int],
    min_notes: int,
    max_notes: int,
    min_occurrences: int,
) -> None:
    # 符号序列：每个发声音符一个时值符号，音符间短休止也各占一个符号。
    syms: list[tuple] = []
    note_sym: list[int] = []
    for i, note in enumerate(notes):
        note_sym.append(len(syms))
        syms.append(("note", round(note.duration_quarters, 6)))
        if i + 1 < len(notes):
            gap = notes[i + 1].onset_quarters - (
                note.onset_quarters + note.duration_quarters
            )
            if gap > EPS:
                syms.append(("rest", round(gap, 6)))

    total = len(notes)
    upper = min(max_notes, total)
    for length in range(min_notes, upper + 1):
        buckets: dict[tuple, list[int]] = defaultdict(list)
        for start in range(total - length + 1):
            if seg_of[start] != seg_of[start + length - 1]:
                continue
            lo = note_sym[start]
            hi = note_sym[start + length - 1] + 1  # 含最后一个音符的符号，不含其后休止
            buckets[tuple(syms[lo:hi])].append(start)
        for token, starts in buckets.items():
            if len(starts) < 2:
                continue
            if not _rhythm_maximal(syms, note_sym, starts, length, seg_of):
                continue
            kept = _non_overlapping_starts(starts, length)
            if len(kept) < min_occurrences:
                continue
            occ = [
                (
                    s,
                    s + length - 1,
                    notes[s].onset_quarters,
                    notes[s + length - 1].onset_quarters
                    + notes[s + length - 1].duration_quarters,
                )
                for s in kept
            ]
            aligned = sum(
                1 for s in kept if notes[s].position_quarters < EPS
            ) / len(kept)
            rh_seq = tuple(
                _rhythm_symbol(n.duration_quarters)
                for n in notes[kept[0] : kept[0] + length]
            )
            candidates.append(
                _Candidate(
                    rep="rhythm",
                    length_notes=length,
                    occ=occ,
                    iv_seq=(),
                    rh_seq=rh_seq,
                    aligned_fraction=aligned,
                    score=_salience(length, len(occ), (), rh_seq, aligned),
                )
            )


# ---------------------------------------------------------------------------
# 最大重复与去重
# ---------------------------------------------------------------------------


def _is_maximal(
    seq: tuple,
    starts: list[int],
    width: int,
    seg_of: list[int],
    length: int,
) -> bool:
    """窗口组是否不能再左右扩展（最大重复）。扩展不能跨段。"""
    # 左扩展：窗口起点向前挪一个音符。
    can_left = all(s > 0 and seg_of[s - 1] == seg_of[s] for s in starts)
    if can_left:
        left = seq[starts[0] - 1]
        if all(seq[s - 1] == left for s in starts):
            return False
    # 右扩展：窗口终点向后挪一个音符（保持不跨段）。
    can_right = all(
        s + length < len(seg_of) and seg_of[s + length] == seg_of[s]
        for s in starts
    )
    if can_right:
        right = seq[starts[0] + width]
        if all(seq[s + width] == right for s in starts):
            return False
    return True


def _rhythm_maximal(
    syms: list[tuple],
    note_sym: list[int],
    starts: list[int],
    length: int,
    seg_of: list[int],
) -> bool:
    """节奏窗口组是否不能再左右扩展。"""
    if all(s > 0 and seg_of[s - 1] == seg_of[s] and note_sym[s] > 0 for s in starts):
        left = note_sym[starts[0]] - 1
        if all(
            note_sym[s] - 1 == left and syms[note_sym[s] - 1] == syms[left]
            for s in starts
        ):
            return False
    if all(
        s + length < len(seg_of) and seg_of[s + length] == seg_of[s]
        for s in starts
    ):
        hi0 = note_sym[starts[0] + length - 1] + 1
        if hi0 < len(syms) and all(
            note_sym[s + length - 1] + 1 == hi0
            and syms[note_sym[s + length - 1] + 1] == syms[hi0]
            for s in starts
        ):
            return False
    return True


def _non_overlapping_starts(starts: list[int], width: int) -> list[int]:
    starts = sorted(set(starts))
    kept: list[int] = []
    for start in starts:
        if not kept or start >= kept[-1] + width:
            kept.append(start)
    return kept


def _span_ratio(lo: float, hi: float, slo: float, shi: float) -> float:
    start = max(lo, slo)
    end = min(hi, shi)
    if end <= start:
        return 0.0
    duration = hi - lo
    if duration <= EPS:
        return 1.0
    return (end - start) / duration


def _covered(candidate: _Candidate, selected: list[_Candidate]) -> bool:
    """候选是否只是某个已选动机的重叠窗口/另一种表示。"""
    for sel in selected:
        sel_spans = [(o[2], o[3]) for o in sel.occ]
        hits = sum(
            1
            for o in candidate.occ
            if any(
                _span_ratio(o[2], o[3], s0, s1) >= 0.7 for s0, s1 in sel_spans
            )
        )
        if hits >= 0.7 * len(candidate.occ):
            return True
    return False


def _select_candidates(
    candidates: list[_Candidate], max_results: int
) -> list[_Candidate]:
    candidates.sort(key=lambda c: (_REP_ORDER[c.rep], -c.length_notes, -c.score))
    selected: list[_Candidate] = []
    for c in candidates:
        if _covered(c, selected):
            continue
        selected.append(c)
        if max_results > 0 and len(selected) >= max_results:
            break
    selected.sort(key=lambda c: -c.score)
    return selected


# ---------------------------------------------------------------------------
# 显著性打分
# ---------------------------------------------------------------------------


def _is_oscillation(iv_seq: tuple) -> bool:
    """邻音来回振荡（如 C-B-C、C-D-C-D 颤音），音乐显著性低。"""
    if len(iv_seq) < 2:
        return False
    values = set(iv_seq)
    if not values <= {-1, 0, 1}:
        return False
    return len(values - {0}) >= 2


def _salience(
    length: int,
    occurrences: int,
    iv_seq: tuple,
    rh_seq: tuple,
    aligned_fraction: float = 0.0,
) -> float:
    n_iv = len(iv_seq)
    unison = n_iv > 0 and all(d == 0 for d in iv_seq)
    distinct_iv = len(set(iv_seq))
    signs = [1 if d > 0 else -1 if d < 0 else 0 for d in iv_seq]
    dir_changes = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
    distinct_rh = len(set(rh_seq))
    score = (
        3.0 * log2(min(occurrences, 8))
        + 1.2 * log2(length)
        + 0.6 * min(distinct_iv, 6)
        + 0.4 * min(dir_changes, 6)
        + 0.5 * min(distinct_rh, 4)
        + 1.5 * aligned_fraction
    )
    if unison:
        score -= 4.0
    elif n_iv > 0 and distinct_iv <= 1:
        score -= 2.5
    elif n_iv > 0 and distinct_iv <= 2 and all(abs(d) <= 2 for d in iv_seq):
        score -= 1.5  # 纯级进/小跳的单调反复
    if _is_oscillation(iv_seq):
        score -= 3.0
    if distinct_rh <= 1:
        score -= 1.5  # 单一节奏型反复
    return score


# ---------------------------------------------------------------------------
# 结果整理
# ---------------------------------------------------------------------------


def _phrase_ranges(notes: list[MelodyNote]) -> dict[int, tuple[int, int]]:
    ranges: dict[int, tuple[int, int]] = {}
    for i, note in enumerate(notes):
        if note.phrase_id is None:
            continue
        lo, hi = ranges.get(note.phrase_id, (i, i))
        ranges[note.phrase_id] = (min(lo, i), max(hi, i))
    return ranges


def _position_in_phrase(
    ranges: dict[int, tuple[int, int]], occurrence: Occurrence
) -> str:
    pid = occurrence.phrase_id
    if pid is None or pid not in ranges:
        return "中段"
    lo, hi = ranges[pid]
    at_start = occurrence.note_start == lo
    at_end = occurrence.note_end == hi
    if at_start and at_end:
        return "整句"
    if at_start:
        return "开头"
    if at_end:
        return "结尾"
    return "中段"


def _label_functions(
    occurrences: list[Occurrence], notes: list[MelodyNote]
) -> None:
    earliest = occurrences[0]
    for occurrence in occurrences:
        last = notes[occurrence.note_end]
        if occurrence is earliest:
            occurrence.function = "呈示"
            continue
        if occurrence.position_in_phrase in ("结尾", "整句"):
            if (
                last.degree_pc is not None
                and last.degree_pc in (0, 4)
                and last.duration_quarters >= 1.0
            ):
                occurrence.function = "解决"
            else:
                occurrence.function = "收束"
        elif occurrence.section != earliest.section:
            occurrence.function = "再现"
        else:
            occurrence.function = "发展"


def _finalize_motif(
    candidate: _Candidate, index: int, notes: list[MelodyNote]
) -> Motif:
    rep_gs, rep_ge, _, _ = min(candidate.occ, key=lambda o: o[2])
    rep_notes = notes[rep_gs : rep_ge + 1]
    rep_rhythm = tuple(_rhythm_symbol(n.duration_quarters) for n in rep_notes)
    ranges = _phrase_ranges(notes)

    occurrences: list[Occurrence] = []
    for gs, ge, onset, end in candidate.occ:
        first = notes[gs]
        rhythm_variant = tuple(
            _rhythm_symbol(n.duration_quarters) for n in notes[gs : ge + 1]
        ) != rep_rhythm
        occurrence = Occurrence(
            note_start=gs,
            note_end=ge,
            onset_quarters=onset,
            end_quarters=end,
            measure=first.measure,
            phrase_id=first.phrase_id,
            section=first.section,
            transposition=first.midi - notes[rep_gs].midi,
            variant=candidate.rep,
            rhythm_variant=rhythm_variant,
        )
        occurrence.position_in_phrase = _position_in_phrase(ranges, occurrence)
        occurrences.append(occurrence)
    occurrences.sort(key=lambda o: o.onset_quarters)
    _label_functions(occurrences, notes)

    first_occ = occurrences[0]
    return Motif(
        motif_id=f"M{index}",
        kind="rhythmic" if candidate.rep == "rhythm" else "melodic",
        length_notes=candidate.length_notes,
        length_quarters=(
            rep_notes[-1].onset_quarters
            + rep_notes[-1].duration_quarters
            - rep_notes[0].onset_quarters
        ),
        interval_seq=candidate.iv_seq,
        rhythm_seq=rep_rhythm,
        representative=first_occ,
        occurrences=occurrences,
        score=candidate.score,
        rep=candidate.rep,
    )
