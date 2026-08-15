"""
分析并拆解 Guitar Pro (.gp / .gpx) 中的旋律轨。

底层只用项目内的 :mod:`gpreader` 解析谱面，不依赖 PyGuitarPro。脚本把
选中轨道的旋律整理成带绝对时间、调性上下文和音级信息的"事件流"，再切
成乐句，并用 :mod:`melody_motifs` 的动机引擎在**整条旋律**上寻找重复
素材（精确反复、移调序列、八度变体、调内音级变体、轮廓反复、纯节奏型），
输出乐句结构、动机使用位置与分布统计的 Markdown 分析报告。

用法示例::

    uv run python melody_analyzer.py "song.gp" --track "Vocals" --report vocal.md
    uv run python melody_analyzer.py "song.gp" --track auto --phrase-gap 1.5
"""

from __future__ import annotations

import argparse
import copy
import sys
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Optional

from gpchords.annotate import measure_key, resolve_key
from gpchords.theory import pc_name
from gpreader import (
    GPNote,
    GPSong,
    GPTrack,
    GuitarProError,
    parse_gp,
    select_track,
)
from melody_motifs import MelodyNote, Motif, find_motifs

EPS = 1e-6
_BASS_KEYWORDS = ("bass", "贝斯")
_MELODY_KEYWORDS = (
    "vocal",
    "voice",
    "solo",
    "lead",
    "melody",
    "人声",
    "主唱",
    "歌",
    "vo.",
)

_MAJOR_PCS = (0, 2, 4, 5, 7, 9, 11)
_MINOR_PCS = (0, 2, 3, 5, 7, 8, 10)
_MAJOR_SOLFEGE = ("do", "re", "mi", "fa", "sol", "la", "ti")
_MINOR_SOLFEGE = ("la", "ti", "do", "re", "mi", "fa", "sol")
_ACCIDENTALS = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "##"}

