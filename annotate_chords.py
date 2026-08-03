"""
自动和弦标注脚本
================

解析 Guitar Pro (.gp / .gpx) 文件，按小节（或半小节 / 节拍）自动识别和弦，
并可与文件里手工标注的和弦进行对照。

原理:

1. 用 :mod:`gp_parser` 解析文件，提取指定轨道的音符与时值。
2. 确定调性：优先使用 GP 文件里的调号；没有调号时用
   Krumhansl-Kessler 键感轮廓估计。
3. 在每个分析窗口内收集音级（按音符时值加权），对 21 种和弦模板
   （大/小/属/挂留/强力和弦等）打分：
   命中音加分、非和弦音扣分、缺失和弦音扣分，并给予「调内根音」
   和「低音等于根音」小幅加成，最终取最高分。
4. 吉他风格下，没有三音时收敛成强力和弦（5），低音不是根音时写成
   斜杠和弦（如 ``C5/G``），与 Guitar Pro 里的常见记法一致。
5. ``--write`` 可以把识别结果写回一个新的 ``.gp`` 文件：向目标轨道的
   和弦库（DiagramCollection）添加和弦项，并在对应拍上写 ``<Chord>`` 引用。
   已有手工标注的小节默认跳过，不会被覆盖。

用法::

    # 默认：交互选择轨道，按小节识别并自动写回 <原名>_chords.gp
    uv run python annotate_chords.py "xxx.gp"

    # 指定轨道，按小节标注 Lead Guitar
    uv run python annotate_chords.py "xxx.gp" --track "Lead Guitar"

    # 按节拍标注，并输出 JSON
    uv run python annotate_chords.py "xxx.gp" --track 0 --window beat --out chords.json

    # 只看分析结果，不写回；--debug 输出每个小节的明细
    uv run python annotate_chords.py "xxx.gp" --track "Lead Guitar" --no-write --debug

    # 不依赖具体文件，看算法演示
    uv run python annotate_chords.py --demo
"""

from __future__ import annotations

import argparse
import copy
import html
import io
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from gp_parser import (
    GPSong,
    GuitarProError,
    GPBeat,
    GPMeasure,
    GPNote,
    GPTrack,
    parse_gp,
    select_track,
)

# ---------------------------------------------------------------------------
# 音乐理论基础
# ---------------------------------------------------------------------------

# 和弦模板: 品质 -> (相对根音的音级集合, 名称后缀)
CHORD_TEMPLATES: dict[str, tuple[tuple[int, ...], str]] = {
    "maj": ((0, 4, 7), ""),
    "min": ((0, 3, 7), "m"),
    "dim": ((0, 3, 6), "dim"),
    "aug": ((0, 4, 8), "aug"),
    "sus2": ((0, 2, 7), "sus2"),
    "sus4": ((0, 5, 7), "sus4"),
    "5": ((0, 7), "5"),
    "6": ((0, 4, 7, 9), "6"),
    "m6": ((0, 3, 7, 9), "m6"),
    "7": ((0, 4, 7, 10), "7"),
    "maj7": ((0, 4, 7, 11), "maj7"),
    "m7": ((0, 3, 7, 10), "m7"),
    "m7b5": ((0, 3, 6, 10), "m7b5"),
    "dim7": ((0, 3, 6, 9), "dim7"),
    "add9": ((0, 2, 4, 7), "add9"),
    "madd9": ((0, 2, 3, 7), "madd9"),
    "9": ((0, 2, 4, 7, 10), "9"),
    "maj9": ((0, 2, 4, 7, 11), "maj9"),
    "m9": ((0, 2, 3, 7, 10), "m9"),
    "7sus4": ((0, 5, 7, 10), "7sus4"),
    "6/9": ((0, 2, 4, 7, 9), "6/9"),
}

_SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
_FLAT_KEYS = {5, 10, 3, 8, 1, 6, 11}  # F Bb Eb Ab Db Gb Cb 用降号

# Krumhansl-Kessler 键感轮廓
KRUMHANSL_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KRUMHANSL_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def pc_name(pc: int, key_root: Optional[int] = None) -> str:
    """音级 -> 音名（按调性选择升/降号记法）。"""
    names = _FLAT_NAMES if key_root in _FLAT_KEYS else _SHARP_NAMES
    return names[pc % 12]


def parse_key_name(text: str) -> tuple[int, str]:
    """解析调名（C / Am / F#m / Bb ...）-> (根音音级, Major|Minor)。"""
    s = text.strip()
    if not s:
        raise ValueError("空调名")
    minor = s.endswith("m") and not s.endswith("maj")
    core = s[:-1] if minor else s
    for names in (_SHARP_NAMES, _FLAT_NAMES):
        if core in names:
            return names.index(core), ("Minor" if minor else "Major")
    raise ValueError(f"无法解析调名: {text!r}")


