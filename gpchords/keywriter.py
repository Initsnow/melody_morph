"""
自动判断调性并写入 Guitar Pro 调号
==================================

解析 .gp / .gpx 后，用 Krumhansl-Kessler 键感轮廓对所选轨道的音符
（按时值加权）估计调性，再把 <Key> 调号写进每个 MasterBar。

默认把估计出的调性写入**全部小节**（覆盖原有调号）；输出是新的
<原名>_key.gp，原文件不会被修改。--per-section 按段落分别估计并写入；
--key 直接指定调性而不估计（等音写法按规范取调号较少者，如 Db 而非 C#）。

用法::

    # 估计调性并写入全部小节 -> <原名>_key.gp（原文件不变）
    uv run gp-key "song.gp"

    # 按段落估计并写入（转调谱）
    uv run gp-key "song.gp" --per-section

    # 强制指定调性 / 只用指定轨道估计
    uv run gp-key "song.gp" --key Am
    uv run gp-key "song.gp" --track "Lead Guitar"

    # 只分析不写回
    uv run gp-key "song.gp" --no-write
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from gpreader import (
    GPSong,
    GPTrack,
    GuitarProError,
    key_name,
    key_signature,
    parse_gp,
    select_tracks,
)
from gpreader.writer import read_gpif, write_gpif
from gpchords.annotate import estimate_key, note_weights, parse_key_name


# ---------------------------------------------------------------------------
# 调性估计
# ---------------------------------------------------------------------------


def estimate_song_key(tracks: list[GPTrack]) -> tuple[int, str]:
    """合并所选轨道音符（按时值加权）后，用 K-K 估计全局调性。"""
    weights: dict[int, float] = {}
    for t in tracks:
        for pc, w in note_weights(t.notes).items():
            weights[pc] = weights.get(pc, 0.0) + w
    if sum(weights.values()) <= 0:
        return 0, "Major"  # 没有可用音符时与 GP 默认调号一致
    return estimate_key(weights)


def estimate_section_keys(
    tracks: list[GPTrack],
) -> dict[Optional[str], tuple[int, str]]:
    """按小节段落分组估计调性；无段落标记的小节归入 None 组。"""
    grouped: dict[Optional[str], dict[int, float]] = {}
    for t in tracks:
        for m in t.measures:
            weights = note_weights([n for b in m.beats for n in b.notes])
            if sum(weights.values()) <= 0:
                continue
            bucket = grouped.setdefault(m.section, {})
            for pc, w in weights.items():
                bucket[pc] = bucket.get(pc, 0.0) + w
    return {sec: estimate_key(w) for sec, w in grouped.items()}


# ---------------------------------------------------------------------------
# 写回 <Key>
# ---------------------------------------------------------------------------


def _key_element(mb_el: ET.Element) -> ET.Element:
    """返回 MasterBar 的 <Key>，不存在时按 GP8 子元素顺序创建。"""
    key_el = mb_el.find("Key")
    if key_el is not None:
        return key_el
    key_el = ET.Element("Key")
    time_el = mb_el.find("Time")
    bars_el = mb_el.find("Bars")
    if time_el is not None:
        mb_el.insert(list(mb_el).index(time_el) + 1, key_el)
    elif bars_el is not None:
        mb_el.insert(list(mb_el).index(bars_el), key_el)
    else:
        mb_el.append(key_el)
    return key_el


def set_key_signature(mb_el: ET.Element, root_pc: int, mode: str) -> bool:
    """写入/替换 MasterBar 调号，返回是否发生了实际变化。

    除 AccidentalCount/Mode 外，同时写 TransposeAs：降号调用 Flats、
    升号调用 Sharps——GP8 自己设置调号时就是这么写的。只改 AccidentalCount
    而保留旧 TransposeAs（通常是 Sharps）会让降号调的还原音符仍按升号显示，
    例如 F 大调的 Bb 渲染成 A#。
    """
    count, mode_name = key_signature(root_pc, mode)
    key_el = _key_element(mb_el)
    count_el = key_el.find("AccidentalCount")
    mode_el = key_el.find("Mode")
    transpose_el = key_el.find("TransposeAs")
    if count_el is None:
        count_el = ET.SubElement(key_el, "AccidentalCount")
    if mode_el is None:
        mode_el = ET.SubElement(key_el, "Mode")
    if transpose_el is None:
        transpose_el = ET.Element("TransposeAs")
        key_el.insert(list(key_el).index(mode_el) + 1, transpose_el)
    new = (str(count), mode_name)
    old = ((count_el.text or "").strip(), (mode_el.text or "").strip())
    count_el.text = new[0]
    mode_el.text = new[1]
    transpose = "Flats" if count < 0 else "Sharps"
    transpose_changed = (transpose_el.text or "").strip() != transpose
    transpose_el.text = transpose
    return old != new or transpose_changed


# 等音拼写重排：升降号惯例互转。只处理单个升降号（x/bb 不转），
# 也不动自然音，保证 MIDI 音高、品位完全不变。
_SHARP_TO_FLAT = {"C#": "Db", "D#": "Eb", "F#": "Gb", "G#": "Ab", "A#": "Bb"}
_FLAT_TO_SHARP = {v: k for k, v in _SHARP_TO_FLAT.items()}


def _respell_note(note_el: ET.Element, accidental_count: int) -> int:
    """把音符的 ConcertPitch/TransposedPitch 拼写重排到调号惯例，返回改动数。"""
    mapping = _SHARP_TO_FLAT if accidental_count < 0 else _FLAT_TO_SHARP
    props = note_el.find("Properties")
    if props is None:
        return 0
    changed = 0
    for prop in props:
        if prop.get("name") not in ("ConcertPitch", "TransposedPitch"):
            continue
        pitch = prop.find("Pitch")
        if pitch is None:
            continue
        step_el = pitch.find("Step")
        acc_el = pitch.find("Accidental")
        if step_el is None or acc_el is None:
            continue
        key = (step_el.text or "").strip() + (acc_el.text or "").strip()
        repl = mapping.get(key)
        if repl is None:
            continue
        step_el.text = repl[0]
        acc_el.text = repl[1:]
        changed += 1
    return changed


def _bar_note_ids(root: ET.Element) -> dict[str, list[str]]:
    """小节 id -> 该小节所有音符 id（按 GPIF 引用展开）。"""
    voices = {v.get("id"): v for v in root.findall("Voices/Voice")}
    beats = {b.get("id"): b for b in root.findall("Beats/Beat")}
    out: dict[str, list[str]] = {}
    for bar in root.findall("Bars/Bar"):
        ids: list[str] = []
        for vid in (bar.findtext("Voices") or "").split():
            voice = voices.get(vid)
            if voice is None:
                continue
            for bid in (voice.findtext("Beats") or "").split():
                beat = beats.get(bid)
                if beat is None:
                    continue
                ids.extend(
                    nid for nid in (beat.findtext("Notes") or "").split()
                    if nid != "-1"
                )
        out[bar.get("id")] = ids
    return out


def write_keys_to_gp(
    input_path: str | Path,
    output_path: str | Path,
    keys_by_bar: dict[int, tuple[int, str]] | None = None,
    default_key: tuple[int, str] | None = None,
    fill_only: bool = False,
) -> dict:
    """
    把调性写回新的 .gp / .gpx 文件（原文件不动）。

    - ``keys_by_bar``: 小节序号（从 1 起）-> (根音音级, Major|Minor)；
    - ``default_key``: 未在 keys_by_bar 里的小节使用的兜底调性；
    - ``fill_only``: 只写缺失调号的小节，保留已有 <Key>。
    """
    root, _ = read_gpif(input_path)
    master_bars = root.findall("MasterBars/MasterBar")
    notes = {n.get("id"): n for n in root.findall("Notes/Note")}
    bar_note_ids = _bar_note_ids(root)
    written = skipped = 0
    respell = 0
    touched: list[tuple[int, tuple[int, str]]] = []
    for i, mb in enumerate(master_bars, start=1):
        key = (keys_by_bar.get(i) if keys_by_bar else None) or default_key
        if key is None:
            continue
        if fill_only and mb.find("Key/AccidentalCount") is not None:
            skipped += 1
            continue
        touched.append((i, key))
        if set_key_signature(mb, *key):
            written += 1
        else:
            skipped += 1  # 值相同也算未写
    for i, key in touched:
        count, _ = key_signature(*key)
        mb = master_bars[i - 1]
        for bid in (mb.findtext("Bars") or "").split():
            for nid in bar_note_ids.get(bid, []):
                note_el = notes.get(nid)
                if note_el is not None:
                    respell += _respell_note(note_el, count)
    write_gpif(input_path, output_path, root)

    # 用解析器自检：每个目标的调号应被读回
    expected = {i: key_name(*key) for i, key in touched}
    verify = parse_gp(output_path)
    track0 = verify.tracks[0] if verify.tracks else None
    match = 0
    if track0 is not None:
        match = sum(
            1
            for m in track0.measures
            if m.key_signature == expected.get(m.index)
        )
    return {
        "written": written,
        "skipped": skipped,
        "respell": respell,
        "bars": len(master_bars),
        "verified_match": match,
        "verified_total": len(touched),
    }


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _default_tracks(song: GPSong) -> list[GPTrack]:
    """默认取全部非鼓、有音符的轨道；都没有时退回全部轨道。"""
    try:
        return select_tracks(song, ["all"])
    except GuitarProError:
        return [t for t in song.tracks if t.notes] or list(song.tracks)


def _print_tracks(tracks: list[GPTrack]) -> None:
    print(
        "估计轨道: "
        + ", ".join(f"[{t.id}] {t.name}" for t in tracks)
    )


def run_analysis(args) -> dict:
    song = parse_gp(args.file)
    if args.track:
        tracks = select_tracks(song, args.track)
    else:
        tracks = _default_tracks(song)
    if not tracks:
        raise GuitarProError("文件里没有可用轨道")

    if args.key:
        global_key = parse_key_name(args.key)
    else:
        global_key = estimate_song_key(tracks)

    per_section = bool(args.per_section and not args.key)
    section_keys: dict[Optional[str], tuple[int, str]] = {}
    keys_by_bar: dict[int, tuple[int, str]] = {}
    if per_section:
        section_keys = estimate_section_keys(tracks)
        for m in tracks[0].measures:
            keys_by_bar[m.index] = section_keys.get(m.section, global_key)
    else:
        keys_by_bar = {m.index: global_key for m in tracks[0].measures}

    _print_tracks(tracks)
    if args.key:
        print(f"调性: {key_name(*global_key)} (--key 指定)")
    elif per_section:
        print("调性: 按段落估计")
        for sec, k in section_keys.items():
            print(f"  {sec or '(无段落)'}: {key_name(*k)}")
    else:
        print(
            f"调性: {key_name(*global_key)} "
            "(Krumhansl-Kessler 估计；--per-section 按段落，--key 可覆盖)"
        )

    stats = None
    if args.no_write:
        print("未写回 .gp（--no-write）。")
    else:
        output_path = (
            args.write
            if args.write
            else str(Path(args.file).with_name(Path(args.file).stem + "_key.gp"))
        )
        if Path(output_path).resolve() == Path(args.file).resolve():
            print("错误: 输出文件与输入文件相同，请用 --write 指定其他路径", file=sys.stderr)
            sys.exit(1)
        stats = write_keys_to_gp(
            args.file,
            output_path,
            keys_by_bar,
            default_key=global_key,
        )
        print(f"写回完成: {output_path}")
        print(
            f"  调号写入 {stats['written']} 个小节 | 未改动 {stats['skipped']} | "
            f"共 {stats['bars']} 个小节"
        )
        print(f"  音符拼写重排 {stats['respell']} 个（与调号升降号惯例对齐）")
        print(
            f"  自检: 输出文件 {stats['verified_match']}/"
            f"{stats['verified_total']} 个写入小节调号一致"
        )

    if args.out:
        payload = {
            "file": args.file,
            "tracks": [{"id": t.id, "name": t.name} for t in tracks],
            "per_section": per_section,
            "key": key_name(*global_key),
            "keys_by_section": (
                {sec: key_name(*k) for sec, k in section_keys.items()}
                if per_section
                else None
            ),
            "stats": stats,
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"已保存: {args.out}")

    return {"key": global_key, "keys_by_bar": keys_by_bar, "stats": stats}


def main() -> None:
    # Windows GBK 控制台打印轨道名（可能含 × 等字符）会抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        description="自动判断 Guitar Pro 文件的调性并写入调号",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("file", help=".gp / .gpx 文件路径")
    parser.add_argument(
        "--track", action="append", default=None, metavar="TRACK",
        help="用于估计调性的轨道（逗号分隔或重复 --track；all=全部非鼓轨道；"
        "默认自动选择全部非鼓轨道）",
    )
    parser.add_argument("--key", help="直接指定调性，如 C / Am / F#m（不估计）")
    parser.add_argument(
        "--per-section", action="store_true",
        help="按段落分别估计并写入（无段落标记的小节用全局估计兜底）",
    )
    parser.add_argument("--write", metavar="OUT.gp", help="输出路径（默认 <原名>_key.gp）")
    parser.add_argument("--no-write", action="store_true", help="只估计/打印，不写回")
    parser.add_argument("--out", help="输出 JSON 结果文件")
    args = parser.parse_args()

    try:
        run_analysis(args)
    except GuitarProError as e:
        print(f"解析失败: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
