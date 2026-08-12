"""
自动分段脚本（gp-sections）
==========================

从 Guitar Pro 文件自动检测歌曲段落并写回 GP 的 ``<Section>`` 段落标记
（Letter + Text，格式与《春日影.gp》一致，如 ``A:Part 1``；所有段落都带
字母，不产生空 Letter）。

方法（多特征 + novelty + 重复起点）：

1. **每小节特征向量**：和弦（罗马度数+品质家族）、鼓指纹（16 分桶）、
   贝斯根音、密度（各轨总音符时长）、人声活动度、人声平均音高（旋律轮廓）、
   轨道活动组合、闷音比例、和声节奏密度（auto 窗口数）、速度；硬信号：
   调号/拍号/速度变化、连续空小节（长休止）。
2. **边界打分**：每个特征建自相似矩阵 → novelty 曲线（左右块内部相似、
   跨块不相似），归一化加权求和（密度权 2.0、人声 1.5、其余 1.0）；
   另加和弦矩阵的**重复起点**曲线——某段与前面某段高度相似也是段落起点
   （副歌复用 Intro 材料这类"相似切换"novelty 抓不到）。峰选取阈值
   均值+kσ、最小间距 gap；硬信号直接强制成边界。
3. **证据标注**：每个候选边界列出触发它的特征（哪个特征在它附近跳变），
   默认 ``--no-write`` 只打印候选供人工确认。
4. **段落聚类命名**：段 profile = 和弦序列对齐相似度（0.6 权重）+ 标量
   特征均值向量相似度（0.4），贪心聚类；重复段共享字母、Text 用
   ``Part N`` 区分；首段唯一且短 → ``Intro``、末段唯一 → ``Outro``，
   两者同样带字母。
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from gpreader import (
    GPSong,
    GuitarProError,
    GPTrack,
    parse_gp,
    select_tracks,
)
from gpreader.writer import read_gpif, restore_section_cdata, write_gpif
from gpchords.annotate import (
    detect_chord,
    measure_key,
    resolve_key,
    segment_auto,
)
from gpchords.roman import chord_to_roman
from gpchords.progression import find_loop_families

# ---------------------------------------------------------------------------
# 每小节特征
# ---------------------------------------------------------------------------

FEATURE_NAMES = {
    "chord": "和弦",
    "drum": "鼓",
    "bass": "贝斯",
    "density": "密度",
    "vocal_act": "人声活动",
    "vocal_pitch": "人声旋律",
    "track_act": "轨道",
    "palm": "闷音",
    "harm_rhythm": "和声密度",
    "tempo": "速度",
}

# novelty 加权（实验标定：密度最强、人声次之）
FEATURE_WEIGHTS = {
    "chord": 1.0,
    "drum": 1.0,
    "bass": 1.0,
    "density": 2.0,
    "vocal_act": 1.5,
    "vocal_pitch": 1.0,
    "track_act": 1.0,
    "palm": 1.0,
    "harm_rhythm": 1.0,
    "tempo": 1.0,
}

_BASS_KEYWORDS = ("bass", "贝斯")
_MELODY_KEYWORDS = ("vocal", "voice", "solo", "lead", "人声", "主唱", "歌", "vo.")
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass
class SongFeatures:
    """每小节特征数组（None 表示该特征不存在/不可用）。"""

    chords: list[Optional[tuple[str, str]]]  # (度数, 品质家族)
    drums: Optional[list[frozenset]] = None
    bass: Optional[list[Optional[int]]] = None
    density: list[float] = field(default_factory=list)
    vocal_act: Optional[list[float]] = None
    vocal_pitch: Optional[list[Optional[float]]] = None
    track_act: list[frozenset] = field(default_factory=list)
    palm: list[float] = field(default_factory=list)
    harm_rhythm: list[float] = field(default_factory=list)
    tempo: Optional[list[Optional[int]]] = None
    hard_events: dict[int, list[str]] = field(default_factory=dict)  # 1 起小节

    @property
    def n(self) -> int:
        return len(self.chords)

    def present(self) -> list[str]:
        out = []
        if self.chords:
            out.append("chord")
        if self.drums is not None:
            out.append("drum")
        if self.bass is not None:
            out.append("bass")
        if self.density:
            out.append("density")
        if self.vocal_act is not None:
            out.append("vocal_act")
        if self.vocal_pitch is not None:
            out.append("vocal_pitch")
        if self.track_act:
            out.append("track_act")
        if self.palm:
            out.append("palm")
        if self.harm_rhythm:
            out.append("harm_rhythm")
        if self.tempo is not None:
            out.append("tempo")
        return out


def _is_bass_track(t: GPTrack) -> bool:
    name = (t.name + " " + t.program).lower()
    if any(k in name for k in _BASS_KEYWORDS):
        return True
    return t.midi_program is not None and 32 <= t.midi_program <= 39


def _melody_score(t: GPTrack) -> float:
    """旋律轨得分：名称/音色像人声的加分 + 单音占比（旋律大多单音）。"""
    name = (t.name + " " + t.program).lower()
    score = 2.0 if any(k in name for k in _MELODY_KEYWORDS) else 0.0
    beats = [
        b
        for m in t.measures
        for b in m.beats
        if any(not x.muted for x in b.notes)
    ]
    if beats:
        mono = sum(
            1
            for b in beats
            if sum(1 for x in b.notes if not x.muted) == 1
        ) / len(beats)
        score += mono
    return score


def _detect_melody_track(song: GPSong) -> Optional[GPTrack]:
    """自动找旋律/人声轨：排除鼓和贝斯，取名称关键词 + 单音占比最高者。"""
    candidates = [
        t
        for t in song.tracks
        if t.midi_program != 0 and not _is_bass_track(t)
    ]
    if not candidates:
        return None
    return max(candidates, key=_melody_score)


def _roman_degree(roman: str) -> str:
    main = roman.split("/")[0].strip()
    for i, ch in enumerate(main):
        if ch in "IViv":
            j = i + 1
            while j < len(main) and main[j] in "IViv":
                j += 1
            return main[:j]
    return main


def _quality_family(quality: str) -> str:
    if "dim" in quality or "ø" in quality:
        return "dim"
    if "aug" in quality:
        return "aug"
    if "sus" in quality:
        return "sus"
    if "maj" in quality:
        return "maj"
    if "m" in quality:
        return "min"
    if "7" in quality:
        return "dom"
    return "maj"


def _meas_notes(m) -> list:
    return [x for b in m.beats for x in b.notes if not x.muted]


def _drum_fingerprint(m) -> frozenset:
    return frozenset(
        (int(round(b.start_quarters * 4)), x.midi)
        for b in m.beats
        for x in b.notes
        if not x.muted
    )


def extract_features(
    song: GPSong,
    track: GPTrack,
    keys_by_bar: dict[int, tuple[int, str]],
    vocal_track: Optional[str] = None,
) -> SongFeatures:
    """提取逐小节特征。``vocal_track`` 可为轨道选择器或 "none"/None（自动）。"""
    n = len(track.measures)
    if vocal_track == "none":
        vocal_el = None
    elif vocal_track:
        vocal_el = select_tracks(song, [vocal_track])[0]
    else:
        vocal_el = _detect_melody_track(song)

    chords: list[Optional[tuple[str, str]]] = []
    for m in track.measures:
        notes = _meas_notes(m)
        if not notes:
            chords.append(None)
            continue
        r = detect_chord(notes, *measure_key(m, keys_by_bar[m.index]), "guitar")
        if r is None:
            chords.append(None)
            continue
        roman = chord_to_roman(r, r.get("key_root", keys_by_bar[m.index][0]))
        chords.append((_roman_degree(roman), _quality_family(r["quality"])))

    drum_track = next(
        (t for t in song.tracks if t.midi_program == 0), None
    )
    drums = (
        [_drum_fingerprint(m) for m in drum_track.measures]
        if drum_track is not None
        else None
    )
    bass_track = next(
        (t for t in song.tracks if _is_bass_track(t) and t.midi_program != 0),
        None,
    )
    bass = (
        [
            min((x.midi for x in _meas_notes(m)), default=None)
            for m in bass_track.measures
        ]
        if bass_track is not None
        else None
    )

    density: list[float] = []
    palm: list[float] = []
    for mi in range(n):
        dur = 0.0
        palm_dur = 0.0
        for t in song.tracks:
            if t.midi_program == 0:
                continue
            for x in _meas_notes(t.measures[mi]):
                dur += x.duration_quarters
                if x.palm_muted:
                    palm_dur += x.duration_quarters
        density.append(dur)
        palm.append(palm_dur / dur if dur else 0.0)
    mx = max(density) or 1.0
    density = [d / mx for d in density]

    vocal_act = (
        [sum(x.duration_quarters for x in _meas_notes(m)) for m in vocal_el.measures]
        if vocal_el is not None
        else None
    )
    vocal_pitch = None
    if vocal_el is not None:
        vocal_pitch = []
        for m in vocal_el.measures:
            ns = _meas_notes(m)
            if not ns:
                vocal_pitch.append(None)
                continue
            w = [max(x.duration_quarters, 0.25) for x in ns]
            vocal_pitch.append(
                sum(x.midi * w[i] for i, x in enumerate(ns)) / sum(w)
            )

    track_act = [
        frozenset(
            t.id
            for t in song.tracks
            if t.midi_program != 0 and _meas_notes(t.measures[mi])
        )
        for mi in range(n)
    ]
    harm_rhythm = [
        len(segment_auto(m, None)) if any(_meas_notes(m)) else 0
        for m in track.measures
    ]
    tempo = (
        [song.tempo_at(i) for i in range(n)] if song.tempos else None
    )

    # 硬信号：调号/拍号/速度变化、连续空小节
    hard: dict[int, list[str]] = {}
    for i, m in enumerate(track.measures, start=1):
        prev = track.measures[i - 2] if i >= 2 else None
        if (
            prev is not None
            and m.key_signature != prev.key_signature
        ):
            hard.setdefault(i, []).append("调号变化")
        if (
            prev is not None
            and m.time_signature != prev.time_signature
        ):
            hard.setdefault(i, []).append("拍号变化")
        if tempo is not None and i >= 2 and tempo[i - 1] != tempo[i - 2]:
            hard.setdefault(i, []).append("速度变化")
    # 长休止 = 全曲编曲级停顿（非鼓轨全部静默 ≥2 小节），而不是单轨换气
    empty_run = 0
    for i, m in enumerate(track.measures, start=1):
        if density[i - 1] <= 0:
            empty_run += 1
            if empty_run >= 2:
                hard.setdefault(i, []).append("长休止")
        else:
            empty_run = 0

    return SongFeatures(
        chords=chords,
        drums=drums,
        bass=bass,
        density=density,
        vocal_act=vocal_act,
        vocal_pitch=vocal_pitch,
        track_act=track_act,
        palm=palm,
        harm_rhythm=harm_rhythm,
        tempo=tempo,
        hard_events=hard,
    )


# ---------------------------------------------------------------------------
# 相似度矩阵 / novelty / 重复起点
# ---------------------------------------------------------------------------


def _chord_S(features: SongFeatures) -> list[list[float]]:
    n = features.n
    S = [[0.0] * n for _ in range(n)]
    T = features.chords
    for i in range(n):
        for j in range(n):
            a, b = T[i], T[j]
            if a is None or b is None:
                S[i][j] = 0.0
            elif a == b:
                S[i][j] = 1.0
            elif a[0] == b[0]:
                S[i][j] = 0.5
    return S


def _categorical_S(values: list, equal) -> list[list[float]]:
    n = len(values)
    return [[equal(values[i], values[j]) for j in range(n)] for i in range(n)]


def _scalar_S(values: list, band: float) -> list[list[float]]:
    n = len(values)
    S = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            a, b = values[i], values[j]
            if a is None and b is None:
                S[i][j] = 1.0  # 都静默/都未知 → 相似
            elif a is None or b is None:
                S[i][j] = 0.0
            else:
                S[i][j] = max(0.0, 1.0 - abs(a - b) / band)
    return S


def _jaccard(a, b) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def feature_matrices(
    features: SongFeatures,
) -> dict[str, list[list[float]]]:
    out: dict[str, list[list[float]]] = {}
    if features.chords:
        out["chord"] = _chord_S(features)
    if features.drums is not None:
        out["drum"] = _categorical_S(features.drums, _jaccard)
    if features.bass is not None:
        out["bass"] = _categorical_S(
            features.bass, lambda a, b: 1.0 if a is not None and a == b else 0.0
        )
    if features.density:
        out["density"] = _scalar_S(features.density, band=2.0)
    if features.vocal_act is not None:
        out["vocal_act"] = _scalar_S(features.vocal_act, band=2.0)
    if features.vocal_pitch is not None:
        out["vocal_pitch"] = _scalar_S(features.vocal_pitch, band=12.0)
    if features.track_act:
        out["track_act"] = _categorical_S(features.track_act, _jaccard)
    if features.palm:
        out["palm"] = _scalar_S(features.palm, band=0.5)
    if features.harm_rhythm:
        out["harm_rhythm"] = _scalar_S(
            [float(x) for x in features.harm_rhythm], band=2.0
        )
    if features.tempo is not None:
        out["tempo"] = _scalar_S(
            [float(x) if x is not None else None for x in features.tempo],
            band=20.0,
        )
    return out


def novelty_curve(S: list[list[float]], L: int) -> list[float]:
    n = len(S)
    cur = [0.0] * n
    for b in range(L, n):
        left, right = range(b - L, b), range(b, min(n, b + L))
        intra = c = 0.0
        for i in left:
            for j in left:
                if j > i:
                    intra += S[i][j]
                    c += 1
        for i in right:
            for j in right:
                if j > i:
                    intra += S[i][j]
                    c += 1
        intra = intra / c if c else 0.0
        cross = c = 0.0
        for i in left:
            for j in right:
                cross += S[i][j]
                c += 1
        cross = cross / c if c else 0.0
        cur[b] = intra - cross
    return cur


def repetition_onset_curve(
    S: list[list[float]], L: int
) -> tuple[list[float], list[int]]:
    """块 [b, b+L) 与前面所有块的**逐位对齐**最大相似度及匹配起点（0 起）。

    段落在复用旧材料也是起点；同时记录最佳匹配块的位置，供"复用起点对齐
    已有边界"的定向判定（避免全曲处处重复造成的 rep 噪声峰）。
    逐位对齐（S[b+i][e+i]）而不是跨位置两两平均：同一段重播时位置是一一
    对应的，跨位置平均会把精确重复稀释成 0.5 附近。
    """
    n = len(S)
    cur = [0.0] * n
    arg = [-1] * n
    for b in range(L, n - L):
        best = 0.0
        best_e = -1
        for e in range(0, b - L + 1):
            v = sum(S[b + i][e + i] for i in range(L)) / L
            if v > best:
                best = v
                best_e = e
        cur[b] = best
        arg[b] = best_e
    return cur, arg


def _norm(c: list[float]) -> list[float]:
    mn, mx = min(c), max(c)
    return [(x - mn) / (mx - mn) if mx > mn else 0.0 for x in c]


def _mean_std(c: list[float]) -> tuple[float, float]:
    m = sum(c) / len(c) if c else 0.0
    sd = (sum((x - m) ** 2 for x in c) / len(c)) ** 0.5 if c else 0.0
    return m, sd


@dataclass
class Boundary:
    bar: int  # 1 起：新段落从这里开始
    score: float
    evidence: list[str] = field(default_factory=list)
    forced: bool = False  # 硬信号强制边界（非 novelty 峰）


def detect_boundaries(
    features: SongFeatures,
    L: int = 4,
    gap: int = 4,
    kthr: float = 0.4,
    rep_weight: float = 0.25,
    split_period: int = 8,
) -> list[Boundary]:
    """多特征 novelty 加权 + 定向重复起点 + 连续循环切分 + 硬信号 → 候选边界。

    - 调号/拍号/速度变化是**软信号**：给组合曲线加固定增益，段内转调
      （如 Bridge 内部转调）不会强制切开；编曲级长休止仍强制。
    - 重复起点曲线独立取峰：新段落在复用前面某段的材料（Bridge 2 复用
      Bridge 1 开头、副歌复用 Intro）时也是段落起点。
    - 连续重复进行按循环遍边界切分（``split_period`` 及以上周期的循环，
      如 8 小节 Verse ×2 → 第二遍起点是段落起点）。
    """
    n = features.n
    if n == 0:
        return []
    matrices = feature_matrices(features)
    curves: dict[str, list[float]] = {}
    thr_map: dict[str, float] = {}
    for name, S in matrices.items():
        c = _norm(novelty_curve(S, L))
        if max(c) == min(c):
            continue  # 该特征全曲无变化（如恒定速度），不参与打分与证据
        curves[name] = c
        thr_map[name] = _mean_std(c)[0] + 0.5 * _mean_std(c)[1]

    combined = [0.0] * n
    for name, c in curves.items():
        w = FEATURE_WEIGHTS.get(name, 1.0)
        for i in range(n):
            combined[i] += w * c[i]
    rep_arg: list[int] = []
    rep_raw: list[float] = []
    if "chord" in matrices:
        rep_raw, rep_arg = repetition_onset_curve(matrices["chord"], min(L, 4))
        rep = _norm(rep_raw)
        if max(rep) > min(rep):
            for i in range(n):
                combined[i] += rep_weight * rep[i]
            curves["_rep"] = rep

    # 硬信号：长休止强制；调号/拍号/速度变化只作证据（软）——段内转调很
    # 常见（如《无论如何》Bridge 内部 C 大调），改分数会抬高全局阈值、
    # 误伤其他弱边界
    forced_indices: set[int] = set()
    for bar, reasons in features.hard_events.items():
        idx = bar - 1
        if not (0 <= idx < n):
            continue
        if "长休止" in reasons:
            forced_indices.add(idx)

    m, sd = _mean_std(combined)
    threshold = m + kthr * sd
    peaks = _peaks(combined, gap, threshold)

    # 定向重复起点：逐位对齐相似度 ≥0.75 的局部峰 + 匹配到的旧块起点是
    # 歌曲开头或已检出边界
    # （《无论如何》Bridge 2 前 4 小节复用 Bridge 1、《春日影》副歌复用
    # Intro 都是这种；全曲处处相似产生的普通 rep 峰被排除）
    # 干净循环区域内部不再报"复用起点"（Intro 2 小节循环让 3/5/7 全变成
    # 复用歌曲开头）；区域起点本身保留（副歌 45 复用 Intro 正是区域起点）。
    # 只有重复遍逐位相似度 ≥0.85 的干净循环才参与抑制——模糊循环的区域
    # 边界不可靠（如《无论如何》94-105 的 6 小节模糊循环会吞掉 Bridge 2
    # 起点 105）。
    loop_interior: set[int] = set()
    if features.chords:
        for f in find_loop_families(features.chords):
            for start, end in f.occurrences:
                if _region_quality(features.chords, start, end, f.period) >= 0.85:
                    loop_interior.update(range(start + 1, end + 1))

    if rep_raw and len(rep_arg) == n:
        rep_peaks: set[int] = set()
        for b in range(gap, n - 1):
            if rep_raw[b] < 0.75:
                continue
            if b + 1 in loop_interior:
                continue
            if not (
                rep_raw[b] >= rep_raw[b - 1] and rep_raw[b] >= rep_raw[b + 1]
            ):
                continue
            e0 = rep_arg[b]
            if e0 < 0:
                continue
            e_bar = e0 + 1
            aligned = e_bar == 1 or any(
                abs(e_bar - p) <= 1 for p in peaks
            )
            if not aligned:
                continue
            if not any(abs(b - p) <= 1 for p in peaks):
                peaks.append(b)
                rep_peaks.add(b)

    # 连续重复循环：周期 ≥ split_period 的循环在每遍起点切分
    if split_period and features.chords:
        for bar in _loop_copy_boundaries(features.chords, split_period):
            idx = bar - 1
            if not any(abs(idx - p) <= 1 for p in peaks):
                peaks.append(idx)

    for idx in sorted(forced_indices):
        if not any(abs(idx - p) <= 1 for p in peaks):
            peaks.append(idx)
    peaks = sorted(set(peaks))
    # 重复起点独证的候选边界需要足够分数才保留（循环多的歌里"某块和前面
    # 某块相似"遍地都是，低分 rep 峰大多是噪声；如《春日影》中段的
    # 46/56/80/84/90/93 分数 1.3-2.6，而《无论如何》Bridge 2 的 103 有 3.1）
    peaks = [
        b
        for b in peaks
        if b not in rep_peaks or combined[b] >= 3.0
    ]

    out: list[Boundary] = []
    for b in peaks:
        evidence = _evidence_at(curves, thr_map, combined, b, gap)
        out.append(
            Boundary(
                bar=b + 1,
                score=round(combined[b], 3),
                evidence=evidence,
                forced=b in forced_indices,
            )
        )

    # 硬信号原因并入附近边界的证据（不再单独强制）
    for bar, reasons in sorted(features.hard_events.items()):
        if not out:
            break
        idx = min(range(len(out)), key=lambda i: abs(out[i].bar - bar))
        if abs(out[idx].bar - bar) <= 1:
            for reason in reasons:
                if reason not in out[idx].evidence:
                    out[idx].evidence.append(reason)
    out.sort(key=lambda x: x.bar)
    return out


def _region_quality(
    chords: list[Optional[tuple[str, str]]],
    start: int,
    end: int,
    period: int,
) -> float:
    """循环区域的重复遍逐位度数一致率（0..1）。"""
    copies = (end - start + 1) // period
    if copies < 2:
        return 1.0
    sims = []
    for k in range(copies - 1):
        a = start - 1 + k * period
        b = a + period
        hits = sum(
            1
            for i in range(period)
            if chords[a + i]
            and chords[b + i]
            and chords[a + i][0] == chords[b + i][0]
        )
        sims.append(hits / period)
    return sum(sims) / len(sims)


def _peaks(curve: list[float], gap: int, threshold: float) -> list[int]:
    n = len(curve)
    out: list[int] = []
    b = gap
    while b < n - 1:
        # 每个 gap 窗口内取最强局部峰（允许尾部边界，如最后 4 小节的 Outro）
        best = -1
        for i in range(b, min(b + gap, n - 1)):
            if curve[i] < threshold:
                continue
            if i > 0 and curve[i] < curve[i - 1]:
                continue
            if i + 1 < n and curve[i] < curve[i + 1]:
                continue
            if best < 0 or curve[i] > curve[best]:
                best = i
        if best >= 0:
            out.append(best)
        b += gap
    return out


def _loop_copy_boundaries(
    chords: list[Optional[tuple[str, str]]], min_period: int
) -> list[int]:
    """连续重复循环的每遍起点（1 起）：8 小节 Verse ×2 -> 第二遍起点。"""
    out: list[int] = []
    for f in find_loop_families(chords):
        if f.period < min_period:
            continue
        for start, end in f.occurrences:
            copies = (end - start + 1) // f.period
            for k in range(1, copies):
                out.append(start + k * f.period)
    return out


def _evidence_at(
    curves: dict[str, list[float]],
    thr_map: dict[str, float],
    combined: list[float],
    b: int,
    gap: int,
) -> list[str]:
    ev: list[str] = []
    for name, c in curves.items():
        if name == "_rep":
            if (
                c[b] >= thr_map.get("chord", 0.5)
                and c[b] >= c[b - 1]
                and c[b] >= c[b + 1]
            ):
                ev.append("重复起点")
            continue
        if c[b] < thr_map[name]:
            continue
        lo = max(b - 1, 0)
        hi = min(b + 1, len(c) - 1)
        if c[b] >= c[lo] and c[b] >= c[hi]:
            ev.append(FEATURE_NAMES[name])
    return ev


# ---------------------------------------------------------------------------
# 分段 / 聚类 / 命名
# ---------------------------------------------------------------------------


@dataclass
class Section:
    start_bar: int
    end_bar: int
    letter: str = ""
    text: str = ""

    @property
    def name(self) -> str:
        return f"{self.letter}:{self.text}" if self.text else self.letter

    @property
    def length(self) -> int:
        return self.end_bar - self.start_bar + 1


def _edit_sim(a: list[str], b: list[str]) -> float:
    """归一化编辑距离相似度：1 - edit_dist/max(len)。比 LCS 严格，
    不把"某段是另一段的子序列"误判成重复段。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    dp = list(range(lb + 1))
    for i in range(1, la + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, lb + 1):
            cur = dp[j]
            dp[j] = min(
                dp[j] + 1,
                dp[j - 1] + 1,
                prev + (0 if a[i - 1] == b[j - 1] else 1),
            )
            prev = cur
    return 1.0 - dp[lb] / max(la, lb)


