"""
gp-sections 自动分段测试。

覆盖：MasterTrack 速度解析、<Section> 写回/跳过/覆盖与 CDATA 形式、
边界检测（密度跳变）、零方差特征排除、段落聚类命名（Intro/Part N）。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from gpreader import parse_gp
from gpreader.writer import restore_section_cdata
from gpchords.sections import (
    Boundary,
    SongFeatures,
    build_sections,
    detect_boundaries,
    extract_features,
    segment_similarity,
    write_sections_to_gp,
)

GPIF_TWO_BARS = """<GPIF>
<GPVersion>8.0</GPVersion>
<Tracks><Track id="0"><Name>L</Name><Staves><Staff><Properties>
<Property name="Tuning"><Pitches>40 45 50 55 59 64</Pitches></Property>
</Properties></Staff></Staves></Track></Tracks>
<MasterBars>
<MasterBar><Time>4/4</Time><Bars>0</Bars></MasterBar>
<MasterBar><Time>4/4</Time><Bars>0</Bars></MasterBar>
</MasterBars>
<Bars><Bar id="0"><Voices>0</Voices></Bar></Bars>
<Voices><Voice id="0"><Beats>0</Beats></Voice></Voices>
<Beats><Beat id="0"><Notes>0</Notes><Rhythm><ref>0</ref></Rhythm></Beat></Beats>
<Notes><Note id="0"><Properties><Property name="Midi"><Number>48</Number></Property>
</Properties></Note></Notes>
<Rhythms><Rhythm id="0"><NoteValue>Quarter</NoteValue></Rhythm></Rhythms>
</GPIF>"""

GPIF_WITH_TEMPO = GPIF_TWO_BARS.replace(
    "<Tracks>",
    "<MasterTrack><Tracks>0</Tracks><Automations><Automation>"
    "<Type>Tempo</Type><Linear>false</Linear><Bar>0</Bar><Position>0</Position>"
    "<Visible>true</Visible><Value>97 2</Value>"
    "</Automation></Automations></MasterTrack><Tracks>",
    1,
)

GPIF_FIRST_SECTIONED = GPIF_TWO_BARS.replace(
    "<MasterBar><Time>4/4</Time><Bars>0</Bars></MasterBar>",
    "<MasterBar><Time>4/4</Time><Section><Letter><![CDATA[A]]></Letter>"
    "<Text><![CDATA[Intro]]></Text></Section><Bars>0</Bars></MasterBar>",
    1,
)


def make_gp(tmp_path: Path, gpif_text: str = GPIF_TWO_BARS) -> Path:
    gp = tmp_path / "mini.gp"
    with zipfile.ZipFile(gp, "w") as z:
        z.writestr("Content/score.gpif", gpif_text)
        z.writestr("VERSION", "8.0")
    return gp


# ---------------------------------------------------------------------------
# 速度解析
# ---------------------------------------------------------------------------


def test_tempo_automation_parsed(tmp_path):
    song = parse_gp(make_gp(tmp_path, GPIF_WITH_TEMPO))
    assert song.tempos == {0: 97}
    assert song.tempo_at(0) == 97
    assert song.tempo_at(1) == 97  # 向前填充


# ---------------------------------------------------------------------------
# Section CDATA 写回
# ---------------------------------------------------------------------------


def test_restore_section_cdata_wraps_new_only():
    xml = (
        "<GPIF><MasterBars><MasterBar>"
        "<Section><Letter>A</Letter><Text>Part 1</Text></Section>"
        "</MasterBar></MasterBars></GPIF>"
    )
    out = restore_section_cdata(xml)
    assert "<Letter><![CDATA[A]]></Letter>" in out
    assert "<Text><![CDATA[Part 1]]></Text>" in out


def test_restore_section_cdata_keeps_existing():
    xml = (
        "<GPIF><MasterBars><MasterBar>"
        "<Section><Letter><![CDATA[A]]></Letter>"
        "<Text><![CDATA[Intro]]></Text></Section>"
        "</MasterBar></MasterBars></GPIF>"
    )
    out = restore_section_cdata(xml)
    assert out.count("<![CDATA[") == 2
    assert "<Section><Letter><![CDATA[A]]></Letter>" in out


def test_restore_section_cdata_ignores_lyrics_text():
    xml = (
        "<GPIF><Lyrics><Line><Text>words</Text></Line></Lyrics>"
        "<Section><Letter>B</Letter><Text>Part 1</Text></Section>"
        "</GPIF>"
    )
    out = restore_section_cdata(xml)
    assert "<Text>words</Text>" in out  # 歌词不被触碰
    assert "<Text><![CDATA[Part 1]]></Text>" in out


def test_write_sections_round_trip(tmp_path):
    gp = make_gp(tmp_path)
    from gpchords.sections import Section

    out = tmp_path / "mini_sections.gp"
    stats = write_sections_to_gp(
        gp,
        out,
        [
            Section(start_bar=1, end_bar=1, letter="A", text="Intro"),
            Section(start_bar=2, end_bar=2, letter="B", text="Part 1"),
        ],
    )
    assert stats["written"] == 2
    assert stats["verified_match"] == 2
    verify = parse_gp(out)
    measures = verify.tracks[0].measures
    assert measures[0].section == "A:Intro"
    assert measures[1].section == "B:Part 1"
    with zipfile.ZipFile(out) as z:
        xml = z.read("Content/score.gpif").decode("utf-8")
    assert "<Letter><![CDATA[A]]></Letter>" in xml
    assert "<Text><![CDATA[Part 1]]></Text>" in xml


def test_write_sections_skips_existing(tmp_path):
    gp = make_gp(tmp_path, GPIF_FIRST_SECTIONED)
    from gpchords.sections import Section

    out = tmp_path / "mini_skip.gp"
    stats = write_sections_to_gp(
        gp,
        out,
        [
            Section(start_bar=1, end_bar=1, letter="X", text="New"),
            Section(start_bar=2, end_bar=2, letter="B", text="Part 1"),
        ],
    )
    assert stats["written"] == 1
    assert stats["skipped"] == 1
    verify = parse_gp(out)
    assert verify.tracks[0].measures[0].section == "A:Intro"  # 原样保留
    assert verify.tracks[0].measures[1].section == "B:Part 1"


def test_write_sections_overwrite(tmp_path):
    gp = make_gp(tmp_path, GPIF_FIRST_SECTIONED)
    from gpchords.sections import Section

    out = tmp_path / "mini_over.gp"
    stats = write_sections_to_gp(
        gp,
        out,
        [Section(start_bar=1, end_bar=1, letter="X", text="New")],
        overwrite=True,
    )
    assert stats["written"] == 1
    verify = parse_gp(out)
    assert verify.tracks[0].measures[0].section == "X:New"


# ---------------------------------------------------------------------------
# 边界检测与聚类
# ---------------------------------------------------------------------------


def test_detect_boundaries_density_jump():
    """密度跳变（前 8 小节低、后 8 小节高）应检出第 9 小节边界。"""
    n = 16
    feat = SongFeatures(
        chords=[("I", "maj")] * n,
        density=[0.2] * 8 + [0.9] * 8,
        track_act=[frozenset({0})] * n,
        palm=[0.0] * n,
        harm_rhythm=[1.0] * n,
    )
    bounds = detect_boundaries(feat, L=4, gap=4, kthr=0.4)
    assert any(abs(b.bar - 9) <= 2 for b in bounds)


def test_constant_tempo_excluded():
    """全曲恒定速度不参与打分、不产生证据（零方差特征排除）。"""
    n = 12
    feat = SongFeatures(
        chords=[("I", "maj")] * n,
        density=[0.3] * 6 + [0.8] * 6,
        tempo=[97] * n,
    )
    bounds = detect_boundaries(feat, L=4, gap=4, kthr=0.4)
    assert not any("速度" in b.evidence for b in bounds)


def test_build_sections_letters_and_intro():
    """Intro（唯一且短）命名 Intro；两个相似段共享字母 Part 1/Part 2。"""
    intro = [("I", "maj"), ("V", "dom")] * 4  # 1-8
    verse1 = [("I", "maj"), ("IV", "maj"), ("V", "dom"), ("vi", "min")] * 2  # 9-16
    verse2 = list(verse1)  # 17-24，与 verse1 相同
    chords = intro + verse1 + verse2
    n = len(chords)
    feat = SongFeatures(
        chords=chords,
        density=[0.2] * 8 + [0.8] * (n - 8),
        track_act=[frozenset({0})] * n,
        palm=[0.0] * n,
        harm_rhythm=[1.0] * n,
    )
    sections = build_sections(
        [
            Boundary(bar=9, score=1.0, evidence=[]),
            Boundary(bar=17, score=1.0, evidence=[]),
        ],
        n,
        feat,
        min_bars=2,
        similarity=0.6,
    )
    assert [s.name for s in sections] == ["A:Intro", "B:Part 1", "B:Part 2"]


def test_segment_similarity_different_than_similar():
    intro = [("I", "maj"), ("V", "dom")] * 4
    verse = [("I", "maj"), ("IV", "maj"), ("V", "dom"), ("vi", "min")] * 2
    chords = intro + verse
    n = len(chords)
    feat = SongFeatures(
        chords=chords,
        density=[0.2] * 8 + [0.8] * 8,
        track_act=[frozenset({0})] * n,
        palm=[0.0] * n,
        harm_rhythm=[1.0] * n,
    )
    intro_verse = segment_similarity((1, 8), (9, 16), feat)
    same = segment_similarity((9, 16), (9, 16), feat)
    assert same > intro_verse
    assert same > 0.8
