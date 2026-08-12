"""
罗马和弦记号（Roman numeral）自由注解测试。

覆盖：B 大调/A 小调下的度数、大小写、品质后缀、斜杠低音、调外根音；
写回 .gp 时 <FreeText> 的 CDATA 形式与拍上位置（以《春日影.gp》里的
``Isus2`` 手工注解为参照）。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from gpchords.annotate import CHORD_TEMPLATES, write_chords_to_gp
from gpchords.roman import chord_to_roman
from gpreader import parse_gp

HARUHIKAGE = Path(
    r"C:\Users\Initsnow\Documents\Audio\谱\sheets\mygo官谱\春日影.gp"
)


def chord(name: str, root: int, quality: str, bass_pc: int | None = None) -> dict:
    return {
        "name": name,
        "root": root,
        "quality": quality,
        "suffix": CHORD_TEMPLATES[quality][1],
        "bass_pc": root if bass_pc is None else bass_pc,
    }


# ---------------------------------------------------------------------------
# 转换单元测试
# ---------------------------------------------------------------------------


def test_example_file_style_b_major():
    """参照《春日影.gp》手工注解：B 大调下 Bsus2 -> Isus2。"""
    assert chord_to_roman(chord("Bsus2", 11, "sus2"), 11, "Major") == "Isus2"


def test_diatonic_b_major():
    assert chord_to_roman(chord("B", 11, "maj"), 11, "Major") == "I"
    assert chord_to_roman(chord("E", 4, "maj"), 11, "Major") == "IV"
    assert chord_to_roman(chord("F#", 6, "maj"), 11, "Major") == "V"
    assert chord_to_roman(chord("G#m", 8, "min"), 11, "Major") == "vi"
    assert chord_to_roman(chord("C#m7", 1, "m7"), 11, "Major") == "ii7"
    assert chord_to_roman(chord("A#dim", 10, "dim"), 11, "Major") == "vii°"
    assert chord_to_roman(chord("F#7sus4", 6, "7sus4"), 11, "Major") == "V7sus4"
    assert chord_to_roman(chord("Bmaj7", 11, "maj7"), 11, "Major") == "Imaj7"
    assert chord_to_roman(chord("E5", 4, "5"), 11, "Major") == "IV5"


def test_slash_bass_keeps_note_name():
    assert chord_to_roman(chord("Bsus2/F#", 11, "sus2", bass_pc=6), 11, "Major") == (
        "Isus2/F#"
    )
    assert chord_to_roman(chord("B5/F#", 11, "5", bass_pc=6), 11, "Major") == "I5/F#"


def test_chromatic_roots():
    # 调外根音按字母对应音级加升降号：C 在 B 大调 -> bII
    assert chord_to_roman(chord("C", 0, "maj"), 11, "Major") == "bII"
    # 根音音级与调内音级同音时功能优先：Gb5（=F#）在 B 大调 -> V5
    assert chord_to_roman(chord("Gb5", 6, "5"), 11, "Major") == "V5"
    # 等音拼写按音级落位：Db（=C#）在 B 大调 -> II
    assert chord_to_roman(chord("Db", 1, "maj"), 11, "Major") == "II"
    assert chord_to_roman(chord("F", 5, "maj"), 11, "Major") == "bV"
    # C 大调里 D#（升号拼写）-> #II
    assert chord_to_roman(chord("D#", 3, "maj"), 0, "Major") == "#II"


def test_minor_key():
    """小调默认按关系大调记：A 小调视作 C 大调，Am -> vi。"""
    assert chord_to_roman(chord("Am", 9, "min"), 9, "Minor") == "vi"
    assert chord_to_roman(chord("C", 0, "maj"), 9, "Minor") == "I"
    assert chord_to_roman(chord("Dm", 2, "min"), 9, "Minor") == "ii"
    assert chord_to_roman(chord("Em", 4, "min"), 9, "Minor") == "iii"
    assert chord_to_roman(chord("E", 4, "maj"), 9, "Minor") == "III"
    assert chord_to_roman(chord("F", 5, "maj"), 9, "Minor") == "IV"
    assert chord_to_roman(chord("G", 7, "maj"), 9, "Minor") == "V"
    assert chord_to_roman(chord("Bdim", 11, "dim"), 9, "Minor") == "vii°"
    assert chord_to_roman(chord("Am7", 9, "m7"), 9, "Minor") == "vi7"
    assert chord_to_roman(chord("Dm7", 2, "m7"), 9, "Minor") == "ii7"
    assert chord_to_roman(chord("C/G", 0, "maj", bass_pc=7), 9, "Minor") == "I/G"
    # 调外根音按字母对应音级加升降号：G#dim（=C 大调的升五度）-> #v°
    assert chord_to_roman(chord("G#dim", 8, "dim"), 9, "Minor") == "#v°"


def test_minor_as_tonic():
    """--roman-tonic-minor：小调按主音小调记，Am -> i。"""
    assert chord_to_roman(chord("Am", 9, "min"), 9, "Minor", minor_as_tonic=True) == "i"
    assert chord_to_roman(chord("C", 0, "maj"), 9, "Minor", minor_as_tonic=True) == "III"
    assert chord_to_roman(chord("Dm", 2, "min"), 9, "Minor", minor_as_tonic=True) == "iv"
    assert chord_to_roman(chord("Em", 4, "min"), 9, "Minor", minor_as_tonic=True) == "v"
    assert chord_to_roman(chord("G", 7, "maj"), 9, "Minor", minor_as_tonic=True) == "VII"
    assert chord_to_roman(chord("Bdim", 11, "dim"), 9, "Minor", minor_as_tonic=True) == "ii°"
    assert chord_to_roman(chord("G#dim", 8, "dim"), 9, "Minor", minor_as_tonic=True) == "#vii°"


def test_suffix_transforms():
    assert chord_to_roman(chord("Cmaj7", 0, "maj7"), 0, "Major") == "Imaj7"
    assert chord_to_roman(chord("G7", 7, "7"), 0, "Major") == "V7"
    assert chord_to_roman(chord("Dm7", 2, "m7"), 0, "Major") == "ii7"
    assert chord_to_roman(chord("Bdim7", 11, "dim7"), 0, "Major") == "vii°7"
    assert chord_to_roman(chord("Bm7b5", 11, "m7b5"), 0, "Major") == "viiø7"
    assert chord_to_roman(chord("Eaug", 4, "aug"), 0, "Major") == "III+"
    assert chord_to_roman(chord("Dm9", 2, "m9"), 0, "Major") == "ii9"
    assert chord_to_roman(chord("F6/9", 5, "6/9"), 0, "Major") == "IV6/9"
    assert chord_to_roman(chord("Cmadd9", 0, "madd9"), 0, "Major") == "iadd9"
    # 小写度数隐含小调性，后缀省略开头的 m
    assert chord_to_roman(chord("Am7", 9, "m7"), 0, "Major") == "vi7"
    assert chord_to_roman(chord("Am", 9, "min"), 0, "Major") == "vi"
    assert chord_to_roman(chord("Am6", 9, "m6"), 0, "Major") == "vi6"
    assert chord_to_roman(chord("Am7(no5)", 9, "m7(no5)"), 0, "Major") == "vi7(no5)"
    # no3/no5 变体后缀保留
    assert chord_to_roman(chord("Cmaj7(no3)", 0, "maj7(no3)"), 0, "Major") == (
        "Imaj7(no3)"
    )


def test_all_template_qualities_convert():
    """CHORD_TEMPLATES 里每个品质都能转换（模板扩充时不漏记法）。"""
    for quality in CHORD_TEMPLATES:
        c = chord("X", 11, quality)
        out = chord_to_roman(c, 11, "Major")
        assert out
        assert out.startswith(("I", "i"))


def test_missing_key_raises():
    with pytest.raises(ValueError):
        chord_to_roman(chord("B", 11, "maj"), None)


# ---------------------------------------------------------------------------
# 写回 <FreeText>（CDATA + 拍上位置）
# ---------------------------------------------------------------------------


def _mini_gp(path: Path) -> None:
    gpif = """<GPIF>
      <GPVersion>8.0</GPVersion>
      <Tracks><Track id="0"><Name>L</Name><Staves><Staff><Properties>
        <Property name="Tuning"><Pitches>40 45 50 55 59 64</Pitches></Property>
        <Property name="DiagramCollection"><Items /></Property>
      </Properties></Staff></Staves></Track></Tracks>
      <MasterBars><MasterBar><Time>4/4</Time>
        <Key><AccidentalCount>0</AccidentalCount><Mode>Major</Mode></Key>
        <Bars>0</Bars></MasterBar></MasterBars>
      <Bars><Bar id="0"><Voices>0</Voices></Bar></Bars>
      <Voices><Voice id="0"><Beats>0</Beats></Voice></Voices>
      <Beats>
        <Beat id="0"><Notes>0</Notes><Rhythm><ref>0</ref></Rhythm></Beat>
      </Beats>
      <Notes>
        <Note id="0"><Properties>
          <Property name="Midi"><Number>48</Number></Property>
          <Property name="Fret"><Fret>1</Fret></Property>
          <Property name="String"><String>3</String></Property>
        </Properties></Note>
      </Notes>
      <Rhythms><Rhythm id="0"><NoteValue>Quarter</NoteValue></Rhythm></Rhythms>
    </GPIF>"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Content/score.gpif", gpif)
        z.writestr("VERSION", "8.0")


