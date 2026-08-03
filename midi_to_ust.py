#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MIDI 旋律 -> UST（调内唱名）

读取 MIDI，把旋律音符转换为以首调唱名（movable-do solfège）为歌词的 UST 文件。
音符音高保持不变，歌词反映该音在给定调内的唱名（do/re/mi... 或 多/来/咪...），
调外变化音使用标准变化唱名（di/ri/fi/si/li 或 ra/me/se/le/te）。

示例：
    uv run python midi_to_ust.py song.mid -o song.ust --key C
    uv run python midi_to_ust.py song.mid -o song.ust --key Am --lyrics zh
    uv run python midi_to_ust.py song.mid -o song.ust --key G --overlap cut
    uv run python midi_to_ust.py --demo
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

import mido
from mido import MidiFile, MidiTrack

import utaupy.ust

UST_PPQ = 480  # UST 每四分音符的 tick 数（UTAU 标准分辨率）
DEFAULT_BPM = 120.0
DEFAULT_US_PER_BEAT = 500_000  # 120 BPM

# 大调音阶相对主音的音高
MAJOR_SCALE = (0, 2, 4, 5, 7, 9, 11)

EN_SYLLABLES = {0: "do", 2: "re", 4: "mi", 5: "fa", 7: "sol", 9: "la", 11: "si"}
ZH_SYLLABLES = {0: "多", 2: "来", 4: "咪", 5: "发", 7: "索", 9: "拉", 11: "西"}
JA_SYLLABLES = {0: "ド", 2: "レ", 4: "ミ", 5: "ファ", 7: "ソ", 9: "ラ", 11: "シ"}

# 变化音唱名（升号拼写 / 降号拼写），键为相对主音的半音数
EN_CHROMATIC = {
    "sharp": {1: "di", 3: "ri", 6: "fi", 8: "si", 10: "li"},
    "flat": {1: "ra", 3: "me", 6: "se", 8: "le", 10: "te"},
}
ZH_CHROMATIC = {
    "sharp": {1: "升多", 3: "升来", 6: "升发", 8: "升索", 10: "升拉"},
    "flat": {1: "降来", 3: "降咪", 6: "降索", 8: "降拉", 10: "降西"},
}
# 日文假名变化音：按日本习惯借相邻音级唱——升号借下方音级、降号借上方音级
# （如 C 大调中 F# 唱 ファ、Bb 唱 ラ），变化音与相邻自然音可能同字，音高以 NoteNum 为准
JA_CHROMATIC = {
    "sharp": {1: "ド", 3: "レ", 6: "ファ", 8: "ソ", 10: "ラ"},
    "flat": {1: "レ", 3: "ミ", 6: "ソ", 8: "ラ", 10: "シ"},
}

LYRIC_TABLES = {
    "en": (EN_SYLLABLES, EN_CHROMATIC),
    "zh": (ZH_SYLLABLES, ZH_CHROMATIC),
    "ja": (JA_SYLLABLES, JA_CHROMATIC),
}

# Krumhansl-Kessler 调性轮廓（音高等级权重，用于调性估计）
KK_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KK_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

_NOTE_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_ACCIDENTAL = {"#": 1, "♯": 1, "b": -1, "♭": -1}
_PC_SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


@dataclass
class NoteEvent:
    """从 MIDI 提取出的音符事件（绝对 tick 表示）。"""

    note: int
    start_tick: int
    end_tick: int
    velocity: int
    channel: int


def parse_key(key: str, mode: Optional[str] = None) -> tuple[int, str]:
    """解析调名，返回 (主音音高, 大小调)。

    支持 C、D#、Fb、Am、Bb minor、C#m、F major 等写法；
    小调采用首调 la 唱法（关系大调唱名，即简谱小调记法）。
    """
    key = key.strip().replace(" ", "")
    m = re.fullmatch(
        r"([A-Ga-g])([#♯b♭]?)(m(?:in(?:or)?)?|M(?:aj(?:or)?)?|maj|major|minor)?",
        key,
    )
    if not m:
        raise ValueError(
            f"无法解析调名 '{key}'，示例：C、D#、Fb、Am、Bb minor、C#m、F major"
        )
    root_pc = (_NOTE_PC[m.group(1).upper()] + _ACCIDENTAL.get(m.group(2), 0)) % 12
    suffix = m.group(3) or ""
    if suffix in ("M", "Maj", "Major", "maj", "major"):
        parsed_mode = "major"
    elif suffix in ("m", "Min", "Minor", "min", "minor"):
        parsed_mode = "minor"
    else:
        parsed_mode = None

    if mode and parsed_mode and mode != parsed_mode:
        raise ValueError(f"--key 后缀与 --mode {mode} 冲突")
    final_mode = mode or parsed_mode or "major"
    return root_pc, final_mode


