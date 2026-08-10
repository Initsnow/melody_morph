"""
gp-chords 基准脚本：输出各场景准确率（可重复运行）。

用法::

    uv run python tests/benchmark_chords.py [gp文件]

不传文件时只跑合成场景；传《无论如何 - 副本.gp》等带手工标注的文件时，
额外输出手工标注对照（整小节 / 标注拍到小节末 / auto 窗口）的
根音与名称准确率。
"""

from __future__ import annotations

import sys
from pathlib import Path

from gpchords.annotate import (
    _manual_root_pc,
    detect_chord,
    measure_key,
    resolve_key,
    segment_auto,
    segment_measure,
)
from gpreader import GPNote, parse_gp


def n(midi: int, dur: float = 1.0) -> GPNote:
    return GPNote(midi=midi, duration_quarters=dur)


# 合成场景：(名称, 音符, 调性, 期望名称)
SYNTHETIC = [
    ("Am7（低音 A，A 小调）", [45, 48, 52, 55, 57], (9, "Minor"), "Am7"),
    ("Dm7/F（F 低音，D 小调）", [41, 50, 57, 60], (2, "Minor"), "Dm7/F"),
    ("Cmaj7/E（E 低音，C 大调）", [40, 48, 55, 59], (0, "Major"), "Cmaj7/E"),
    ("Am9（A 小调）", [45, 48, 52, 55, 59], (9, "Minor"), "Am9"),
    ("C7/Bb 斜杠拼写（C 大调）", [46, 48, 52, 55], (0, "Major"), "C7/Bb"),
    ("C7/Bb 斜杠拼写（F 大调）", [46, 48, 52, 55], (5, "Major"), "C7/Bb"),
    ("C 大三和弦", [48, 52, 55], (0, "Major"), "C"),
    ("A5 强力和弦", [45, 52, 57], (9, "Major"), "A5"),
    ("C5/G 斜杠强力和弦", [43, 60, 67], (0, "Major"), "C5/G"),
    ("Fmaj7（C 大调）", [53, 60, 64, 69], (0, "Major"), "Fmaj7"),
    ("G6/9/E（C 大调）", [40, 43, 47, 50, 57], (0, "Major"), "G6/9/E"),
    ("Cmaj7（样例小节 53）", [48, 52, 55, 59, 64], (0, "Major"), "Cmaj7"),
    ("C7b5", [48, 52, 54, 58], (0, "Major"), "C7b5"),
    ("C7#5", [48, 52, 56, 58], (0, "Major"), "C7#5"),
    ("C7b9", [48, 52, 55, 58, 49], (0, "Major"), "C7b9"),
    ("C7#9", [48, 52, 55, 58, 51], (0, "Major"), "C7#9"),
    ("C9sus4", [48, 53, 55, 58, 50], (0, "Major"), "C9sus4"),
    ("C7#11", [48, 52, 55, 58, 54], (0, "Major"), "C7#11"),
    ("C9#11", [48, 52, 55, 58, 50, 54], (0, "Major"), "C9#11"),
    ("Cmaj7#11", [48, 52, 55, 59, 54], (0, "Major"), "Cmaj7#11"),
    ("Cmaj7#5", [48, 52, 56, 59], (0, "Major"), "Cmaj7#5"),
    ("Cmaj7sus2", [48, 50, 55, 59], (0, "Major"), "Cmaj7sus2"),
    ("Cadd11", [48, 52, 53, 55], (0, "Major"), "Cadd11"),
    ("Cmadd4", [48, 51, 53, 55], (0, "Major"), "Cmadd4"),
    ("Cmmaj7", [48, 51, 55, 59], (0, "Major"), "Cmmaj7"),
    ("Cm6/9", [48, 51, 55, 57, 50], (0, "Major"), "Cm6/9"),
    ("C11", [48, 52, 55, 58, 50, 53], (0, "Major"), "C11"),
    ("Cm11", [48, 51, 55, 58, 50, 53], (0, "Major"), "Cm11"),
    ("C13", [48, 52, 55, 58, 50, 57, 53], (0, "Major"), "C13"),
    ("Cmaj13", [48, 52, 55, 59, 50, 57, 53], (0, "Major"), "Cmaj13"),
]

