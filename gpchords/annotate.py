"""
自动和弦标注脚本
================

解析 Guitar Pro (.gp / .gpx) 文件，按和弦变化自动切窗（也可固定按
小节 / 半小节 / 节拍）识别和弦，并可与文件里手工标注的和弦进行对照。
支持多轨道：默认每轨单独分析、单独标注；``--merge`` 可将所选轨道按
小节/拍位合并音符后识别一次（和弦拆在多轨、或需要贝斯补低音时），
写回时默认第一个分析轨道，``--write-tracks all`` 可写回全部分析轨道。

原理:

1. 用 :mod:`gpchords.parser` 解析文件，提取指定轨道的音符与时值。
2. 确定调性：优先使用每小节自己的调号（支持中途转调）；没有调号时
   回退全局调号或 Krumhansl-Kessler 键感轮廓估计。
3. 在每个分析窗口内收集音级（按真实时值加权，延音延续不重复计权，
   低音音级放大），对 39 种和弦模板（大/小/属/挂留/强力和弦等）打分：
   命中音加分、非和弦音扣分、缺失和弦音扣分；调性只用于拼写与
   极小破平，最终取最高分。
4. 吉他风格下，没有三音时收敛成强力和弦（5），低音不是根音时写成
   斜杠和弦（如 ``C5/G``），与 Guitar Pro 里的常见记法一致。
5. ``--write`` 可以把识别结果写回一个新的 ``.gp`` 文件：向目标轨道的
   和弦库（DiagramCollection）添加和弦项，并在对应拍上写 ``<Chord>`` 引用。
   已有手工标注的小节默认跳过，不会被覆盖。

用法::

    # 默认：交互选择轨道，按小节识别并自动写回 <原名>_chords.gp
    uv run gp-chords "xxx.gp"

    # 指定轨道，按小节标注 Lead Guitar
    uv run gp-chords "xxx.gp" --track "Lead Guitar"

    # 多轨道：每轨单独分析、单独标注
    uv run gp-chords "xxx.gp" --track "Lead Guitar,Rhythm Guitar"

    # 合并多轨音符识别（和弦拆在两轨/需要贝斯补低音），默认写回第一轨
    uv run gp-chords "xxx.gp" --track "Lead Guitar,Rhythm Guitar" --merge

    # 合并识别并写回全部分析轨道（各轨和弦库/指板图按各自调弦生成）
    uv run gp-chords "xxx.gp" --track all --merge --write-tracks all --no-write

    # 按节拍标注，并输出 JSON
    uv run gp-chords "xxx.gp" --track 0 --window beat --out chords.json

    # 按和弦变化自动切窗（默认），并在转调段按段落调性处理
    uv run gp-chords "xxx.gp" --track "Lead Guitar" --key-per-section

    # 只看分析结果，不写回；--debug 输出每个小节的明细
    uv run gp-chords "xxx.gp" --track "Lead Guitar" --no-write --debug

    # 不依赖具体文件，看算法演示
    uv run gp-chords --demo
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

from gpchords.parser import (
    GPSong,
    GuitarProError,
    GPBeat,
    GPMeasure,
    GPNote,
    GPTrack,
    parse_gp,
    select_tracks,
)

# ---------------------------------------------------------------------------
# 音乐理论基础
# ---------------------------------------------------------------------------

# 打分系数（第 2 步：统一收进常量表）
UNMATCHED_PENALTY = 0.8  # 非和弦音扣分系数
MISSING_PENALTY = 1.0  # 和弦音缺失扣分（每个缺失音级）
COMPLEXITY_PENALTY = 0.5  # 奥卡姆剃刀：模板每多一个音付出的代价
BASS_WEIGHT_MULTIPLIER = 2.0  # 低音音级权重放大（低音定根音）

# auto 切窗：某组音符权重不足小节总权重该比例时并入相邻组（视为经过音）
WINDOW_MIN_SHARE = 0.2
# --key-per-section：段落调内覆盖率低于该值时尝试 K-K 自动估计
SECTION_KEY_COVERAGE = 0.65

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
    # ---- 第 4 步扩充（参照 pychord DEFAULT_QUALITIES，去别名、对齐 GP 记法）
    "7b5": ((0, 4, 6, 10), "7b5"),
    "7#5": ((0, 4, 8, 10), "7#5"),
    "7b9": ((0, 1, 4, 7, 10), "7b9"),
    "7#9": ((0, 3, 4, 7, 10), "7#9"),
    "9sus4": ((0, 2, 5, 7, 10), "9sus4"),
    "7#11": ((0, 4, 6, 7, 10), "7#11"),
    "9#11": ((0, 2, 4, 6, 7, 10), "9#11"),
    "maj7#11": ((0, 4, 6, 7, 11), "maj7#11"),
    "maj7#5": ((0, 4, 8, 11), "maj7#5"),
    "maj7sus2": ((0, 2, 7, 11), "maj7sus2"),
    "add11": ((0, 4, 5, 7), "add11"),
    "add11(no5)": ((0, 4, 5), "add11(no5)"),
    "madd4": ((0, 3, 5, 7), "madd4"),
    "madd11(no5)": ((0, 3, 5), "madd11(no5)"),
    "mmaj7": ((0, 3, 7, 11), "mmaj7"),
    "m6/9": ((0, 2, 3, 7, 9), "m6/9"),
    "11": ((0, 2, 4, 5, 7, 10), "11"),
    "m11": ((0, 2, 3, 5, 7, 10), "m11"),
    # 13/maj13 与 pychord 一致：含 11 音。吉他实际弹奏常省略 11，
    # 此时应写成 9/6/9 而不是 13——模板要求 11 存在才能叫 13。
    "13": ((0, 2, 4, 5, 7, 9, 10), "13"),
    "maj13": ((0, 2, 4, 5, 7, 9, 11), "maj13"),
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
    """
    音符 -> 音级权重（按真实四分音符时值加权，不做下限抬高）。

    - 延音延续音符（tie destination）不重复计权；其时长并入同窗内的
      延音起点，保持"实际发声时长"不变。
    - 跨窗延音（延音起点在上一窗/上一小节）在有其他音符的窗口里也按
      实际时值计权——它是真实在响的音（如 A-G-C-G-B-C-G 的 A 踏板
      延进下一小节），不能因为找不到同窗起点就丢掉。
    - 时值为 0 的音符（GP 里的重复引用/装饰音）权重为 0，不再与
      四分音符同权。
    """
    origin_pcs = {m.midi % 12 for m in notes if m.tie_origin}
    weights: dict[int, float] = defaultdict(float)
    for n in notes:
        if n.tie_destination and n.midi % 12 in origin_pcs:
            continue
        weights[n.midi % 12] += n.duration_quarters
    # 把同窗延音目标的时长并入其延音起点（保持"实际发声时长"不变）
    for n in notes:
        if not n.tie_origin:
            continue
        for m in notes:
            if m.tie_destination and m.midi % 12 == n.midi % 12:
                weights[n.midi % 12] += m.duration_quarters
    return dict(weights)


# ---------------------------------------------------------------------------
# 斜杠低音拼写：按和弦品质的度数关系选择正确的等音（C7/A# -> C7/Bb）
# ---------------------------------------------------------------------------

# GPIF 度数 -> (字母步进, 无变化音时的半音数)
_INTERVAL_OFFSETS = {
    "Second": (1, 2),
    "Third": (2, 4),
    "Fourth": (3, 5),
    "Fifth": (4, 7),
    "Sixth": (5, 9),
    "Seventh": (6, 11),
    "Ninth": (1, 2),
    "Eleventh": (3, 5),
    "Thirteenth": (5, 9),
}
# GPIF 变化音 -> 相对该度数无变化音的半音偏移
_ALTERATION_OFFSETS = {
    "Perfect": 0,
    "Major": 0,
    "Minor": -1,
    "Augmented": 1,
    "Diminished": -1,
    "DoubleFlat": -2,
    "DoubleSharp": 2,
}
_LETTER_NAMES = ["C", "D", "E", "F", "G", "A", "B"]
_DIATONIC_SEMITONES = [0, 2, 4, 5, 7, 9, 11]
_ACCIDENTALS = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "##"}


def _degree_name(root_pc: int, quality: str, pc: int, key_root: Optional[int]) -> Optional[str]:
    """
    若 pc 是该品质和弦的度数音，按度数拼写音名（如 C7 的 b7 -> "Bb"）。

    返回 None 表示 pc 不是该品质的和弦音，调用方再回退到调性拼写。
    """
    degrees = DEGREES.get(quality)
    if degrees is None:
        return None
    root_name = pc_name(root_pc % 12, key_root)
    root_letter = _LETTER_NAMES.index(root_name[0])
    for interval, alteration in degrees:
        letter_step, base = _INTERVAL_OFFSETS[interval]
        off = _ALTERATION_OFFSETS[alteration]
        if (root_pc + base + off) % 12 == pc % 12:
            letter = (root_letter + letter_step) % 7
            natural = _DIATONIC_SEMITONES[letter]
            alt = (root_pc + base + off) - natural
            if alt > 2:
                alt -= 12
            elif alt < -2:
                alt += 12
            return f"{_LETTER_NAMES[letter]}{_ACCIDENTALS[alt]}"
    return None


def _bass_name(
    root_pc: int, bass_pc: int, quality: str, key_root: Optional[int]
) -> str:
    """低音音名：优先按和弦度数拼写（C7/A# -> C7/Bb），否则按调性。"""
    if bass_pc == root_pc:
        return pc_name(root_pc, key_root)
    name = _degree_name(root_pc, quality, bass_pc, key_root)
    if name is None:
        return pc_name(bass_pc, key_root)
    # 双升/双降度数（如 E7#9 的 #9 低音拼成 F##）在 GP 记法里读起来怪异，
    # 实际弹奏/显示的也是等音（G），这里回退到调性拼写。
    if "##" in name or "bb" in name:
        return pc_name(bass_pc, key_root)
    return name


