"""
查看 Guitar Pro (.gp / .gpx) 文件内容
======================================

用法::

    # 轨道概览
    uv run gp-info "xxx.gp"

    # 查看某个轨道每个小节的音符与和弦标注
    uv run gp-info "xxx.gp" --track "Lead Guitar"

    # 只查看有和弦标注的小节
    uv run gp-info "xxx.gp" --track "Lead Guitar" --chords
"""

from __future__ import annotations

import argparse
import sys

from gpchords.parser import GuitarProError, parse_gp, select_track


def print_summary(song) -> None:
    print(f"标题: {song.title or '(无)'}  艺术家: {song.artist or '(无)'}")
    print(f"GP 版本: {song.gp_version or '(未知)'}  小节数: {len(song.tracks[0].measures) if song.tracks else 0}")
    print()
    print(f"{'ID':>3}  {'名称':<24} {'音色':<22} {'小节':>5} {'音符':>6}  和弦库")
    print("-" * 88)
    for track in song.tracks:
        chords = ", ".join(c.name for c in track.chords) or "(无)"
        print(
            f"{track.id:>3}  {track.name:<24} {track.program:<22} "
            f"{len(track.measures):>5} {len(track.notes):>6}  {chords}"
        )


def print_track(track: GPTrack, chords_only: bool) -> None:
    print(f"\n轨道 [{track.id}] {track.name}  （音色: {track.program or '(未设置)'}）")
    if track.tuning:
        print(f"调弦: {track.tuning}  (MIDI, 低 -> 高)")
    if track.chords:
        print("和弦库: " + ", ".join(f"{c.index}:{c.name}" for c in track.chords))
    print()

    shown = 0
    for measure in track.measures:
        line_parts = []
        for beat in measure.beats:
            if not beat.notes and beat.chord is None:
                continue
            parts = []
            if beat.chord is not None:
                parts.append(f"({beat.chord.name})")
            parts.extend(n.pitch_name or str(n.midi) for n in beat.notes)
            if parts:
                line_parts.append(" ".join(parts))
        if not line_parts:
            continue
        if chords_only and not any(b.chord is not None for b in measure.beats):
            continue
        section = f" [{measure.section}]" if measure.section else ""
        print(f"小节 {measure.index:>3}{section}: " + " | ".join(line_parts))
        shown += 1
    if shown == 0:
        print("（该轨道没有音符）" if not chords_only else "（没有带和弦标注的小节）")


def main() -> None:
    parser = argparse.ArgumentParser(description="查看 Guitar Pro 文件中的轨道、音符与和弦标注")
    parser.add_argument("file", help=".gp / .gpx 文件路径")
    parser.add_argument("--track", help="轨道名称或索引（默认只显示概览）")
    parser.add_argument("--chords", action="store_true", help="只显示带和弦标注的小节")
    args = parser.parse_args()

    try:
        song = parse_gp(args.file)
    except GuitarProError as e:
        print(f"解析失败: {e}", file=sys.stderr)
        sys.exit(1)

    print_summary(song)
    if args.track:
        try:
            track = select_track(song, args.track)
        except GuitarProError as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
        print_track(track, chords_only=args.chords)


if __name__ == "__main__":
    main()