# 切窗场景：(名称, 小节拍序列, 期望窗口数, 期望和弦[, 下一小节拍序列])
AUTO_CASES = [
    (
        "C+F 一小节两和弦",
        [
            (0.0, [(48, 2.0), (52, 2.0), (55, 2.0)]),
            (2.0, [(53, 2.0), (57, 2.0), (60, 2.0)]),
        ],
        2,
        ["C", "F"],
    ),
    (
        "G5+C 一小节两和弦",
        [
            (0.0, [(43, 2.0), (50, 2.0)]),
            (2.0, [(48, 2.0), (52, 2.0), (55, 2.0)]),
        ],
        2,
        ["G5", "C"],
    ),
    (
        "样例小节 51（Em 延长 + 16 分先现 Dm）",
        [
            (0.0, [(40, 3.5), (47, 3.5), (52, 3.5), (55, 3.5), (59, 3.5), (64, 3.5)]),
            (3.5, [(50, 0.25), (57, 0.25), (62, 0.25), (65, 0.25)]),
            (3.75, [(50, 0.25), (57, 0.25), (62, 0.25), (65, 0.25)]),
        ],
        2,
        ["Em", "Dm"],
        [(0.0, [(50, 0.5), (57, 0.5), (62, 0.5), (65, 0.5)])],
    ),
]


def run_synthetic() -> tuple[int, int]:
    root_ok = name_ok = 0
    print("=== 合成场景 ===")
    print(f"{'场景':<34} {'期望':<12} {'识别':<12} 根音 名称")
    for label, midis, key, expected in SYNTHETIC:
        notes = [n(m) for m in midis]
        result = detect_chord(notes, key[0], key[1], "guitar")
        got = result["name"] if result else None
        r = result["root"] if result else None
        e_root = _manual_root_pc(expected)
        rk = "OK" if r == e_root else "--"
        nk = "OK" if got == expected else "--"
        root_ok += rk == "OK"
        name_ok += nk == "OK"
        print(f"{label:<34} {expected:<12} {str(got):<12} {rk:>4}  {nk:>4}")
    print(f"合成场景: 根音准确率 {root_ok}/{len(SYNTHETIC)}，名称准确率 {name_ok}/{len(SYNTHETIC)}")
    return root_ok, name_ok


def run_auto_cases() -> tuple[int, int, int]:
    ok_windows = ok_names = 0
    from gpchords.annotate import SEGMENTERS
    from gpreader import GPBeat, GPMeasure

    print("\n=== auto 切窗场景 ===")
    for label, beats, want_n, want_names, *rest in AUTO_CASES:
        gbeats = []
        for start, items in beats:
            gbeats.append(
                GPBeat(
                    id=f"b{start}",
                    start_quarters=start,
                    duration_quarters=max(d for _, d in items),
                    notes=[n(m, d) for m, d in items],
                    voice_id="v1",
                    position_in_voice=0,
                )
            )
        m = GPMeasure(index=1, time_signature=(4, 4), beats=gbeats)
        next_beats = rest[0] if rest else None
        next_m = None
        if next_beats:
            next_m = GPMeasure(
                index=2,
                time_signature=(4, 4),
                beats=[
                    GPBeat(
                        id=f"n{start}",
                        start_quarters=start,
                        duration_quarters=max(d for _, d in items),
                        notes=[n(midi, d) for midi, d in items],
                        voice_id="v1",
                        position_in_voice=0,
                    )
                    for start, items in next_beats
                ],
            )
        segs = SEGMENTERS["auto"](m, next_m)
        got = []
        for seg in segs:
            r = detect_chord(seg.notes, 0, "Major", "guitar")
            got.append(r["name"] if r else None)
        wok = len(segs) == want_n
        nok = got == want_names
        ok_windows += wok
        ok_names += nok
        print(f"{label:<28} 期望 {want_n} 窗 {want_names} | 实际 {len(segs)} 窗 {got} | "
              f"{'OK' if wok else '--'} {'OK' if nok else '--'}")
    return ok_windows, ok_names, len(AUTO_CASES)


