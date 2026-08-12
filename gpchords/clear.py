"""
清除指定轨道的和弦标注与自由文本
================================

给 ``gp-clear`` 提供核心：从 Guitar Pro (.gp/.gpx) 文件里清除指定轨道的
拍上和弦引用（``<Chord>``）与自由文本注解（``<FreeText>``），并清空该
轨道的和弦库（DiagramCollection / ChordCollection / DiagramWorkingSet），
写回新的 .gp 文件（原文件不变）。用于重新标注前把轨道还原成干净状态，
避免旧标注残留（如已过滤的静态 ``P3: I-I-I`` 行）。

用法::

    # 清除 Rhythm Guitar 轨道的和弦与自由文本 -> <原名>_cleared.gp
    uv run gp-clear "song.gp" --track "Rhythm Guitar"

    # 多个轨道 / 全部非鼓轨道
    uv run gp-clear "song.gp" --track "Lead Guitar,Rhythm Guitar"
    uv run gp-clear "song.gp" --track all

    # 指定输出路径 / 只统计不清除
    uv run gp-clear "song.gp" --track 0 --write out.gp
    uv run gp-clear "song.gp" --track 0 --no-write
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from gpreader import GuitarProError, parse_gp, select_tracks
from gpreader.writer import read_gpif, write_gpif
from gpchords.annotate import prompt_tracks

# 轨道内、只被本轨拍引用的和弦库属性名（清除引用后一并清空）
_LIBRARY_PROPS = ("DiagramCollection", "ChordCollection", "DiagramWorkingSet")


def _track_beat_ids(
    root: ET.Element, tid: str
) -> tuple[set[str], ET.Element | None]:
    """目标轨道引用的拍 id 集合与轨道元素。

    GPIF 里 ``MasterBar.<Bars>`` 的空格分隔 id 列表按轨道顺序排列：
    第 i 个 id 属于第 i 个轨道。轨道 -> Bar（<Voices>）-> Voice（<Beats>）
    即可拿到该轨道真正引用到的拍，不会碰到其他轨道的拍。
    """
    track_els = root.findall("Tracks/Track")
    track_order = [t.get("id", str(i)) for i, t in enumerate(track_els)]
    if tid not in track_order:
        raise GuitarProError(f"文件里找不到轨道 id {tid}")
    pos = track_order.index(tid)
    bars = {b.get("id"): b for b in root.findall("Bars/Bar")}
    voices = {v.get("id"): v for v in root.findall("Voices/Voice")}

    beat_ids: set[str] = set()
    for mb in root.findall("MasterBars/MasterBar"):
        ids = (mb.findtext("Bars") or "").split()
        if pos >= len(ids):
            continue
        bar = bars.get(ids[pos])
        if bar is None:
            continue
        for vid in (bar.findtext("Voices") or "").split():
            if vid == "-1":
                continue
            voice = voices.get(vid)
            if voice is None:
                continue
            for bid in (voice.findtext("Beats") or "").split():
                if bid != "-1":
                    beat_ids.add(bid)
    track_el = next((t for t in track_els if t.get("id") == tid), None)
    return beat_ids, track_el


def clear_track(root: ET.Element, tid: str) -> dict[str, int]:
    """清除指定轨道的 <Chord> / <FreeText> 与和弦库，返回统计。"""
    beat_ids, track_el = _track_beat_ids(root, tid)
    beats = {b.get("id"): b for b in root.findall("Beats/Beat")}
    chords = freetexts = 0
    for bid in beat_ids:
        beat = beats.get(bid)
        if beat is None:
            continue
        chord_el = beat.find("Chord")
        if chord_el is not None:
            beat.remove(chord_el)
            chords += 1
        ft_el = beat.find("FreeText")
        if ft_el is not None:
            beat.remove(ft_el)
            freetexts += 1

    # 拍上的引用已全部移除，和弦库项成为孤儿：一并清空，轨道还原成
    # 干净状态（gp-chords 重新标注时会按需重建）。
    if track_el is not None:
        staff_props = track_el.find("Staves/Staff/Properties")
        if staff_props is not None:
            for prop in list(staff_props):
                if prop.get("name") not in _LIBRARY_PROPS:
                    continue
                items = prop.find("Items")
                if items is not None:
                    for item in list(items):
                        items.remove(item)
    return {"beats": len(beat_ids), "chords": chords, "freetexts": freetexts}


def _run(args) -> None:
    song = parse_gp(args.file)
    tracks = (
        select_tracks(song, args.track)
        if args.track
        else prompt_tracks(song)
    )

    root, _ = read_gpif(args.file)
    total = {"beats": 0, "chords": 0, "freetexts": 0}
    for t in tracks:
        stats = clear_track(root, str(t.id))
        for key in total:
            total[key] += stats[key]
        print(
            f"轨道 [{t.id}] {t.name}: 检查 {stats['beats']} 个拍，"
            f"清除和弦 {stats['chords']} 处、自由文本 {stats['freetexts']} 处"
        )

    if args.no_write:
        print("未写回 .gp（--no-write）。")
        return
    if args.write == "__default__":
        output_path = str(
            Path(args.file).with_name(Path(args.file).stem + "_cleared.gp")
        )
    else:
        output_path = args.write
    if Path(output_path).resolve() == Path(args.file).resolve():
        parser.error("输出文件与输入文件相同，请用 --write 指定其他路径")

    write_gpif(args.file, output_path, root)
    print(
        f"\n写回完成: {output_path}（共清除和弦 {total['chords']} 处、"
        f"自由文本 {total['freetexts']} 处）"
    )

    # 自检：目标轨道不应再有任何和弦/自由文本
    verify_song = parse_gp(output_path)
    for t in tracks:
        vt = next((x for x in verify_song.tracks if x.id == t.id), None)
        if vt is None:
            continue
        n_chords = sum(1 for m in vt.measures for b in m.beats if b.chord)
        n_ft = sum(1 for m in vt.measures for b in m.beats if b.free_text)
        if n_chords or n_ft:
            print(
                f"警告: 轨道 [{t.id}] 自检发现残留和弦 {n_chords} 处、"
                f"自由文本 {n_ft} 处",
                file=sys.stderr,
            )


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        description="清除指定轨道的和弦标注与自由文本（原文件不变）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("file", help=".gp / .gpx 文件路径")
    parser.add_argument(
        "--track", action="append", default=None, metavar="TRACK",
        help="清除的轨道（逗号分隔或重复 --track；all=全部非鼓轨道；"
        "不指定时交互选择）",
    )
    parser.add_argument(
        "--write", nargs="?", const="__default__", default="__default__",
        metavar="OUT.gp",
        help="输出路径（默认 <原名>_cleared.gp；--no-write 只统计）",
    )
    parser.add_argument("--no-write", action="store_true", help="只统计不清除")
    args = parser.parse_args()

    try:
        _run(args)
    except GuitarProError as e:
        print(f"解析失败: {e}", file=sys.stderr)
        sys.exit(1)
    except (ValueError, ET.ParseError, zipfile.BadZipFile) as e:
        print(f"文件处理失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
