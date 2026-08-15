# -*- coding: utf-8 -*-
"""GPIF 谱面重排版测试。

覆盖：时值表达/拆分、跨小节音符拆开补连音、小节×2、tempo 真实改写、
实际时长保持、整数倍约束（非整数倍/降速报错）、CLI 端到端。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from gpreader import detect_song_bpm, parse_gp, song_real_duration
from gpreader.parser import GuitarProError
from gpreader.reengrave import express_units, reengrave_tempo
from gpreader.writer import read_gpif, write_gpif
from midi_bpm_changer import main

_FIXTURE = """<GPIF>
  <GPVersion>8.0</GPVersion>
  <MasterTrack>
    <Automations>
      <Automation>
        <Type>Tempo</Type>
        <Linear>false</Linear>
        <Bar>0</Bar>
        <Position>0</Position>
        <Visible>false</Visible>
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
    <Voice id="0"><Beats>0 1 2</Beats></Voice>
    <Voice id="1"><Beats>3</Beats></Voice>
  </Voices>
  <Beats>
    <Beat id="0"><Rhythm ref="2"/><Notes>0</Notes></Beat>
    <Beat id="1"><Rhythm ref="1"/><Notes>1</Notes></Beat>
    <Beat id="2"><Rhythm ref="2"/><Notes>2</Notes></Beat>
    <Beat id="3"><Rhythm ref="0"/><Notes>3</Notes></Beat>
  </Beats>
  <Rhythms>
    <Rhythm id="0"><NoteValue>Whole</NoteValue></Rhythm>
    <Rhythm id="1"><NoteValue>Half</NoteValue></Rhythm>
    <Rhythm id="2"><NoteValue>Quarter</NoteValue></Rhythm>
  </Rhythms>
  <Notes>
    <Note id="0"><Properties><Property name="Midi"><Number>60</Number>
      </Property></Properties></Note>
    <Note id="1"><Properties><Property name="Midi"><Number>64</Number>
      </Property></Properties></Note>
    <Note id="2"><Properties><Property name="Midi"><Number>67</Number>
      </Property></Properties></Note>
    <Note id="3"><Properties><Property name="Midi"><Number>72</Number>
      </Property></Properties></Note>
  </Notes>
