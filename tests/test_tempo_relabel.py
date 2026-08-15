# -*- coding: utf-8 -*-
"""GPIF tempo automation 直改测试。

覆盖：拍单位换算、relabel 数学（精确/最近档）、XML 文本改写
（只改第一条、strict 报错）、zip 端到端（.gp → .gp，其余条目与
谱面逐字节保留、幂等）、CLI 输出消息。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from gpreader import detect_song_bpm, parse_gp
from gpreader.tempo import (find_tempo_automations, parse_tempo_value,
                            relabel_ref, relabel_tempo_value,
                            rewrite_tempo_values_in_text)
from gpreader.writer import rewrite_gpif_text
from midi_bpm_changer import main

# ---------------------------------------------------------------------------
# 拍单位换算 / relabel 数学
# ---------------------------------------------------------------------------


def test_parse_tempo_value():
    assert parse_tempo_value("73 2") == (73.0, 2, 73.0)
    assert parse_tempo_value("146 1") == (146.0, 1, 73.0)  # 八分音符
    assert parse_tempo_value("100 4") == (100.0, 4, 200.0)  # 二分音符
    assert parse_tempo_value("90") == (90.0, 2, 90.0)  # 缺省拍单位
    assert parse_tempo_value("90 9") == (90.0, 2, 90.0)  # 非法拍单位回退


def test_relabel_ref_exact():
    # 73 -> 146: 需要 factor 0.5 → ref 1（八分），精确
    ref, exact = relabel_ref(effective_bpm=73.0, target_bpm=146.0)
    assert (ref, exact) == (1, True)
    # 标签不变
    ref, exact = relabel_ref(effective_bpm=73.0, target_bpm=73.0)
    assert (ref, exact) == (2, True)


def test_relabel_ref_nearest():
    # 120 -> 150: 需要 factor 0.8，不在表内 → 最近档，非精确
    ref, exact = relabel_ref(effective_bpm=120.0, target_bpm=150.0)
    assert exact is False
    assert ref in (1, 2)
    # 90 -> 146: 需要 factor 0.616 → 最近 0.5 (ref 1)
    ref, exact = relabel_ref(effective_bpm=90.0, target_bpm=146.0)
    assert (ref, exact) == (1, False)


def test_relabel_tempo_value():
    new_value, info = relabel_tempo_value("73 2", 146.0)
    assert new_value == "146 1"
    assert info["exact"] is True
    assert info["effective"] == pytest.approx(73.0)
    assert info["new_effective"] == pytest.approx(73.0)


# ---------------------------------------------------------------------------
# XML 文本改写
# ---------------------------------------------------------------------------

_GPIF_WITH_TEMPO = """<GPIF>
  <GPVersion>8.0</GPVersion>
  <MasterTrack>
    <Automations>
      <Automation>
        <Type>Tempo</Type>
        <Bar>0</Bar>
        <Value>73 2</Value>
      </Automation>
      <Automation>
        <Type>Volume</Type>
        <Bar>0</Bar>
        <Value>100</Value>
      </Automation>
      <Automation>
        <Type>Tempo</Type>
        <Bar>30</Bar>
        <Value>100 2</Value>
      </Automation>
    </Automations>
  </MasterTrack>
</GPIF>
"""


def test_find_tempo_automations():
    autos = find_tempo_automations(_GPIF_WITH_TEMPO)
    assert [(a["bar"], a["old_value"]) for a in autos] == [(0, "73 2"), (30, "100 2")]


def test_rewrite_only_first_automation():
    new_text, changes = rewrite_tempo_values_in_text(_GPIF_WITH_TEMPO, 146.0)
    assert len(changes) == 1
    assert changes[0]["bar"] == 0
    assert changes[0]["new_value"] == "146 1"
    assert changes[0]["exact"] is True
    # 只有第一条被改，第二条 Tempo 与 Volume automation 原样
    assert "<Value>146 1</Value>" in new_text
    assert "<Value>100 2</Value>" in new_text
    assert "<Value>100</Value>" in new_text
    assert "<Value>73 2</Value>" not in new_text


def test_rewrite_strict_raises_on_inexact():
    with pytest.raises(ValueError, match="无法用 GP 拍单位"):
        rewrite_tempo_values_in_text(_GPIF_WITH_TEMPO, 150.0, strict=True)
    # 非 strict：取最近档并标记 exact=False
    _, changes = rewrite_tempo_values_in_text(_GPIF_WITH_TEMPO, 150.0)
    assert changes[0]["exact"] is False


def test_rewrite_no_tempo_automation():
    xml = "<GPIF><MasterTrack></MasterTrack></GPIF>"
    new_text, changes = rewrite_tempo_values_in_text(xml, 146.0)
    assert changes == []
    assert new_text == xml


# ---------------------------------------------------------------------------
# zip 端到端（沿用 test_gp_to_midi 的最小 .gp fixture 写法）
# ---------------------------------------------------------------------------

_GPIF_FULL = """<GPIF>
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
  <Score><Title>Test</Title><Artist>Artist</Artist></Score>
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
        z.writestr("Content/score.gpif", _GPIF_FULL)
        z.writestr("VERSION", "8.0")
        z.writestr("Content/Binary/sample.bin", b"\x00\x01\x02binary")