def estimate_key(weights: dict[int, float]) -> tuple[int, str]:
    """Krumhansl-Kessler：根据音级权重估计 (根音, 调式)。"""
    best: tuple[float, int, str] = (-1e18, 0, "Major")
    for root in range(12):
        for mode, profile in (("Major", KRUMHANSL_MAJOR), ("Minor", KRUMHANSL_MINOR)):
            corr = sum(weights.get((root + i) % 12, 0.0) * profile[i] for i in range(12))
            if corr > best[0]:
                best = (corr, root, mode)
    return best[1], best[2]


def _diatonic_pcs(root: int, mode: str) -> set[int]:
    if mode == "Minor":  # 自然小调
        return {(root + i) % 12 for i in (0, 2, 3, 5, 7, 8, 10)}
    return {(root + i) % 12 for i in (0, 2, 4, 5, 7, 9, 11)}


def _manual_root_pc(name: str) -> Optional[int]:
    """从和弦名里取出根音音级，如 'C5/G' -> 0，'Bb7' -> 10。"""
    s = name.strip()
    for names in (_SHARP_NAMES, _FLAT_NAMES):
        for root in sorted(names, key=len, reverse=True):
            if s.startswith(root):
                return names.index(root)
    return None


# ---------------------------------------------------------------------------
# 和弦识别
# ---------------------------------------------------------------------------


def note_weights(notes: list[GPNote]) -> dict[int, float]:
    """音符 -> 音级权重（按四分音符时值加权；无时值信息时按 1）。"""
    weights: dict[int, float] = defaultdict(float)
    for n in notes:
        weights[n.midi % 12] += max(n.duration_quarters, 1.0)
    return dict(weights)


def detect_chord(
    notes: list[GPNote],
    key_root: Optional[int] = None,
    key_mode: str = "Major",
    style: str = "guitar",
) -> Optional[dict]:
    """
    识别一段音符最可能的和弦。

    style="guitar" 时没有三音会收敛为强力和弦（5），低音非根音写成斜杠和弦，
    与 Guitar Pro 常见记法一致；style="theory" 时输出理论上的完整和弦。
    """
    if not notes:
        return None
    weights = note_weights(notes)
    total = sum(weights.values())
    bass_pc = min(n.midi for n in notes) % 12
    key_pcs = _diatonic_pcs(key_root, key_mode) if key_root is not None else None

    candidates = []
    for root in range(12):
        for quality, (tpl, suffix) in CHORD_TEMPLATES.items():
            tset = {(pc + root) % 12 for pc in tpl}
            matched = sum(v for pc, v in weights.items() if pc in tset)
            unmatched = total - matched
            # 奥卡姆剃刀：模板每多一个音都要付出代价，
            # 只有音符确实构成 7/9/11 和弦时扩展模板才划算。
            score = matched - 0.8 * unmatched - 1.0 * len(tpl)
            # 调性先验：主音 > 其他调内音 > 调外音
            if key_pcs is not None and root in key_pcs:
                score += (0.20 if root == key_root else 0.10) * matched
            # 低音一致性：低音等于根音时加分
            if bass_pc == root:
                score += 0.05 * matched
            candidates.append((score, matched, root, quality, suffix, tset))

    # 排序：分数 > 根音出现权重 > 模板更简单 > 根音编号更小
    candidates.sort(
        key=lambda c: (c[0], weights.get(c[2], 0.0), -len(c[4]), -c[2]),
        reverse=True,
    )
    score, matched, root, quality, suffix, tset = candidates[0]

    matched_pcs = {pc for pc in weights if pc in tset}
    if style == "guitar" and quality != "5" and matched_pcs <= {root, (root + 7) % 12}:
        quality, suffix = "5", "5"
        tset = {root, (root + 7) % 12}
        matched = sum(v for pc, v in weights.items() if pc in tset)
        score = matched - 0.8 * (total - matched)

    name = f"{pc_name(root, key_root)}{suffix}"
    if bass_pc != root:
        name += f"/{pc_name(bass_pc, key_root)}"
    return {
        "name": name,
        "root": root,
        "quality": quality,
        "bass_pc": bass_pc,
        "score": round(score, 2),
        "weights": {
            pc_name(pc, key_root): round(v, 2) for pc, v in sorted(weights.items())
        },
    }


# ---------------------------------------------------------------------------
# 分析窗口
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    bar: int
    section: Optional[str]
    window: str  # measure / half / beat
    start_quarters: float
    duration_quarters: float
    notes: list[GPNote]
    manual: Optional[str] = None
    anchor_beat_id: Optional[str] = None  # 写回 .gp 时挂和弦的目标拍
    anchor_voice_id: Optional[str] = None  # 目标拍所在声部（定位用）
    anchor_pos: int = -1  # 目标拍在声部 Beats 序列中的位置


def _measure_duration(measure: GPMeasure) -> float:
    if measure.time_signature:
        num, den = measure.time_signature
        return num * 4.0 / den
    if measure.beats:
        return sum(b.duration_quarters for b in measure.beats)
    return 4.0


def segment_measure(measure: GPMeasure) -> list[Segment]:
    notes = [n for b in measure.beats for n in b.notes]
    manual = next((b.chord.name for b in measure.beats if b.chord), None)
    anchor = next((b for b in measure.beats if b.notes), None)
    return [
        Segment(
            bar=measure.index,
            section=measure.section,
            window="measure",
            start_quarters=0.0,
            duration_quarters=_measure_duration(measure),
            notes=notes,
            manual=manual,
            anchor_beat_id=anchor.id if anchor else None,
            anchor_voice_id=anchor.voice_id if anchor else None,
            anchor_pos=anchor.position_in_voice if anchor else -1,
        )
    ]