</GPIF>
"""


def _write_gp(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Content/score.gpif", _FIXTURE)
        z.writestr("VERSION", "8.0")


def _reengrave(src: Path, out: Path, target: float) -> dict:
    root, _ = read_gpif(src)
    info = reengrave_tempo(root, target)
    write_gpif(src, out, root)
    return info


# ---------------------------------------------------------------------------
# 时值表达
# ---------------------------------------------------------------------------


def test_express_units():
    assert express_units(1.5) == [("Quarter", 1)]  # 附点四分
    assert express_units(2.0) == [("Half", 0)]
    assert express_units(2.5) == [("Half", 0), ("Eighth", 0)]  # 拆分
    assert express_units(3.5) == [("Half", 2)]  # 双附点二分


def test_express_units_inexpressible():
    with pytest.raises(GuitarProError):
        express_units(0.1)


# ---------------------------------------------------------------------------
# 重排版
# ---------------------------------------------------------------------------


def test_reengrave_basic(tmp_path):
    src = tmp_path / "song.gp"
    out = tmp_path / "song_146.gp"
    _write_gp(src)
    info = _reengrave(src, out, 146.0)

    assert info["factor"] == 2
    assert info["bars_in"] == 2 and info["bars_out"] == 4
    assert info["tempo_before"] == 73.0 and info["tempo_after"] == 146.0
    assert info["splits"] == 2  # 二分@1 和 全音符 跨小节
    assert info["notes_cloned"] == 4  # 每个拆分的音符克隆 2 份 × 2 个拆分

    song = parse_gp(out)
    assert detect_song_bpm(song) == 146
    assert len(song.tracks[0].measures) == 4
    # 实际时长不变：2×4×(60/73) == 4×4×(60/146)
    assert song_real_duration(song) == pytest.approx(8 * 60 / 73)
    # 4 个原始音符 + 2 个拆分各多 1 份连音音符 = 6
    assert len(song.tracks[0].notes) == 6


def test_reengrave_splits_add_ties(tmp_path):
    src = tmp_path / "song.gp"
    out = tmp_path / "song_146.gp"
    _write_gp(src)
    _reengrave(src, out, 146.0)

    song = parse_gp(out)
    # 全部音符事件（按拍展开）：原 4 个，拆分后 6 个
    notes = song.tracks[0].notes
    ties_origin = sum(1 for n in notes if n.tie_origin)
    ties_dest = sum(1 for n in notes if n.tie_destination)
    assert ties_origin == 2 and ties_dest == 2  # 两个拆分链
    # 音高序列：E4(64) 和 C5(72) 各出现两次（拆分出连音）
    midis = sorted(n.midi for n in notes)
    assert midis == [60, 64, 64, 67, 72, 72]


def test_reengrave_rejects_non_integer_factor(tmp_path):
    src = tmp_path / "song.gp"
    _write_gp(src)
    root, _ = read_gpif(src)
    with pytest.raises(GuitarProError, match="不是整数倍"):
        reengrave_tempo(root, 150.0)


def test_reengrave_rejects_slowdown(tmp_path):
    """降速（目标 < 有效）比例不是整数，同样报错。"""
    src = tmp_path / "song.gp"
    _write_gp(src)
    root, _ = read_gpif(src)
    with pytest.raises(GuitarProError, match="不是整数倍"):
        reengrave_tempo(root, 60.0)


def test_reengrave_reuses_rhythm_pool(tmp_path):
    """拆分的拍引用既有节奏池（Half/Whole），不新增重复节奏。"""
    src = tmp_path / "song.gp"
    out = tmp_path / "song_146.gp"
    _write_gp(src)
    _reengrave(src, out, 146.0)

    with zipfile.ZipFile(out) as z:
        xml = z.read("Content/score.gpif").decode("utf-8")
    # 原始音符元素保留（未拆分的拍继续共享引用）
    assert '<Note id="0">' in xml and '<Note id="2">' in xml
    # 节奏池只含原有的 3 个（拆分产物复用 Whole/Half）
    assert xml.count("<Rhythm id=") == 3
    # 拆分产生的连音音符带 Tie
    assert 'origin="true"' in xml and 'destination="true"' in xml


def test_reengrave_creates_new_rhythm_for_dotted(tmp_path):
    """附点八分 ×2 = 附点四分，节奏池没有时需要新建。"""
    gpif = _FIXTURE.replace(
        '<Beat id="0"><Rhythm ref="2"/><Notes>0</Notes></Beat>',
        '<Beat id="0"><Rhythm ref="3"/><Notes>0</Notes></Beat>',
    ).replace(
        "<Rhythm id=\"2\"><NoteValue>Quarter</NoteValue></Rhythm>",
        "<Rhythm id=\"2\"><NoteValue>Quarter</NoteValue></Rhythm>\n"
        '    <Rhythm id="3"><NoteValue>Eighth</NoteValue>\n'
        '      <AugmentationDot count="1"/></Rhythm>',
    )
    src = tmp_path / "song.gp"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Content/score.gpif", gpif)
    out = tmp_path / "song_146.gp"
    _reengrave(src, out, 146.0)
    with zipfile.ZipFile(out) as z:
        xml = z.read("Content/score.gpif").decode("utf-8")
    # 附点八分(0.75) ×2 = 附点四分(1.5)：节奏池新增；池内出现附点节奏
    assert xml.count("<Rhythm id=") > 3
    assert '<AugmentationDot' in xml


# ---------------------------------------------------------------------------
# CLI 端到端
# ---------------------------------------------------------------------------


def test_cli_reengrave_gp(tmp_path, capsys):
    gp = tmp_path / "mini.gp"
    _write_gp(gp)
    assert main([str(146), str(gp)]) == 0

    out = tmp_path / "mini_modified.gp"
    assert out.is_file()
    captured = capsys.readouterr()
    assert "73 BPM" in captured.out
    assert "真实 146 BPM" in captured.out
    assert "小节 2→4" in captured.out

    song = parse_gp(out)
    assert detect_song_bpm(song) == 146
    assert len(song.tracks[0].measures) == 4
    assert song_real_duration(song) == pytest.approx(8 * 60 / 73)