def test_gp_rewrite_preserves_everything_else(tmp_path):
    gp = tmp_path / "song.gp"
    _write_gp(gp)
    out = tmp_path / "song_out.gp"

    def transform(xml_text: str) -> str:
        new_text, _ = rewrite_tempo_values_in_text(xml_text, 146.0)
        return new_text

    rewrite_gpif_text(str(gp), str(out), transform)

    # zip 条目与顺序一致；非 gpif 条目逐字节相同
    with zipfile.ZipFile(gp) as a, zipfile.ZipFile(out) as b:
        assert a.namelist() == b.namelist()
        assert a.read("VERSION") == b.read("VERSION")
        assert a.read("Content/Binary/sample.bin") == b.read("Content/Binary/sample.bin")
    # gpif 文本只在 <Value> 处不同
    with zipfile.ZipFile(gp) as a, zipfile.ZipFile(out) as b:
        old = a.read("Content/score.gpif").decode("utf-8")
        new = b.read("Content/score.gpif").decode("utf-8")
    assert old.replace("73 2", "146 1") == new
    # 谱面数据（轨道/音符/拍号）原样
    song = parse_gp(out)
    assert len(song.tracks) == 1
    assert len(song.tracks[0].measures) == 2
    assert song.tracks[0].measures[0].time_signature == (4, 4)
    # 有效速度不变：146 × 0.5 = 73
    assert detect_song_bpm(song) == 73


def test_gp_rewrite_idempotent(tmp_path):
    gp = tmp_path / "song.gp"
    _write_gp(gp)

    def run(src: Path, dst: Path) -> None:
        rewrite_gpif_text(str(src), str(dst),
                         lambda t: rewrite_tempo_values_in_text(t, 146.0)[0])

    out1 = tmp_path / "out1.gp"
    out2 = tmp_path / "out2.gp"
    run(gp, out1)
    run(out1, out2)  # 再次执行同一目标
    with zipfile.ZipFile(out1) as a, zipfile.ZipFile(out2) as b:
        assert a.read("Content/score.gpif") == b.read("Content/score.gpif")


def test_cli_relabel_gp(tmp_path, capsys):
    gp = tmp_path / "mini.gp"
    _write_gp(gp)
    assert main(["--relabel", str(146), str(gp)]) == 0

    out = tmp_path / "mini_modified.gp"
    assert out.is_file()
    captured = capsys.readouterr()
    assert "标签 73 BPM" in captured.out
    assert "146 BPM" in captured.out
    assert "实际速度不变 (有效 73 BPM)" in captured.out

    # 标签 146（八分音符单位），有效速度仍是 73
    song = parse_gp(out)
    assert detect_song_bpm(song) == 73


def test_cli_strict_inexact_fails(tmp_path, capsys):
    gp = tmp_path / "mini.gp"
    _write_gp(gp)
    assert main(["--relabel", "--strict", str(150), str(gp)]) == 1
    captured = capsys.readouterr()
    assert "无法用 GP 拍单位精确表达" in captured.err
    assert not (tmp_path / "mini_modified.gp").exists()


def test_cli_reengrave_non_integer_factor_fails(tmp_path, capsys):
    """默认重排版只支持整数倍：73 -> 150 报错。"""
    gp = tmp_path / "mini.gp"
    _write_gp(gp)
    assert main([str(150), str(gp)]) == 1
    captured = capsys.readouterr()
    assert "不是整数倍" in captured.err
    assert not (tmp_path / "mini_modified.gp").exists()