def _result_for(beat, bar: int, key_root: int, chord: dict) -> dict:
    return {
        "bar": bar,
        "section": None,
        "window": "measure",
        "start_quarters": beat.start_quarters,
        "duration_quarters": beat.duration_quarters,
        "key": "C",
        "key_root": key_root,
        "key_mode": "Major",
        "anchor_beat_id": beat.id,
        "anchor_voice_id": beat.voice_id,
        "anchor_pos": beat.position_in_voice,
        "notes": [],
        "chord": chord,
        "manual": None,
    }


def test_write_freetext_roman(tmp_path):
    gp = tmp_path / "mini.gp"
    _mini_gp(gp)
    song = parse_gp(gp)
    track = song.tracks[0]
    beat = track.measures[0].beats[0]
    results = [_result_for(beat, 1, 0, chord("Csus2", 0, "sus2"))]
    out = tmp_path / "mini_chords.gp"
    stats = write_chords_to_gp(str(gp), str(out), song, track, results, key_root=0)

    assert stats["written"] == 1
    assert stats["free_text_beats"] == 1
    with zipfile.ZipFile(out) as z:
        xml = z.read("Content/score.gpif").decode("utf-8")
    # CDATA 形式 + 与《春日影.gp》一致的位置（FreeText 在 Chord 前）
    assert "<FreeText><![CDATA[Isus2]]></FreeText><Chord><![CDATA[0]]></Chord>" in xml

    verify = parse_gp(out)
    vb = verify.tracks[0].measures[0].beats[0]
    assert vb.free_text == "Isus2"
    assert vb.chord.name == "Csus2"


