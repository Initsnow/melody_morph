# -*- coding: utf-8 -*-
"""gpreader.midi GP→MIDI 导出测试。

覆盖：速度检测/tempo map、实际时长、目标 BPM 导出时实际时刻不变、
忠实导出、连音合并、哑音跳过、非法 BPM、CLI 端到端（.gp → .mid）。
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import mido
import pytest
from mido import MidiFile

from gpreader import parse_gp
from gpreader.midi import (detect_song_bpm, song_real_duration, song_tempos,
                           song_to_midi)
from gpreader.parser import GPBeat, GPMeasure, GPNote, GPSong, GPTrack
from midi_bpm_changer import main

PPQ = 480


def _note(midi, dur=1.0, tie_origin=False, tie_dest=False, muted=False):
    return GPNote(id=f"n{midi}-{dur}", midi=midi, duration_quarters=dur,
                  tie_origin=tie_origin, tie_destination=tie_dest, muted=muted)


def make_song(tempos=None) -> GPSong:
    """两小节 4/4 的测试曲：

    bar1:  C4(八分连音→C4) E4(四分) G4(四分) + C3 哑音
    bar2:  C5(四分) E5(四分)
    """
    b1 = GPBeat(id="b1", start_quarters=0.0, duration_quarters=0.5,
                voice_id="v0", notes=[_note(60, 0.5, tie_origin=True)])
    b2 = GPBeat(id="b2", start_quarters=0.5, duration_quarters=0.5,
                voice_id="v0", notes=[_note(60, 0.5, tie_dest=True)])
    b3 = GPBeat(id="b3", start_quarters=1.0, duration_quarters=1.0,
                voice_id="v0", notes=[_note(64)])
    b4 = GPBeat(id="b4", start_quarters=2.0, duration_quarters=1.0,
                voice_id="v0", notes=[_note(67), _note(48, muted=True)])
    b5 = GPBeat(id="b5", start_quarters=0.0, duration_quarters=1.0,
                voice_id="v1", notes=[_note(72)])
    b6 = GPBeat(id="b6", start_quarters=1.0, duration_quarters=1.0,
                voice_id="v1", notes=[_note(76)])
    m1 = GPMeasure(index=1, time_signature=(4, 4), beats=[b1, b2, b3, b4])
    m2 = GPMeasure(index=2, time_signature=(4, 4), beats=[b5, b6])
    track = GPTrack(id=0, name="Test", midi_program=24, measures=[m1, m2])
    track.notes = [n for m in (m1, m2) for b in m.beats for n in b.notes]
    return GPSong(tempos=tempos or {0: 120, 1: 120}, tracks=[track])


def _note_on_ticks(mid: MidiFile) -> list[int]:
    ticks: list[int] = []
    for t in mid.tracks[1:]:
        tick = 0
        for msg in t:
            tick += msg.time
            if msg.type == "note_on":
                ticks.append(tick)
    return sorted(ticks)


def test_detect_song_bpm_and_tempos():
    song = make_song()
    assert detect_song_bpm(song) == 120
    assert song_tempos(song) == [(0, 120)]


def test_song_tempos_multiple():
    song = make_song(tempos={0: 120, 1: 90})
    assert song_tempos(song) == [(0, 120), (1, 90)]


def test_song_real_duration():
    assert song_real_duration(make_song()) == pytest.approx(4.0)  # 2 小节 4/4 @120
    song = make_song(tempos={0: 120, 1: 90})
    assert song_real_duration(song) == pytest.approx(2.0 + 4 * 60 / 90)  # 4.667


def test_song_to_midi_constant_bpm_preserves_real_time():
    song = make_song()
    mid = song_to_midi(song, bpm=146)
    assert mid.ticks_per_beat == PPQ

    tempo = next(m.tempo for t in mid.tracks for m in t
                 if m.type == "set_tempo")
    assert mido.tempo2bpm(tempo) == pytest.approx(146)

    # 期望 tick：真实秒数 × 146 × PPQ / 60
    # bar1 @120: q=0→0s, q=0.5→0.25s, q=1→0.5s, q=2→1.0s, q=3→1.5s
    # bar2 @120: q=4→2.0s, q=5→2.5s
    # 连音 C4 合并为 0..0.5s；哑音 C3 不导出
    assert _note_on_ticks(mid) == [
        round(0.0 * 146 * PPQ / 60),       # C4
        round(0.5 * 146 * PPQ / 60),       # E4
        round(1.0 * 146 * PPQ / 60),       # G4
        round(2.0 * 146 * PPQ / 60),       # C5
        round(2.5 * 146 * PPQ / 60),       # E5
    ]

    # 无哑音音高、连音只出一个音符
    notes = {m.note for t in mid.tracks for m in t if m.type == "note_on"}
    assert 48 not in notes
    assert len([m for t in mid.tracks for m in t if m.type == "note_on"]) == 5


def test_song_to_midi_faithful_keeps_original_tempo_map():
    song = make_song(tempos={0: 120, 1: 90})
    mid = song_to_midi(song)  # bpm=None
    tempos = [(round(mido.tempo2bpm(m.tempo), 3), m.time)
              for t in mid.tracks for m in t if m.type == "set_tempo"]
    assert tempos == [(120.0, 0), (90.0, 4 * PPQ)]
    # 忠实模式 tick = 四分音符 × PPQ：C4@0, E4@1, G4@2, C5@4, E5@5
    assert _note_on_ticks(mid) == [0, PPQ, 2 * PPQ, 4 * PPQ, 5 * PPQ]


def test_song_to_midi_invalid_bpm():
    song = make_song()
    with pytest.raises(ValueError):
        song_to_midi(song, bpm=0)
    with pytest.raises(ValueError):
        song_to_midi(song, bpm=-5)


def test_song_to_midi_tie_merging():
    song = make_song()
    mid = song_to_midi(song)
    ons = [(m.note, m.time) for t in mid.tracks for m in t
           if m.type == "note_on"]
    # 连音 C4 只出现一次
    assert sum(1 for n, _ in ons if n == 60) == 1


# ---------------------------------------------------------------------------
# CLI 端到端（最小 .gp fixture，沿用项目现有 GPIF zip 写法）
# ---------------------------------------------------------------------------

_MINIMAL_GPIF = """<GPIF>
  <GPVersion>8.0</GPVersion>
  <MasterTrack>
    <Automations>
      <Automation>
        <Type>Tempo</Type>
        <Bar>0</Bar>
        <Value>73 2</Value>
      </Automation>
    </Automations>
  </MasterTrack>
  <Score><Title>T</Title><Artist>A</Artist></Score>
  <MasterBars>
    <MasterBar><Time>4/4</Time><Key><AccidentalCount>0</AccidentalCount>
      <Mode>Major</Mode></Key><Bars>0</Bars></MasterBar>
    <MasterBar><Time>4/4</Time><Key><AccidentalCount>0</AccidentalCount>
      <Mode>Major</Mode></Key><Bars>1</Bars></MasterBar>
  </MasterBars>
  <Tracks>
    <Track id="0"><Name>Test</Name><Staves><Staff><Properties>
      <Property name="Tuning"><Pitches>40 45 50 55 59 64</Pitches></Property>
    </Properties></Staff></Staves></Track>
  </Tracks>
  <Bars>
    <Bar id="0"><Voices>0</Voices></Bar>
    <Bar id="1"><Voices>1</Voices></Bar>
  </Bars>
  <Voices>
    <Voice id="0"><Beats>0</Beats></Voice>
    <Voice id="1"><Beats>1</Beats></Voice>
  </Voices>
  <Beats>
    <Beat id="0"><Notes>0</Notes><Rhythm><ref>0</ref></Rhythm></Beat>
    <Beat id="1"><Notes>1</Notes><Rhythm><ref>0</ref></Rhythm></Beat>
  </Beats>
  <Rhythms>
    <Rhythm id="0"><NoteValue>Quarter</NoteValue></Rhythm>
  </Rhythms>
  <Notes>
    <Note id="0"><Properties><Property name="Midi"><Number>60</Number>
      </Property></Properties></Note>
    <Note id="1"><Properties><Property name="Midi"><Number>64</Number>
      </Property></Properties></Note>
  </Notes>