def build_tempo_changes(mid: MidiFile) -> list[tuple[int, int]]:
    """收集所有 set_tempo 事件（绝对 tick, 微秒/四分音符），按 tick 排序。"""
    changes: list[tuple[int, int]] = []
    for track in mid.tracks:
        t = 0
        for msg in track:
            t += msg.time
            if msg.type == "set_tempo":
                changes.append((t, msg.tempo))
    changes.sort(key=lambda x: x[0])
    return changes


def tick_to_seconds(
    tick: int, ticks_per_beat: int, changes: Sequence[tuple[int, int]]
) -> float:
    """把 MIDI tick 换算为绝对秒数（考虑变速）。"""
    if tick <= 0:
        return 0.0
    tempo = changes[0][1] if changes and changes[0][0] <= 0 else DEFAULT_US_PER_BEAT
    secs = 0.0
    prev_tick = 0
    for change_tick, change_tempo in changes:
        if change_tick <= prev_tick:
            continue
        if tick <= change_tick:
            break
        secs += (change_tick - prev_tick) * tempo / 1_000_000 / ticks_per_beat
        prev_tick, tempo = change_tick, change_tempo
    secs += (tick - prev_tick) * tempo / 1_000_000 / ticks_per_beat
    return secs


def extract_notes(track: MidiTrack, allow_drums: bool = False) -> list[NoteEvent]:
    """把一条 MIDI 轨道解析为音符事件列表。"""
    pending: dict[tuple[int, int], tuple[int, int]] = {}  # (channel,note)->(start,vel)
    notes: list[NoteEvent] = []
    t = 0
    for msg in track:
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            if not allow_drums and msg.channel == 9:
                continue
            key = (msg.channel, msg.note)
            if key in pending:  # 同一音高再次触发：先闭合上一个
                st, vel = pending.pop(key)
                notes.append(NoteEvent(msg.note, st, t, vel, msg.channel))
            pending[key] = (t, msg.velocity)
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            key = (msg.channel, msg.note)
            if key in pending:
                st, vel = pending.pop(key)
                notes.append(NoteEvent(msg.note, st, t, vel, msg.channel))
    # 轨道结束时仍未闭合的音符，视为持续到轨道末尾
    for (channel, note), (st, vel) in pending.items():
        notes.append(NoteEvent(note, st, t, vel, channel))
    return notes


def collect_notes(
    mid: MidiFile,
    track_idx: Optional[int] = None,
    channel: Optional[int] = None,
    merge: bool = False,
) -> list[NoteEvent]:
    """按用户选择策略收集音符。

    --track N   只取指定轨道（含打击乐通道）
    --channel N 只取指定通道
    --merge     合并所有非打击乐通道
    默认        取音符最多的非打击乐轨道（通常是旋律轨）
    """
    candidates: list[list[NoteEvent]] = []
    for i, track in enumerate(mid.tracks):
        if track_idx is not None and i != track_idx:
            continue
        allow_drums = track_idx is not None or channel == 9
        notes = extract_notes(track, allow_drums=allow_drums)
        if channel is not None:
            notes = [n for n in notes if n.channel == channel]
        candidates.append(notes)

    if track_idx is not None:
        return [n for ns in candidates for n in ns]
    if merge:
        return [n for ns in candidates for n in ns]
    if channel is not None:
        return [n for ns in candidates for n in ns]
    best = max(candidates, key=lambda ns: len(ns))
    return best