def test_write_no_roman(tmp_path):
    gp = tmp_path / "mini.gp"
    _mini_gp(gp)
    song = parse_gp(gp)
    track = song.tracks[0]
    beat = track.measures[0].beats[0]
    results = [_result_for(beat, 1, 0, chord("C", 0, "maj"))]
    out = tmp_path / "mini_no_roman.gp"
    stats = write_chords_to_gp(
        str(gp), str(out), song, track, results, key_root=0, roman=False
    )
    assert stats["free_text_beats"] == 0
    with zipfile.ZipFile(out) as z:
        xml = z.read("Content/score.gpif").decode("utf-8")
    assert "<FreeText>" not in xml


def test_progression_label_multiline_and_shared_beat(tmp_path):
    """循环进行标注写两行 FreeText（进行 + 单拍罗马数字），共享拍克隆后
    和弦引用不丢失（回归：克隆的 beat 没登记进 beat_els，主写回循环
    找不到它 -> 和弦符号被顶替）。"""
    gpif = """<GPIF>
      <GPVersion>8.0</GPVersion>
      <Tracks><Track id="0"><Name>L</Name><Staves><Staff><Properties>
        <Property name="Tuning"><Pitches>40 45 50 55 59 64</Pitches></Property>
        <Property name="DiagramCollection"><Items /></Property>
      </Properties></Staff></Staves></Track></Tracks>
      <MasterBars>
        <MasterBar><Time>4/4</Time><Key><AccidentalCount>0</AccidentalCount>
          <Mode>Major</Mode></Key><Bars>0</Bars></MasterBar>
        <MasterBar><Time>4/4</Time><Key><AccidentalCount>0</AccidentalCount>
          <Mode>Major</Mode></Key><Bars>1</Bars></MasterBar>
      </MasterBars>
      <Bars>
        <Bar id="0"><Voices>0</Voices></Bar>
        <Bar id="1"><Voices>1</Voices></Bar>
      </Bars>
      <Voices>
        <Voice id="0"><Beats>0</Beats></Voice>
        <Voice id="1"><Beats>0</Beats></Voice>
      </Voices>
      <Beats>
        <Beat id="0"><Notes>0</Notes><Rhythm><ref>0</ref></Rhythm></Beat>
      </Beats>
      <Notes>
        <Note id="0"><Properties>
          <Property name="Midi"><Number>48</Number></Property>
          <Property name="Fret"><Fret>1</Fret></Property>
          <Property name="String"><String>3</String></Property>
        </Properties></Note>
      </Notes>
      <Rhythms><Rhythm id="0"><NoteValue>Quarter</NoteValue></Rhythm></Rhythms>
    </GPIF>"""
    gp = tmp_path / "shared.gp"
    with zipfile.ZipFile(gp, "w") as z:
        z.writestr("Content/score.gpif", gpif)
        z.writestr("VERSION", "8.0")

    song = parse_gp(gp)
    track = song.tracks[0]
    beat = track.measures[0].beats[0]
    results = [
        _result_for(beat, 1, 0, chord("Csus2", 0, "sus2"))
    ]
    out = tmp_path / "shared_chords.gp"
    stats = write_chords_to_gp(
        str(gp),
        str(out),
        song,
        track,
        results,
        key_root=0,
        progression_labels={1: "P1: I-IV-V-vi"},
        progression_romans={1: "Isus2"},
    )
    assert stats["written"] == 1
    verify = parse_gp(out)
    m1 = verify.tracks[0].measures[0]
    b1 = m1.beats[0]
    assert b1.chord is not None  # 共享拍克隆后和弦引用没丢
    assert b1.free_text == "P1: I-IV-V-vi\nIsus2"
    m2 = verify.tracks[0].measures[1]
    assert m2.beats[0].chord is None  # 另一个共享位置不被污染