def _profile_vector(
    start: int, end: int, features: SongFeatures
) -> list[float]:
    """段的标量特征均值向量（用于相似度；缺失特征记 0，分母按存在特征数）。"""
    def mean(vals: list[float]) -> float:
        return sum(vals[start - 1 : end]) / (end - start + 1) if vals else 0.0

    vec = [mean(features.density)]
    if features.vocal_act is not None:
        vec.append(mean(features.vocal_act))
    if features.vocal_pitch is not None:
        pts = [
            x if x is not None else 0.0
            for x in features.vocal_pitch[start - 1 : end]
        ]
        vec.append(sum(pts) / (end - start + 1) if pts else 0.0)
    if features.palm:
        vec.append(mean(features.palm))
    if features.harm_rhythm:
        vec.append(mean(features.harm_rhythm))
    vec.append(
        sum(len(features.track_act[i]) for i in range(start - 1, end))
        / (end - start + 1)
    )
    return vec


def _cosine(a: list[float], b: list[float]) -> float:
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def segment_similarity(
    s1: tuple[int, int],
    s2: tuple[int, int],
    features: SongFeatures,
) -> float:
    """段相似度 = 0.7×和弦序列编辑距离 + 0.3×标量均值向量余弦。

    长度差超过 2 倍的段不做重复归类（长度本身是段落身份的强信号）。
    """
    a = [t[0] for t in features.chords[s1[0] - 1 : s1[1]] if t is not None]
    b = [t[0] for t in features.chords[s2[0] - 1 : s2[1]] if t is not None]
    if not a or not b:
        return 0.4 * _cosine(
            _profile_vector(*s1, features), _profile_vector(*s2, features)
        )
    long, short = max(len(a), len(b)), min(len(a), len(b))
    if long > 2 * short:
        chord_sim = 0.0
    else:
        chord_sim = _edit_sim(a, b)
    prof_sim = _cosine(
        _profile_vector(*s1, features), _profile_vector(*s2, features)
    )
    return 0.7 * chord_sim + 0.3 * prof_sim