def resolve_overlaps(
    notes: Sequence[NoteEvent], policy: str
) -> tuple[list[NoteEvent], int]:
    """处理同时发声的音符（和弦/复调）。

    keep  保留原样（警告）
    top   每个同时发声组取最高音，并整理为单音旋律线（默认）
    cut   后一个音符被截短到前一个结束后
    drop  丢弃与前一个重叠的音符，保留先发声者
    返回 (处理后的音符, 被丢弃/截短数量)。
    """
    events = sorted(notes, key=lambda n: (n.start_tick, -n.end_tick))
    if policy == "keep":
        return events, 0
    if policy == "top":
        return _resolve_top_line(events)
    out: list[NoteEvent] = []
    dropped = 0
    cursor = -1
    for n in events:
        if n.start_tick >= cursor:
            out.append(n)
            cursor = n.end_tick
        elif policy == "cut" and n.end_tick > cursor:
            trimmed = NoteEvent(n.note, cursor, n.end_tick, n.velocity, n.channel)
            out.append(trimmed)
            cursor = trimmed.end_tick
            dropped += 1
        else:
            dropped += 1
    return out, dropped


def _resolve_top_line(events: Sequence[NoteEvent]) -> tuple[list[NoteEvent], int]:
    """提取最高音旋律线：按起始 tick 分组取最高音，再整理为单音序列。"""
    by_onset: dict[int, NoteEvent] = {}
    for n in events:
        cur = by_onset.get(n.start_tick)
        if cur is None or (n.note, n.end_tick) > (cur.note, cur.end_tick):
            by_onset[n.start_tick] = n
    tops = sorted(by_onset.values(), key=lambda n: (n.start_tick, -n.note))
    out: list[NoteEvent] = []
    dropped = len(events) - len(tops)
    cursor = -1
    for n in tops:
        if n.start_tick >= cursor:
            out.append(n)
            cursor = n.end_tick
        elif n.end_tick > cursor:
            out.append(NoteEvent(n.note, cursor, n.end_tick, n.velocity, n.channel))
            cursor = n.end_tick
        else:
            dropped += 1
    return out, dropped


def count_overlaps(notes: Sequence[NoteEvent]) -> int:
    """统计与更早音符有重叠的音符数量。"""
    events = sorted(notes, key=lambda n: n.start_tick)
    overlaps = 0
    last_end = -1
    for n in events:
        if n.start_tick < last_end:
            overlaps += 1
        last_end = max(last_end, n.end_tick)
    return overlaps


def first_time_signature(mid: MidiFile) -> Optional[tuple[int, int]]:
    """取 MIDI 中第一个拍号事件。"""
    for track in mid.tracks:
        t = 0
        for msg in track:
            t += msg.time
            if msg.type == "time_signature":
                return msg.numerator, msg.denominator
    return None


def first_key_signature(mid: MidiFile) -> Optional[str]:
    """取 MIDI 中第一个调号（key_signature）元事件的调名，如 'F#m'、'Bb'。"""
    for track in mid.tracks:
        t = 0
        for msg in track:
            t += msg.time
            if msg.type == "key_signature":
                return msg.key
    return None


def estimate_key(
    events: Sequence[NoteEvent], mid: MidiFile
) -> tuple[int, str]:
    """按音符时值加权音高分布估计调性（Krumhansl-Kessler 轮廓匹配）。

    返回 (主音音高, 大小调)。这是启发式估计，结果仅供参考。
    """
    changes = build_tempo_changes(mid)
    weights = [0.0] * 12
    for n in events:
        dur = tick_to_seconds(n.end_tick, mid.ticks_per_beat, changes) - tick_to_seconds(
            n.start_tick, mid.ticks_per_beat, changes
        )
        if dur > 0:
            weights[n.note % 12] += dur
    if sum(weights) <= 0:
        return 0, "major"

    best_pc, best_mode, best_score = 0, "major", -1.0
    for tonic in range(12):
        for mode, profile in (("major", KK_MAJOR), ("minor", KK_MINOR)):
            score = sum(
                weights[(tonic + i) % 12] * profile[i] for i in range(12)
            )
            if score > best_score:
                best_pc, best_mode, best_score = tonic, mode, score
    return best_pc, best_mode