def segment_half(measure: GPMeasure) -> list[Segment]:
    half = _measure_duration(measure) / 2.0
    groups: dict[int, list[GPBeat]] = {0: [], 1: []}
    for beat in measure.beats:
        groups[0 if beat.start_quarters < half else 1].append(beat)
    segments = []
    for idx, beats in groups.items():
        notes = [n for b in beats for n in b.notes]
        if not notes:
            continue
        manual = next((b.chord.name for b in beats if b.chord), None)
        anchor = next((b for b in beats if b.notes), None)
        segments.append(
            Segment(
                bar=measure.index,
                section=measure.section,
                window=f"half{idx + 1}",
                start_quarters=0.0 if idx == 0 else half,
                duration_quarters=half,
                notes=notes,
                manual=manual,
                anchor_beat_id=anchor.id if anchor else None,
                anchor_voice_id=anchor.voice_id if anchor else None,
                anchor_pos=anchor.position_in_voice if anchor else -1,
            )
        )
    return segments


def segment_beat(measure: GPMeasure) -> list[Segment]:
    segments = []
    for beat in measure.beats:
        if not beat.notes:
            continue
        segments.append(
            Segment(
                bar=measure.index,
                section=measure.section,
                window=f"beat@{beat.start_quarters:g}",
                start_quarters=beat.start_quarters,
                duration_quarters=beat.duration_quarters,
                notes=list(beat.notes),
                manual=beat.chord.name if beat.chord else None,
                anchor_beat_id=beat.id,
                anchor_voice_id=beat.voice_id,
                anchor_pos=beat.position_in_voice,
            )
        )
    return segments


SEGMENTERS = {
    "measure": segment_measure,
    "half": segment_half,
    "beat": segment_beat,
}


def resolve_key(song, track: GPTrack, override: Optional[str]) -> tuple[int, str]:
    """确定调性：--key > 文件调号 > Krumhansl-Kessler 估计。"""
    if override:
        return parse_key_name(override)
    sig_counts = Counter(m.key_signature for m in track.measures if m.key_signature)
    if sig_counts:
        return parse_key_name(sig_counts.most_common(1)[0][0])
    weights = note_weights(track.notes)
    return estimate_key(weights)


# ---------------------------------------------------------------------------
# 手工标注对照
# ---------------------------------------------------------------------------


def compare_manual(
    track: GPTrack,
    key_root: int,
    key_mode: str,
    style: str,
) -> list[dict]:
    """
    对每个手工标注：分别用「整小节」和「标注拍到小节末」的音符识别和弦并对照。

    之所以有两种窗口，是因为 GP 里用户常把和弦挂在琶音/和弦的首个低音上，
    单看标注那一拍只有一两个音。
    """
    rows = []
    for measure in track.measures:
        for beat in measure.beats:
            if beat.chord is None:
                continue
            whole_notes = [n for b in measure.beats for n in b.notes]
            tail_notes = [
                n
                for b in measure.beats
                if b.start_quarters >= beat.start_quarters - 1e-9
                for n in b.notes
            ]
            whole = detect_chord(whole_notes, key_root, key_mode, style)
            tail = detect_chord(tail_notes, key_root, key_mode, style)
            manual_root = _manual_root_pc(beat.chord.name)
            rows.append(
                {
                    "bar": measure.index,
                    "section": measure.section,
                    "manual": beat.chord.name,
                    "manual_root": manual_root,
                    "whole": whole["name"] if whole else None,
                    "whole_root": whole["root"] if whole else None,
                    "tail": tail["name"] if tail else None,
                    "tail_root": tail["root"] if tail else None,
                }
            )
    return rows


def print_comparison(rows: list[dict]) -> None:
    if not rows:
        print("\n文件中没有手工和弦标注，跳过对照。")
        return
    print("\n手工标注对照")
    print(f"{'小节':>4}  {'手动':<8} {'整小节':<10} {'标注拍→小节末':<14} 结果")
    print("-" * 58)
    whole_exact = whole_root = tail_exact = tail_root = 0
    for r in rows:
        if r["manual_root"] is not None and r["manual_root"] == r["whole_root"]:
            whole_root += 1
        if r["manual"] == r["whole"]:
            whole_exact += 1
        if r["manual_root"] is not None and r["manual_root"] == r["tail_root"]:
            tail_root += 1
        if r["manual"] == r["tail"]:
            tail_exact += 1
        match = "OK" if r["manual"] == r["whole"] or r["manual"] == r["tail"] else "--"
        print(
            f"{r['bar']:>4}  {r['manual']:<8} {str(r['whole']):<10} "
            f"{str(r['tail']):<14} {match}"
        )
    n = len(rows)
    print("-" * 58)
    print(f"共 {n} 处手动标注 | 整小节: 名称一致 {whole_exact}/{n}，根音一致 {whole_root}/{n}")
    print(f"                   标注拍→小节末: 名称一致 {tail_exact}/{n}，根音一致 {tail_root}/{n}")


