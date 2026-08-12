"""
gp-clear 清除轨道和弦/自由文本测试。

覆盖：只清目标轨道（另一轨道不受影响）、和弦库一并清空、CLI 端到端
写回自检。
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from gpchords.clear import clear_track, main
from gpreader import parse_gp
from gpreader.writer import read_gpif, write_gpif


def _two_track_gp(path: Path) -> None:
    gpif = """<GPIF>
      <GPVersion>8.0</GPVersion>
      <Tracks>
        <Track id="0"><Name>L</Name><Staves><Staff><Properties>
          <Property name="Tuning"><Pitches>40 45 50 55 59 64</Pitches></Property>
          <Property name="DiagramCollection"><Items>
            <Item id="0" name="C" />
          </Items></Property>
        </Properties></Staff></Staves></Track>
        <Track id="1"><Name>R</Name><Staves><Staff><Properties>
          <Property name="Tuning"><Pitches>40 45 50 55 59 64</Pitches></Property>
          <Property name="DiagramCollection"><Items>
            <Item id="0" name="D" />
          </Items></Property>
        </Properties></Staff></Staves></Track>
      </Tracks>
      <MasterBars>
        <MasterBar><Time>4/4</Time><Key><AccidentalCount>0</AccidentalCount>
          <Mode>Major</Mode></Key><Bars>0 1</Bars></MasterBar>
      </MasterBars>
      <Bars>
        <Bar id="0"><Voices>0</Voices></Bar>
        <Bar id="1"><Voices>1</Voices></Bar>
      </Bars>
      <Voices>
        <Voice id="0"><Beats>0</Beats></Voice>
        <Voice id="1"><Beats>1</Beats></Voice>
      </Voices>
      <Beats>
        <Beat id="0"><Chord>0</Chord><FreeText><![CDATA[Isus2]]></FreeText>
          <Notes>0</Notes><Rhythm><ref>0</ref></Rhythm></Beat>
        <Beat id="1"><Chord>0</Chord><Notes>1</Notes><Rhythm><ref>0</ref></Rhythm></Beat>
      </Beats>
      <Notes>
        <Note id="0"><Properties>
          <Property name="Midi"><Number>48</Number></Property>
          <Property name="Fret"><Fret>1</Fret></Property>
          <Property name="String"><String>3</String></Property>
        </Properties></Note>
        <Note id="1"><Properties>
          <Property name="Midi"><Number>52</Number></Property>
          <Property name="Fret"><Fret>2</Fret></Property>
          <Property name="String"><String>3</String></Property>
        </Properties></Note>
      </Notes>
      <Rhythms><Rhythm id="0"><NoteValue>Quarter</NoteValue></Rhythm></Rhythms>
    </GPIF>"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Content/score.gpif", gpif)
        z.writestr("VERSION", "8.0")


def test_clear_track_only_target(tmp_path):
    gp = tmp_path / "two.gp"
    _two_track_gp(gp)
    song = parse_gp(gp)
    b0 = song.tracks[0].measures[0].beats[0]
    assert b0.chord is not None
    assert b0.free_text == "Isus2"
    assert song.tracks[1].measures[0].beats[0].chord is not None

    root, _ = read_gpif(gp)
    stats = clear_track(root, "0")
    assert stats == {"beats": 1, "chords": 1, "freetexts": 1}
    out = tmp_path / "two_cleared.gp"
    write_gpif(gp, out, root)

    verify = parse_gp(out)
    t0 = verify.tracks[0]
    t1 = verify.tracks[1]
    cleared = t0.measures[0].beats[0]
    assert cleared.chord is None
    assert not cleared.free_text
    assert t0.chords == []  # 和弦库一并清空
    kept = t1.measures[0].beats[0]
    assert kept.chord is not None  # 另一轨道不受影响
    assert len(t1.chords) == 1


def test_clear_cli(tmp_path, monkeypatch):
    gp = tmp_path / "two.gp"
    _two_track_gp(gp)
    out = tmp_path / "out.gp"
    monkeypatch.setattr(
        sys, "argv", ["gp-clear", str(gp), "--track", "0", "--write", str(out)]
    )
    main()
    verify = parse_gp(out)
    assert verify.tracks[0].measures[0].beats[0].chord is None
    assert not verify.tracks[0].measures[0].beats[0].free_text
    assert verify.tracks[1].measures[0].beats[0].chord is not None
