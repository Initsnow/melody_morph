"""
gp-key 调性估计与调号写回测试。

覆盖：规范调号映射（Db 而非 C#、F# 而非 Gb）、MasterBar <Key> 写入顺序、
写回/保留/兜底行为、K-K 全局与分段估计、真实样例文件写回。
"""

from __future__ import annotations

import os
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from gpchords.annotate import parse_key_name
from gpchords.keywriter import (
    estimate_section_keys,
    estimate_song_key,
    set_key_signature,
    write_keys_to_gp,
)
from gpreader import (
    GPBeat,
    GPMeasure,
    GPNote,
    GPTrack,
    key_name,
    key_signature,
    parse_gp,
)

SAMPLE_FILE = Path(
    os.environ.get("GP_TEST_FILE", r"C:\Users\Initsnow\Documents\Audio\谱\无论如何 - 副本.gp")
)


def note(midi: int, dur: float = 1.0) -> GPNote:
    return GPNote(midi=midi, duration_quarters=dur)


def track_with_notes(midis: list[int], durs: list[float] | None = None) -> GPTrack:
    durs = durs or [1.0] * len(midis)
    beats = [
        GPBeat(
            id="b0",
            start_quarters=0.0,
            duration_quarters=1.0,
            notes=[note(m, d) for m, d in zip(midis, durs)],
            voice_id="v1",
            position_in_voice=0,
        )
    ]
    measure = GPMeasure(index=1, time_signature=(4, 4), beats=beats)
    t = GPTrack(id=0, name="T", tuning=[40, 45, 50, 55, 59, 64], measures=[measure])
    t.notes = list(measure.beats[0].notes)
    return t


# ---------------------------------------------------------------------------
# 规范调号映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "root,mode,expected",
    [
        (0, "Major", (0, "Major")),
        (7, "Major", (1, "Major")),
        (9, "Major", (3, "Major")),
        (11, "Major", (5, "Major")),
        (6, "Major", (6, "Major")),  # F# 而非 Gb
        (1, "Major", (-5, "Major")),  # Db 而非 C#
        (5, "Major", (-1, "Major")),
        (10, "Major", (-2, "Major")),
        (8, "Major", (-4, "Major")),
        (3, "Major", (-3, "Major")),
        (9, "Minor", (0, "Minor")),
        (4, "Minor", (1, "Minor")),
        (6, "Minor", (3, "Minor")),
        (8, "Minor", (5, "Minor")),  # G#m（5 升号）而非 Abm（7 降号）
        (2, "Minor", (-1, "Minor")),
        (7, "Minor", (-2, "Minor")),
        (10, "Minor", (-5, "Minor")),
    ],
)
def test_key_signature_canonical(root, mode, expected):
    assert key_signature(root, mode) == expected


def test_key_name_round_trip_all_roots():
    for root in range(12):
        for mode in ("Major", "Minor"):
            name = key_name(root, mode)
            assert parse_key_name(name) == (root, mode), (root, mode, name)


def test_key_element_inserted_before_bars():
    mb = ET.fromstring("<MasterBar><Time>4/4</Time><Bars>0</Bars></MasterBar>")
    assert set_key_signature(mb, 0, "Major")
    assert [c.tag for c in mb] == ["Time", "Key", "Bars"]
    assert mb.find("Key/AccidentalCount").text == "0"
    assert mb.find("Key/Mode").text == "Major"
    assert mb.find("Key/TransposeAs").text == "Sharps"


def test_set_key_signature_reports_no_change_when_same():
    mb = ET.fromstring(
        "<MasterBar><Key><AccidentalCount>1</AccidentalCount><Mode>Major</Mode>"
        "<TransposeAs>Sharps</TransposeAs></Key></MasterBar>"
    )
    assert not set_key_signature(mb, 7, "Major")


def test_set_key_signature_transpose_as_matches_key():
    flat = ET.fromstring("<MasterBar><Key></Key></MasterBar>")
    assert set_key_signature(flat, 5, "Major")  # F 大调：降号调
    assert flat.find("Key/TransposeAs").text == "Flats"
    # 只改 TransposeAs 也算实际变化：重跑可修复旧文件里的 Sharps 残留
    assert set_key_signature(flat, 5, "Major") is False
    sharp = ET.fromstring("<MasterBar><Key></Key></MasterBar>")
    assert set_key_signature(sharp, 7, "Major")  # G 大调：升号调
    assert sharp.find("Key/TransposeAs").text == "Sharps"


# ---------------------------------------------------------------------------
# 调性估计
# ---------------------------------------------------------------------------


def test_estimate_song_key_c_major():
    t = track_with_notes(
        [48, 52, 55, 60, 64, 67, 72],
        [2, 2, 2, 2, 1, 2, 1],
    )
    assert estimate_song_key([t]) == (0, "Major")


def test_estimate_song_key_empty_falls_back_c():
    t = track_with_notes([])
    assert estimate_song_key([t]) == (0, "Major")


def test_estimate_section_keys_groups_by_section():
    c_major = track_with_notes(
        [48, 52, 55, 60, 64, 67, 72],
        [2, 2, 2, 2, 1, 2, 1],
    )
    a_minor = track_with_notes(
        [45, 48, 52, 57, 60, 64],
        [2, 2, 2, 2, 1, 1],
    )
    c_major.measures[0].section = "A:Intro"
    a_minor.measures[0].section = "B:Chorus"
    keys = estimate_section_keys([c_major, a_minor])
    assert keys["A:Intro"] == (0, "Major")
    assert keys["B:Chorus"] == (9, "Minor")


# ---------------------------------------------------------------------------
# 写回 .gp
# ---------------------------------------------------------------------------


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