_INTERVAL_NAMES = {
    0: "unison",
    1: "minor 2nd",
    2: "major 2nd",
    3: "minor 3rd",
    4: "major 3rd",
    5: "perfect 4th",
    6: "tritone",
    7: "perfect 5th",
    8: "minor 6th",
    9: "major 6th",
    10: "minor 7th",
    11: "major 7th",
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

_REP_LABELS = {
    "exact": "逐字反复",
    "interval": "移调",
    "octave": "八度变体",
    "degree": "音级变体",
    "contour": "轮廓",
    "rhythm": "节奏型",
}


@dataclass
class MeasureSpan:
    index: int
    start_quarters: float
    duration_quarters: float
    bpm: int

    @property
    def end_quarters(self) -> float:
        return self.start_quarters + self.duration_quarters


@dataclass
class MelodyEvent:
    index: int
    measure: int
    position_quarters: float
    onset_quarters: float
    onset_seconds: float
    duration_quarters: float
    duration_seconds: float
    is_rest: bool
    midi: Optional[int]
    pitch: Optional[str]
    tie_origin: bool
    tie_destination: bool
    section: Optional[str]
    degree: Optional[str]
    degree_pc: Optional[int]
    interval_semitones: Optional[int]
    interval_name: Optional[str]
    direction: Optional[str]
    rhythm: Optional[str]


@dataclass
class Phrase:
    id: int
    start_measure: int
    end_measure: int
    start_seconds: float
    end_seconds: float
    note_count: int
    pitch_min: str
    pitch_max: str
    section: Optional[str]
    boundary_reason: str
    events: list[MelodyEvent] = field(default_factory=list)


@dataclass
class _RawNote:
    measure: int
    position_quarters: float
    onset_quarters: float
    duration_quarters: float
    note: GPNote
    section: Optional[str]


def _is_bass_track(track: GPTrack) -> bool:
    name = (track.name + " " + track.program).lower()
    if any(k in name for k in _BASS_KEYWORDS):
        return True
    return track.midi_program is not None and 32 <= track.midi_program <= 39


def _melody_score(track: GPTrack) -> float:
    name = (track.name + " " + track.program).lower()
    score = 2.0 if any(k in name for k in _MELODY_KEYWORDS) else 0.0
    sounding_beats = [
        b
        for m in track.measures
        for b in m.beats
        if any(not n.muted for n in b.notes)
    ]
    if sounding_beats:
        mono = sum(
            1
            for b in sounding_beats
            if sum(1 for n in b.notes if not n.muted) == 1
        ) / len(sounding_beats)
        score += mono
    return score


def _detect_melody_track(song: GPSong) -> Optional[GPTrack]:
    candidates = [
        t
        for t in song.tracks
        if t.midi_program != 0 and not _is_bass_track(t)
    ]
    if not candidates:
        candidates = [t for t in song.tracks if t.midi_program != 0]
    if not candidates:
        return None
    return max(candidates, key=_melody_score)


def _select_track(song: GPSong, selector: str) -> GPTrack:
    if selector.strip().lower() == "auto":
        track = _detect_melody_track(song)
        if track is None:
            raise GuitarProError("没有可分析的旋律轨")
        return track
    return select_track(song, selector)


def _nominal_measure_quarters(measure) -> float:
    if measure.time_signature is None:
        return 4.0
    numerator, denominator = measure.time_signature
    return numerator * (4.0 / denominator)


def build_measure_spans(song: GPSong, track: GPTrack) -> dict[int, MeasureSpan]:
    spans: dict[int, MeasureSpan] = {}
    offset = 0.0
    for measure in track.measures:
        beat_end = max(
            (b.start_quarters + b.duration_quarters for b in measure.beats),
            default=0.0,
        )
        duration = max(beat_end, _nominal_measure_quarters(measure))
        bpm = song.tempo_at(measure.index - 1) or 120
        spans[measure.index] = MeasureSpan(
            index=measure.index,
            start_quarters=offset,
            duration_quarters=duration,
            bpm=bpm,
        )
        offset += duration
    return spans


def _quarter_span_seconds(
    spans: dict[int, MeasureSpan], start_quarters: float, end_quarters: float
) -> float:
    if end_quarters <= start_quarters:
        return 0.0
    total = 0.0
    for span in spans.values():
        lo = max(start_quarters, span.start_quarters)
        hi = min(end_quarters, span.end_quarters)
        if hi > lo:
            total += (hi - lo) * 60.0 / span.bpm
    return total


def _principal_note(notes: Iterable[GPNote]) -> Optional[GPNote]:
    sounding = [n for n in notes if not n.muted]
    if not sounding:
        return None
    if len(sounding) == 1:
        return sounding[0]
    # 多音拍默认取最高音作为旋律主音；吉他制音/和弦扫弦的顶部通常是旋律线。
    return max(sounding, key=lambda n: n.midi)


def _polyphonic_beat_count(track: GPTrack) -> int:
    return sum(
        1
        for measure in track.measures
        for beat in measure.beats
        if sum(1 for note in beat.notes if not note.muted) > 1
    )


def _section_by_measure(track: GPTrack) -> dict[int, Optional[str]]:
    result: dict[int, Optional[str]] = {}
    current: Optional[str] = None
    for measure in track.measures:
        if measure.section:
            current = measure.section
        result[measure.index] = current
    return result


def _collect_raw_notes(
    song: GPSong, track: GPTrack
) -> tuple[list[_RawNote], dict[int, MeasureSpan]]:
    spans = build_measure_spans(song, track)
    sections = _section_by_measure(track)
    raw: list[_RawNote] = []
    for measure in track.measures:
        span = spans[measure.index]
        section = sections[measure.index]
        for beat in measure.beats:
            note = _principal_note(beat.notes)
            if note is None:
                continue
            onset = span.start_quarters + beat.start_quarters
            raw.append(
                _RawNote(
                    measure=measure.index,
                    position_quarters=beat.start_quarters,
                    onset_quarters=onset,
                    duration_quarters=note.duration_quarters or beat.duration_quarters,
                    note=copy.copy(note),
                    section=section,
                )
            )
    raw.sort(key=lambda x: x.onset_quarters)
    return raw, spans


def _merge_ties(raw: list[_RawNote]) -> list[_RawNote]:
    merged: list[_RawNote] = []
    i = 0
    while i < len(raw):
        current = raw[i]
        end = current.onset_quarters + current.duration_quarters
        j = i + 1
        while (
            j < len(raw)
            and current.note.tie_origin
            and raw[j].note.tie_destination
            and raw[j].note.midi == current.note.midi
            and abs(raw[j].onset_quarters - end) < EPS
        ):
            end += raw[j].duration_quarters
            j += 1
        if j > i + 1:
            merged_note = copy.copy(current.note)
            merged_note.duration_quarters = end - current.onset_quarters
            merged_note.tie_origin = False
            merged_note.tie_destination = False
            current = _RawNote(
                measure=current.measure,
                position_quarters=current.position_quarters,
                onset_quarters=current.onset_quarters,
                duration_quarters=merged_note.duration_quarters,
                note=merged_note,
                section=current.section,
            )
        merged.append(current)
        i = j
    # 延音已经在上面合并成单个长音符；后续所有事件都不需要再输出 ABC tie。
    for item in merged:
        item.note.tie_origin = False
        item.note.tie_destination = False
    return merged


def _rhythm_name(duration_quarters: float) -> str:
    if duration_quarters <= 0:
        return "zero"
    for value, name in _RHYTHM_NAMES:
        if abs(duration_quarters - value) < 0.001:
            return name
    return f"{duration_quarters:g}q"


def _degree_info(midi: int, key_root: int, key_mode: str) -> tuple[str, str, Optional[int]]:
    """返回 (音级字符串, 唱名, 音级数值偏移)。

    音级数值偏移：主音=0、属音=4，供动机引擎做调内音级匹配；
    变化音（不在调内）返回 None，不参与音级匹配。
    """
    pc = midi % 12
    if key_mode == "Minor":
        pcs = _MINOR_PCS
        solfege = _MINOR_SOLFEGE
    else:
        pcs = _MAJOR_PCS
        solfege = _MAJOR_SOLFEGE

    target = (pc - key_root) % 12
    if target in pcs:
        degree_index = pcs.index(target)
        return str(degree_index + 1), solfege[degree_index], degree_index

    best = None
    for i, diatonic in enumerate(pcs):
        diff = (pc - (key_root + diatonic)) % 12
        if diff > 6:
            diff -= 12
        if best is None or abs(diff) < abs(best[1]):
            best = (i, diff)
    degree_index, alteration = best
    accidental = _ACCIDENTALS.get(alteration, "#" if alteration > 0 else "b")
    return f"{accidental}{degree_index + 1}", solfege[degree_index], None


def _interval_name(semitones: int) -> str:
    magnitude = abs(semitones)
    octaves, interval_class = divmod(magnitude, 12)
    base = _INTERVAL_NAMES[interval_class]
    if octaves == 0:
        return base
    return f"{base} + {octaves} octave{'s' if octaves > 1 else ''}"


def _is_melodic_connection(
    previous: MelodyEvent,
    current: MelodyEvent,
    phrase_gap: float,
) -> bool:
    """两个发声音符之间是否属于连续旋律连接。

    用乐句分界阈值排除跨乐句/长休止的伪音程；负 gap 表示谱面拍位重叠，
    仍按连续处理。
    """
    gap = current.onset_quarters - (
        previous.onset_quarters + previous.duration_quarters
    )
    return gap <= EPS or gap < phrase_gap


def _add_rests(
    audible: list[MelodyEvent],
    track_end_quarters: float,
    spans: dict[int, MeasureSpan],
) -> list[MelodyEvent]:
    events: list[MelodyEvent] = []
    cursor = 0.0
    for i, event in enumerate(audible):
        if event.onset_quarters > cursor + EPS:
            events.append(
                _make_rest_event(
                    cursor,
                    event.onset_quarters - cursor,
                    event.measure,
                    event.section,
                    spans,
                    len(events),
                )
            )
        events.append(event)
        cursor = event.onset_quarters + event.duration_quarters
    if track_end_quarters > cursor + EPS:
        last_measure = audible[-1].measure if audible else 1
        last_section = audible[-1].section if audible else None
        events.append(
            _make_rest_event(
                cursor,
                track_end_quarters - cursor,
                last_measure,
                last_section,
                spans,
                len(events),
            )
        )
    return events


def _make_rest_event(
    onset_quarters: float,
    duration_quarters: float,
    measure: int,
    section: Optional[str],
    spans: dict[int, MeasureSpan],
    index: int,
) -> MelodyEvent:
    # 用休止起点所在小节做时间位置与调性上下文。
    span = next(
        (
            s
            for s in spans.values()
            if s.start_quarters <= onset_quarters < s.end_quarters
        ),
        None,
    )
    measure = span.index if span is not None else measure
    position = onset_quarters - span.start_quarters if span is not None else 0.0
    return MelodyEvent(
        index=index,
        measure=measure,
        position_quarters=position,
        onset_quarters=onset_quarters,
        onset_seconds=_quarter_span_seconds(spans, 0.0, onset_quarters),
        duration_quarters=duration_quarters,
        duration_seconds=_quarter_span_seconds(
            spans, onset_quarters, onset_quarters + duration_quarters
        ),
        is_rest=True,
        midi=None,
        pitch="Rest",
        tie_origin=False,
        tie_destination=False,
        section=section,
        degree=None,
        degree_pc=None,
        interval_semitones=None,
        interval_name=None,
        direction=None,
        rhythm=_rhythm_name(duration_quarters),
    )


def build_events(
    song: GPSong,
    track: GPTrack,
    phrase_gap: float = 2.0,
    min_phrase_notes: int = 4,
) -> tuple[list[MelodyEvent], list[Phrase], dict[int, MeasureSpan], tuple[int, str]]:
    raw, spans = _collect_raw_notes(song, track)
    merged = _merge_ties(raw)
    global_key = resolve_key(song, track, None)

    audible: list[MelodyEvent] = []
    for index, item in enumerate(merged):
        note = item.note
        midi = note.midi
        key_root, key_mode = measure_key(
            next(m for m in track.measures if m.index == item.measure),
            global_key,
        )
        degree, solfege, degree_pc = _degree_info(midi, key_root, key_mode)
        onset_seconds = _quarter_span_seconds(spans, 0.0, item.onset_quarters)
        duration_seconds = _quarter_span_seconds(
            spans, item.onset_quarters, item.onset_quarters + item.duration_quarters
        )
        audible.append(
            MelodyEvent(
                index=index,
                measure=item.measure,
                position_quarters=item.position_quarters,
                onset_quarters=item.onset_quarters,
                onset_seconds=onset_seconds,
                duration_quarters=item.duration_quarters,
                duration_seconds=duration_seconds,
                is_rest=False,
                midi=midi,
                pitch=pc_name(midi, key_root),
                tie_origin=note.tie_origin,
                tie_destination=note.tie_destination,
                section=item.section,
                degree=degree,
                degree_pc=degree_pc,
                interval_semitones=None,
                interval_name=None,
                direction=None,
                rhythm=_rhythm_name(item.duration_quarters),
            )
        )

    for previous, event in zip(audible, audible[1:]):
        if previous.midi is None or event.midi is None:
            continue
        if not _is_melodic_connection(previous, event, phrase_gap):
            continue
        interval_semitones = event.midi - previous.midi
        event.interval_semitones = interval_semitones
        event.interval_name = _interval_name(interval_semitones)
        event.direction = (
            "up"
            if interval_semitones > 0
            else "down"
            if interval_semitones < 0
            else "same"
        )

    track_end = max(span.end_quarters for span in spans.values()) if spans else 0.0
    events = _add_rests(audible, track_end, spans)
    events = _reindex_events(events)
    phrases = _segment_phrases(audible, phrase_gap, min_phrase_notes)
    phrases = _attach_phrase_events(phrases, events)
    return events, phrases, spans, global_key


def _reindex_events(events: list[MelodyEvent]) -> list[MelodyEvent]:
    for index, event in enumerate(events, 1):
        event.index = index
    return events


def _attach_phrase_events(
    phrases: list[Phrase],
    events: list[MelodyEvent],
) -> list[Phrase]:
    """把乐句事件从纯发声音符替换为包含中间休止的连续事件切片。

    乐句边界仍然只由发声音符之间的间隔决定，但 ABC 与节奏动机需要看到
    乐句内部的休止，否则节奏片段不是完整的时值序列。
    """
    if not phrases or not events:
        return phrases
    event_indices = {id(event): index for index, event in enumerate(events)}
    for phrase in phrases:
        if not phrase.events:
            continue
        start = event_indices.get(id(phrase.events[0]))
        end = event_indices.get(id(phrase.events[-1]))
        if start is None or end is None or start > end:
            continue
        phrase.events = events[start : end + 1]
    return phrases


def _segment_phrases(
    audible: list[MelodyEvent],
    phrase_gap: float,
    min_phrase_notes: int = 4,
) -> list[Phrase]:
    if not audible:
        return []
    groups: list[list[MelodyEvent]] = [[audible[0]]]
    boundary_reasons: list[str] = ["旋律起点"]
    for previous, current in zip(audible, audible[1:]):
        gap = current.onset_quarters - (previous.onset_quarters + previous.duration_quarters)
        if current.section != previous.section:
            groups.append([current])
            boundary_reasons.append(f"段落变化：{current.section or '未命名段落'}")
        elif gap >= phrase_gap:
            groups.append([current])
            boundary_reasons.append(f"休止 {gap:g} 个四分音符")
        else:
            groups[-1].append(current)

    groups, boundary_reasons = _merge_short_groups(
        groups, boundary_reasons, min_phrase_notes
    )

    phrases: list[Phrase] = []
    for idx, group in enumerate(groups, 1):
        midis = [e.midi for e in group if e.midi is not None]
        min_midi = min(midis)
        max_midi = max(midis)
        phrases.append(
            Phrase(
                id=idx,
                start_measure=group[0].measure,
                end_measure=group[-1].measure,
                start_seconds=group[0].onset_seconds,
                end_seconds=group[-1].onset_seconds + group[-1].duration_seconds,
                note_count=len(group),
                pitch_min=_midi_to_name(min_midi),
                pitch_max=_midi_to_name(max_midi),
                section=group[0].section,
                boundary_reason=boundary_reasons[idx - 1],
                events=group,
            )
        )
    return phrases


def _merge_short_groups(
    groups: list[list[MelodyEvent]],
    boundary_reasons: list[str],
    min_notes: int,
) -> tuple[list[list[MelodyEvent]], list[str]]:
    """把零散单音/双音吸收进相邻乐句，避免出现"单音乐句"。

    合并只能在同一 section 内进行；跨 section 的短乐句保留为独立乐句，
    否则会把"为什么断开"的段落说明和实际起点段落弄错。
    """
    unmergeable: set[int] = set()
    while True:
        merge_index = next(
            (
                i
                for i, g in enumerate(groups)
                if len(g) < min_notes and i not in unmergeable
            ),
            None,
        )
        if merge_index is None:
            return groups, boundary_reasons

        group = groups[merge_index]
        candidates: list[tuple[int, int, int]] = []
        if merge_index > 0:
            prev_group = groups[merge_index - 1]
            gap = group[0].onset_quarters - (
                prev_group[-1].onset_quarters + prev_group[-1].duration_quarters
            )
            if prev_group[-1].section == group[0].section:
                candidates.append((merge_index - 1, gap, 0))
        if merge_index < len(groups) - 1:
            next_group = groups[merge_index + 1]
            gap = next_group[0].onset_quarters - (
                group[-1].onset_quarters + group[-1].duration_quarters
            )
            if group[-1].section == next_group[0].section:
                candidates.append((merge_index + 1, gap, 1))

        if not candidates:
            unmergeable.add(merge_index)
            continue

        # 距离相同时优先并入前一个乐句，保持自然从左到右的生长方向。
        target = min(candidates, key=lambda x: (x[1], x[2]))
        target_index = target[0]
        if target_index < merge_index:
            groups[target_index] = groups[target_index] + group
        else:
            groups[target_index] = group + groups[target_index]
            boundary_reasons[target_index] = boundary_reasons[merge_index]
        groups[target_index].sort(key=lambda e: e.onset_quarters)
        del groups[merge_index]
        del boundary_reasons[merge_index]
        unmergeable.clear()


def _midi_to_name(midi: int) -> str:
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return f"{names[midi % 12]}{midi // 12 - 1}"


def _key_name_text(key: tuple[int, str]) -> str:
    root, mode = key
    return f"{pc_name(root, root)}{'m' if mode == 'Minor' else ''}"


def _abc_accidental(symbol: str) -> str:
    return {
        "": "",
        "#": "^",
        "b": "_",
        "##": "^^",
        "bb": "__",
    }.get(symbol, symbol)


def _abc_pitch(midi: int, key_root: int) -> str:
    spelling = pc_name(midi % 12, key_root)
    letter = spelling[0]
    accidental = _abc_accidental(spelling[1:])
    octave_shift = midi // 12 - 5
    if octave_shift >= 1:
        body = letter.lower() + "'" * (octave_shift - 1)
    else:
        body = letter.upper() + "," * (-octave_shift)
    return accidental + body


def _abc_duration(duration_quarters: float) -> str:
    """按 L:1/8 输出显式 ABC 时值后缀。

    ABC 的时值写在音名/休止符之后，而不是之前。这里总是写出乘数，
    例如八分音符是 ``G1``、四分音符是 ``G2``、十六分音符是 ``G/2``。
    """
    if duration_quarters <= 0:
        return "1"
    value = Fraction(duration_quarters / 0.5).limit_denominator(8)
    if value.denominator == 1:
        return str(value.numerator)
    if value.numerator == 1:
        return f"/{value.denominator}"
    return f"{value.numerator}/{value.denominator}"


def _abc_header(
    title: str,
    key: tuple[int, str],
    time_sig: tuple[int, int],
    bpm: int,
) -> list[str]:
    return [
        "X:1",
        f"T:{title}",
        f"M:{time_sig[0]}/{time_sig[1]}",
        "L:1/8",
        f"Q:1/4={bpm}",
        f"K:{_key_name_text(key)}",
    ]


def _abc_event_token(event: MelodyEvent, key_root: int) -> str:
    if event.is_rest:
        return "z" + _abc_duration(event.duration_quarters)
    token = _abc_pitch(
        event.midi or 60, key_root
    ) + _abc_duration(event.duration_quarters)
    if event.tie_origin:
        token += "-"
    return token


def _phrase_abc(
    phrase: Phrase,
    track: GPTrack,
    global_key: tuple[int, str],
    spans: dict[int, MeasureSpan],
) -> str:
    measure = next(
        (m for m in track.measures if m.index == phrase.start_measure),
        None,
    )
    key = measure_key(measure, global_key) if measure else global_key
    time_sig = measure.time_signature if measure and measure.time_signature else (4, 4)
    bpm = spans[phrase.start_measure].bpm if phrase.start_measure in spans else 120
    tokens: list[str] = []
    span = spans.get(phrase.start_measure)
    cursor = span.start_quarters if span is not None else 0.0
    for event in phrase.events:
        if event.onset_quarters > cursor + EPS:
            gap = event.onset_quarters - cursor
            tokens.append("z" + _abc_duration(gap))
        tokens.append(_abc_event_token(event, key[0]))
        cursor = event.onset_quarters + event.duration_quarters
    body_lines: list[str] = []
    for start in range(0, len(tokens), 16):
        body_lines.append(" ".join(tokens[start : start + 16]))
    return "\n".join(
        _abc_header(f"乐句 {phrase.id}", key, time_sig, bpm) + body_lines
    )


def _motif_abc(
    motif: Motif,
    note_events: list[MelodyEvent],
    track: GPTrack,
    global_key: tuple[int, str],
    spans: dict[int, MeasureSpan],
    title: str = "",
) -> str:
    rep = motif.representative
    events = note_events[rep.note_start : rep.note_end + 1]
    if not events:
        return ""
    first = events[0]
    measure = next((m for m in track.measures if m.index == first.measure), None)
    key = measure_key(measure, global_key) if measure else global_key
    time_sig = measure.time_signature if measure and measure.time_signature else (4, 4)
    bpm = spans[first.measure].bpm if first.measure in spans else 120
    tokens = [_abc_event_token(e, key[0]) for e in events]
    abc_title = title or f"动机 {motif.motif_id}"
    return "\n".join(
        _abc_header(abc_title, key, time_sig, bpm) + [" ".join(tokens)]
    )


# ---------------------------------------------------------------------------
# 动机引擎接入
# ---------------------------------------------------------------------------


def _collect_melody_notes(
    events: list[MelodyEvent],
    phrases: list[Phrase],
) -> tuple[list[MelodyNote], list[MelodyEvent]]:
    """把事件流里的发声音符转成动机引擎输入，并保留事件映射用于 ABC。"""
    notes: list[MelodyNote] = []
    note_events: list[MelodyEvent] = []
    for event in events:
        if event.is_rest or event.midi is None:
            continue
        notes.append(
            MelodyNote(
                index=len(notes),
                midi=event.midi,
                onset_quarters=event.onset_quarters,
                duration_quarters=event.duration_quarters,
                measure=event.measure,
                position_quarters=event.position_quarters,
                section=event.section,
                degree_pc=event.degree_pc,
            )
        )
        note_events.append(event)

    phrase_of_event: dict[int, int] = {}
    for phrase in phrases:
        for event in phrase.events:
            phrase_of_event[event.index] = phrase.id
    for note in notes:
        event = note_events[note.index]
        note.phrase_id = phrase_of_event.get(event.index)
    return notes, note_events


def analyze_track(
    song: GPSong,
    track: GPTrack,
    phrase_gap: float = 2.0,
    min_phrase_notes: int = 4,
    min_motif: int = 3,
    max_motif: int = 24,
    min_occurrences: int = 2,
    motif_gap: float = 1.0,
    max_results: int = 40,
) -> dict:
    events, phrases, spans, global_key = build_events(
        song, track, phrase_gap, min_phrase_notes
    )
    audible = [e for e in events if not e.is_rest]

    notes, note_events = _collect_melody_notes(events, phrases)
    motifs = find_motifs(
        notes,
        min_notes=min_motif,
        max_notes=max_motif,
        min_occurrences=min_occurrences,
        segment_gap=motif_gap,
        max_results=max_results,
    )
    for motif in motifs:
        motif.abc = _motif_abc(
            motif,
            note_events,
            track,
            global_key,
            spans,
            f"动机 {motif.motif_id}",
        )
    phrase_motif_refs = _build_phrase_motif_refs(phrases, motifs)

    key_events = _key_events(track, global_key)
    stats = _build_stats(audible, phrases, motifs)
    return {
        "meta": {
            "file": "",
            "title": song.title,
            "subtitle": song.subtitle,
            "artist": song.artist,
            "album": song.album,
            "gp_version": song.gp_version,
        },
        "track": {
            "id": track.id,
            "name": track.name,
            "short_name": track.short_name,
            "program": track.program,
            "midi_program": track.midi_program,
            "measure_count": len(track.measures),
            "source_note_count": len(track.notes),
            "selected_note_count": len(audible),
            "polyphonic_beat_count": _polyphonic_beat_count(track),
            "melody_voice": "每拍最高发声音符",
        },
        "key_events": key_events,
        "phrases": [
            _phrase_to_dict(
                p,
                _phrase_abc(p, track, global_key, spans),
                phrase_motif_refs.get(p.id, []),
            )
            for p in phrases
        ],
        "motifs": [_motif_to_dict(m, note_events) for m in motifs],
        "motif_limit": max_results,
        "stats": stats,
    }


def _key_events(track: GPTrack, global_key: tuple[int, str]) -> list[dict]:
    result: list[dict] = []
    previous: Optional[tuple[int, str]] = None
    for measure in track.measures:
        key = measure_key(measure, global_key)
        if key == previous:
            continue
        previous = key
        root, mode = key
        result.append(
            {
                "measure": measure.index,
                "key": f"{pc_name(root, root)}{'m' if mode == 'Minor' else ''}",
                "root_pc": root,
                "mode": mode,
            }
        )
    return result


def _build_stats(
    audible: list[MelodyEvent],
    phrases: list[Phrase],
    motifs: list[Motif],
) -> dict:
    if not audible:
        return {"total_notes": 0}
    midis = [e.midi for e in audible if e.midi is not None]
    intervals = [
        e.interval_semitones
        for e in audible
        if e.interval_semitones is not None
    ]
    degree_counter = Counter(e.degree for e in audible if e.degree)
    rhythm_counter = Counter(e.rhythm for e in audible if e.rhythm)
    interval_counter = Counter(intervals)
    direction_counter = Counter(e.direction for e in audible if e.direction)
    return {
        "total_notes": len(audible),
        "pitch_min": _midi_to_name(min(midis)),
        "pitch_max": _midi_to_name(max(midis)),
        "pitch_range_semitones": max(midis) - min(midis),
        "phrase_count": len(phrases),
        "melodic_motif_count": sum(1 for m in motifs if m.kind == "melodic"),
        "rhythmic_motif_count": sum(1 for m in motifs if m.kind == "rhythmic"),
        "melodic_interval_count": len(intervals),
        "degree_distribution": dict(sorted(degree_counter.items())),
        "interval_distribution": {
            f"{semitones:+d}": count
            for semitones, count in sorted(interval_counter.items())
        },
        "direction_distribution": dict(direction_counter),
        "rhythm_distribution": dict(sorted(rhythm_counter.items())),
    }


def _build_phrase_motif_refs(
    phrases: list[Phrase], motifs: list[Motif]
) -> dict[int, list[str]]:
    refs: dict[int, list[str]] = {phrase.id: [] for phrase in phrases}
    for motif in motifs:
        for occurrence in motif.occurrences:
            phrase_id = occurrence.phrase_id
            if phrase_id is not None and motif.motif_id not in refs[phrase_id]:
                refs[phrase_id].append(motif.motif_id)
    for phrase_id in refs:
        refs[phrase_id].sort(key=lambda value: int(value[1:]))
    return refs


def _compact_refs(refs: list[str], limit: int = 8) -> str:
    """把乐句的动机引用压缩成可读字符串，过长时显示数量。"""
    if not refs:
        return "—"
    if len(refs) <= limit:
        return "、".join(refs)
    return "、".join(refs[:limit]) + f" 等 {len(refs)} 个"


def _phrase_to_dict(
    p: Phrase,
    abc_text: str = "",
    motif_refs: Optional[list[str]] = None,
) -> dict:
    return {
        "id": p.id,
        "section": p.section,
        "start_measure": p.start_measure,
        "end_measure": p.end_measure,
        "start_seconds": round(p.start_seconds, 6),
        "end_seconds": round(p.end_seconds, 6),
        "note_count": p.note_count,
        "pitch_min": p.pitch_min,
        "pitch_max": p.pitch_max,
        "boundary_reason": p.boundary_reason,
        "motif_refs": motif_refs or [],
        "abc": abc_text,
    }


def _occurrence_to_dict(occurrence) -> dict:
    return {
        "measure": occurrence.measure,
        "position": f"m{occurrence.measure} {occurrence.onset_quarters:g}q",
        "onset_quarters": round(occurrence.onset_quarters, 6),
        "end_quarters": round(occurrence.end_quarters, 6),
        "phrase_id": occurrence.phrase_id,
        "section": occurrence.section,
        "transposition": occurrence.transposition,
        "variant": occurrence.variant,
        "rhythm_variant": occurrence.rhythm_variant,
        "function": occurrence.function,
        "position_in_phrase": occurrence.position_in_phrase,
    }


def _motif_content(m: Motif, note_events: list[MelodyEvent]) -> str:
    rep = m.representative
    events = note_events[rep.note_start : rep.note_end + 1]
    rhythm_counter = Counter(e.rhythm for e in events if e.rhythm)
    rhythm_text = " · ".join(
        f"{name}×{count}"
        for name, count in sorted(rhythm_counter.items(), key=lambda kv: -kv[1])
    )
    if m.kind == "rhythmic":
        return rhythm_text
    names = "→".join(e.pitch or _midi_to_name(e.midi or 0) for e in events)
    return f"{names}（{rhythm_text}）"


def _motif_to_dict(m: Motif, note_events: list[MelodyEvent]) -> dict:
    return {
        "id": m.motif_id,
        "kind": m.kind,
        "rep": m.rep,
        "note_count": m.length_notes,
        "length_quarters": round(m.length_quarters, 6),
        "score": round(m.score, 2),
        "content": _motif_content(m, note_events),
        "transpositions": [o.transposition for o in m.occurrences],
        "variants": sorted({o.variant for o in m.occurrences}),
        "occurrences": [_occurrence_to_dict(o) for o in m.occurrences],
        "abc": m.abc,
    }


def _has_variants(motif: dict) -> bool:
    if any(t != 0 for t in motif["transpositions"]):
        return True
    if any(o["rhythm_variant"] for o in motif["occurrences"]):
        return True
    return len(motif["variants"]) > 1


# ---------------------------------------------------------------------------
# 控制台摘要
# ---------------------------------------------------------------------------


def _print_summary(result: dict, track_selector: str, top: int = 10) -> None:
    meta = result["meta"]
    track = result["track"]
    stats = result["stats"]
    print(
        f"{meta['title'] or '(无标题)'} - {meta['artist'] or '(无艺术家)'} "
        f"[GP {meta['gp_version'] or '?'}]"
    )
    print(
        f"分析轨道: {track['name']} ({track_selector})  小节 {track['measure_count']}  "
        f"源音符 {track['source_note_count']}  旋律事件 {track['selected_note_count']}"
    )
    if not result["key_events"]:
        print("调性: 未检测到")
    else:
        print(
            "调性变化: "
            + "  ".join(
                f"m{k['measure']}:{k['key']}" for k in result["key_events"]
            )
        )

    if stats.get("total_notes"):
        print(
            f"音域: {stats['pitch_min']} - {stats['pitch_max']} "
            f"({stats['pitch_range_semitones']} 半音)  乐句 {stats['phrase_count']}"
        )
    print()

    if result["phrases"]:
        print(f"{'乐句':>3} {'小节':>12} {'音符':>4} {'音域':>18}  {'动机':>12}  边界")
        print("-" * 82)
        for p in result["phrases"]:
            measure_text = (
                str(p["start_measure"])
                if p["start_measure"] == p["end_measure"]
                else f"{p['start_measure']}-{p['end_measure']}"
            )
            refs = p.get("motif_refs") or []
            motif_text = _compact_refs(refs, limit=6)
            print(
                f"{p['id']:>3} {measure_text:>12} {p['note_count']:>4} "
                f"{p['pitch_min'] + '-' + p['pitch_max']:>18}  "
                f"{motif_text:>12}  {p['boundary_reason']}"
            )
        print()

    _print_motifs("旋律动机", result["motifs"], limit=top)


def _print_motifs(title: str, motifs: list[dict], limit: int) -> None:
    if not motifs:
        return
    print(f"{title}（按显著性排序，前 {min(limit, len(motifs))}）")
    print("-" * 76)
    for motif in motifs[:limit]:
        trans = ",".join(str(t) for t in motif["transpositions"])
        positions = ", ".join(
            f"P{o.get('phrase_id', '?')} "
            f"m{o['measure']}:{o['onset_quarters']:g}q"
            f"[{o['function']}]"
            for o in motif["occurrences"][:6]
        )
        if len(motif["occurrences"]) > 6:
            positions += " ..."
        print(
            f"{motif['id']} [{motif['kind']}·{motif['note_count']}音, "
            f"{len(motif['occurrences'])}次, 移调{trans}] "
            f"{motif['content']}  ->  {positions}"
        )
    print()


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _bar(value: int, max_value: int, width: int = 14) -> str:
    if max_value <= 0:
        return ""
    count = max(1, round(value / max_value * width))
    return "█" * count


def _render_markdown(result: dict, track_selector: str) -> str:
    meta = result["meta"]
    track = result["track"]
    stats = result["stats"]
    lines: list[str] = []
    lines.append(f"# 旋律拆解：{track['name']}")
    lines.append("")
    lines.append(
        f"- 歌曲：{meta['title'] or '(无标题)'} / {meta['artist'] or '(无艺术家)'}"
    )
    lines.append(
        f"- 轨道：{track['name']}（{track_selector}，{track['program'] or '未知音色'}）"
    )
    lines.append(f"- GP 版本：{meta['gp_version'] or '未知'}")
    lines.append(
        f"- 规模：{track['measure_count']} 小节，旋律音符 {track['selected_note_count']} 个"
    )
    if track.get("polyphonic_beat_count"):
        lines.append(
            "- 旋律提取说明：多音拍取最高 MIDI 发声音符；"
            f"本轨 {track['polyphonic_beat_count']} 个多音拍被降维，"
            f"{track['selected_note_count']} 个旋律音符来自 {track['source_note_count']} 个源音符，"
            "不是对复音谱面的逐音单音还原。"
        )
    if stats.get("total_notes"):
        lines.append(
            f"- 音域：{stats['pitch_min']} ~ {stats['pitch_max']} "
            f"（{stats['pitch_range_semitones']} 个半音），乐句 {stats['phrase_count']} 个"
        )
        lines.append(
            f"- 旋律音程样本：{stats.get('melodic_interval_count', 0)} 个"
            "（跨乐句或长休止连接不计入）"
        )
        lines.append(
            f"- 动机：旋律型 {stats.get('melodic_motif_count', 0)} 个，"
            f"节奏型 {stats.get('rhythmic_motif_count', 0)} 个"
            "（在全曲范围内搜索：逐字反复 / 移调序列 / 八度变体 / "
            "调内音级 / 轮廓 / 纯节奏型）"
        )
    lines.append("")

    if result["key_events"]:
        lines.append("## 调性")
        lines.append("")
        lines.append(
            "、".join(
                f"第 {k['measure']} 小节起为 {k['key']}（{k['mode']}）"
                for k in result["key_events"]
            )
        )
        lines.append("")

    if result["phrases"]:
        lines.append("## 乐句")
        lines.append("")
        lines.append("| 乐句 | 小节 | 段落 | 音符 | 音域 | 使用动机 | 为什么断开 |")
        lines.append("|---:|---:|---|---:|---:|---|---|")
        for p in result["phrases"]:
            measure = (
                str(p["start_measure"])
                if p["start_measure"] == p["end_measure"]
                else f"{p['start_measure']}–{p['end_measure']}"
            )
            refs = p.get("motif_refs") or []
            motif_text = _compact_refs(refs)
            lines.append(
                f"| {p['id']} | {measure} | {p['section'] or '—'} | "
                f"{p['note_count']} | {p['pitch_min']}–{p['pitch_max']} | "
                f"{motif_text} | {p['boundary_reason']} |"
            )
        lines.append("")

    motifs = result["motifs"]
    _append_motif_table(lines, motifs)
    _append_variant_table(lines, motifs)
    _append_abc_snippets(
        lines,
        "动机 ABC",
        motifs,
        limit=12,
        title_key="动机",
        fence="abc",
    )

    _append_distribution(
        lines,
        "音级分布",
        stats.get("degree_distribution", {}),
        "音级",
    )
    _append_distribution(
        lines,
        "节奏分布",
        stats.get("rhythm_distribution", {}),
        "节奏型",
    )
    _append_interval_distribution(lines, stats.get("interval_distribution", {}))
    lines.append("")
    return "\n".join(lines)


def _append_motif_table(lines: list[str], motifs: list[dict]) -> None:
    if not motifs:
        return
    lines.append("## 动机")
    lines.append("")
    lines.append(
        "| ID | 类别 | 长度 | 出现 | 移调 | 内容 | 使用位置 |"
    )
    lines.append("|---|---:|---:|---:|---|---|---|")
    for motif in motifs:
        kind = "旋律" if motif["kind"] == "melodic" else "节奏"
        trans = "、".join(
            f"{t:+d}" if t else "0" for t in motif["transpositions"]
        )
        positions = "；".join(
            f"乐句 {o.get('phrase_id', '?')} · {o['position']} · "
            f"{o.get('section') or '—'} · {o['function']}"
            for o in motif["occurrences"][:6]
        )
        if len(motif["occurrences"]) > 6:
            positions += " …"
        lines.append(
            f"| {motif['id']} | {kind} | {motif['note_count']} | "
            f"{len(motif['occurrences'])} | {trans} | "
            f"{motif['content']} | {positions} |"
        )
    lines.append("")


def _append_variant_table(lines: list[str], motifs: list[dict]) -> None:
    with_variants = [m for m in motifs if _has_variants(m)]
    if not with_variants:
        return
    lines.append("## 动机变体")
    lines.append("")
    lines.append(
        "同一动机家族内部的移调/八度/音级/节奏差异（相对代表出现）。"
    )
    lines.append("")
    for motif in with_variants:
        lines.append(f"### {motif['id']} {motif['content']}")
        lines.append("")
        lines.append("| 出现 | 小节 | 乐句 | 段落 | 功能 | 移调 | 变体 |")
        lines.append("|---|---:|---:|---|---|---:|---|")
        for index, occurrence in enumerate(motif["occurrences"], 1):
            variant_text = _REP_LABELS.get(occurrence["variant"], occurrence["variant"])
            if occurrence["rhythm_variant"]:
                variant_text += " · 节奏变体"
            trans = f"{occurrence['transposition']:+d}" if occurrence["transposition"] else "0"
            lines.append(
                f"| {index} | m{occurrence['measure']} | "
                f"{occurrence.get('phrase_id') or '—'} | "
                f"{occurrence.get('section') or '—'} | "
                f"{occurrence['function']} | {trans} | {variant_text} |"
            )
        lines.append("")


def _append_abc_snippets(
    lines: list[str],
    title: str,
    items: list[dict],
    limit: int,
    title_key: str,
    fence: str = "abc",
) -> None:
    if not items:
        return
    lines.append(f"## {title}")
    lines.append("")
    for index, item in enumerate(items[:limit] if limit else items, 1):
        abc_text = item.get("abc") or ""
        if not abc_text:
            continue
        label = item.get("id", index)
        lines.append(f"### {title_key} {label}")
        lines.append("")
        lines.append(f"```{fence}")
        lines.append(abc_text.rstrip())
        lines.append("```")
        lines.append("")


def _append_distribution(
    lines: list[str],
    title: str,
    distribution: dict,
    label: str,
) -> None:
    if not distribution:
        return
    items = sorted(distribution.items(), key=lambda kv: -kv[1])
    max_count = max((v for _, v in items), default=0)
    lines.append(f"## {title}")
    lines.append("")
    lines.append(f"| {label} | 次数 | 占比条 |")
    lines.append("|---|---|---|")
    for key, count in items:
        lines.append(f"| {key} | {count} | {_bar(count, max_count)} |")
    lines.append("")


def _append_interval_distribution(lines: list[str], distribution: dict) -> None:
    if not distribution:
        return
    items = sorted(
        distribution.items(),
        key=lambda kv: (-kv[1], kv[0]),
    )
    max_count = max((v for _, v in items), default=0)
    lines.append("## 音程分布")
    lines.append("")
    lines.append("| 音程 | 次数 | 占比条 |")
    lines.append("|---|---|---|")
    for key, count in items:
        semitones = int(key)
        readable = f"{_interval_name(semitones)}（{key} 半音）"
        lines.append(f"| {readable} | {count} | {_bar(count, max_count)} |")
    lines.append("")


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="分析并拆解 Guitar Pro 旋律轨（使用 gpreader）"
    )
    parser.add_argument("file", help=".gp / .gpx 文件路径")
    parser.add_argument(
        "--track",
        default="auto",
        help="轨道名称、索引或 auto（默认自动寻找旋律轨）",
    )
    parser.add_argument(
        "--report",
        help="输出带 ABC 代码块的 Markdown 报告路径",
    )
    parser.add_argument(
        "--phrase-gap",
        type=float,
        default=2.0,
        help="超过该四分音符长度的休止视为乐句边界（默认 2.0）",
    )
    parser.add_argument(
        "--min-phrase-notes",
        type=int,
        default=4,
        help="乐句至少包含多少个音，短片段会并入相邻乐句（默认 4）",
    )
    parser.add_argument(
        "--min-motif-notes",
        type=int,
        default=3,
        help="最短动机长度，按音符数（默认 3）",
    )
    parser.add_argument(
        "--max-motif-notes",
        type=int,
        default=24,
        help="最长动机长度，按音符数（默认 24）",
    )
    parser.add_argument(
        "--min-occurrences",
        type=int,
        default=2,
        help="动机至少非重叠出现多少次（默认 2）",
    )
    parser.add_argument(
        "--motif-gap",
        type=float,
        default=1.0,
        help="超过该四分音符长度的休止视为动机匹配的旋律段边界（默认 1.0）",
    )
    parser.add_argument(
        "--max-motifs",
        type=int,
        default=40,
        help="报告中最多保留的动机数量（默认 40，0 表示全部）",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="摘要中显示的最多动机数量（默认 10）",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="只写 Markdown 报告，不打印摘要",
    )
    args = parser.parse_args(argv)

    try:
        song = parse_gp(args.file)
        track = _select_track(song, args.track)
        result = analyze_track(
            song,
            track,
            phrase_gap=args.phrase_gap,
            min_phrase_notes=args.min_phrase_notes,
            min_motif=args.min_motif_notes,
            max_motif=args.max_motif_notes,
            min_occurrences=args.min_occurrences,
            motif_gap=args.motif_gap,
            max_results=args.max_motifs,
        )
        result["meta"]["file"] = str(Path(args.file))
    except GuitarProError as e:
        print(f"解析失败: {e}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(f"处理失败: {e}", file=sys.stderr)
        return 1

    if args.report:
        report_path = Path(args.report)
        report_path.write_text(
            _render_markdown(result, args.track),
            encoding="utf-8",
        )
        if not args.quiet:
            print(f"Markdown 报告已写入: {report_path}")
    if not args.quiet:
        _print_summary(result, args.track, top=args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