</GPIF>
"""


def _write_gp(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Content/score.gpif", _MINIMAL_GPIF)
        z.writestr("VERSION", "8.0")


def test_parse_minimal_gp(tmp_path):
    gp = tmp_path / "mini.gp"
    _write_gp(gp)
    song = parse_gp(gp)
    assert detect_song_bpm(song) == 73
    assert len(song.tracks) == 1
    assert len(song.tracks[0].measures) == 2
    assert song_real_duration(song) == pytest.approx(8 * 60 / 73)  # 6.575 s


def test_cli_converts_gp_to_midi_opt_in(tmp_path, capsys):
    """GP 默认直改 .gp；显式 --to-midi 才导出 MIDI。"""
    gp = tmp_path / "mini.gp"
    _write_gp(gp)
    assert main(["--to-midi", str(146), str(gp)]) == 0

    out = tmp_path / "mini_modified.mid"
    assert out.is_file()
    mid = MidiFile(str(out))
    tempo = next(m.tempo for t in mid.tracks for m in t
                 if m.type == "set_tempo")
    assert mido.tempo2bpm(tempo) == pytest.approx(146)

    captured = capsys.readouterr()
    assert "已导出 MIDI" in captured.out
    assert "mini_modified.gp" not in captured.out


def test_cli_detect_gp(tmp_path, capsys):
    gp = tmp_path / "mini.gp"
    _write_gp(gp)
    assert main(["-d", str(gp)]) == 0
    captured = capsys.readouterr()
    assert "标签 73 BPM" in captured.out
    assert "有效 73 BPM" in captured.out
    assert not (tmp_path / "mini_modified.gp").exists()


def test_cli_mixed_midi_and_gp(tmp_path, capsys):
    """同一命令混用 .mid 与 .gp。"""
    gp = tmp_path / "mini.gp"
    _write_gp(gp)
    assert main([str(146), str(gp), str(gp)]) == 0
    assert (tmp_path / "mini_modified.gp").is_file()