def detect_chord(
    notes: list[GPNote],
    key_root: Optional[int] = None,
    key_mode: str = "Major",
    style: str = "guitar",
) -> Optional[dict]:
    """
    识别一段音符最可能的和弦。

    打分：命中音加分 - 非和弦音扣分 - 缺失和弦音扣分。
    调性（主音/调内）不参与主分数，只作为同分时的极小破平；
    低音音级在计权时放大，且低音等于根音在同分时优先。

    证据门槛：单音无法确定和弦，返回 None；双音只有纯五度
    （强力和弦）可以确定，其余双音（三度/七度/二度等）同样返回 None，
    避免把旋律单音或和弦碎片硬猜成 C5 / C/E / Fsus4 之类的和弦符号。

    style="guitar" 时没有三音会收敛为强力和弦（5），低音非根音写成斜杠和弦，
    与 Guitar Pro 常见记法一致；style="theory" 时输出理论上的完整和弦。
    """
    if not notes:
        return None
    raw = note_weights(notes)
    weights = dict(raw)
    bass_pc = min(n.midi for n in notes) % 12
    if bass_pc in weights:
        weights[bass_pc] *= BASS_WEIGHT_MULTIPLIER
    total = sum(weights.values())
    key_pcs = _diatonic_pcs(key_root, key_mode) if key_root is not None else None
    present_pcs = {pc for pc, v in raw.items() if v > 0}
    if len(present_pcs) <= 1:
        return None
    if len(present_pcs) == 2:
        a, b = sorted(present_pcs)
        if (b - a) % 12 not in (5, 7):  # 只有纯五度（两个方向）是强力双音
            return None

    candidates = []
    for root in range(12):
        for quality, (tpl, suffix) in CHORD_TEMPLATES.items():
            tset = {(pc + root) % 12 for pc in tpl}
            matched = sum(v for pc, v in weights.items() if pc in tset)
            unmatched = total - matched
            # (no5) 是不完整记法（省略五音）：只在窗口音符恰好都是和弦音时
            # 启用，避免把 {G,B,C} 从更大的 A 踏板琶音里单独拎出来当根音
            # （如 A-G-C-G-B-C-G 应判 Am7 而不是 Gadd11(no5)/A）。
            if quality.endswith("(no5)") and unmatched > 1e-9:
                continue
            # 奥卡姆剃刀：缺失的和弦音扣分，且模板每多一个音都付出
            # 小代价——只有音符确实构成 7/9/11/13 和弦时扩展模板才划算。
            missing = sum(1 for pc in tpl if (root + pc) % 12 not in present_pcs)
            score = (
                matched
                - UNMATCHED_PENALTY * unmatched
                - MISSING_PENALTY * missing
                - COMPLEXITY_PENALTY * len(tpl)
            )
            if key_pcs is not None and root in key_pcs:
                key_rank = 2 if root == key_root else 1
            else:
                key_rank = 0
            candidates.append(
                (score, raw.get(root, 0.0), key_rank, bass_pc == root, root, quality, suffix, tset)
            )

    # 排序：分数 > 调内/主音破平 > 低音=根音 > 根音出现权重 > 模板更简单 > 根音编号更小
    # 调性只做极小破平：不参与主分数，仅在同分时先看调内/主音。
    candidates.sort(
        key=lambda c: (
            c[0],
            c[2],
            c[3],
            c[1],
            -len(CHORD_TEMPLATES[c[5]][0]),
            -c[4],
        ),
        reverse=True,
    )
    score, _, _, _, root, quality, suffix, tset = candidates[0]

    matched_pcs = {pc for pc in weights if pc in tset}
    if style == "guitar" and quality != "5" and matched_pcs <= {root, (root + 7) % 12}:
        quality, suffix = "5", "5"
        tset = {root, (root + 7) % 12}
        matched = sum(v for pc, v in weights.items() if pc in tset)
        score = matched - UNMATCHED_PENALTY * (total - matched)

    name = f"{pc_name(root, key_root)}{suffix}"
    if bass_pc != root:
        name += f"/{_bass_name(root, bass_pc, quality, key_root)}"
    return {
        "name": name,
        "root": root,
        "quality": quality,
        "bass_pc": bass_pc,
        "score": round(score, 2),
        "weights": {
            pc_name(pc, key_root): round(v, 2)
            for pc, v in sorted(raw.items())
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


def segment_measure(measure: GPMeasure, next_measure: Optional[GPMeasure] = None) -> list[Segment]:
    """整小节窗口（next_measure 仅用于统一调用接口，不使用）。"""
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


def segment_half(measure: GPMeasure, next_measure: Optional[GPMeasure] = None) -> list[Segment]:
    """半小节窗口（next_measure 仅用于统一调用接口，不使用）。"""
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


def segment_beat(measure: GPMeasure, next_measure: Optional[GPMeasure] = None) -> list[Segment]:
    """逐拍窗口（next_measure 仅用于统一调用接口，不使用）。"""
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


def _beat_fingerprint(beat: GPBeat) -> frozenset[int]:
    """拍的和弦指纹：音级集合。"""
    return frozenset(n.midi % 12 for n in beat.notes)


def _group_weight(beats: list[GPBeat]) -> float:
    return sum(n.duration_quarters for b in beats for n in b.notes)


def _is_stepwise(fps: set[int], last: set[int]) -> bool:
    """新拍与上一拍是否级进（小二/大二度内）——级进视为经过音，不合并。"""
    for a in fps:
        for b in last:
            d = abs(a - b) % 12
            if min(d, 12 - d) <= 2:
                return True
    return False


def _fits_some_template(pcs: set[int]) -> bool:
    """这组音级能否全部落在某个模板和弦的音级集合里（琶音合并用）。"""
    for root in range(12):
        for tpl, _ in CHORD_TEMPLATES.values():
            tones = {(root + i) % 12 for i in tpl}
            if pcs <= tones:
                return True
    return False


def _first_content_group(measure: GPMeasure) -> list[GPNote]:
    """小节第一个内容组（按指纹分组，不做吸收），用于先现音跨小节比对。"""
    beats = [b for b in measure.beats if b.notes]
    if not beats:
        return []
    groups: list[list[GPBeat]] = []
    fps: list[set[int]] = []
    for beat in beats:
        fp = set(_beat_fingerprint(beat))
        if groups and (fp == fps[-1] or fp <= fps[-1] or fps[-1] <= fp):
            groups[-1].append(beat)
            fps[-1] |= fp
        else:
            groups.append([beat])
            fps.append(set(fp))
    return [n for b in groups[0] for n in b.notes]


def _is_trailing_anticipation(
    group: list[GPBeat],
    groups: list[list[GPBeat]],
    measure: GPMeasure,
    next_measure: Optional[GPMeasure],
) -> bool:
    """
    判断组是否先现音（anticipation）：小节末短促进入下一小节和弦的音。

    先现音不一定要 tie 进下一小节——短尾组若与下一小节首组和弦同根音，
    同样按先现音处理，保留为独立窗口（不并入主窗口）。
    """
    if next_measure is None or group is not groups[-1]:
        return False
    start = min(b.start_quarters for b in group)
    if start < _measure_duration(measure) * 0.75 - 1e-9:
        return False
    group_notes = [n for b in group for n in b.notes]
    next_notes = _first_content_group(next_measure)
    if not next_notes:
        return False
    group_chord = detect_chord(group_notes, None, "Major", "guitar")
    next_chord = detect_chord(next_notes, None, "Major", "guitar")
    return bool(
        group_chord
        and next_chord
        and group_chord["root"] == next_chord["root"]
    )


def _single_note_fits_neighbor_chord(
    group: list[GPBeat], neighbor: list[GPBeat]
) -> bool:
    """
    先导单音是否能并入相邻成形和弦（如 Fsus2 琶音开头的 G 单音）。

    相邻组必须明显更重（已成形），且并集能识别出包含该单音的三音以上
    和弦——音阶跑动里相邻单音等权，不会触发。
    """
    pcs = {n.midi % 12 for b in group for n in b.notes}
    if len(pcs) != 1:
        return False
    if _group_weight(group) >= _group_weight(neighbor):
        return False
    union_notes = [n for b in group + neighbor for n in b.notes]
    r = detect_chord(union_notes, None, "Major", "guitar")
    if r is None:
        return False
    tones = {(r["root"] + i) % 12 for i in CHORD_TEMPLATES[r["quality"]][0]}
    return len(tones) >= 3 and pcs <= tones


def segment_auto(
    measure: GPMeasure, next_measure: Optional[GPMeasure] = None
) -> list[Segment]:
    """
    按和弦变化自动切窗：

    1. 逐拍合并指纹相同或互为子集的拍（同和弦重复/琶音尾）；
    2. 单音/双音碎片按"级进判定 + 模板兼容"合并——逐音琶音
       （C-G-B-E）合成一窗，音阶跑动（C-D-E-F-G）因级进不合并；
    3. PC 集不再兼容时切分；
    4. 权重占比过小的独立组（经过音、尾音）并入相邻组，
       避免把 16 分音符经过音切成单独和弦；但 tie 进下一小节或与
       下一小节首组同根音的小节末组是先现音（anticipation），保留
       为独立窗口。
    """
    beats = [b for b in measure.beats if b.notes]
    if not beats:
        return []

    groups: list[list[GPBeat]] = []
    group_fps: list[set[int]] = []
    last_fp: set[int] | None = None  # 当前组内上一拍的指纹（级进判定）
    for beat in beats:
        fp = set(_beat_fingerprint(beat))
        if groups:
            gfp = group_fps[-1]
            if fp == gfp or fp <= gfp or gfp <= fp:
                groups[-1].append(beat)
                group_fps[-1] |= fp
                last_fp = fp
                continue
            # 琶音碎片合并：新拍最多 2 音；双音碎片只并入尚未成形的组；
            # 相对上一拍必须是跳进，且并集能落在某个模板和弦内。
            if (
                len(fp) <= 2
                and (len(fp) == 1 or len(gfp) <= 2)
                and last_fp is not None
                and not _is_stepwise(fp, last_fp)
                and _fits_some_template(gfp | fp)
            ):
                groups[-1].append(beat)
                group_fps[-1] |= fp
                last_fp = fp
                continue
        groups.append([beat])
        group_fps.append(set(fp))
        last_fp = fp

    # 吸收占比过小的组：并入权重更大的相邻组（并集做整体识别）
    total = sum(_group_weight(g) for g in groups)
    while len(groups) > 1 and total > 0:
        absorbed = False
        for i, g in enumerate(groups):
            is_anticipation = g and all(n.tie_origin for b in g for n in b.notes)
            if not is_anticipation and _is_trailing_anticipation(g, groups, measure, next_measure):
                continue
            if is_anticipation:
                continue
            left_w = _group_weight(groups[i - 1]) if i > 0 else -1.0
            right_w = _group_weight(groups[i + 1]) if i + 1 < len(groups) else -1.0
            neighbor_idx = i + 1 if right_w > left_w else i - 1
            neighbor = groups[neighbor_idx]
            share = _group_weight(g) / total
            if (
                share < WINDOW_MIN_SHARE
                or _single_note_fits_neighbor_chord(g, neighbor)
            ):
                if neighbor_idx > i:
                    groups[neighbor_idx] = groups[i] + groups[neighbor_idx]
                else:
                    groups[neighbor_idx] = groups[neighbor_idx] + groups[i]
                del groups[i]
                absorbed = True
                break
        if not absorbed:
            break

    segments = []
    for group in groups:
        notes = [n for b in group for n in b.notes]
        if not notes:
            continue
        start = min(b.start_quarters for b in group)
        end = max(b.start_quarters + b.duration_quarters for b in group)
        manual = next((b.chord.name for b in group if b.chord), None)
        anchor = next((b for b in group if b.notes), None)
        segments.append(
            Segment(
                bar=measure.index,
                section=measure.section,
                window="auto",
                start_quarters=start,
                duration_quarters=max(end - start, 0.0),
                notes=notes,
                manual=manual,
                anchor_beat_id=anchor.id if anchor else None,
                anchor_voice_id=anchor.voice_id if anchor else None,
                anchor_pos=anchor.position_in_voice if anchor else -1,
            )
        )
    return segments


SEGMENTERS = {
    "auto": segment_auto,
    "measure": segment_measure,
    "half": segment_half,
    "beat": segment_beat,
}


def merge_tracks(tracks: list[GPTrack]) -> GPTrack:
    """
    按小节/拍位合并多轨音符为一个合成轨道（用于多轨和弦识别）。

    - 同一起始位置的拍合并：音符拼接、时值取最长、拍信息（id/声部/位置）
      取第一个轨道（primary）的拍——写回时按 primary 锚定；
    - primary 在该位置没有拍时，拍信息取其余轨道的拍（锚点随后由
      :func:`reanchor_results` 映射回目标轨道）；
    - 小节元信息（拍号/调号/段落）与手工和弦标注取自 primary。
    """
    if len(tracks) == 1:
        return tracks[0]
    primary = tracks[0]
    merged = GPTrack(
        id=primary.id,
        name=" + ".join(t.name for t in tracks),
        short_name="+".join(t.short_name or t.name for t in tracks),
        program=primary.program,
        midi_program=primary.midi_program,
        tuning=primary.tuning,
        chords=primary.chords,
    )
    by_bar: dict[int, list[GPMeasure]] = {}
    for t in tracks:
        for m in t.measures:
            by_bar.setdefault(m.index, []).append(m)
    for index in sorted(by_bar):
        measures = by_bar[index]
        pm = measures[0]
        merged_measure = GPMeasure(
            index=index,
            time_signature=pm.time_signature,
            key_signature=pm.key_signature,
            section=pm.section,
        )
        buckets: dict[float, list[GPBeat]] = {}
        order: list[float] = []
        for m in measures:
            for b in m.beats:
                key = round(b.start_quarters, 4)
                if key not in buckets:
                    buckets[key] = []
                    order.append(key)
                buckets[key].append(b)
        order.sort()
        for key in order:
            group = buckets[key]
            notes = [n for b in group for n in b.notes]
            if not notes:
                continue
            anchor = group[0]  # primary 有拍时必是 group[0]（按轨道顺序遍历）
            merged_measure.beats.append(
                GPBeat(
                    id=anchor.id,
                    start_quarters=key,
                    duration_quarters=max(b.duration_quarters for b in group),
                    chord=anchor.chord,
                    notes=notes,
                    voice_id=anchor.voice_id,
                    position_in_voice=anchor.position_in_voice,
                )
            )
        merged_measure.beats.sort(key=lambda b: b.start_quarters)
        merged.measures.append(merged_measure)
        for b in merged_measure.beats:
            merged.notes.extend(b.notes)
    return merged


def resolve_key(song, track: GPTrack, override: Optional[str]) -> tuple[int, str]:
    """确定调性：--key > 文件调号 > Krumhansl-Kessler 估计。"""
    if override:
        return parse_key_name(override)
    sig_counts = Counter(m.key_signature for m in track.measures if m.key_signature)
    if sig_counts:
        return parse_key_name(sig_counts.most_common(1)[0][0])
    weights = note_weights(track.notes)
    return estimate_key(weights)


def measure_key(measure: GPMeasure, fallback: tuple[int, str]) -> tuple[int, str]:
    """小节自己的调号优先；没有调号回退全局调性。"""
    if measure.key_signature:
        try:
            return parse_key_name(measure.key_signature)
        except ValueError:
            pass
    return fallback


def _diatonic_coverage(
    notes: list[GPNote], key_root: int, key_mode: str
) -> float:
    w = note_weights(notes)
    total = sum(w.values())
    if total <= 0:
        return 1.0
    pcs = _diatonic_pcs(key_root, key_mode)
    return sum(v for pc, v in w.items() if pc in pcs) / total


def resolve_section_keys(
    track: GPTrack, global_key: tuple[int, str]
) -> dict[Optional[str], tuple[int, str]]:
    """
    --key-per-section：每个段落取其首个带调号小节的调性；
    若该段音符大量落在预期调外（调内覆盖率过低），用 K-K 对段内音符
    重新估计（如《无论如何》47-49 桥段的 F#maj7 等调外和弦）。
    """
    sections: dict[Optional[str], tuple[int, str]] = {}
    for m in track.measures:
        sec = m.section
        if sec in sections:
            continue
        nominal: Optional[tuple[int, str]] = None
        for m2 in track.measures:
            if m2.section == sec and m2.key_signature:
                try:
                    nominal = parse_key_name(m2.key_signature)
                except ValueError:
                    nominal = None
                break
        if nominal is None:
            nominal = global_key
        notes = [
            n
            for m2 in track.measures
            if m2.section == sec
            for b in m2.beats
            for n in b.notes
        ]
        if _diatonic_coverage(notes, *nominal) < SECTION_KEY_COVERAGE:
            kk = estimate_key(note_weights(notes))
            if _diatonic_coverage(notes, *kk) > _diatonic_coverage(notes, *nominal):
                nominal = kk
        sections[sec] = nominal
    return sections


# ---------------------------------------------------------------------------
# 手工标注对照
# ---------------------------------------------------------------------------


def compare_manual(
    track: GPTrack,
    keys_by_bar: dict[int, tuple[int, str]],
    style: str,
) -> list[dict]:
    """
    对每个手工标注：分别用「整小节」和「标注拍到小节末」的音符识别和弦并对照。

    之所以有两种窗口，是因为 GP 里用户常把和弦挂在琶音/和弦的首个低音上，
    单看标注那一拍只有一两个音。
    """
    rows = []
    for measure in track.measures:
        key_root, key_mode = keys_by_bar[measure.index]
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
# 注意：GP8 对 9/11/13 度的无变化写法是 "Perfect"（实测原生文件全部如此），
# 9 度降半音是 "Diminished"、升半音是 "Augmented"；写成 "Ninth Major/Minor"
# 或 "Thirteenth Major" GP8 不认，会把构成音显示成默认的 C。
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
    "add9": [("Third", "Major"), ("Fifth", "Perfect"), ("Ninth", "Perfect")],
    "madd9": [("Third", "Minor"), ("Fifth", "Perfect"), ("Ninth", "Perfect")],
    "9": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Minor"), ("Ninth", "Perfect")],
    "maj9": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Major"), ("Ninth", "Perfect")],
    "m9": [("Third", "Minor"), ("Fifth", "Perfect"), ("Seventh", "Minor"), ("Ninth", "Perfect")],
    "7sus4": [("Fourth", "Perfect"), ("Fifth", "Perfect"), ("Seventh", "Minor")],
    "6/9": [("Third", "Major"), ("Fifth", "Perfect"), ("Sixth", "Major"), ("Ninth", "Perfect")],
    "7b5": [("Third", "Major"), ("Fifth", "Diminished"), ("Seventh", "Minor")],
    "7#5": [("Third", "Major"), ("Fifth", "Augmented"), ("Seventh", "Minor")],
    "7b9": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Minor"), ("Ninth", "Diminished")],
    "7#9": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Minor"), ("Ninth", "Augmented")],
    "9sus4": [("Fourth", "Perfect"), ("Fifth", "Perfect"), ("Seventh", "Minor"), ("Ninth", "Perfect")],
    "7#11": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Minor"), ("Eleventh", "Augmented")],
    "9#11": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Minor"), ("Ninth", "Perfect"), ("Eleventh", "Augmented")],
    "maj7#11": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Major"), ("Eleventh", "Augmented")],
    "maj7#5": [("Third", "Major"), ("Fifth", "Augmented"), ("Seventh", "Major")],
    "maj7sus2": [("Second", "Major"), ("Fifth", "Perfect"), ("Seventh", "Major")],
    "add11": [("Third", "Major"), ("Fifth", "Perfect"), ("Eleventh", "Perfect")],
    "add11(no5)": [("Third", "Major"), ("Fifth", "Perfect"), ("Eleventh", "Perfect")],
    "madd4": [("Third", "Minor"), ("Fourth", "Perfect"), ("Fifth", "Perfect")],
    "madd11(no5)": [("Third", "Minor"), ("Fifth", "Perfect"), ("Eleventh", "Perfect")],
    "mmaj7": [("Third", "Minor"), ("Fifth", "Perfect"), ("Seventh", "Major")],
    "m6/9": [("Third", "Minor"), ("Fifth", "Perfect"), ("Sixth", "Major"), ("Ninth", "Perfect")],
    "11": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Minor"), ("Ninth", "Perfect"), ("Eleventh", "Perfect")],
    "m11": [("Third", "Minor"), ("Fifth", "Perfect"), ("Seventh", "Minor"), ("Ninth", "Perfect"), ("Eleventh", "Perfect")],
    "13": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Minor"), ("Ninth", "Perfect"), ("Eleventh", "Perfect"), ("Thirteenth", "Perfect")],
    "maj13": [("Third", "Major"), ("Fifth", "Perfect"), ("Seventh", "Major"), ("Ninth", "Perfect"), ("Eleventh", "Perfect"), ("Thirteenth", "Perfect")],
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
    omit_fifth = chord["quality"].endswith("(no5)")
    for interval, alteration in DEGREES.get(chord["quality"], DEGREES["maj"]):
        ET.SubElement(
            chord_el,
            "Degree",
            {
                "interval": interval,
                "alteration": alteration,
                "omitted": "true" if omit_fifth and interval == "Fifth" else "false",
            },
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


def _find_anchor_beat(
    measure: Optional[GPMeasure], result: dict
) -> Optional[GPBeat]:
    """在目标轨道的小节里找挂和弦的拍：优先窗口起始拍，否则窗口内首拍。"""
    if measure is None:
        return None
    beats = [b for b in measure.beats if b.notes]
    if not beats:
        return None
    start = result.get("start_quarters", 0.0)
    end = start + result.get("duration_quarters", 0.0)
    exact = [b for b in beats if abs(b.start_quarters - start) < 1e-3]
    if exact:
        return exact[0]
    inside = [
        b
        for b in beats
        if b.start_quarters >= start - 1e-3 and b.start_quarters <= end + 1e-3
    ]
    return min(inside, key=lambda b: b.start_quarters) if inside else None


def reanchor_results(
    results: list[dict],
    track: GPTrack,
    measures_by_bar: Optional[dict[int, GPMeasure]] = None,
) -> list[dict]:
    """
    把分析结果锚点映射到目标轨道（多轨写回用）。

    合并分析产生的锚点指向 primary 轨道；写回其他轨道时，按
    (小节, 窗口位置) 在目标轨道找对应拍并替换锚点。目标轨道在该小节
    窗口内没有音符时锚点置空，写回时自动跳过该窗口。
    """
    if measures_by_bar is None:
        measures_by_bar = {m.index: m for m in track.measures}
    out = []
    for r in results:
        r2 = dict(r)
        beat = _find_anchor_beat(measures_by_bar.get(r.get("bar")), r)
        if beat is not None:
            r2["anchor_beat_id"] = beat.id
            r2["anchor_voice_id"] = beat.voice_id
            r2["anchor_pos"] = beat.position_in_voice
        else:
            r2["anchor_beat_id"] = None
            r2["anchor_voice_id"] = None
            r2["anchor_pos"] = -1
        out.append(r2)
    return out


def resolve_write_tracks(
    song: GPSong,
    analysis_tracks: list[GPTrack],
    selectors: Optional[list[str]],
    default_all: bool,
) -> list[GPTrack]:
    """
    解析写回轨道：默认第一个分析轨道；``all`` = 全部分析轨道；
    其他按 :func:`select_tracks` 规则选择，且必须是分析轨道之一。
    """
    if not selectors:
        return analysis_tracks if default_all else analysis_tracks[:1]
    if any(part.strip().lower() == "all" for sel in selectors for part in sel.split(",")):
        return analysis_tracks
    targets = select_tracks(song, selectors)
    analyzed_ids = {t.id for t in analysis_tracks}
    for t in targets:
        if t.id not in analyzed_ids:
            raise GuitarProError(
                f"写回轨道 [{t.id}] {t.name} 不在分析轨道内，请先通过 --track 选择"
            )
    return targets


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

    # 第一遍：先决定哪些窗口真的会写入（跳过的不会进和弦库），
    # 避免"全部被跳过"时仍往 DiagramCollection 追加一堆无人引用的死项。
    writable: list[tuple[dict, ET.Element, int, str, ET.Element]] = []
    skipped_manual = skipped_existing = missing = 0
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
        writable.append((r, voice_el, pos, current_id, beat_el))

    # 只给实际写入的和弦建库项（同名复用已有项）
    needed: list[dict] = []
    seen_names: set[str] = set()
    for r, *_ in writable:
        chord = r["chord"]
        if chord["name"] in seen_names:
            continue
        seen_names.add(chord["name"])
        needed.append(chord)

    if overwrite:
        # --overwrite：清空本轨和弦库与编辑用 working set，按本次识别结果
        # 整体重建，避免旧项/旧度数残留（如旧版 Ninth Major 导致九音显示成 C）。
        for it in list(coll_el.findall("Item")):
            coll_el.remove(it)
        if working_el is not None:
            for it in list(working_el.findall("Item")):
                working_el.remove(it)
        existing: dict[str, int] = {}
    else:
        existing = {c.name: i for i, c in enumerate(track.chords)}
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
    written = cloned = 0
    rewritten_beat_ids: set[str] = set()
    for r, voice_el, pos, current_id, beat_el in writable:
        beats_tokens = (voice_el.findtext("Beats") or "").split()
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

        _set_beat_chord(beat_el, name_to_index[r["chord"]["name"]])
        rewritten_beat_ids.add(beat_el.get("id"))
        written += 1

    if overwrite and beats_container is not None:
        # 清库重建后，本轨未被重写的旧和弦引用会指向不存在的库项，
        # 一并移除（GP8 从不复用带和弦的 beat，不会误伤其他轨道）。
        track_beat_ids = {b.id for m in track.measures for b in m.beats}
        for beat_el in beats_container.findall("Beat"):
            if beat_el.get("id") not in track_beat_ids:
                continue
            if beat_el.get("id") in rewritten_beat_ids:
                continue
            chord_el = beat_el.find("Chord")
            if chord_el is not None:
                beat_el.remove(chord_el)

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


def prompt_tracks(song: GPSong) -> list[GPTrack]:
    """未指定 --track 时交互选择轨道；非交互环境退化为第一个有音符的轨道。"""
    candidates = [t for t in song.tracks if t.notes] or [song.tracks[0]]
    if len(candidates) == 1:
        return [candidates[0]]
    if not sys.stdin.isatty():
        print(f"轨道: [0] {candidates[0].name}（未指定 --track，非交互环境取第一个）")
        return [candidates[0]]
    print("可用轨道:")
    for t in song.tracks:
        chords = ", ".join(c.name for c in t.chords) or "无"
        print(f"  [{t.id}] {t.name:<28} 音符 {len(t.notes):>5}  和弦库: {chords}")
    while True:
        try:
            selector = input(
                "选择轨道（编号/名称；逗号分隔多个；all=全部；回车取第一个）: "
            ).strip()
        except EOFError:  # 非交互输入流
            print("未读到输入，取第一个轨道。")
            return [candidates[0]]
        if not selector:
            return [candidates[0]]
        try:
            return select_tracks(song, [selector])
        except GuitarProError as e:
            print(f"  {e}，请重新选择")


def _key_for_track(
    song: GPSong, track: GPTrack, args
) -> tuple[tuple[int, str], dict[int, tuple[int, str]]]:
    """每个分析窗口的调性：--key 强制全局；否则默认每小节自己的调号
    （无调号回退全局）；--key-per-section 时无调号的小节回退段落调性
    （段落调内覆盖率过低时用 K-K 自动估计），有小节调号时仍以小节调号为准。"""
    global_key = resolve_key(song, track, args.key)
    if args.key:
        keys_by_bar = {m.index: global_key for m in track.measures}
    elif args.key_per_section:
        section_keys = resolve_section_keys(track, global_key)
        keys_by_bar = {
            m.index: measure_key(m, section_keys[m.section]) for m in track.measures
        }
    else:
        keys_by_bar = {m.index: measure_key(m, global_key) for m in track.measures}
    return global_key, keys_by_bar


def _analyze_measures(
    track: GPTrack,
    keys_by_bar: dict[int, tuple[int, str]],
    segmenter,
    args,
) -> list[dict]:
    """对单个（或合并后的）轨道执行切窗 + 和弦识别。"""
    results = []
    measures = track.measures
    for mi, measure in enumerate(measures):
        seg_key = keys_by_bar[measure.index]
        next_measure = measures[mi + 1] if mi + 1 < len(measures) else None
        for seg in segmenter(measure, next_measure):
            if len(seg.notes) < args.min_notes:
                continue
            detected = detect_chord(seg.notes, *seg_key, args.style)
            results.append(
                {
                    "bar": seg.bar,
                    "section": seg.section,
                    "window": seg.window,
                    "start_quarters": seg.start_quarters,
                    "key": f"{pc_name(seg_key[0], seg_key[0])}{'m' if seg_key[1] == 'Minor' else ''}",
                    "anchor_beat_id": seg.anchor_beat_id,
                    "anchor_voice_id": seg.anchor_voice_id,
                    "anchor_pos": seg.anchor_pos,
                    "notes": [n.pitch_name or str(n.midi) for n in seg.notes],
                    "chord": detected,
                    "manual": seg.manual,
                }
            )
    return results


def _print_debug(results: list[dict], args) -> None:
    if not args.debug:
        return
    print(f"{'小节':>4}  {'窗口':<10} {'自动和弦':<12} {'音符':<32} 手动")
    print("-" * 86)
    for r in results:
        chord = r["chord"]
        weights = chord["weights"] if chord else {}
        pcs = " ".join(f"{k}:{v:g}" for k, v in weights.items()) if weights else "-"
        print(
            f"{r['bar']:>4}  {r['window']:<10} {(chord['name'] if chord else '-'):<12} "
            f"{pcs:<32} {r['manual'] or ''}"
        )


def _print_key(key_root: int, key_mode: str, args) -> None:
    if args.key is None:
        print(
            f"调性: {pc_name(key_root, key_root)}{'m' if key_mode == 'Minor' else ''} "
            "(全局；转调段按各小节调号，可用 --key 覆盖)"
        )
    else:
        print(
            f"调性: {pc_name(key_root, key_root)}{'m' if key_mode == 'Minor' else ''} "
            "(--key 指定)"
        )


def _write_back(
    args, song: GPSong, tracks: list[GPTrack], track_results: dict[int, list[dict]],
    write_keys: dict[int, int], merged_results: Optional[list[dict]],
    merged_key_root: Optional[int],
) -> None:
    """写回 .gp：默认分析模式写全部分析轨道，合并模式写第一个分析轨道。"""
    if args.no_write:
        print("未写回 .gp（--no-write）。")
        return
    if args.write == "__default__":
        output_path = str(Path(args.file).with_name(Path(args.file).stem + "_chords.gp"))
    else:
        output_path = args.write
    if Path(output_path).resolve() == Path(args.file).resolve():
        print("错误: 输出文件与输入文件相同，请用 --write 指定其他路径", file=sys.stderr)
        sys.exit(1)
    default_all = merged_results is None
    targets = resolve_write_tracks(song, tracks, args.write_tracks, default_all=default_all)
    current_input = args.file
    for target in targets:
        if merged_results is not None:
            measures_by_bar = {m.index: m for m in target.measures}
            target_results = reanchor_results(merged_results, target, measures_by_bar)
            key_root = merged_key_root
        else:
            target_results = track_results[target.id]
            key_root = write_keys[target.id]
        stats = write_chords_to_gp(
            current_input,
            output_path,
            song,
            target,
            target_results,
            key_root,
            overwrite=args.overwrite,
        )
        current_input = output_path
        print(
            f"\n写回完成: {output_path}（轨道 [{target.id}] {target.name}）\n"
            f"  新写入和弦: {stats['written']} 处 | 和弦库新增: {stats['new_chords']} 个"
            f"（库内共 {stats['total_chords_in_library']} 个）\n"
            f"  共享拍克隆: {stats.get('cloned', 0)} 个 | "
            f"跳过的手工标注小节: {stats['skipped_manual']} | "
            f"跳过已有和弦的拍: {stats['skipped_existing']}\n"
            f"  验证通过：输出文件中带和弦标注的拍: {stats['annotated_beats']}"
        )


def run_analysis(args) -> list[dict]:
    song = parse_gp(args.file)
    if args.track:
        try:
            tracks = select_tracks(song, args.track)
        except GuitarProError as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        tracks = prompt_tracks(song)

    segmenter = SEGMENTERS[args.window]
    primary = tracks[0]

    if args.merge:
        # 合并模式：多轨音符合并后识别一次（和弦拆在多轨/需要贝斯补低音时）
        analysis_track = merge_tracks(tracks)
        global_key, keys_by_bar = _key_for_track(song, analysis_track, args)
        key_root, key_mode = global_key
        _print_key(key_root, key_mode, args)
        if not args.key and args.key_per_section:
            print(f"调性模式: 按段落（{len(set(m.section for m in analysis_track.measures))} 个段落）")
        elif not args.key:
            print("调性模式: 每小节调号（无调号回退全局）")
        results = _analyze_measures(analysis_track, keys_by_bar, segmenter, args)
        print(
            "轨道: "
            + " + ".join(f"[{t.id}] {t.name}" for t in tracks)
            + f"  窗口: {args.window}  风格: {args.style}"
        )
        _print_debug(results, args)
        print(f"共分析 {len(results)} 个窗口。")
        comparison = []
        if not args.no_compare:
            comparison = compare_manual(analysis_track, keys_by_bar, args.style)
            print_comparison(comparison)

        if args.out:
            payload = {
                "file": args.file,
                "merged": True,
                "tracks": [{"id": t.id, "name": t.name} for t in tracks],
                "track": {"id": primary.id, "name": primary.name},  # 兼容旧字段
                "key": f"{pc_name(key_root, key_root)}{'m' if key_mode == 'Minor' else ''}",
                "window": args.window,
                "style": args.style,
                "results": results,
                "manual_comparison": comparison,
            }
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"已保存: {args.out}")

        _write_back(
            args, song, tracks, {}, {primary.id: key_root},
            merged_results=results, merged_key_root=key_root,
        )
        return results

    # 分析模式（默认）：每个轨道单独分析、单独标注
    track_results: dict[int, list[dict]] = {}
    write_keys: dict[int, int] = {}
    payload_tracks: list[dict] = []
    last_results: list[dict] = []
    for track in tracks:
        global_key, keys_by_bar = _key_for_track(song, track, args)
        key_root, key_mode = global_key
        _print_key(key_root, key_mode, args)
        if not args.key and args.key_per_section:
            print(f"调性模式: 按段落（{len(set(m.section for m in track.measures))} 个段落）")
        elif not args.key:
            print("调性模式: 每小节调号（无调号回退全局）")
        results = _analyze_measures(track, keys_by_bar, segmenter, args)
        print(f"轨道: [{track.id}] {track.name}  窗口: {args.window}  风格: {args.style}")
        _print_debug(results, args)
        print(f"共分析 {len(results)} 个窗口。")
        comparison = []
        if not args.no_compare:
            comparison = compare_manual(track, keys_by_bar, args.style)
            print_comparison(comparison)
        track_results[track.id] = results
        write_keys[track.id] = key_root
        last_results = results
        payload_tracks.append(
            {
                "id": track.id,
                "name": track.name,
                "key": f"{pc_name(key_root, key_root)}{'m' if key_mode == 'Minor' else ''}",
                "results": results,
                "manual_comparison": comparison,
            }
        )

    if args.out:
        payload: dict = {
            "file": args.file,
            "merged": False,
            "tracks": payload_tracks,
            "window": args.window,
            "style": args.style,
        }
        if len(tracks) == 1:
            # 兼容旧字段
            payload.update(
                {
                    "track": {"id": primary.id, "name": primary.name},
                    "key": payload_tracks[0]["key"],
                    "results": payload_tracks[0]["results"],
                    "manual_comparison": payload_tracks[0]["manual_comparison"],
                }
            )
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"已保存: {args.out}")

    _write_back(args, song, tracks, track_results, write_keys, None, None)
    return last_results


def demo() -> None:
    """不依赖文件，用示例音符演示和弦识别。"""
    from gpchords.parser import GPNote

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
    # Windows GBK 控制台打印轨道名（可能含 × 等字符）会抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        description="从 Guitar Pro 文件自动识别并标注和弦",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("file", nargs="?", help=".gp / .gpx 文件路径（--demo 时不需要）")
    parser.add_argument(
        "--track", action="append", default=None, metavar="TRACK",
        help="分析轨道，可多个（逗号分隔或重复 --track；all=全部非鼓轨道；"
        "默认每轨单独分析单独标注；不指定时交互选择）",
    )
    parser.add_argument(
        "--merge", action="store_true",
        help="合并所选轨道音符后识别一次（和弦拆在多轨/需要贝斯补低音时）",
    )
    parser.add_argument(
        "--window", choices=["auto", "measure", "half", "beat"], default="auto",
        help="分析窗口：auto=按和弦变化切窗（默认），measure/half/beat=固定窗口",
    )
    parser.add_argument(
        "--style", choices=["guitar", "theory"], default="guitar",
        help="guitar=强力/斜杠记法，theory=完整理论和弦",
    )
    parser.add_argument("--key", help="指定调性，如 C / Am / F#m（默认读文件调号）")
    parser.add_argument(
        "--key-per-section", action="store_true",
        help="没有小节调号时按段落确定调性（段落调内覆盖率过低时用 K-K 自动估计）",
    )
    parser.add_argument("--min-notes", type=int, default=1, help="少于该音符数的窗口跳过")
    parser.add_argument("--out", help="输出 JSON 结果文件")
    parser.add_argument(
        "--write", nargs="?", const="__default__", default="__default__", metavar="OUT.gp",
        help="写回路径（默认自动写 <原名>_chords.gp，--no-write 关闭）",
    )
    parser.add_argument("--no-write", action="store_true", help="不写回 .gp（只分析/输出 JSON）")
    parser.add_argument(
        "--write-tracks", action="append", default=None, metavar="TRACK",
        help="写回轨道（分析模式默认全部分析轨道；合并模式默认第一个分析轨道；"
        "all=全部分析轨道；可逗号分隔或重复）",
    )
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
