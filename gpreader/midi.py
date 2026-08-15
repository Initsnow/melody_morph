"""
Guitar Pro -> MIDI 导出
=======================

把 :func:`gpreader.parser.parse_gp` 解析出的 :class:`GPSong` 转成
``mido`` 的 :class:`MidiFile`。这是 midi_bpm_changer 的 GP 支持层：
GP 是记谱格式，音符以“四分音符”计长、速度按小节由 tempo automation
决定；导出 MIDI 时提供两种速度语义：

* ``bpm=None``（默认）：忠实还原原曲——按原 tempo map 逐小节写
  ``set_tempo``，tick = 四分音符 × PPQ，导出结果与原 GP 播放完全一致；
* ``bpm=<目标值>``：速度标记改为目标 BPM，但每个音符的**实际播放时刻
  不变**（等价于 midi_bpm_changer 的“改 BPM 不动速度”语义）。做法是
  先按原 tempo map 算出每个音符的真实秒数，再按目标 BPM 反算 tick：
  ``tick = 真实秒数 × BPM × PPQ / 60``。多段速度的 GP 文件也成立。

示例::

    from gpreader import parse_gp
    from gpreader.midi import song_to_midi, song_real_duration

    song = parse_gp("song.gp")
    mid = song_to_midi(song, bpm=146)          # 标 146 BPM、实际速度不变
    print(song_real_duration(song))            # 实际时长（秒）
    mid.save("song_modified.mid")
"""

from __future__ import annotations

from typing import Optional

import mido
from mido import MidiFile, MidiTrack, Message, MetaMessage

from gpreader.parser import GPSong, GuitarProError

DEFAULT_BPM = 120.0
DEFAULT_PPQ = 480  # 与 midi_to_ust 的 UST_PPQ 一致
DEFAULT_VELOCITY = 100
DRUM_CHANNEL = 9  # MIDI 标准打击乐通道（0 起）

# 每小节: (四分音符数, 该小节原 BPM, 小节起始四分音符, 小节起始真实秒数)
_BarInfo = tuple[float, float, float, float]


def detect_song_bpm(song: GPSong) -> float:
    """取 GP 文件的“原始 BPM”：第一个 tempo automation（第 0 小节起）。

    与 midi_bpm_changer 对 MIDI 取第一条 set_tempo 的语义一致。
    """
    for i in range(len(song.tracks[0].measures) if song.tracks else 0):
        bpm = song.tempo_at(i)
        if bpm is not None:
            return float(bpm)
    return DEFAULT_BPM


def song_tempos(song: GPSong) -> list[tuple[int, int]]:
    """按小节序返回 tempo map：``[(小节序号, BPM), ...]``（含向前继承的
    有效速度，仅列发生变化的点，第 0 小节恒有）。"""
    out: list[tuple[int, int]] = []
    current: Optional[int] = None
    for i in range(len(song.tracks[0].measures) if song.tracks else 0):
        bpm = song.tempo_at(i)
        if bpm is not None and bpm != current:
            out.append((i, int(bpm)))
            current = bpm
    return out


def _bar_info(song: GPSong) -> list[_BarInfo]:
    """展开每小节信息：``(四分音符数, BPM, 起始四分音符, 起始真实秒数)``。"""
    ref = song.tracks[0].measures if song.tracks else []
    info: list[_BarInfo] = []
    q = 0.0
    real = 0.0
    for m in ref:
        num, den = m.time_signature or (4, 4)
        quarters = num * 4.0 / den
        bpm = song.tempo_at(len(info)) or DEFAULT_BPM
        info.append((quarters, float(bpm), q, real))
        q += quarters
        real += quarters * 60.0 / bpm
    return info


def song_real_duration(song: GPSong) -> float:
    """按原 tempo map 计算整曲真实时长（秒）。"""
    info = _bar_info(song)
    if not info:
        return 0.0
    quarters, bpm, _, real = info[-1]
    return real + quarters * 60.0 / bpm


def _note_events(track, bar_info: list[_BarInfo], bpm: Optional[float],
                 ppq: int) -> list[list[float]]:
    """把轨道拍/音符展开成 ``[起始tick, 结束tick, midi]`` 事件列表。

    同一声部内首尾相接的同音高音符视为连音（tie），合并为单个事件。
    """
    constant = bpm is not None
    events: list[list[float]] = []
    # 按声部分组，保持拍序（parser 已按 start_quarters 排序）
    voices: dict[str, list] = {}
    for mi, measure in enumerate(track.measures):
        if mi >= len(bar_info):
            break
        for beat in measure.beats:
            voices.setdefault(beat.voice_id, []).append((mi, beat))

    for items in voices.values():
        chain: dict[int, list[float]] = {}  # midi -> 进行中的连音事件
        for mi, beat in items:
            _, bpm_orig, q_start, real_start = bar_info[mi]
            abs_q = q_start + beat.start_quarters
            if constant:
                real_s = real_start + beat.start_quarters * 60.0 / bpm_orig
                t0 = real_s * bpm * ppq / 60.0
                t1 = (real_s + beat.duration_quarters * 60.0 / bpm_orig) \
                    * bpm * ppq / 60.0
            else:
                t0 = abs_q * ppq
                t1 = (abs_q + beat.duration_quarters) * ppq
            for note in beat.notes:
                if note.muted:  # X 哑音无实际音高
                    continue
                prev = chain.get(note.midi)
                if note.tie_destination and prev is not None \
                        and abs(prev[1] - t0) < 1e-6:
                    prev[1] = t1  # 续写连音
                else:
                    ev = [t0, t1, float(note.midi)]
                    events.append(ev)
                    prev = ev
                if note.tie_origin:
                    chain[note.midi] = prev
                else:
                    chain.pop(note.midi, None)
    return events