def resolve_key(
    key: str,
    mode: Optional[str],
    mid: MidiFile,
    events: Sequence[NoteEvent],
) -> tuple[int, str, str, str]:
    """确定调性，返回 (主音音高, 大小调, 来源, 调名显示)。

    来源：explicit（--key 手动指定）/ midi（读取 MIDI 调号）/ estimated（估计）。
    """
    key = (key or "auto").strip()
    if key.lower() != "auto":
        pc, m = parse_key(key, mode)
        return pc, m, "explicit", key

    sig = first_key_signature(mid)
    if sig is not None:
        try:
            pc, sig_mode = parse_key(sig)
            return pc, mode or sig_mode, "midi", sig
        except ValueError:
            pass  # 调号异常，回退到估计

    pc, est_mode = estimate_key(events, mid)
    label = f"{_PC_SHARP_NAMES[pc]}{'m' if est_mode == 'minor' else ''}"
    return pc, mode or est_mode, "estimated", label


def lyric_for(
    rel_pc: int, chromatic: str, lyrics: str
) -> tuple[str, str]:
    """返回 (唱名词汇, 度数文本)。rel_pc 为相对主音的半音数（小调为相对 la）。"""
    base_table, chrom_table = LYRIC_TABLES[lyrics]
    if rel_pc in MAJOR_SCALE:
        degree = MAJOR_SCALE.index(rel_pc) + 1
        return base_table[rel_pc], str(degree)
    # 变化音：升号拼写取下方最近的音级加升号；降号拼写取上方最近音级加降号
    if chromatic == "sharp":
        lower = rel_pc - 1
        degree = MAJOR_SCALE.index(lower) + 1
        text = f"#{degree}"
    else:
        upper = rel_pc + 1
        degree = MAJOR_SCALE.index(upper) + 1
        text = f"b{degree}"
    return chrom_table[chromatic][rel_pc], text


def pick_encoding(lyrics: Sequence[str], encoding: str) -> str:
    """auto：ASCII 或日文假名歌词用 cp932（经典 UTAU 兼容），
    中文等其他文字用 utf-8（OpenUTAU 标准）。"""
    if encoding != "auto":
        return encoding
    text = "".join(lyrics)
    if all(ord(c) < 128 or 0x3040 <= ord(c) <= 0x30FF for c in text):
        return "cp932"
    return "utf-8"