GPIF_TWO_BARS_FIRST_KEYED = GPIF_TWO_BARS.replace(
    "<MasterBar><Time>4/4</Time><Bars>0</Bars></MasterBar>",
    "<MasterBar><Time>4/4</Time><Key><AccidentalCount>1</AccidentalCount>"
    "<Mode>Major</Mode></Key><Bars>0</Bars></MasterBar>",
    1,
)

GPIF_NOTE_SPELLED = """<GPIF>
<GPVersion>8.0</GPVersion>
<Tracks><Track id="0"><Name>L</Name><Staves><Staff><Properties>
<Property name="Tuning"><Pitches>40 45 50 55 59 64</Pitches></Property>
</Properties></Staff></Staves></Track></Tracks>
<MasterBars><MasterBar><Time>4/4</Time><Bars>0</Bars></MasterBar></MasterBars>
<Bars><Bar id="0"><Voices>0</Voices></Bar></Bars>
<Voices><Voice id="0"><Beats>0</Beats></Voice></Voices>
<Beats><Beat id="0"><Notes>0</Notes><Rhythm><ref>0</ref></Rhythm></Beat></Beats>
<Notes><Note id="0"><Properties>
<Property name="ConcertPitch"><Pitch><Step>A</Step><Accidental>#</Accidental><Octave>4</Octave></Pitch></Property>
<Property name="Midi"><Number>58</Number></Property>
<Property name="TransposedPitch"><Pitch><Step>A</Step><Accidental>#</Accidental><Octave>5</Octave></Pitch></Property>
</Properties></Note></Notes>
<Rhythms><Rhythm id="0"><NoteValue>Quarter</NoteValue></Rhythm></Rhythms>
</GPIF>"""


def make_gp(tmp_path: Path, gpif_text: str = GPIF_TWO_BARS) -> Path:
    gp = tmp_path / "mini.gp"
    with zipfile.ZipFile(gp, "w") as z:
        z.writestr("Content/score.gpif", gpif_text)
        z.writestr("VERSION", "8.0")
    return gp


def keys_of(out: Path) -> list[str]:
    song = parse_gp(out)
    return [m.key_signature for m in song.tracks[0].measures]


def test_write_keys_global_with_overrides(tmp_path):
    gp = make_gp(tmp_path)
    out = tmp_path / "out.gp"
    stats = write_keys_to_gp(gp, out, {1: (9, "Major")}, default_key=(0, "Major"))
    assert stats["written"] == 2
    assert stats["bars"] == 2
    assert stats["verified_match"] == stats["verified_total"] == 2
    assert keys_of(out) == ["A", "C"]
    with zipfile.ZipFile(out) as z:
        assert "VERSION" in z.namelist()


def test_fill_only_keeps_existing(tmp_path):
    gp = make_gp(tmp_path, GPIF_TWO_BARS_FIRST_KEYED)
    out = tmp_path / "out.gp"
    stats = write_keys_to_gp(gp, out, {}, default_key=(0, "Major"), fill_only=True)
    assert stats["written"] == 1  # 只补第二小节
    assert stats["skipped"] == 1  # 第一小节已有调号 G
    assert stats["verified_match"] == stats["verified_total"] == 1
    assert keys_of(out) == ["G", "C"]


def _spelling(out: Path, prop: str) -> str:
    with zipfile.ZipFile(out) as z:
        root = ET.fromstring(z.read("Content/score.gpif"))
    note = root.find("Notes/Note")
    pitch = note.find(f'Properties/Property[@name="{prop}"]/Pitch')
    return "".join(
        (pitch.findtext(tag) or "")
        for tag in ("Step", "Accidental", "Octave")
    )


def test_flat_key_respells_sharp_notes(tmp_path):
    gp = make_gp(tmp_path, GPIF_NOTE_SPELLED)
    out = tmp_path / "out.gp"
    stats = write_keys_to_gp(gp, out, {}, default_key=(5, "Major"))  # F 大调
    assert stats["respell"] == 2
    assert _spelling(out, "ConcertPitch") == "Bb4"
    assert _spelling(out, "TransposedPitch") == "Bb5"
    assert keys_of(out) == ["F"]


def test_sharp_key_respells_flat_notes(tmp_path):
    gpif = GPIF_NOTE_SPELLED.replace(
        "<Step>A</Step><Accidental>#</Accidental>",
        "<Step>B</Step><Accidental>b</Accidental>",
    )
    gp = make_gp(tmp_path, gpif)
    out = tmp_path / "out.gp"
    stats = write_keys_to_gp(gp, out, {}, default_key=(7, "Major"))  # G 大调
    assert stats["respell"] == 2
    assert _spelling(out, "ConcertPitch") == "A#4"
    assert keys_of(out) == ["G"]


@pytest.mark.skipif(not SAMPLE_FILE.exists(), reason="样例文件不存在")
def test_write_keys_to_real_gp(tmp_path):
    out = tmp_path / "keyed.gp"
    stats = write_keys_to_gp(
        SAMPLE_FILE,
        out,
        {49: (9, "Major"), 50: (0, "Major"), 57: (9, "Major")},
        default_key=(9, "Major"),
    )
    assert stats["bars"] >= 57
    with zipfile.ZipFile(SAMPLE_FILE) as z:
        original_names = set(z.namelist())
    with zipfile.ZipFile(out) as z:
        assert set(z.namelist()) == original_names  # 资源逐项保留
    keys = {m.index: m.key_signature for m in parse_gp(out).tracks[0].measures}
    assert keys[49] == "A"
    assert keys[50] == "C"
    assert keys[57] == "A"