def _emit_notes(track: MidiTrack, events: list[list[float]], channel: int,
                velocity: int) -> None:
    """按 tick 排序后把事件写成 note_on/note_off（同一 tick 先 off 后 on）。"""
    seq: list[tuple[int, int, int, int]] = []  # (tick, off=0/on=1, note, ch)
    for t0, t1, midi in events:
        seq.append((int(round(t0)), 1, int(midi), channel))
        seq.append((int(round(t1)), 0, int(midi), channel))
    seq.sort(key=lambda e: (e[0], e[1]))  # off(0) 先于 on(1)
    last = 0
    for tick, kind, note, ch in seq:
        track.append(Message(
            'note_on' if kind else 'note_off',
            note=note, velocity=velocity if kind else 0, channel=ch,
            time=tick - last,
        ))
        last = tick


def song_to_midi(song: GPSong, bpm: Optional[float] = None, ppq: int = DEFAULT_PPQ,
                 velocity: int = DEFAULT_VELOCITY) -> MidiFile:
    """把 :class:`GPSong` 导出为 ``mido`` MidiFile。

    :param bpm: 目标 BPM；``None`` 表示按原 tempo map 忠实导出。
        给定数值时实际播放时刻与原始文件一致（速度标记改为目标值）。
    :param ppq: MIDI 每四分音符 tick 数（默认 480）。
    """
    if not song.tracks or not song.tracks[0].measures:
        raise GuitarProError("歌曲没有可导出的轨道/小节")
    if bpm is not None and bpm <= 0:
        raise ValueError(f"目标 BPM 必须为正数，收到: {bpm!r}")

    constant = bpm is not None
    target = float(bpm) if constant else None
    bar_info = _bar_info(song)

    # 各小节起始 tick（用于指挥轨的拍号/速度事件定位）
    def bar_tick(i: int) -> int:
        if constant:
            _, _, _, real_start = bar_info[i]
            return int(round(real_start * target * ppq / 60.0))
        _, _, q_start, _ = bar_info[i]
        return int(round(q_start * ppq))

    out = MidiFile(type=1, ticks_per_beat=ppq)

    # ---- 指挥轨：速度 + 拍号 ----
    cond = MidiTrack()
    cond.append(MetaMessage("track_name", name="Tempo", time=0))
    last = 0
    prev_sig: Optional[tuple[int, int]] = None
    prev_bpm: Optional[int] = None
    measures = song.tracks[0].measures
    for i, (quarters, bpm_i, _, _) in enumerate(bar_info):
        tick = bar_tick(i)
        # 拍号（GPIF 的 (num, den) 与四分音符折算一致）
        sig = measures[i].time_signature or (int(round(quarters)), 4)
        if sig != prev_sig:
            cond.append(MetaMessage(
                "time_signature", numerator=sig[0], denominator=sig[1],
                clocks_per_click=24, notated_32nd_notes_per_beat=8,
                time=tick - last))
            last = tick
            prev_sig = sig
        # 速度（忠实模式逐变化点写）
        if not constant and bpm_i != prev_bpm:
            cond.append(MetaMessage("set_tempo",
                                    tempo=mido.bpm2tempo(bpm_i),
                                    time=tick - last))
            last = tick
            prev_bpm = int(bpm_i)
    if constant:
        cond.append(MetaMessage("set_tempo",
                                tempo=mido.bpm2tempo(target), time=0))
    cond.append(MetaMessage("end_of_track", time=0))
    out.tracks.append(cond)

    # ---- 音符轨道 ----
    for gp_track in song.tracks:
        if gp_track.midi_program == 0:
            channel = DRUM_CHANNEL
        else:
            channel = (gp_track.id % 16)
            if channel == DRUM_CHANNEL:
                channel = 0

        nt = MidiTrack()
        nt.append(MetaMessage("track_name", name=gp_track.name
                              or f"Track {gp_track.id}", time=0))
        if gp_track.midi_program and gp_track.midi_program != 0:
            nt.append(Message("program_change",
                              program=max(0, gp_track.midi_program - 1),
                              channel=channel, time=0))
        events = _note_events(gp_track, bar_info, target, ppq)
        _emit_notes(nt, events, channel, velocity)
        nt.append(MetaMessage("end_of_track", time=0))
        out.tracks.append(nt)

    return out