def format_bar_beat(tick: int, numerator: int = 4, denominator: int = 4) -> str:
    """把 UST tick 转为「小节:拍」显示，如 152:2.5（拍为 1 基）。"""
    beat = tick / UST_PPQ
    beats_per_bar = numerator * 4 / denominator
    bar = int(beat // beats_per_bar) + 1
    pos = beat % beats_per_bar + 1
    return f"{bar}:{pos:g}"


def convert_midi(
    mid: MidiFile,
    key: str,
    mode: Optional[str] = None,
    *,
    lyrics: str = "en",
    chromatic: str = "sharp",
    track_idx: Optional[int] = None,
    channel: Optional[int] = None,
    merge: bool = False,
    overlap: str = "keep",
    final_rest: bool = True,
) -> tuple[utaupy.ust.Ust, list[dict], dict]:
    """核心转换：MIDI -> UST。返回 (Ust 对象, 音符摘要, 统计信息)。"""
    tempo_changes = build_tempo_changes(mid)
    bpm = (
        mido.tempo2bpm(tempo_changes[0][1])
        if tempo_changes
        else DEFAULT_BPM
    )
    notes = collect_notes(mid, track_idx=track_idx, channel=channel, merge=merge)
    if not notes:
        raise RuntimeError("所选轨道/通道中没有找到音符")
    key_pc, mode, key_source, key_label = resolve_key(key, mode, mid, notes)

    events, dropped = resolve_overlaps(notes, overlap)
    overlaps = count_overlaps(notes) if overlap == "keep" else 0

    # 小调采用首调 la 唱法：以关系大调为 do 框架
    do_base = key_pc if mode == "major" else (key_pc - 9) % 12
    seconds: Callable[[int], float] = lambda t: tick_to_seconds(
        t, mid.ticks_per_beat, tempo_changes
    )
    ust_tick: Callable[[float], int] = lambda sec: max(
        0, round(sec * bpm / 60 * UST_PPQ)
    )

    ust = utaupy.ust.Ust()
    ust.tempo = round(bpm, 2)
    ts = first_time_signature(mid)
    if ts:
        ust.timesignatures = f"({ts[0]}/{ts[1]}/0)"

    cursor = 0
    records: list[dict] = []
    for i, ev in enumerate(events):
        st = ust_tick(seconds(ev.start_tick))
        en = max(st + 1, ust_tick(seconds(ev.end_tick)))
        if st > cursor:  # 休止
            rest = utaupy.ust.Note()
            rest.length = st - cursor
            rest.lyric = "R"
            rest.notenum = 0
            ust.notes.append(rest)
            cursor = st

        rel_pc = (ev.note % 12 - do_base) % 12
        syllable, degree_text = lyric_for(rel_pc, chromatic, lyrics)
        note = utaupy.ust.Note()
        note.length = en - st
        note.notenum = ev.note
        note.lyric = syllable
        note.intensity = max(1, min(100, round(ev.velocity / 127 * 100)))
        ust.notes.append(note)
        cursor = max(cursor, en)
        records.append(
            {
                "index": i,
                "notenum": ev.note,
                "name": utaupy.ust.notenum_as_abc(ev.note),
                "start": st,
                "end": en,
                "length": en - st,
                "lyric": syllable,
                "degree": degree_text,
                "velocity": ev.velocity,
            }
        )

    if final_rest:
        ust.make_final_note_R()
    # 规范化休止符：音高归零、去掉继承来的音量
    for n in ust.notes:
        if n.lyric == "R":
            n.notenum = 0
            if "Intensity" in n:
                del n["Intensity"]

    stats = {
        "bpm": bpm,
        "mode": mode,
        "key_source": key_source,
        "key_label": key_label,
        "note_count": len(records),
        "rest_count": sum(1 for n in ust.notes if n.lyric == "R"),
        "overlaps": overlaps,
        "dropped": dropped,
        "tempo_changes": len({t for _, t in tempo_changes}),
    }
    return ust, records, stats


def make_demo_midi() -> MidiFile:
    """生成演示用 C 大调旋律（含变化音 F#/Bb 与一个休止）。"""
    mid = MidiFile(ticks_per_beat=UST_PPQ)
    track = MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0))
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    melody = [  # (note, length_ticks)
        (60, 480), (62, 480), (64, 480), (66, 480),  # C D E F#
        (67, 960),  # G（二分音符）
        (None, 480),  # 休止
        (69, 480), (70, 480), (71, 480), (72, 960),  # A Bb B C
    ]
    t = 0
    for note, length in melody:
        if note is None:
            t += length
            continue
        track.append(mido.Message("note_on", note=note, velocity=90, time=t))
        t = 0
        track.append(mido.Message("note_off", note=note, velocity=0, time=length))
    return mid