def run_real_file(path: Path) -> None:
    print(f"\n=== 样例文件手工标注对照: {path.name} ===")
    song = parse_gp(path)
    track = song.tracks[1]
    gk = resolve_key(song, track, None)
    by_bar = {m.index: measure_key(m, gk) for m in track.measures}

    def stats(rows):
        n = len(rows)
        w_name = sum(1 for r in rows if r["manual"] == r["whole"])
        w_root = sum(
            1 for r in rows if r["manual_root"] is not None and r["manual_root"] == r["whole_root"]
        )
        t_name = sum(1 for r in rows if r["manual"] == r["tail"])
        t_root = sum(
            1 for r in rows if r["manual_root"] is not None and r["manual_root"] == r["tail_root"]
        )
        a_name = sum(1 for r in rows if r["manual"] == r["auto"])
        a_root = sum(
            1 for r in rows if r["manual_root"] is not None and r["manual_root"] == r["auto_root"]
        )
        print(f"共 {n} 处手工标注")
        print(f"  整小节:        名称 {w_name}/{n}，根音 {w_root}/{n}")
        print(f"  标注拍到小节末: 名称 {t_name}/{n}，根音 {t_root}/{n}")
        print(f"  auto 窗口:     名称 {a_name}/{n}，根音 {a_root}/{n}")
        return n, w_name, w_root, t_name, t_root, a_name, a_root

    rows = []
    for m in track.measures:
        key = by_bar[m.index]
        auto_segs = segment_auto(m)
        for b in m.beats:
            if b.chord is None:
                continue
            whole_notes = [x for bb in m.beats for x in bb.notes]
            tail_notes = [
                x
                for bb in m.beats
                if bb.start_quarters >= b.start_quarters - 1e-9
                for x in bb.notes
            ]
            whole = detect_chord(whole_notes, *key, "guitar")
            tail = detect_chord(tail_notes, *key, "guitar")
            auto = None
            for seg in auto_segs:
                if seg.start_quarters <= b.start_quarters < seg.start_quarters + seg.duration_quarters:
                    auto = detect_chord(seg.notes, *key, "guitar")
                    break
            if auto is None and auto_segs:
                auto = detect_chord(auto_segs[0].notes, *key, "guitar")
            rows.append(
                {
                    "bar": m.index,
                    "manual": b.chord.name,
                    "manual_root": _manual_root_pc(b.chord.name),
                    "whole": whole["name"] if whole else None,
                    "whole_root": whole["root"] if whole else None,
                    "tail": tail["name"] if tail else None,
                    "tail_root": tail["root"] if tail else None,
                    "auto": auto["name"] if auto else None,
                    "auto_root": auto["root"] if auto else None,
                }
            )
    stats(rows)

    print("\n=== 验收小节（默认逐小节调号 + auto 窗口，带下一小节上下文）===")
    measures = track.measures
    for bar, expected in ((51, "Em"), (53, "Cmaj7"), (56, "Cmaj7")):
        mi = next(i for i, x in enumerate(measures) if x.index == bar)
        m = measures[mi]
        nxt = measures[mi + 1] if mi + 1 < len(measures) else None
        segs = segment_auto(m, nxt)
        r = detect_chord(segs[0].notes, *by_bar[bar], "guitar")
        got = r["name"] if r else None
        print(f"  小节 {bar}: 期望 {expected:<10} 实际 {got}  {'OK' if got == expected else '--'}")
    # 整小节视图：51 小节仍是 G6/9/E
    # 注：排除 X 哑音后，整小节视图与主窗口一致，均为 Em
    m51 = next(x for x in measures if x.index == 51)
    r51 = detect_chord(segment_measure(m51)[0].notes, *by_bar[51], "guitar")
    print(f"  小节 51（整小节视图）: 期望 Em          实际 {r51['name'] if r51 else None}")


def main() -> None:
    r_ok, n_ok = run_synthetic()
    w_ok, w_n, w_total = run_auto_cases()
    print(f"\nauto 切窗: 窗口数正确 {w_ok}/{w_total}，和弦正确 {w_n}/{w_total}")
    if len(sys.argv) > 1:
        run_real_file(Path(sys.argv[1]))
    else:
        print("\n（未提供样例文件，跳过手工标注对照；可传：")
        print("  uv run python tests/benchmark_chords.py \"C:/Users/Initsnow/Documents/Audio/谱/无论如何 - 副本.gp\"）")


if __name__ == "__main__":
    main()