# ---------------------------------------------------------------------------
# 写回 .gp 文件
# ---------------------------------------------------------------------------

# 品质 -> (Interval, Alteration)，对应 GPIF 的 <Degree>
DEGREES: dict[str, list[tuple[str, str]]] = {
    "maj": [("Third", "Major"), ("Fifth", "Perfect")],
    "min": [("Third", "Minor"), ("Fifth", "Perfect")],
    "dim": [("Third", "Minor"), ("Fifth", "Diminished")],
    "aug": [("Third", "Major"), ("Fifth", "Augmented")],
    "sus2": [("Second", "Major"), ("Fifth", "Perfect")],
    "sus4": [("Fourth", "Perfect"), ("Fifth", "Perfect")],
    "5": [("Fifth", "Perfect")],
    "6": [("Third", "Major"), ("Fifth", "Perfect"), ("Sixth", "Major")],
    "m6": [("Third", "Minor"), ("Fifth", "Perfect"), ("Sixth", "Major")],
    "7": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Minor")],
    "maj7": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Major")],
    "m7": [("Third", "Minor"), ("Fifth", "Perfect"), ("Seventh", "Minor")],
    "m7b5": [("Third", "Minor"), ("Fifth", "Diminished"), ("Seventh", "Minor")],
    "dim7": [("Third", "Minor"), ("Fifth", "Diminished"), ("Seventh", "Diminished")],
    "add9": [("Third", "Major"), ("Fifth", "Perfect"), ("Ninth", "Major")],
    "madd9": [("Third", "Minor"), ("Fifth", "Perfect"), ("Ninth", "Major")],
    "9": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Minor"), ("Ninth", "Major")],
    "maj9": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Major"), ("Ninth", "Major")],
    "m9": [("Third", "Minor"), ("Fifth", "Perfect"), ("Seventh", "Minor"), ("Ninth", "Major")],
    "7sus4": [("Fourth", "Perfect"), ("Fifth", "Perfect"), ("Seventh", "Minor")],
    "6/9": [("Third", "Major"), ("Fifth", "Perfect"), ("Sixth", "Major"), ("Ninth", "Major")],
}


def _name_to_gpif(name: str) -> tuple[str, str]:
    """音名 -> (GPIF 的 step, accidental)。如 'C#' -> ('C','Sharp')。"""
    if name.endswith("bb"):
        return name[0], "DoubleFlat"
    if name.endswith("##"):
        return name[0], "DoubleSharp"
    if name.endswith("b"):
        return name[0], "Flat"
    if name.endswith("#"):
        return name[0], "Sharp"
    return name, "Natural"


def _make_diagram_frets(
    root_pc: int,
    quality: str,
    bass_pc: int,
    tuning: list[int],
) -> tuple[dict[int, int], int]:
    """
    为和弦生成一个简单、能放进 GP8 固定 5 品窗口的吉他指法（贪心、从低音弦往高音弦铺）。

    返回 (GPIF 弦号(0=最低音弦, 5=最高音弦) -> 品, baseFret)。开放式（品 0）的弦不返回，
    与 GP 自身保存的和弦图一致。指法只用于显示，和弦符号以名称为准。
    GP8 的指板图固定 fretCount=5，所有按弦品必须落在 (baseFret, baseFret+5] 内，
    否则 GP8 会拒绝打开文件。
    """
    if not tuning:
        tuning = [40, 45, 50, 55, 59, 64]  # 标准调弦 E A D G B E
    tpl = CHORD_TEMPLATES[quality][0]
    chord_pcs = {(root_pc + i) % 12 for i in tpl} | {bass_pc}
    target = bass_pc if bass_pc != root_pc else root_pc
    n = len(tuning)
    frets: list[Optional[int]] = [None] * n

    # 低音弦：5 品内优先，否则用最低弦
    root_si = root_fret = None
    for si in range(n):
        fret = (target - tuning[si] % 12) % 12
        if fret <= 5:
            root_si, root_fret = si, fret
            break
    if root_si is None:
        root_si = 0
        root_fret = (target - tuning[0] % 12) % 12
    frets[root_si] = root_fret
    prev_pitch = tuning[root_si] + root_fret

    for si in range(root_si + 1, n):
        best = None
        # 只搜低把位 0..5，保证结果一定能放进 5 品窗口
        for fret in range(6):
            if (tuning[si] + fret) % 12 in chord_pcs:
                pitch = tuning[si] + fret
                score = (abs(pitch - (prev_pitch + 3)), pitch)
                if best is None or score < best[0]:
                    best = (score, fret, pitch)
        if best is None:
            continue
        _, frets[si], prev_pitch = best

    fretted = {si: f for si, f in enumerate(frets) if f is not None and f > 0}
    if not fretted:
        return {}, 0
    max_fret = max(fretted.values())
    base_fret = max(0, max_fret - 5) if max_fret > 5 else 0
    if base_fret:
        # 罕见情况（非标准调弦）：超出 5 品窗口的按弦只能舍弃，保证文件可打开
        fretted = {si: f for si, f in fretted.items() if base_fret < f <= base_fret + 5}
        if not fretted:
            return {}, 0
    # GP8 的弦号是 0 起的低音到高音：0=最低音弦（如标准调弦的 6 弦 E）
    return {si: f for si, f in fretted.items()}, base_fret


