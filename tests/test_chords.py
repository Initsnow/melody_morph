"""
gp-chords 和弦识别测试集（gp-chords-plan 第 1 步建立，后续步骤回归）。

覆盖：已知失败用例（Am7/C6、Dm7/F、C+F 混窗、G5+C、Cmaj7/E、Am9、
C7/Bb 拼写、跨窗延音）、模板扩充、auto 切窗、逐小节/逐段调性。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gpchords.annotate import (
    CHORD_TEMPLATES,
    DEGREES,
    _analyze_measures,
    _key_for_track,
    detect_chord,
    measure_key,
    merge_tracks,
    note_weights,
    reanchor_results,
    resolve_key,
    resolve_section_keys,
    resolve_write_tracks,
    segment_auto,
    segment_measure,
)
from gpreader import (
    GPSong,
    GPBeat,
    GPMeasure,
    GPNote,
    GPTrack,
    GuitarProError,
    parse_gp,
    select_tracks,
)
from gpchords.annotate import SEGMENTERS

SAMPLE_FILE = Path(
    os.environ.get("GP_TEST_FILE", r"C:\Users\Initsnow\Documents\Audio\谱\无论如何 - 副本.gp")
)


def note(
    midi: int,
    dur: float = 1.0,
    tie_origin=False,
    tie_destination=False,
    muted=False,
    palm_muted=False,
) -> GPNote:
    return GPNote(
        midi=midi,
        duration_quarters=dur,
        tie_origin=tie_origin,
        tie_destination=tie_destination,
        muted=muted,
        palm_muted=palm_muted,
    )


def detect(
    midis: list[int],
    key_root=None,
    key_mode: str = "Major",
    style: str = "guitar",
    durs: list[float] | None = None,
):
    notes = [note(m, d) for m, d in zip(midis, durs or [1.0] * len(midis))]
    return detect_chord(notes, key_root, key_mode, style)


def beat(start: float, items: list[tuple[int, float]]) -> GPBeat:
    notes = [note(m, d) for m, d in items]
    return GPBeat(
        id=f"b{start}",
        start_quarters=start,
        duration_quarters=max(d for _, d in items),
        notes=notes,
        voice_id="v1",
        position_in_voice=0,
    )


def measure(index: int, beats: list[GPBeat], key: str | None = None) -> GPMeasure:
    return GPMeasure(index=index, time_signature=(4, 4), key_signature=key, beats=beats)


def track(id_: int, name: str, measures: list[GPMeasure]) -> GPTrack:
    t = GPTrack(id=id_, name=name, tuning=[40, 45, 50, 55, 59, 64])
    t.measures = measures
    for m in measures:
        for b in m.beats:
            t.notes.extend(b.notes)
    return t


def make_song() -> GPSong:
    drum = track(0, "Drum", [measure(1, [beat(0.0, [(36, 1.0)])])])
    drum.midi_program = 0  # 鼓组
    silent = track(1, "Silent", [measure(1, [])])
    guitar = track(2, "Guitar", [measure(1, [beat(0.0, [(48, 1.0), (52, 1.0)])])])
    bass = track(3, "Bass", [measure(1, [beat(0.0, [(43, 1.0)])])])
    return GPSong(tracks=[drum, silent, guitar, bass])


def names(segments: list) -> list[str]:
    out = []
    for seg in segments:
        r = detect_chord(seg.notes, 0, "Major", "guitar")
        out.append(r["name"] if r else None)
    return out


# ---------------------------------------------------------------------------


def test_select_tracks_multi_and_all():
    song = make_song()
    assert [t.id for t in select_tracks(song, ["2,3"])] == [2, 3]
    # all：排除鼓组（MIDI Program 0）与无音符轨道
    assert [t.id for t in select_tracks(song, ["all"])] == [2, 3]
    # 重复选择去重
    assert [t.id for t in select_tracks(song, ["2", "2,3"])] == [2, 3]
    with pytest.raises(GuitarProError):
        select_tracks(song, ["不存在的轨道"])


def test_merge_tracks_combines_simultaneous_notes():
    # 吉他只弹 C+E（大三度双音无法定和弦），贝斯补 G 后才能识别为 C/G
    g = track(0, "Guitar", [measure(1, [beat(0.0, [(48, 1.0), (52, 1.0)])])])
    b = track(4, "Bass", [measure(1, [beat(0.0, [(43, 2.0)])])])
    merged = merge_tracks([g, b])
    assert merged is not g
    mb = merged.measures[0].beats[0]
    assert sorted(n.midi for n in mb.notes) == [43, 48, 52]
    assert mb.duration_quarters == 2.0  # 时值取最长
    r = detect_chord(mb.notes, 0, "Major", "guitar")
    assert r is not None and r["name"] == "C/G"
    # 锚点取自 primary（吉他轨的拍）
    assert mb.voice_id == "v1" and mb.position_in_voice == 0


def test_merge_tracks_keeps_separate_positions():
    g = track(
        0, "Guitar",
        [measure(1, [beat(0.0, [(48, 1.0)]), beat(0.5, [(50, 1.0)])])],
    )
    b = track(4, "Bass", [measure(1, [beat(0.0, [(43, 2.0)])])])
    merged = merge_tracks([g, b])
    beats = merged.measures[0].beats
    assert [x.start_quarters for x in beats] == [0.0, 0.5]
    assert [len(x.notes) for x in beats] == [2, 1]


def test_merge_tracks_borrows_anchor_when_primary_silent():
    g = track(0, "Guitar", [measure(1, [beat(2.0, [(48, 1.0)])])])
    b = track(4, "Bass", [measure(1, [beat(0.0, [(43, 1.0)])])])
    merged = merge_tracks([g, b])
    beats = merged.measures[0].beats
    assert [x.start_quarters for x in beats] == [0.0, 2.0]
    assert beats[0].voice_id == "v1"  # primary 该位置无拍，借用贝斯拍信息


def test_reanchor_results_maps_to_target_track():
    primary = track(0, "G", [measure(1, [beat(0.0, [(48, 1.0)]), beat(2.0, [(50, 1.0)])])])
    secondary = track(
        4, "B",
        [measure(1, [beat(0.0, [(43, 1.0)])]), measure(2, [beat(0.0, [(45, 1.0)])])],
    )
    results = [
        {"bar": 1, "start_quarters": 0.0, "duration_quarters": 1.0},
        {"bar": 1, "start_quarters": 2.0, "duration_quarters": 1.0},
        {"bar": 2, "start_quarters": 0.0, "duration_quarters": 1.0},
    ]
    out = reanchor_results(results, secondary)
    assert out[0]["anchor_voice_id"] == "v1" and out[0]["anchor_pos"] == 0
    assert out[1]["anchor_voice_id"] is None  # 目标轨小节 1 在 2.0 没有音符
    assert out[2]["anchor_pos"] == 0


def test_resolve_write_tracks():
    song = make_song()
    analyzed = [song.tracks[2], song.tracks[3]]
    assert [t.id for t in resolve_write_tracks(song, analyzed, None, default_all=True)] == [2, 3]
    assert [t.id for t in resolve_write_tracks(song, analyzed, None, default_all=False)] == [2]
    assert [t.id for t in resolve_write_tracks(song, analyzed, ["all"], default_all=False)] == [2, 3]
    with pytest.raises(GuitarProError):
        resolve_write_tracks(song, analyzed, ["0"], default_all=True)  # 鼓轨未分析


def test_analyze_measures_flow():
    from types import SimpleNamespace

    t = track(0, "G", [measure(1, [beat(0.0, [(48, 2.0), (52, 2.0), (55, 2.0)])])])
    args = SimpleNamespace(
        min_notes=1, style="guitar", key=None, key_per_section=False
    )
    _, keys = _key_for_track(None, t, args)
    results = _analyze_measures(t, keys, SEGMENTERS["measure"], args)
    assert results and results[0]["chord"]["name"] == "C"
# 第 2 步：已知失败用例 + 调性极小破平
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "midis,key,expected",
    [
        ([45, 48, 52, 55, 57], (9, "Minor"), "Am7"),  # Am7（低音 A）不得判成 C6/A
        ([41, 50, 57, 60], (2, "Minor"), "Dm7/F"),  # 转位 Dm7/F 不得判成 F6
        ([40, 48, 55, 59], (0, "Major"), "Cmaj7/E"),  # Cmaj7/E 不得判成 Em7/C
        ([45, 48, 52, 55, 59], (9, "Minor"), "Am9"),
        ([46, 48, 52, 55], (0, "Major"), "C7/Bb"),  # 斜杠低音拼写：A# -> Bb
        ([46, 48, 52, 55], (5, "Major"), "C7/Bb"),
        ([48, 52, 55], (0, "Major"), "C"),  # 基本三和弦回归
        ([45, 52, 57], (9, "Major"), "A5"),  # 强力和弦
        ([43, 60, 67], (0, "Major"), "C5/G"),  # 斜杠强力和弦
        ([53, 60, 64, 69], (0, "Major"), "Fmaj7"),
    ],
)
def test_detect_chord_core(midis, key, expected):
    result = detect(midis, key_root=key[0], key_mode=key[1])
    assert result["name"] == expected


@pytest.mark.parametrize("midis", [[48], [50], [52], [55]])
def test_single_note_not_detected(midis):
    # 单音无法确定和弦：不得硬猜成 C5 / C5/G 之类
    assert detect(midis, key_root=0, key_mode="Major") is None


@pytest.mark.parametrize(
    "midis",
    [
        [48, 58],  # 小七度（C+Bb）
        [48, 50],  # 大二度（C+D）
        [48, 54],  # 增四度（C+F#）
    ],
)
def test_non_power_dyad_not_detected(midis):
    # 二度/七度/增四度双音是和弦碎片或经过音：不得硬猜成和弦
    assert detect(midis, key_root=0, key_mode="Major") is None


@pytest.mark.parametrize(
    "midis,expected",
    [
        ([48, 52], "C(no5)"),  # 大三度：根音+三度定性质
        ([52, 55], "Em(no5)"),  # 小三度
        ([48, 51], "Cm(no5)"),
        ([52, 60], "C(no5)/E"),  # 转位：三度在低音时斜杠
    ],
)
def test_third_dyad_detected(midis, expected):
    result = detect(midis, key_root=0, key_mode="Major")
    assert result is not None and result["name"] == expected


@pytest.mark.parametrize(
    "midis,expected",
    [
        ([48, 55], "C5"),  # 纯五度
        ([43, 50], "G5"),
        ([48, 53], "F5/C"),  # 反向纯五度（F-C），低音 C 的强力转位
    ],
)
def test_power_dyad_detected(midis, expected):
    result = detect(midis, key_root=0, key_mode="Major")
    assert result is not None
    assert result["name"] == expected


def test_tonic_bonus_removed_am7_in_a_major():
    # 主音加分已删除：A 大调内低音 A 的 Am7 仍应是 Am7
    result = detect([45, 48, 52, 55, 57], key_root=9, key_mode="Major")
    assert result["name"] == "Am7"


# ---------------------------------------------------------------------------
# 第 2 步：真实时值 / 延音计权
# ---------------------------------------------------------------------------


def test_note_weights_real_duration_no_floor():
    # 十六分音符不再与四分音符同权
    w = note_weights([note(48, 1.0), note(50, 0.25), note(52, 0.0)])
    assert w[0] == pytest.approx(1.0)
    assert w[2] == pytest.approx(0.25)
    assert w[4] == pytest.approx(0.0)


def test_tie_destination_not_double_counted():
    # 延音延续音符不重复计权，时长并入延音起点
    notes = [
        note(48, 2.0, tie_origin=True),
        note(48, 1.5, tie_destination=True),
    ]
    w = note_weights(notes)
    assert w[0] == pytest.approx(3.5)


def test_cross_window_tie_fallback():
    # 跨窗延音：整窗只有延音延续音符（承接窗）时按实际时值计权，
    # 识别出延续中的和弦而不崩溃。
    notes = [
        note(48, 2.0, tie_destination=True),
        note(52, 2.0, tie_destination=True),
        note(55, 2.0, tie_destination=True),
    ]
    w = note_weights(notes)
    assert w == {0: 2.0, 4: 2.0, 7: 2.0}
    result = detect_chord(notes, 0, "Major", "guitar")
    assert result["name"] == "C"


def test_cross_window_tie_in_mixed_window_counted():
    # A3 从上一小节延进来（tie destination），同窗还有其他音符：
    # 延音目标必须计权（A 踏板不能丢），否则 {G,B,C} 会被误判成
    # Gadd11(no5)/A，而正确结果是 A 根音的 Am9。
    notes = [
        note(45, 0.5, tie_destination=True),  # A3 延音
        note(55, 0.5), note(60, 0.5), note(55, 0.5),  # G4 C5 G4
        note(59, 0.5), note(60, 0.5), note(55, 0.5), note(60, 0.5),  # B4 C5 G4 C5
    ]
    w = note_weights(notes)
    assert w[9] == pytest.approx(0.5)
    r = detect_chord(notes, 0, "Major", "guitar")
    assert r is not None and r["name"] == "Am9"


def test_muted_dead_notes_excluded_from_weights():
    # X 哑音的 MIDI 只是"若不制音会发出的音"，没有实际音高，不计权
    w = note_weights([note(48, 1.0), note(50, 0.5, muted=True)])
    assert w == {0: 1.0}


def test_detect_chord_ignores_dead_notes_including_bass():
    # 哑音不参与低音判定：A2 哑音不能把 C 和弦拉成 A 斜杠和弦
    notes = [
        note(48, 1.0),  # C3
        note(52, 1.0),  # E3
        note(55, 1.0),  # G3
        note(45, 0.5, muted=True),  # A2 X 哑音
    ]
    r = detect_chord(notes, 0, "Major", "guitar")
    assert r is not None
    assert r["name"] == "C"
    assert r["bass_pc"] == 0


def test_palm_muted_notes_still_counted():
    # P.M. 闷音音高明确，就是正在演奏的和声，必须正常参与识别
    w = note_weights([note(48, 1.0, palm_muted=True), note(52, 1.0, palm_muted=True)])
    assert w == {0: 1.0, 4: 1.0}
    r = detect_chord(
        [note(48, 1.0, palm_muted=True), note(55, 1.0, palm_muted=True)],
        0,
        "Major",
        "guitar",
    )
    assert r is not None and r["name"] == "C5"


def test_segment_measure_filters_dead_notes():
    m = measure(
        1,
        [
            beat(0.0, [(48, 1.0), (52, 1.0)]),
            beat(1.0, [(45, 0.5), (50, 0.5), (55, 0.5)]),  # 纯哑音拍
        ],
    )
    # 把第二个拍替换成纯哑音拍
    m.beats[1] = GPBeat(
        id="bx",
        start_quarters=1.0,
        duration_quarters=0.5,
        notes=[note(45, 0.5, muted=True), note(50, 0.5, muted=True)],
        voice_id="v1",
        position_in_voice=1,
    )
    segs = segment_measure(m)
    assert len(segs) == 1
    assert all(not n.muted for n in segs[0].notes)
    assert [n.midi for n in segs[0].notes] == [48, 52]


def test_auto_window_skips_muted_only_beats():
    # 两个 C5 拍之间夹一个 X 哑音拍：纯打击效果不产生窗口、不切断分组
    m = measure(
        1,
        [
            beat(0.0, [(48, 0.5), (55, 0.5)]),
            beat(0.5, [(45, 0.5)]),  # 哑音，稍后替换
            beat(1.0, [(48, 0.5), (55, 0.5)]),
        ],
    )
    m.beats[1] = GPBeat(
        id="bx",
        start_quarters=0.5,
        duration_quarters=0.5,
        notes=[note(45, 0.5, muted=True)],
        voice_id="v1",
        position_in_voice=1,
    )
    segs = segment_auto(m)
    assert len(segs) == 1
    assert [n.midi for n in segs[0].notes] == [48, 55, 48, 55]
    r = detect_chord(segs[0].notes, 0, "Major", "guitar")
    assert r is not None and r["name"] == "C5"


# ---------------------------------------------------------------------------
# 第 3 步：auto 切窗
# ---------------------------------------------------------------------------


def test_auto_split_c_f_one_measure():
    # 一小节两和弦（C + F）切成两个窗口
    m = measure(
        1,
        [
            beat(0.0, [(48, 2.0), (52, 2.0), (55, 2.0)]),
            beat(2.0, [(53, 2.0), (57, 2.0), (60, 2.0)]),
        ],
    )
    segs = segment_auto(m)
    assert len(segs) == 2
    assert names(segs) == ["C", "F"]


def test_auto_split_g5_c_one_measure():
    m = measure(
        1,
        [
            beat(0.0, [(43, 2.0), (50, 2.0)]),
            beat(2.0, [(48, 2.0), (52, 2.0), (55, 2.0)]),
        ],
    )
    segs = segment_auto(m)
    assert len(segs) == 2
    assert names(segs) == ["G5", "C"]


def test_auto_merge_arpeggio():
    # 同和弦琶音尾（与首拍互为子集）合并成一个窗口
    m = measure(
        1,
        [
            beat(0.0, [(48, 1.0), (52, 1.0), (55, 1.0), (59, 1.0)]),
            beat(1.0, [(52, 1.0), (55, 1.0), (59, 1.0)]),
            beat(2.0, [(55, 1.0), (59, 1.0)]),
            beat(3.0, [(59, 1.0)]),
        ],
    )
    segs = segment_auto(m)
    assert len(segs) == 1
    assert names(segs) == ["Cmaj7"]


@pytest.mark.parametrize("seq", [[0, 4, 7, 11], [0, 7, 11, 4]])
def test_auto_merge_one_note_arpeggio(seq):
    # 逐音琶音（每拍一个音）合成一窗，识别成完整和弦而非一堆 X5
    m = measure(
        1,
        [beat(float(i), [(48 + pc, 1.0)]) for i, pc in enumerate(seq)],
    )
    segs = segment_auto(m)
    assert len(segs) == 1
    assert names(segs) == ["Cmaj7"]


def test_auto_scale_run_stays_split_and_unlabeled():
    # 音阶跑动因级进不合并；切开的单音窗不产生垃圾和弦标签
    m = measure(
        1,
        [beat(float(i), [(48 + pc, 1.0)]) for i, pc in enumerate([0, 2, 4, 5, 7])],
    )
    segs = segment_auto(m)
    assert len(segs) == 5
    assert all(r is None for r in names(segs))


def test_auto_keeps_real_chord_change_after_chord():
    # C 和弦之后接 G5 双音是真实和弦变化，不得误并入 Cadd9
    m = measure(
        1,
        [
            beat(0.0, [(48, 2.0), (52, 2.0), (55, 2.0)]),
            beat(2.0, [(43, 2.0), (50, 2.0)]),
        ],
    )
    segs = segment_auto(m)
    assert len(segs) == 2
    assert names(segs) == ["C", "G5"]


def test_auto_absorb_tail_passing_chord():
    # 尾拍 16 分经过和弦（权重 <20%）并入主窗口：整小节 -> G6/9/E
    m = measure(
        1,
        [
            beat(0.0, [(40, 3.5), (47, 3.5), (52, 3.5), (55, 3.5), (59, 3.5), (64, 3.5)]),
            beat(3.5, [(50, 0.25), (57, 0.25), (62, 0.25), (65, 0.25)]),
            beat(3.75, [(50, 0.25), (57, 0.25), (62, 0.25), (65, 0.25)]),
        ],
    )
    segs = segment_auto(m)
    assert len(segs) == 1
    assert names(segs) == ["G6/9/E"]


def test_auto_anticipation_tied_into_next_bar_not_absorbed():
    # 小节末 tie 进下一小节的先现音（anticipation）是下一小节和弦的起点，
    # 不得被吸收进本小节窗口：A-E 主和弦 + B-F#-B 抢拍应切成 A5 + B5
    main = [GPNote(midi=m, duration_quarters=0.5) for m in (45, 52, 57)]  # A E A
    anti = [GPNote(midi=m, duration_quarters=0.5, tie_origin=True) for m in (47, 54, 59)]  # B F# B
    beats = []
    for i in range(6):
        beats.append(GPBeat(id=f"m{i}", start_quarters=i * 0.5, duration_quarters=0.5,
                            notes=[GPNote(midi=m, duration_quarters=0.5) for m in (45, 52, 57)],
                            voice_id="v", position_in_voice=i))
    beats.append(GPBeat(id="a", start_quarters=3.5, duration_quarters=0.5,
                        notes=anti, voice_id="v", position_in_voice=6))
    m = measure(1, beats)
    segs = segment_auto(m)
    assert len(segs) == 2
    assert names(segs) == ["A5", "B5"]


def test_auto_anticipation_not_tied_matches_next_bar():
    # 不延音的先现音：小节末 16 分尾组与下一小节首组同根音（Dm），
    # 同样保留为独立窗口，不并入主窗口
    m = measure(
        1,
        [
            beat(0.0, [(40, 3.5), (47, 3.5), (52, 3.5), (55, 3.5), (59, 3.5), (64, 3.5)]),
            beat(3.5, [(50, 0.25), (57, 0.25), (62, 0.25), (65, 0.25)]),
            beat(3.75, [(50, 0.25), (57, 0.25), (62, 0.25), (65, 0.25)]),
        ],
    )
    nxt = measure(
        2,
        [beat(0.0, [(50, 0.5), (57, 0.5), (62, 0.5), (65, 0.5)])],
    )
    segs = segment_auto(m, nxt)
    assert len(segs) == 2
    assert names(segs) == ["Em", "Dm"]


def test_auto_leading_single_note_merges_into_stable_chord():
    # Fsus2 琶音 G-F-C-F：开头的挂留单音 G 并入已成形的 F-C-F，
    # 整小节识别为 Fsus2（不再切成单音窗 + F5）
    m = measure(
        1,
        [
            beat(0.0, [(79, 1.0)]),  # G6
            beat(1.0, [(77, 1.0)]),  # F6
            beat(2.0, [(84, 1.0)]),  # C7
            beat(3.0, [(77, 1.0)]),  # F6
        ],
    )
    segs = segment_auto(m)
    assert len(segs) == 1
    r = detect_chord(segs[0].notes, 9, "Major", "guitar")  # 样例实际调性 A
    assert r["name"] == "Fsus2"


def test_auto_normal_tail_16th_still_absorbed():
    # 没有下一小节上下文时，16 分经过音仍按 20% 规则吸收（回归保护）
    m = measure(
        1,
        [
            beat(0.0, [(48, 3.5), (52, 3.5), (55, 3.5)]),
            beat(3.5, [(50, 0.25), (57, 0.25), (62, 0.25), (65, 0.25)]),
        ],
    )
    segs = segment_auto(m)
    assert len(segs) == 1


def test_auto_keeps_fixed_windows():
    from gpchords.annotate import SEGMENTERS

    assert "auto" in SEGMENTERS
    assert {"measure", "half", "beat"} <= set(SEGMENTERS)


# ---------------------------------------------------------------------------
# 第 4 步：模板扩充（参照 pychord，去别名、对齐 GP 记法）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quality,midis,expected",
    [
        ("7b5", [48, 52, 54, 58], "C7b5"),
        ("7#5", [48, 52, 56, 58], "C7#5"),
        ("7b9", [48, 52, 55, 58, 49], "C7b9"),
        ("7#9", [48, 52, 55, 58, 51], "C7#9"),
        ("9sus4", [48, 53, 55, 58, 50], "C9sus4"),
        ("7#11", [48, 52, 55, 58, 54], "C7#11"),
        ("9#11", [48, 52, 55, 58, 50, 54], "C9#11"),
        ("maj7#11", [48, 52, 55, 59, 54], "Cmaj7#11"),
        ("maj7#5", [48, 52, 56, 59], "Cmaj7#5"),
        ("maj7sus2", [48, 50, 55, 59], "Cmaj7sus2"),
        ("add11", [48, 52, 53, 55], "Cadd11"),
        ("add11(no5)", [48, 52, 53], "Cadd11(no5)"),
        ("madd4", [48, 51, 53, 55], "Cmadd4"),
        ("madd11(no5)", [48, 51, 53], "Cmadd11(no5)"),
        ("mmaj7", [48, 51, 55, 59], "Cmmaj7"),
        ("m6/9", [48, 51, 55, 57, 50], "Cm6/9"),
        ("11", [48, 52, 55, 58, 50, 53], "C11"),
        ("m11", [48, 51, 55, 58, 50, 53], "Cm11"),
        ("13", [48, 52, 55, 58, 50, 57, 53], "C13"),
        ("maj13", [48, 52, 55, 59, 50, 57, 53], "Cmaj13"),
    ],
)
def test_expanded_templates(quality, midis, expected):
    assert quality in CHORD_TEMPLATES
    assert quality in DEGREES  # GPIF 写回需要
    result = detect(midis, key_root=0, key_mode="Major")
    assert result["name"] == expected


def test_13_requires_eleventh():
    # 既没有 11 音也没有 13 音时不得叫 13（应退回 9）
    result = detect([48, 52, 55, 58, 50], key_root=0, key_mode="Major")
    assert result["name"] == "C9"


def test_add11_no5_arpeggio_and_omitted_fifth():
    # 主音琶音 F-C-E-C：C 当根音、F 在低音 -> Cadd11(no5)/F（而非 F5）
    r = detect([53, 60, 64, 60], key_root=0, key_mode="Major")
    assert r is not None and r["name"] == "Cadd11(no5)/F"
    # 写回时第五度标记 omitted=true（与 GP8 原生 no5 记法一致）
    from gpchords.annotate import _build_chord_item

    item = _build_chord_item(0, r, [40, 45, 50, 55, 59, 64], 0)
    degs = {
        (d.get("interval"), d.get("alteration")): d.get("omitted")
        for d in item.findall("Chord/Degree")
    }
    assert degs[("Fifth", "Perfect")] == "true"
    assert degs[("Eleventh", "Perfect")] == "false"


def test_add11_no5_does_not_steal_larger_arpeggio():
    # A-G-C-G-B-C-G（A 踏板，B 是 9 音）：{G,B,C} 只是子集，
    # 不得判成 Gadd11(no5)/A，应保持 A 根音（Am9）。
    r = detect([45, 55, 60, 55, 59, 60, 55, 45], key_root=0, key_mode="Major")
    assert r is not None and r["name"] == "Am9"


def test_maj7_no3_preferred_over_gadd11_no5_when_bass_is_root():
    # C-G-B（C 低音）：和声是 C 根音（Cmaj7 缺三音），
    # 不得判成 Gadd11(no5)/C；写回时三度标记 omitted=true。
    r = detect([48, 55, 59], key_root=0, key_mode="Major")
    assert r is not None and r["name"] == "Cmaj7(no3)"
    from gpchords.annotate import _build_chord_item

    item = _build_chord_item(0, r, [40, 45, 50, 55, 59, 64], 0)
    degs = {
        (d.get("interval"), d.get("alteration")): d.get("omitted")
        for d in item.findall("Chord/Degree")
    }
    assert degs[("Third", "Major")] == "true"
    assert degs[("Fifth", "Perfect")] == "false"
    assert degs[("Seventh", "Major")] == "false"


def test_m7_no3():
    # C-G-Bb（C 低音）-> Cm7(no3)
    r = detect([48, 55, 58], key_root=0, key_mode="Major")
    assert r is not None and r["name"] == "Cm7(no3)"


def test_slash_bass_double_sharp_spelled_naturally():
    # E7#9 的 #9 低音是 G（理论度数拼写 F##），GP 记法应写成 E7#9/G
    result = detect([43, 52, 56, 59, 62], key_root=9, key_mode="Major")
    assert result is not None and result["name"] == "E7#9/G"


def test_slash_bass_flat_degree_kept():
    # 单降/单升度数拼写保留（C7/Bb 不因新规则回退成 A#）
    result = detect([46, 48, 52, 55], key_root=0, key_mode="Major")
    assert result["name"] == "C7/Bb"


def test_no_aliases_duplicated():
    # 去别名：不保留 M7/69/mM7 等重复写法
    suffixes = [s for _, s in CHORD_TEMPLATES.values()]
    assert len(set(suffixes)) == len(suffixes)
    assert "M7" not in CHORD_TEMPLATES
    assert "69" not in CHORD_TEMPLATES
    assert "mM7" not in CHORD_TEMPLATES


def test_degrees_use_gp8_native_alterations():
    # GP8 原生文件的惯例：9/11/13 度的无变化写法是 Perfect，
    # b9 是 Diminished、#9 是 Augmented。写成 Major/Minor 会让
    # GP8 把构成音显示成默认的 C（实测 F#9sus4 / Eadd9 的九音显示成 C）。
    assert ("Ninth", "Perfect") in DEGREES["add9"]
    assert ("Ninth", "Perfect") in DEGREES["maj9"]
    assert ("Ninth", "Perfect") in DEGREES["m9"]
    assert ("Ninth", "Perfect") in DEGREES["9sus4"]
    assert ("Ninth", "Diminished") in DEGREES["7b9"]
    assert ("Ninth", "Augmented") in DEGREES["7#9"]
    assert ("Eleventh", "Perfect") in DEGREES["11"]
    assert ("Thirteenth", "Perfect") in DEGREES["13"]
    assert ("Thirteenth", "Perfect") in DEGREES["maj13"]
    for quality, degs in DEGREES.items():
        for interval, alteration in degs:
            assert alteration not in ("Major", "Minor") or interval not in (
                "Ninth",
                "Eleventh",
                "Thirteenth",
            ), (quality, interval, alteration)


def test_restore_cdata_multiline_gp8_text():
    # GP8 原生文本（段落标记的 Letter/Text 等）CDATA 前后带换行，
    # 旧正则漏掉，写回后变成普通文本被 GP8 静默丢弃。
    import xml.etree.ElementTree as ET

    from gpreader.writer import cdata_pairs_from, restore_cdata

    original = (
        "<GPIF><MasterBars><MasterBar>"
        "<Section>\n<Letter>\n<![CDATA[A]]>\n</Letter>\n"
        "<Text>\n<![CDATA[Intro 1]]>\n</Text>\n</Section>"
        "</MasterBar></MasterBars></GPIF>"
    )
    pairs = cdata_pairs_from(original)
    assert ("Letter", "A", "\n", "\n") in pairs
    assert ("Text", "Intro 1", "\n", "\n") in pairs
    serialized = ET.tostring(ET.fromstring(original), encoding="unicode")
    assert "<![CDATA[" not in serialized
    restored = restore_cdata(serialized, pairs)
    assert "<Letter>\n<![CDATA[A]]>\n</Letter>" in restored
    assert "<Text>\n<![CDATA[Intro 1]]>\n</Text>" in restored


# ---------------------------------------------------------------------------
# 第 5 步：逐小节调性 / 转调
# ---------------------------------------------------------------------------


def test_measure_key_prefers_own_signature():
    m = measure(1, [], key="C")
    assert measure_key(m, (9, "Major")) == (0, "Major")
    m2 = measure(2, [])
    assert measure_key(m2, (9, "Major")) == (9, "Major")


@pytest.mark.skipif(not SAMPLE_FILE.exists(), reason="样例文件不存在")
def test_sample_file_modulation_key_signatures():
    song = parse_gp(SAMPLE_FILE)
    track = song.tracks[1]
    keys = {m.index: m.key_signature for m in track.measures}
    assert keys[49] == "A"
    assert keys[50] == "C"
    assert keys[57] == "A"


@pytest.mark.skipif(not SAMPLE_FILE.exists(), reason="样例文件不存在")
def test_sample_file_acceptance_bars():
    """
    验收：C 段小节 51 -> 主窗口 Em + 先现音 Dm（整小节视图 G6/9/E），
    53/56 -> Cmaj7。
    """
    song = parse_gp(SAMPLE_FILE)
    track = song.tracks[1]
    gk = resolve_key(song, track, None)
    by_bar = {m.index: measure_key(m, gk) for m in track.measures}
    measures = track.measures

    def bar_detect(bar: int, seg_fn) -> str | None:
        mi = next(i for i, x in enumerate(measures) if x.index == bar)
        m = measures[mi]
        nxt = measures[mi + 1] if mi + 1 < len(measures) else None
        segs = seg_fn(m, nxt)
        r = detect_chord(segs[0].notes, *by_bar[bar], "guitar")
        return r["name"] if r else None

    # 51 小节：主窗口 Em；小节尾的 D-A-F 是 X 哑音扫弦（无实际音高），
    # 不参与识别——auto 只有 Em 一窗，整小节视图也不再被哑音的理论
    # 音高污染成 G6/9/E。
    assert bar_detect(51, segment_auto) == "Em"
    assert bar_detect(51, segment_measure) == "Em"
    assert bar_detect(53, segment_measure) == "Cmaj7"
    assert bar_detect(53, segment_auto) == "Cmaj7"
    assert bar_detect(56, segment_measure) == "Cmaj7"
    assert bar_detect(56, segment_auto) == "Cmaj7"


@pytest.mark.skipif(not SAMPLE_FILE.exists(), reason="样例文件不存在")
def test_sample_file_16th_duration_parsed():
    song = parse_gp(SAMPLE_FILE)
    m51 = next(x for x in song.tracks[1].measures if x.index == 51)
    sixteenths = [b for b in m51.beats if b.start_quarters >= 3.5 and b.notes]
    assert sixteenths
    assert all(b.duration_quarters == 0.25 for b in sixteenths)


@pytest.mark.skipif(not SAMPLE_FILE.exists(), reason="样例文件不存在")
def test_sample_file_sharp_pitch_names():
    song = parse_gp(SAMPLE_FILE)
    notes = [n for m in song.tracks[1].measures for b in m.beats for n in b.notes if n.midi == 54]
    assert notes
    assert all(n.pitch_name.startswith("F#") for n in notes)


@pytest.mark.skipif(not SAMPLE_FILE.exists(), reason="样例文件不存在")
def test_sample_file_section_keys():
    song = parse_gp(SAMPLE_FILE)
    track = song.tracks[1]
    gk = resolve_key(song, track, None)
    sections = resolve_section_keys(track, gk)
    # 无段落标记的小节最早出现在小节 2（A 大调）；50-56 的 C 大调由
    # 默认的逐小节调号模式处理（见 test_sample_file_acceptance_bars）
    assert sections["A:Intro"] == (9, "Major")
    assert sections[None] == (9, "Major")
    # --key-per-section 模式下，有小节调号的 50-56 小节仍以小节调号 C 为准
    m51 = next(m for m in track.measures if m.index == 51)
    assert measure_key(m51, sections[m51.section]) == (0, "Major")


def test_parse_palm_muted_and_dead_note_flags(tmp_path):
    """GPIF 的 PalmMuted（P.M. 闷音）与 Muted（X 哑音）属性应被读出。"""
    import zipfile

    gpif = """<GPIF>
      <GPVersion>8.0</GPVersion>
      <Tracks><Track id="0"><Name>L</Name><Staves><Staff><Properties>
        <Property name="Tuning"><Pitches>40 45 50 55 59 64</Pitches></Property>
      </Properties></Staff></Staves></Track></Tracks>
      <MasterBars><MasterBar><Time>4/4</Time>
        <Key><AccidentalCount>0</AccidentalCount><Mode>Major</Mode></Key>
        <Bars>0</Bars></MasterBar></MasterBars>
      <Bars><Bar id="0"><Voices>0</Voices></Bar></Bars>
      <Voices><Voice id="0"><Beats>0 1</Beats></Voice></Voices>
      <Beats>
        <Beat id="0"><Notes>0</Notes><Rhythm><ref>0</ref></Rhythm></Beat>
        <Beat id="1"><Notes>1</Notes><Rhythm><ref>0</ref></Rhythm></Beat>
      </Beats>
      <Notes>
        <Note id="0"><Properties>
          <Property name="Midi"><Number>48</Number></Property>
          <Property name="Fret"><Fret>1</Fret></Property>
          <Property name="String"><String>3</String></Property>
          <Property name="PalmMuted"><Enable /></Property>
        </Properties></Note>
        <Note id="1"><Properties>
          <Property name="Midi"><Number>45</Number></Property>
          <Property name="Muted"><Enable /></Property>
        </Properties></Note>
      </Notes>
      <Rhythms><Rhythm id="0"><NoteValue>Quarter</NoteValue></Rhythm></Rhythms>
    </GPIF>"""
    gp = tmp_path / "mini.gp"
    with zipfile.ZipFile(gp, "w") as z:
        z.writestr("Content/score.gpif", gpif)
        z.writestr("VERSION", "8.0")

    song = parse_gp(gp)
    track = song.tracks[0]
    palm = [n for n in track.notes if n.palm_muted]
    dead = [n for n in track.notes if n.muted]
    assert [n.midi for n in palm] == [48]
    assert [n.midi for n in dead] == [45]
    assert all(not n.muted for n in palm)
    assert all(not n.palm_muted for n in dead)