def build_sections(
    boundaries: list[Boundary],
    n_bars: int,
    features: SongFeatures,
    min_bars: int = 2,
    similarity: float = 0.6,
) -> list[Section]:
    """边界 → 段；合并过短段；贪心聚类分配字母与 Part N 文本。"""
    bars = [b.bar for b in boundaries if 1 < b.bar <= n_bars]
    bars = sorted(set(bars))
    spans: list[tuple[int, int]] = []
    prev = 1
    for b in bars:
        spans.append((prev, b - 1))
        prev = b
    spans.append((prev, n_bars))

    # 合并过短段（并入和弦相似度更高的一侧，默认并入前段）
    changed = True
    while changed and len(spans) > 1:
        changed = False
        for i, (s, e) in enumerate(spans):
            if e - s + 1 >= min_bars:
                continue
            left = spans[i - 1] if i > 0 else None
            right = spans[i + 1] if i + 1 < len(spans) else None
            if left is None and right is None:
                continue
            if right is None or (
                left is not None
                and segment_similarity(left, (s, e), features)
                >= segment_similarity(right, (s, e), features)
            ):
                spans[i - 1] = (left[0], e)
            else:
                spans[i + 1] = (s, right[1])
            del spans[i]
            changed = True
            break

    # 贪心聚类：与之前最相似的段同字母
    sections: list[Section] = []
    letter_of: dict[int, str] = {}
    letter_count = 0
    for s, e in spans:
        best_idx = -1
        best_sim = 0.0
        for j, sec in enumerate(sections):
            sim = segment_similarity((s, e), (sec.start_bar, sec.end_bar), features)
            if sim > best_sim:
                best_sim = sim
                best_idx = j
        if best_idx >= 0 and best_sim >= similarity:
            letter = letter_of[best_idx]
        else:
            letter = (
                _LETTERS[letter_count % 26]
                + (str(letter_count // 26) if letter_count >= 26 else "")
            )
            letter_count += 1
        sections.append(Section(start_bar=s, end_bar=e, letter=letter))
        letter_of[len(sections) - 1] = letter

    # Part N / Intro / Outro 文本（字母保留，Intro/Outro 只改 Text）
    counts: dict[str, int] = {}
    for sec in sections:
        counts[sec.letter] = counts.get(sec.letter, 0) + 1
    seq: dict[str, int] = {}
    for idx, sec in enumerate(sections):
        seq[sec.letter] = seq.get(sec.letter, 0) + 1
        if (
            idx == 0
            and counts[sec.letter] == 1
            and sec.length <= 8
        ):
            sec.text = "Intro"
        elif idx == len(sections) - 1 and counts[sec.letter] == 1:
            sec.text = "Outro"
        else:
            sec.text = f"Part {seq[sec.letter]}"
    return sections


# ---------------------------------------------------------------------------
# 写回 <Section>
# ---------------------------------------------------------------------------


def _set_section(mb: ET.Element, letter: str, text: str) -> None:
    old = mb.find("Section")
    if old is not None:
        mb.remove(old)
    sec = ET.Element("Section")
    ET.SubElement(sec, "Letter").text = letter
    ET.SubElement(sec, "Text").text = text
    bars_el = mb.find("Bars")
    if bars_el is not None:
        mb.insert(list(mb).index(bars_el), sec)
    else:
        time_el = mb.find("Time")
        if time_el is not None:
            mb.insert(list(mb).index(time_el) + 1, sec)
        else:
            mb.append(sec)


def write_sections_to_gp(
    input_path: str | Path,
    output_path: str | Path,
    sections: list[Section],
    overwrite: bool = False,
) -> dict:
    """把段落标记写回新的 .gp 文件（原文件不动）。已有段落的小节默认跳过。"""
    root, _ = read_gpif(input_path)
    master_bars = root.findall("MasterBars/MasterBar")
    written = skipped = 0
    for sec in sections:
        idx = sec.start_bar - 1
        if not (0 <= idx < len(master_bars)):
            continue
        mb = master_bars[idx]
        if mb.find("Section") is not None and not overwrite:
            skipped += 1
            continue
        _set_section(mb, sec.letter, sec.text)
        written += 1
    write_gpif(input_path, output_path, root, extra_fix=restore_section_cdata)

    verify = parse_gp(output_path)
    track0 = verify.tracks[0] if verify.tracks else None
    match = 0
    if track0 is not None:
        match = sum(
            1
            for s in sections
            if 0 < s.start_bar <= len(track0.measures)
            and track0.measures[s.start_bar - 1].section == s.name
        )
    return {
        "written": written,
        "skipped": skipped,
        "sections": len(sections),
        "verified_match": match,
        "verified_total": len(sections),
    }


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _default_track(song: GPSong) -> GPTrack:
    """默认取非鼓、非贝斯中音符最多的轨（通常是节奏/伴奏轨，和弦特征最实）。"""
    cands = [
        t for t in song.tracks if t.notes and t.midi_program != 0 and not _is_bass_track(t)
    ]
    if not cands:
        cands = [t for t in song.tracks if t.notes] or list(song.tracks)
    return max(cands, key=lambda t: len(t.notes))


def run(args) -> dict:
    song = parse_gp(args.file)
    if args.track:
        track = select_tracks(song, [args.track])[0]
    else:
        track = _default_track(song)
    global_key = resolve_key(song, track, None)
    keys_by_bar = {m.index: measure_key(m, global_key) for m in track.measures}
    features = extract_features(
        song, track, keys_by_bar, vocal_track=args.vocal_track
    )
    boundaries = detect_boundaries(
        features,
        L=args.novelty_l,
        gap=args.gap,
        kthr=args.threshold,
    )
    sections = build_sections(
        boundaries,
        features.n,
        features,
        min_bars=args.min_bars,
        similarity=args.similarity,
    )
    for override in args.name or []:
        letter, _, text = override.partition("=")
        for sec in sections:
            if sec.letter == letter.strip():
                sec.text = text.strip() or sec.text

    print(
        f"轨道: [{track.id}] {track.name}  小节: {features.n}  "
        f"特征: {'、'.join(FEATURE_NAMES[k] for k in features.present())}"
    )
    print(f"候选边界 {len(boundaries)} 个（--no-write 只预览）:")
    for b in boundaries:
        flag = " [强制]" if b.forced else ""
        ev = "、".join(b.evidence) if b.evidence else "-"
        print(f"  第 {b.bar:>3} 小节起  score={b.score:+.2f}{flag}  证据: {ev}")
    print("\n段落:")
    for sec in sections:
        print(
            f"  {sec.name:<10} 第 {sec.start_bar:>3}-{sec.end_bar:>3} 小节"
            f"（{sec.length} 小节）"
        )

    stats = None
    if not args.no_write:
        output_path = (
            args.write
            if args.write
            else str(Path(args.file).with_name(Path(args.file).stem + "_sections.gp"))
        )
        if Path(output_path).resolve() == Path(args.file).resolve():
            print("错误: 输出文件与输入文件相同，请用 --write 指定其他路径", file=sys.stderr)
            sys.exit(1)
        stats = write_sections_to_gp(
            args.file, output_path, sections, overwrite=args.overwrite
        )
        print(f"\n写回完成: {output_path}")
        print(
            f"  写入 {stats['written']} 个段落标记 | 跳过已有 {stats['skipped']} | "
            f"自检 {stats['verified_match']}/{stats['verified_total']}"
        )
    else:
        print("\n未写回 .gp（--no-write）。")

    if args.out:
        payload = {
            "file": args.file,
            "track": {"id": track.id, "name": track.name},
            "features": [FEATURE_NAMES[k] for k in features.present()],
            "boundaries": [
                {
                    "bar": b.bar,
                    "score": b.score,
                    "evidence": b.evidence,
                    "forced": b.forced,
                }
                for b in boundaries
            ],
            "sections": [
                {
                    "start_bar": s.start_bar,
                    "end_bar": s.end_bar,
                    "letter": s.letter,
                    "text": s.text,
                    "name": s.name,
                    "length": s.length,
                }
                for s in sections
            ],
            "stats": stats,
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"已保存: {args.out}")
    return {"sections": sections, "boundaries": boundaries, "stats": stats}


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        description="自动检测 Guitar Pro 歌曲段落并写回 <Section> 标记",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("file", help=".gp / .gpx 文件路径")
    parser.add_argument(
        "--track", help="分析轨道（默认自动选第一个非鼓、非贝斯的音符轨）"
    )
    parser.add_argument(
        "--vocal-track", default=None, metavar="TRACK",
        help="人声/旋律轨（默认自动识别；none 关闭人声特征）",
    )
    parser.add_argument("--min-bars", type=int, default=2, help="过短段合并阈值（小节）")
    parser.add_argument("--similarity", type=float, default=0.6, help="段落聚类相似度阈值")
    parser.add_argument("--novelty-l", type=int, default=6, help="novelty 块长（小节）")
    parser.add_argument("--gap", type=int, default=4, help="候选边界最小间距（小节）")
    parser.add_argument(
        "--threshold", type=float, default=0.4,
        help="峰阈值 = 均值 + threshold×标准差",
    )
    parser.add_argument(
        "--split-period", type=int, default=8,
        help="连续重复循环的切分周期下限（小节）：周期达到该值的循环"
        "在每遍起点切段（0 关闭；如 8 小节 Verse ×2 -> 第二遍是新段）",
    )
    parser.add_argument(
        "--name", action="append", default=None, metavar="A=Verse 1",
        help="覆盖某字母的段落文本，可重复",
    )
    parser.add_argument(
        "--write", metavar="OUT.gp", help="输出路径（默认 <原名>_sections.gp）"
    )
    parser.add_argument("--no-write", action="store_true", help="只检测/打印，不写回")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有段落标记")
    parser.add_argument("--out", help="输出 JSON 结果文件")
    args = parser.parse_args()
    try:
        run(args)
    except GuitarProError as e:
        print(f"解析失败: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