def _build_chord_item(
    index: Optional[int],
    chord: dict,
    tuning: list[int],
    key_root: Optional[int],
) -> ET.Element:
    """把 detect_chord 的结果构造成 GPIF 的 <Item>（和弦库项）。"""
    name = chord["name"]
    if index is not None:
        item = ET.Element("Item", {"id": str(index), "name": name})  # 属性顺序同 GP8
    else:
        item = ET.Element("Item", {"name": name})

    frets, base_fret = _make_diagram_frets(
        chord["root"], chord["quality"], chord["bass_pc"], tuning
    )
    diagram = ET.SubElement(
        item,
        "Diagram",
        {
            "stringCount": str(len(tuning)),
            # GP8 自己的文件固定 5 品窗口；fretCount 不是最大品数
            "fretCount": "5",
            "baseFret": str(base_fret),
            "barsStates": "1 1 1 1 1",
        },
    )
    for string_no in sorted(frets):
        ET.SubElement(
            diagram,
            "Fret",
            {
                "string": str(string_no),
                "fret": str(frets[string_no] - base_fret),  # Fret 值为相对 baseFret
            },
        )

    # GP8 的 Diagram 子元素顺序：Fret* -> Fingering -> Property*（Property 直接挂在 Diagram 下）
    fingering = ET.SubElement(diagram, "Fingering")
    finger_names = {1: "Index", 2: "Middle", 3: "Ring", 4: "Pinky"}
    for string_no, fret in sorted(frets.items(), key=lambda kv: (kv[1], kv[0])):
        rel_fret = fret - base_fret
        ET.SubElement(
            fingering,
            "Position",
            {
                "finger": finger_names.get(rel_fret, "Pinky"),
                "fret": str(rel_fret),
                "string": str(string_no),
            },
        )
    for string_no in range(len(tuning)):
        if string_no not in frets:
            ET.SubElement(
                fingering,
                "Position",
                {"finger": "None", "fret": "4294967295", "string": str(string_no)},
            )
    for prop_name, value in (
        ("ShowName", "true"),
        ("ShowDiagram", "true"),
        ("ShowFingering", "false"),
    ):
        ET.SubElement(
            diagram,
            "Property",
            {"name": prop_name, "type": "bool", "value": value},
        )

    chord_el = ET.SubElement(item, "Chord")
    key_step, key_acc = _name_to_gpif(pc_name(chord["root"], key_root))
    ET.SubElement(chord_el, "KeyNote", {"step": key_step, "accidental": key_acc})
    bass_step, bass_acc = _name_to_gpif(pc_name(chord["bass_pc"], key_root))
    ET.SubElement(chord_el, "BassNote", {"step": bass_step, "accidental": bass_acc})
    for interval, alteration in DEGREES.get(chord["quality"], DEGREES["maj"]):
        ET.SubElement(
            chord_el,
            "Degree",
            {"interval": interval, "alteration": alteration, "omitted": "false"},
        )
    return item


def _set_beat_chord(beat_el: ET.Element, index: int) -> None:
    """给 <Beat> 写入/替换 <Chord>CDATA[i]</Chord>。"""
    chord_el = beat_el.find("Chord")
    if chord_el is None:
        chord_el = ET.Element("Chord")
        notes_el = beat_el.find("Notes")
        if notes_el is not None:
            beat_el.insert(list(beat_el).index(notes_el), chord_el)
        else:
            beat_el.append(chord_el)
    chord_el.text = str(index)


_CDATA_PAIR_RE = re.compile(r"<(\w+)><!\[CDATA\[(.*?)\]\]></\1>", re.S)


def _cdata_pairs_from(xml_text: str) -> list[tuple[str, str]]:
    """收集原文件里用 CDATA 包裹过的 (标签, 文本) 对。"""
    return [(m.group(1), m.group(2)) for m in _CDATA_PAIR_RE.finditer(xml_text)]


def _restore_cdata(xml_text: str, pairs: list[tuple[str, str]]) -> str:
    """
    把 ET 序列化时丢失的 CDATA 包回对应标签。

    GP8 的 GPIFReader 只认 CDATA 形式的文本（实测拍上的 <Chord> 引用若写成
    普通文本，GP8 会静默丢弃所有和弦标注）。ET 不会输出 CDATA，因此在
    序列化完成后按原文件的 (标签, 文本) 对逐个恢复；另外把拍上的
    <Chord> 数字引用全部恢复为 CDATA（新增的和弦也适用）。
    """
    # 空 CDATA 的元素（如 <SubTitle><![CDATA[]]></SubTitle>）
    empty_tags = {tag for tag, value in pairs if value == ""}
    for tag in empty_tags:
        xml_text = re.sub(
            rf"<{tag} />",
            f"<{tag}><![CDATA[]]></{tag}>",
            xml_text,
        )
    # 按 (标签, 文本) 精确匹配：ET 序列化时文本里的 & < > 已转义
    for tag, value in pairs:
        if value == "":
            continue
        xml_text = re.sub(
            rf"<{tag}>{re.escape(html.escape(value, quote=False))}</{tag}>",
            f"<{tag}><![CDATA[{value}]]></{tag}>",
            xml_text,
        )
    # 拍上的和弦引用：<Chord>CDATA[i]</Chord>
    xml_text = re.sub(
        r"<Chord>(\d+)</Chord>",
        r"<Chord><![CDATA[\1]]></Chord>",
        xml_text,
    )
    return xml_text


