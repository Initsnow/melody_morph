# -*- coding: utf-8 -*-
"""midi_bpm_changer 核心逻辑测试。"""

import os

import mido
import pytest
from mido import MidiFile, MetaMessage, Message

from midi_bpm_changer import change_bpm, detect_bpm, main


def make_midi_with_tempo(tmp_path, tempo=500000, ticks=480, times=(0, 480, 960)):
    """构造一个含 set_tempo 和三个音符的 MIDI type 0 文件，返回路径。"""
    path = tmp_path / "with_tempo.mid"
    mid = MidiFile(type=0, ticks_per_beat=ticks)
    track = mido.MidiTrack()
    track.append(MetaMessage("set_tempo", tempo=tempo, time=0))
    for i, t in enumerate(times):
        track.append(Message("note_on", note=60 + i, velocity=100, time=t))
        track.append(Message("note_off", note=60 + i, velocity=0, time=480))
    mid.tracks.append(track)
    mid.save(str(path))
    return str(path)


def make_midi_without_tempo(tmp_path, ticks=480):
    """构造一个没有 set_tempo 的 MIDI 文件，返回路径。"""
    path = tmp_path / "no_tempo.mid"
    mid = MidiFile(type=0, ticks_per_beat=ticks)
    track = mido.MidiTrack()
    track.append(Message("note_on", note=60, velocity=100, time=0))
    track.append(Message("note_off", note=60, velocity=0, time=480))
    track.append(Message("note_on", note=62, velocity=100, time=480))
    track.append(Message("note_off", note=62, velocity=0, time=480))
    mid.tracks.append(track)
    mid.save(str(path))
    return str(path)


def test_detect_bpm(tmp_path):
    path = make_midi_with_tempo(tmp_path, tempo=500000)  # 120 BPM
    mid = MidiFile(path)
    assert detect_bpm(mid) == 120


def test_detect_bpm_default_when_no_tempo(tmp_path):
    path = make_midi_without_tempo(tmp_path)
    mid = MidiFile(path)
    assert detect_bpm(mid) == 120  # 默认值


def test_change_bpm_scales_time_and_tempo(tmp_path):
    path = make_midi_with_tempo(tmp_path, tempo=500000, ticks=480)
    mid = MidiFile(path)
    converted = change_bpm(mid, 150)

    assert converted.ticks_per_beat == mid.ticks_per_beat  # 元数据保留
    assert converted.type == mid.type

    # 验证 tempo 与时间缩放
    assert mido.tempo2bpm(converted.tracks[0][0].tempo) == pytest.approx(150)

    note_msgs = [m for m in converted.tracks[0] if m.type == "note_on"]
    assert [m.time for m in note_msgs] == [0, 600, 1200]  # 480 * 1.25


def test_change_bpm_inserts_tempo_when_missing(tmp_path):
    path = make_midi_without_tempo(tmp_path)
    mid = MidiFile(path)
    converted = change_bpm(mid, 90)

    first = converted.tracks[0][0]
    assert first.type == "set_tempo"
    assert mido.tempo2bpm(first.tempo) == pytest.approx(90)
    # 时间应整体按 90/120 缩放
    note_msgs = [m for m in converted.tracks[0] if m.type == "note_on"]
    assert note_msgs[0].time == 0
    assert note_msgs[1].time == int(round(480 * (90 / 120)))  # 360


def test_change_bpm_rejects_invalid_bpm(tmp_path):
    path = make_midi_with_tempo(tmp_path)
    mid = MidiFile(path)
    with pytest.raises(ValueError):
        change_bpm(mid, 0)
    with pytest.raises(ValueError):
        change_bpm(mid, -10)


def test_input_not_mutated(tmp_path):
    path = make_midi_with_tempo(tmp_path)
    mid = MidiFile(path)
    original_times = [m.time for m in mid.tracks[0]]
    change_bpm(mid, 200)
    assert [m.time for m in mid.tracks[0]] == original_times


def test_cli_converts_file(tmp_path, capsys):
    path = make_midi_with_tempo(tmp_path)
    exit_code = main([str(150), path])
    assert exit_code == 0

    out_path = os.path.splitext(path)[0] + "_modified.mid"
    assert os.path.isfile(out_path)
    out_mid = MidiFile(out_path)
    assert mido.tempo2bpm(out_mid.tracks[0][0].tempo) == pytest.approx(150)

    captured = capsys.readouterr()
    assert "120 BPM -> 150 BPM" in captured.out


def test_cli_detect_only(tmp_path, capsys):
    path = make_midi_with_tempo(tmp_path)
    exit_code = main(["-d", str(120), path])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "原始 BPM = 120" in captured.out
    # 不应生成输出文件
    assert not os.path.exists(os.path.splitext(path)[0] + "_modified.mid")


def test_cli_missing_file_fails(tmp_path, capsys):
    exit_code = main([str(120), str(tmp_path / "nope.mid")])
    assert exit_code == 1
    assert "文件不存在" in capsys.readouterr().err