def demo() -> None:
    print("=" * 60)
    print("MIDI -> UST（调内唱名）演示")
    print("=" * 60)
    mid = make_demo_midi()
    ust, records, stats = convert_midi(mid, "C", lyrics="ja")
    print("\n演示旋律（C 大调，含 F# / Bb 变化音）：\n")
    print(f"{'MIDI':<6}{'度数':<6}{'唱名':<8}{'起(小节:拍)':<12}{'长(拍)':<8}")
    for r in records:
        bar = format_bar_beat(r["start"])
        length = r["length"] / UST_PPQ
        print(f"{r['name']:<6}{r['degree']:<6}{r['lyric']:<8}{bar:<12}{length:g}")
    print(f"\nUST 输出（BPM={stats['bpm']:.0f}，{stats['note_count']} 个音符，"
          f"{'大调' if stats['mode'] == 'major' else '小调'}）：\n")
    print(ust.write("_demo.ust", encoding="utf-8"))
    Path("_demo.ust").unlink(missing_ok=True)
    print("运行真实转换示例：uv run python midi_to_ust.py song.mid -o song.ust --key Am --lyrics zh")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="读取 MIDI，将旋律转换为调内唱名的 UST 文件"
    )
    parser.add_argument("input", nargs="?", help="输入 MIDI 文件路径")
    parser.add_argument("-o", "--output", help="输出 UST 路径（默认：输入同名 .ust）")
    parser.add_argument(
        "-k",
        "--key",
        default="auto",
        help="调，如 C、D#、Am、Bb minor；auto=读取 MIDI 调号，读不到则估计（默认 auto）",
    )
    parser.add_argument(
        "--mode", choices=["major", "minor"], help="大小调（与 --key 后缀二选一）"
    )
    parser.add_argument(
        "--lyrics",
        choices=["ja", "en", "zh"],
        default="ja",
        help="唱名词汇：ja=ドレミファソラシ（默认），en=do re mi，zh=多来咪",
    )
    parser.add_argument(
        "--chromatic",
        choices=["sharp", "flat"],
        default="sharp",
        help="变化音拼写：sharp=di/ri/fi/si/li，flat=ra/me/se/le/te（默认 sharp）",
    )
    parser.add_argument(
        "--track", type=int, help="指定 MIDI 轨道索引（默认自动选音符最多的非打击乐轨）"
    )
    parser.add_argument("--channel", type=int, help="指定 MIDI 通道（0-15）")
    parser.add_argument(
        "--merge", action="store_true", help="合并所有非打击乐通道的音符"
    )
    parser.add_argument(
        "--overlap",
        choices=["keep", "top", "cut", "drop"],
        default="top",
        help="重叠音符处理：top 取最高音（默认）、keep 保留/警告、cut 截短、drop 丢弃",
    )
    parser.add_argument(
        "--encoding",
        default="auto",
        help="UST 编码：auto/utf-8/cp932/...（auto 按歌词自动选，默认 auto）",
    )
    parser.add_argument(
        "--no-final-rest", action="store_true", help="不自动在结尾追加休止符"
    )
    parser.add_argument(
        "--debug", action="store_true", help="打印逐音符转换明细（调试用）"
    )
    parser.add_argument("--demo", action="store_true", help="运行演示")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:  # 防止 emoji/中文在 GBK 控制台下报错
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.demo:
        demo()
        return 0
    if not args.input:
        parser.error("请提供输入 MIDI 文件路径，或使用 --demo 运行演示")

    input_path = Path(args.input)
    if not input_path.exists():
        parser.error(f"输入文件不存在: {input_path}")
    output_path = Path(args.output) if args.output else input_path.with_suffix(".ust")

    mid = MidiFile(input_path)
    try:
        ust, records, stats = convert_midi(
            mid,
            args.key,
            args.mode,
            lyrics=args.lyrics,
            chromatic=args.chromatic,
            track_idx=args.track,
            channel=args.channel,
            merge=args.merge,
            overlap=args.overlap,
            final_rest=not args.no_final_rest,
        )
    except ValueError as e:
        parser.error(str(e))
    except RuntimeError as e:
        parser.error(str(e))

    encoding = pick_encoding([r["lyric"] for r in records], args.encoding)
    ust.write(output_path, encoding=encoding)

    mode_label = {"major": "大调", "minor": "小调"}[stats["mode"]]
    source_label = {
        "explicit": "手动指定",
        "midi": "MIDI 调号",
        "estimated": "估计",
    }[stats["key_source"]]
    print(f"🎵 已转换 {input_path} -> {output_path}")
    print(f"   调: {stats['key_label']}（{mode_label}，来源：{source_label}） | "
          f"音符: {stats['note_count']} | "
          f"休止: {stats['rest_count']} | BPM: {stats['bpm']:.2f} | 编码: {encoding}")
    if stats["key_source"] == "estimated":
        print("   ℹ 未在 MIDI 中找到调号，调性为根据音符分布估计，重要场合请用 --key 指定")
    if stats["tempo_changes"] > 1:
        print("   ⚠ MIDI 含变速，已按时间换算并统一到起始速度")
    if stats["overlaps"]:
        print(f"   ⚠ 检测到 {stats['overlaps']} 处重叠音符（和弦/复调），"
              "UST 不支持叠置音符，可用 --overlap top/cut/drop 或 --track 选择旋律轨")
    if stats["dropped"]:
        print(f"   ⚠ {stats['dropped']} 个重叠音符已按 --overlap {args.overlap} 处理")

    if args.debug:
        ts = first_time_signature(mid)
        ts_num, ts_den = ts if ts else (4, 4)
        print(f"\n{'MIDI':<7}{'度数':<6}{'唱名':<10}{'起(小节:拍)':<14}{'长(拍)':<8}")
        for r in records:
            bar = format_bar_beat(r["start"], ts_num, ts_den)
            length = r["length"] / UST_PPQ
            print(f"{r['name']:<7}{r['degree']:<6}{r['lyric']:<10}{bar:<14}{length:g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