def write_chords_to_gp(
    input_path: str,
    output_path: str,
    song: GPSong,
    track: GPTrack,
    results: list[dict],
    key_root: Optional[int],
    overwrite: bool = False,
) -> dict:
    """
    把自动识别的和弦写回一个新的 .gp 文件（原文件不被修改）。

    规则：
    - 同名和弦已存在于轨道和弦库时直接复用，否则新增 Item（含和弦构成与指板图）。
    - 每个分析窗口挂到该窗口的第一个有音符的拍上。
    - 已有手工标注的小节默认跳过（--overwrite 时强制覆盖）。
    """
    needed: list[dict] = []
    seen_names: set[str] = set()
    for r in results:
        chord = r.get("chord")
        if chord is None or chord["name"] in seen_names:
            continue
        seen_names.add(chord["name"])
        needed.append(chord)

    existing = {c.name: i for i, c in enumerate(track.chords)}

    with zipfile.ZipFile(input_path) as zin:
        zin_infos = zin.infolist()
        file_data = {i.filename: zin.read(i.filename) for i in zin_infos}
    gpif_name = "Content/score.gpif" if "Content/score.gpif" in file_data else "score.gpif"
    root = ET.fromstring(file_data[gpif_name])

    track_el = next(
        (t for t in root.findall("Tracks/Track") if t.get("id") == str(track.id)),
        None,
    )
    if track_el is None:
        raise GuitarProError(f"在文件里找不到轨道 [{track.id}] {track.name}")
    staff_props = track_el.find("Staves/Staff/Properties")
    coll_el = working_el = None
    if staff_props is not None:
        for prop in list(staff_props):
            if prop.get("name") == "DiagramCollection":
                coll_el = prop.find("Items")
            elif prop.get("name") == "DiagramWorkingSet":
                working_el = prop.find("Items")
    if coll_el is None:
        raise GuitarProError(f"轨道 {track.name} 没有 DiagramCollection，无法写入和弦")

    # 分配新和弦的和弦库索引（追加在现有项之后）
    item_count = sum(1 for it in list(coll_el) if it.tag == "Item")
    name_to_index = dict(existing)
    new_names: list[str] = []
    for chord in needed:
        if chord["name"] in name_to_index:
            continue
        name_to_index[chord["name"]] = item_count
        new_names.append(chord["name"])
        item_count += 1

    for name in new_names:
        chord = next(c for c in needed if c["name"] == name)
        coll_el.append(_build_chord_item(name_to_index[name], chord, track.tuning, key_root))
        if working_el is not None:
            working_item = _build_chord_item(None, chord, track.tuning, key_root)
            working_el.append(working_item)

    # --- 写拍引用 ----------------------------------------------------------
    # GPIF 的 beat 对象是全局复用的（同一个 riff 拍被几十上百个位置引用），
    # 而带和弦的 beat 从不复用。因此目标拍若被多处引用，必须先克隆一个新的
    # beat 并把该位置的引用替换过去，否则和弦会泄漏到所有复用位置。
    beats_container = root.find("Beats")
    voice_els = {v.get("id"): v for v in root.findall("Voices/Voice")}
    beat_els = {b.get("id"): b for b in root.findall("Beats/Beat")}

    usage: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for v in voice_els.values():
        for pos, bid in enumerate((v.findtext("Beats") or "").split()):
            if bid != "-1":
                usage[bid].append((v.get("id", ""), pos))

    numeric_ids = [int(i) for i in beat_els if i.isdigit()]
    next_beat_id = max(numeric_ids) + 1 if numeric_ids else len(beat_els) + 1

    written = skipped_manual = skipped_existing = missing = cloned = 0
    for r in results:
        chord = r.get("chord")
        if chord is None or not r.get("anchor_voice_id") or r.get("anchor_pos", -1) < 0:
            continue
        measure = track.measures[r["bar"] - 1] if 0 < r["bar"] <= len(track.measures) else None
        if measure is None:
            continue
        if any(b.chord is not None for b in measure.beats) and not overwrite:
            skipped_manual += 1
            continue

        voice_el = voice_els.get(r["anchor_voice_id"])
        if voice_el is None:
            missing += 1
            continue
        beats_tokens = (voice_el.findtext("Beats") or "").split()
        pos = r["anchor_pos"]
        if pos >= len(beats_tokens):
            missing += 1
            continue
        current_id = beats_tokens[pos]
        beat_el = beat_els.get(current_id)
        if beat_el is None:
            missing += 1
            continue
        if not overwrite and beat_el.find("Chord") is not None:
            skipped_existing += 1
            continue

        if len(usage.get(current_id, [])) > 1:
            # 共享 beat：克隆一份，替换本位置引用
            new_id = str(next_beat_id)
            next_beat_id += 1
            new_beat = copy.deepcopy(beat_el)
            new_beat.set("id", new_id)
            if beats_container is not None:
                beats_container.append(new_beat)
            beats_tokens[pos] = new_id
            beats_el = voice_el.find("Beats")
            if beats_el is not None:
                beats_el.text = " ".join(beats_tokens)
            beat_el = new_beat
            cloned += 1

        _set_beat_chord(beat_el, name_to_index[chord["name"]])
        written += 1

    # 写新 zip：逐项保留原文件的压缩方式与时间戳，GP8 对 zip 容器结构敏感
    buffer = io.BytesIO()
    xml_text = ET.tostring(root, encoding="unicode")
    xml_text = _restore_cdata(
        xml_text, _cdata_pairs_from(file_data[gpif_name].decode("utf-8"))
    )
    xml_bytes = (
        '<?xml version="1.0" encoding="utf-8"?>\n'.encode("utf-8") + xml_text.encode("utf-8")
    )
    with zipfile.ZipFile(buffer, "w") as zout:
        for info in zin_infos:
            content = xml_bytes if info.filename == gpif_name else file_data[info.filename]
            new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new_info.compress_type = info.compress_type
            new_info.external_attr = info.external_attr
            if info.extra:
                new_info.extra = info.extra
            zout.writestr(new_info, content)
    with open(output_path, "wb") as f:
        f.write(buffer.getvalue())

    # 用解析器验证写回结果
    verify_song = parse_gp(output_path)
    verify_track = next((t for t in verify_song.tracks if t.id == track.id), None)
    annotated_beats = 0
    if verify_track is not None:
        annotated_beats = sum(1 for m in verify_track.measures for b in m.beats if b.chord)

    return {
        "written": written,
        "skipped_manual": skipped_manual,
        "skipped_existing": skipped_existing,
        "missing_beats": missing,
        "cloned": cloned,
        "new_chords": len(new_names),
        "total_chords_in_library": len(verify_track.chords) if verify_track else len(track.chords),
        "annotated_beats": annotated_beats,
    }


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def prompt_track(song: GPSong) -> GPTrack:
    """未指定 --track 时交互选择轨道；非交互环境退化为第一个轨道。"""
    if len(song.tracks) == 1:
        return song.tracks[0]
    if not sys.stdin.isatty():
        print(f"轨道: [0] {song.tracks[0].name}（未指定 --track，非交互环境取第一个）")
        return song.tracks[0]
    print("可用轨道:")
    for t in song.tracks:
        chords = ", ".join(c.name for c in t.chords) or "无"
        print(f"  [{t.id}] {t.name:<28} 音符 {len(t.notes):>5}  和弦库: {chords}")
    while True:
        try:
            selector = input("选择轨道（编号或名称，回车默认 0）: ").strip()
        except EOFError:  # 非交互输入流
            print("未读到输入，取第一个轨道。")
            return song.tracks[0]
        if not selector:
            return song.tracks[0]
        try:
            return select_track(song, selector)
        except GuitarProError as e:
            print(f"  {e}，请重新选择")