def test_overwrite_keeps_progression_label(tmp_path):
    """--overwrite 时罗马数字写回不能顶掉刚写好的循环进行注解
    （回归：进行标注先写、和弦罗马后写，overwrite 直接替换 FreeText
    导致 P 行整行消失，谱面上只剩单拍罗马数字）。"""
    gp = tmp_path / "mini.gp"
    _mini_gp(gp)
    song = parse_gp(gp)
    track = song.tracks[0]
    beat = track.measures[0].beats[0]
    results = [_result_for(beat, 1, 0, chord("C", 0, "maj"))]
    out = tmp_path / "mini_overwrite.gp"
    stats = write_chords_to_gp(
        str(gp),
        str(out),
        song,
        track,
        results,
        key_root=0,
        overwrite=True,
        progression_labels={1: "P1: I-IV-V-vi"},
        progression_romans={1: "I"},
    )
    assert stats["written"] == 1
    verify = parse_gp(out)
    vb = verify.tracks[0].measures[0].beats[0]
    assert vb.chord is not None
    assert vb.free_text == "P1: I-IV-V-vi\nI"


def test_restore_cdata_wraps_new_freetext():
    from gpreader.writer import restore_cdata

    xml = (
        '<GPIF><Beats><Beat id="0">'
        "<FreeText>Isus2</FreeText><Chord>0</Chord>"
        "</Beat></Beats></GPIF>"
    )
    restored = restore_cdata(xml, [])
    assert "<FreeText><![CDATA[Isus2]]></FreeText>" in restored
    assert "<Chord><![CDATA[0]]></Chord>" in restored


def test_restore_cdata_does_not_double_wrap_freetext():
    from gpreader.writer import restore_cdata

    xml = (
        '<GPIF><Beats><Beat id="0">'
        "<FreeText><![CDATA[Isus2]]></FreeText>"
        "</Beat></Beats></GPIF>"
    )
    restored = restore_cdata(xml, [("FreeText", "Isus2", "", "")])
    assert restored.count("<![CDATA[") == 1


@pytest.mark.skipif(not HARUHIKAGE.exists(), reason="春日影.gp 不存在")
def test_sample_freetext_parsed():
    """参照文件里的手工注解 Isus2 应能被解析器读出。"""
    song = parse_gp(HARUHIKAGE)
    texts = [b.free_text for t in song.tracks for m in t.measures for b in m.beats]
    assert "Isus2" in texts


@pytest.mark.skipif(not HARUHIKAGE.exists(), reason="春日影.gp 不存在")
def test_write_roman_to_sample(tmp_path):
    """真实 GP8 文件上写罗马数字自由注解：B 大调 Bsus2 -> Isus2。"""
    song = parse_gp(HARUHIKAGE)
    track = next(t for t in song.tracks if t.name == "Rhythm Guitar")
    measure = next(m for m in track.measures if any(b.notes for b in m.beats))
    beat = next(b for b in measure.beats if b.notes)
    results = [_result_for(beat, measure.index, 11, chord("Bsus2", 11, "sus2"))]
    out = tmp_path / "haruhikage_roman.gp"
    stats = write_chords_to_gp(
        str(HARUHIKAGE), str(out), song, track, results,
        key_root=11, overwrite=True, roman=True,
    )
    assert stats["written"] == 1
    assert stats["free_text_beats"] >= 1
    verify = parse_gp(out)
    vt = next(t for t in verify.tracks if t.name == "Rhythm Guitar")
    texts = [b.free_text for m in vt.measures for b in m.beats if b.free_text]
    assert "Isus2" in texts
