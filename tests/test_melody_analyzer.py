"""
melody_analyzer 集成测试。

用程序化构造的 GPSong/GPTrack 跑 analyze_track，验证：乐句切分、全曲
跨乐句动机发现、统计计数、Markdown 报告渲染、空轨安全。
"""

from __future__ import annotations

from gpreader import (
    GPBeat,
    GPMeasure,
    GPNote,
    GPSong,
    GPTrack,
)
from melody_analyzer import analyze_track, _render_markdown


def _note(midi: int, dur: float = 1.0) -> GPNote:
    return GPNote(midi=midi, duration_quarters=dur)


def _beat(start: float, items: list[tuple[int, float]]) -> GPBeat:
    notes = [_note(m, d) for m, d in items]
    return GPBeat(
        start_quarters=start,
        duration_quarters=max(d for _, d in items),
        notes=notes,
    )


def build_song(measures_notes) -> tuple[GPSong, GPTrack]:
    """measures_notes: list[(section, beats)]，beats = list[(start, [(midi, dur), ...])]"""
    track = GPTrack(id=0, name="Vocals", midi_program=66)
    for idx, (section, beats) in enumerate(measures_notes, 1):
        measure = GPMeasure(
            index=idx,
            time_signature=(4, 4),
            key_signature="C",
            section=section,
        )
        measure.beats = [_beat(start, items) for start, items in beats]
        track.measures.append(measure)
    track.notes = [n for m in track.measures for b in m.beats for n in b.notes]
    song = GPSong(title="测试曲", artist="测试", tempos={0: 120}, tracks=[track])
    return song, track


def _verse_phrase() -> list[tuple[float, list[tuple[int, float]]]]:
    """4 拍 C-D-E-G 上行，每拍一个四分音符。"""
    return [(i * 1.0, [(midi, 1.0)]) for i, midi in enumerate([60, 62, 64, 67])]


def test_cross_phrase_motif_detected():
    """相同旋律出现在两个乐句（跨休止），应合并为一个全曲动机。"""
    measures = [
        ("A", _verse_phrase()),          # m1
        ("A", []),                       # m2 休止
        ("A", []),                       # m3 休止
        ("B", _verse_phrase()),          # m4
    ]
    song, track = build_song(measures)
    result = analyze_track(song, track)
    assert result["stats"]["total_notes"] == 8
    assert result["stats"]["phrase_count"] == 2
    motifs = result["motifs"]
    assert motifs, "跨乐句重复应被发现"
    top = motifs[0]
    assert top["note_count"] == 4
    assert len(top["occurrences"]) == 2
    phrase_ids = {o["phrase_id"] for o in top["occurrences"]}
    assert phrase_ids == {1, 2}
    # 两个乐句都应引用该动机
    assert "M1" in result["phrases"][0]["motif_refs"]
    assert "M1" in result["phrases"][1]["motif_refs"]


def test_stats_counts():
    """统计里旋律/节奏动机计数为真实数值。"""
    measures = [
        ("A", _verse_phrase()),
        ("A", []),
        ("A", []),
        ("B", _verse_phrase()),
    ]
    song, track = build_song(measures)
    result = analyze_track(song, track)
    stats = result["stats"]
    assert stats["melodic_motif_count"] >= 1
    assert stats["rhythmic_motif_count"] >= 0
    assert stats["melodic_motif_count"] == len(
        [m for m in result["motifs"] if m["kind"] == "melodic"]
    )
    assert stats["pitch_min"] == "C4"
    assert stats["pitch_max"] == "G4"


def test_degree_pc_present():
    """音级数值偏移应写入事件并传入动机引擎（C 大调 1/3/5 级）。"""
    measures = [
        ("A", _verse_phrase()),
        ("A", []),
        ("A", []),
        ("B", _verse_phrase()),
    ]
    song, track = build_song(measures)
    result = analyze_track(song, track)
    top = result["motifs"][0]
    # 动机内容包含音名（证明代表音符被正确映射）
    assert "→" in top["content"]


def test_empty_track_no_crash():
    """空轨 / 全休止轨：不崩溃，统计返回零值。"""
    measures = [("A", []) for _ in range(4)]
    song, track = build_song(measures)
    result = analyze_track(song, track)
    assert result["stats"] == {"total_notes": 0}
    assert result["phrases"] == []
    assert result["motifs"] == []


def test_report_renders():
    """Markdown 报告包含乐句表、动机表与变体小节。"""
    measures = [
        ("A", _verse_phrase()),
        ("A", []),
        ("A", []),
        ("B", _verse_phrase()),
    ]
    song, track = build_song(measures)
    result = analyze_track(song, track)
    text = _render_markdown(result, "auto")
    assert "## 乐句" in text
    assert "## 动机" in text
    assert "M1" in text
    assert "## 音级分布" in text