def run_analysis(args) -> list[dict]:
    song = parse_gp(args.file)
    if args.track:
        try:
            track = select_track(song, args.track)
        except GuitarProError as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        track = prompt_track(song)

    key_root, key_mode = resolve_key(song, track, args.key)
    if args.key is None:
        print(
            f"调性: {pc_name(key_root, key_root)}{'m' if key_mode == 'Minor' else ''} "
            "(来自调号，可用 --key 覆盖)"
        )
    else:
        print(f"调性: {pc_name(key_root, key_root)}{'m' if key_mode == 'Minor' else ''} (--key 指定)")

    segmenter = SEGMENTERS[args.window]
    results = []
    for measure in track.measures:
        for seg in segmenter(measure):
            if len(seg.notes) < args.min_notes:
                continue
            detected = detect_chord(seg.notes, key_root, key_mode, args.style)
            results.append(
                {
                    "bar": seg.bar,
                    "section": seg.section,
                    "window": seg.window,
                    "start_quarters": seg.start_quarters,
                    "anchor_beat_id": seg.anchor_beat_id,
                    "anchor_voice_id": seg.anchor_voice_id,
                    "anchor_pos": seg.anchor_pos,
                    "notes": [n.pitch_name or str(n.midi) for n in seg.notes],
                    "chord": detected,
                    "manual": seg.manual,
                }
            )

    print(f"轨道: [{track.id}] {track.name}  窗口: {args.window}  风格: {args.style}")
    if args.debug:
        print(f"{'小节':>4}  {'窗口':<10} {'自动和弦':<12} {'音符':<32} 手动")
        print("-" * 86)
        for r in results:
            weights = r["chord"]["weights"]
            pcs = " ".join(f"{k}:{v:g}" for k, v in weights.items()) if weights else "-"
            print(
                f"{r['bar']:>4}  {r['window']:<10} {r['chord']['name']:<12} "
                f"{pcs:<32} {r['manual'] or ''}"
            )
    print(f"共分析 {len(results)} 个窗口。")

    if not args.no_compare:
        rows = compare_manual(track, key_root, key_mode, args.style)
        print_comparison(rows)

    if args.out:
        payload = {
            "file": args.file,
            "track": {"id": track.id, "name": track.name},
            "key": f"{pc_name(key_root, key_root)}{'m' if key_mode == 'Minor' else ''}",
            "window": args.window,
            "style": args.style,
            "results": results,
            "manual_comparison": compare_manual(track, key_root, key_mode, args.style),
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"已保存: {args.out}")

    if args.no_write:
        print("未写回 .gp（--no-write）。")
    else:
        if args.write == "__default__":
            output_path = str(Path(args.file).with_name(Path(args.file).stem + "_chords.gp"))
        else:
            output_path = args.write
        if Path(output_path).resolve() == Path(args.file).resolve():
            print("错误: 输出文件与输入文件相同，请用 --write 指定其他路径", file=sys.stderr)
            sys.exit(1)
        stats = write_chords_to_gp(
            args.file,
            output_path,
            song,
            track,
            results,
            key_root,
            overwrite=args.overwrite,
        )
        print(
            f"\n写回完成: {output_path}\n"
            f"  新写入和弦: {stats['written']} 处 | 和弦库新增: {stats['new_chords']} 个"
            f"（库内共 {stats['total_chords_in_library']} 个）\n"
            f"  共享拍克隆: {stats.get('cloned', 0)} 个 | "
            f"跳过的手工标注小节: {stats['skipped_manual']} | "
            f"跳过已有和弦的拍: {stats['skipped_existing']}\n"
            f"  验证通过：输出文件中带和弦标注的拍: {stats['annotated_beats']}"
        )
    return results


def demo() -> None:
    """不依赖文件，用示例音符演示和弦识别。"""
    from gp_parser import GPNote

    def n(midi: int, dur: float = 1.0) -> GPNote:
        name = _SHARP_NAMES[midi % 12] + str(midi // 12 - 1)
        return GPNote(midi=midi, pitch_name=name, duration_quarters=dur)

    samples = [
        ("C 大调琶音", [n(48), n(52), n(55), n(60), n(64), n(67)]),
        ("A 小调琶音", [n(45), n(48), n(52), n(57), n(60), n(64)]),
        ("C5/G 强力和弦", [n(43), n(60), n(67)]),
        ("Fsus4（F 大调内）", [n(41), n(46), n(48), n(53), n(60), n(65)]),
        ("Dm7 琶音", [n(38), n(45), n(50), n(53), n(60), n(62)]),
    ]
    print("和弦识别演示（调性: C 大调，风格: guitar）\n")
    print(f"{'示例':<16} {'识别结果':<12} 音级(加权)")
    print("-" * 52)
    for label, notes in samples:
        result = detect_chord(notes, key_root=0, key_mode="Major", style="guitar")
        pcs = " ".join(f"{k}:{v:g}" for k, v in result["weights"].items())
        print(f"{label:<16} {result['name']:<12} {pcs}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="从 Guitar Pro 文件自动识别并标注和弦",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("file", nargs="?", help=".gp / .gpx 文件路径（--demo 时不需要）")
    parser.add_argument("--track", default=None, help="轨道名称或索引（不指定时交互选择）")
    parser.add_argument(
        "--window", choices=["measure", "half", "beat"], default="measure",
        help="分析窗口：整小节 / 半小节 / 每个节拍",
    )
    parser.add_argument(
        "--style", choices=["guitar", "theory"], default="guitar",
        help="guitar=强力/斜杠记法，theory=完整理论和弦",
    )
    parser.add_argument("--key", help="指定调性，如 C / Am / F#m（默认读文件调号）")
    parser.add_argument("--min-notes", type=int, default=1, help="少于该音符数的窗口跳过")
    parser.add_argument("--out", help="输出 JSON 结果文件")
    parser.add_argument(
        "--write", nargs="?", const="__default__", default="__default__", metavar="OUT.gp",
        help="写回路径（默认自动写 <原名>_chords.gp，--no-write 关闭）",
    )
    parser.add_argument("--no-write", action="store_true", help="不写回 .gp（只分析/输出 JSON）")
    parser.add_argument("--debug", action="store_true", help="输出每个小节的识别明细")
    parser.add_argument(
        "--overwrite", action="store_true",
        help="覆盖已有手工标注的和弦（默认跳过已标注的小节）",
    )
    parser.add_argument("--no-compare", action="store_true", help="不做手工标注对照")
    parser.add_argument("--demo", action="store_true", help="运行算法演示")
    args = parser.parse_args()

    if args.demo:
        demo()
        return
    if not args.file:
        parser.error("需要提供文件路径，或使用 --demo")
    try:
        run_analysis(args)
    except GuitarProError as e:
        print(f"解析失败: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
