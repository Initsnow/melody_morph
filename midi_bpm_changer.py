#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MIDI/GP BPM Changer — 逻辑提取自 https://github.com/shshouse/MidiBpmChanger

原项目是一个 tkinter GUI 应用（midi_bpm_changer.py，依赖 mido），
本脚本把核心逻辑提取成可复用的函数，并提供命令行批量处理入口。

MIDI 文件（.mid）：
  1. 找到第一条 set_tempo 消息得出“原始 BPM”（默认 120）；
  2. 计算缩放比例 scale_factor = 目标BPM / 原始BPM；
  3. 每个轨道的每条消息 time 按比例缩放（int(round(...))）；
  4. 所有 set_tempo 替换为目标 BPM 对应 tempo；
  5. 若原本没有 set_tempo，在第一轨开头插入一条；
  6. 保存为 ``原文件名_modified.mid``。
  语义：改变 BPM 标记，但所有音符的**实际播放时刻不变**（tick × tempo
  相互抵消）。

Guitar Pro 文件（.gp / .gpx）——默认**重排谱面并直改 .gp 文件本身**：
  把 tempo 改成真实的目标四分音符 BPM（``73 2`` -> ``146 2``），
  音符/拍时值 ×factor、小节 ×factor（保持拍号）、跨小节音符拆开补
  连音，实际播放时长不变；技法/和弦/歌词等元素全部原样保留。
  仅支持整数倍提速（如 73 -> 146）；含跨小节三连音的拍暂不支持。
  其他可选模式：
    --relabel  改为“标签=目标 BPM、有效速度不变”的拍单位写法
               （如 ``73 2`` -> ``146 1``，实际仍是 73 四分BPM，不重排谱面）
    --to-midi  导出 MIDI（会丢失技法）
  多段速度文件以第一条（基础速度）为基准整体缩放。

用法示例:
    python midi_bpm_changer.py 150 song.mid
    python midi_bpm_changer.py 146 song.gp             # 直改 .gp：真实 146 BPM，谱面重排
    python midi_bpm_changer.py 146 song.gp --relabel   # 可选：只改标签不改谱面
    python midi_bpm_changer.py 146 song.gp --to-midi   # 可选：导出 MIDI
    python midi_bpm_changer.py -d song.gp              # 检测标签与有效 BPM
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path
from typing import List, Optional

import mido
from mido import MetaMessage, MidiFile

from gpreader import parse_gp
from gpreader.midi import song_to_midi
from gpreader.reengrave import reengrave_tempo
from gpreader.tempo import (REFERENCE_NAME, find_tempo_automations,
                            parse_tempo_value, rewrite_tempo_values_in_text)
from gpreader.writer import read_gpif, rewrite_gpif_text, write_gpif

# 文件中没有 set_tempo 时使用的默认 BPM（与原项目一致）
DEFAULT_BPM = 120.0
GP_SUFFIXES = {".gp", ".gpx"}


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------
def detect_bpm(midi: MidiFile) -> float:
    """检测 MIDI 文件的原始 BPM。

    扫描所有轨道，返回第一条 ``set_tempo`` 消息对应的 BPM；
    若文件中没有 set_tempo，返回默认值 120（与原项目 get_current_bpm 一致）。
    """
    for track in midi.tracks:
        for msg in track:
            if msg.type == 'set_tempo':
                return int(60000000 / msg.tempo)
    return DEFAULT_BPM


def _find_first_tempo(midi: MidiFile) -> Optional[int]:
    """返回文件中第一条 set_tempo 的 tempo 值（微秒/四分音符），没有则返回 None。"""
    for track in midi.tracks:
        for msg in track:
            if msg.type == 'set_tempo':
                return msg.tempo
    return None


def change_bpm(midi: MidiFile, new_bpm: float) -> MidiFile:
    """把 MIDI 的 BPM 改为 ``new_bpm``，返回一个新的 MidiFile（不修改入参）。

    逻辑与原项目 convert_bpm 完全一致：
      * 旧 tempo 取第一条 set_tempo，否则按默认 120 BPM；
      * 缩放系数 = new_bpm / old_bpm；
      * 所有消息的 time 按比例缩放（四舍五入取整）；
      * set_tempo 全部替换为 new_tempo；
      * 若转换后仍无 set_tempo，在第一轨开头插入一条。
    """
    if new_bpm <= 0:
        raise ValueError(f"目标 BPM 必须为正数，收到: {new_bpm!r}")

    old_tempo = _find_first_tempo(midi) or mido.bpm2tempo(DEFAULT_BPM)
    old_bpm = mido.tempo2bpm(old_tempo)
    new_tempo = mido.bpm2tempo(new_bpm)
    scale_factor = new_bpm / old_bpm

    # 新建 MidiFile，保留原文件的元数据（type / ticks_per_beat / charset）
    out = MidiFile(type=midi.type, ticks_per_beat=midi.ticks_per_beat,
                   charset=midi.charset)

    has_tempo = False
    for track in midi.tracks:
        new_track = mido.MidiTrack()
        for msg in track:
            scaled_time = int(round(msg.time * scale_factor))
            if msg.type == 'set_tempo':
                has_tempo = True
                new_track.append(MetaMessage('set_tempo', tempo=new_tempo,
                                             time=scaled_time))
            else:
                # 元消息与普通消息都只是复制并缩放 time
                new_track.append(msg.copy(time=scaled_time))
        out.tracks.append(new_track)

    # 原文件没有 tempo 消息时，在第一轨开头插入（与原项目一致）
    if not has_tempo:
        if not out.tracks:
            out.tracks.append(mido.MidiTrack())
        out.tracks[0].insert(0, MetaMessage('set_tempo', tempo=new_tempo))

    return out


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def _resolve_output_path(src_path: str, suffix: str, out_dir: Optional[str],
                         keep_ext: bool = False) -> str:
    base, ext = os.path.splitext(src_path)
    out_path = base + suffix + (ext if keep_ext else ".mid")
    if out_dir:
        out_path = os.path.join(out_dir, os.path.basename(out_path))
    return out_path


def is_gp_path(src_path: str) -> bool:
    """判断是否为 Guitar Pro 文件（.gp / .gpx）。"""
    return Path(src_path).suffix.lower() in GP_SUFFIXES


def _gp_tempo_label(src_path: str) -> dict:
    """读取 GP 文件第一条 Tempo automation，返回 {label, ref, effective}。"""
    with zipfile.ZipFile(src_path) as z:
        names = set(z.namelist())
        gpif_name = ("Content/score.gpif" if "Content/score.gpif" in names
                     else "score.gpif")
        xml_text = z.read(gpif_name).decode("utf-8")
    autos = find_tempo_automations(xml_text)
    if not autos:
        raise ValueError("文件中没有 Tempo automation")
    label, ref, effective = parse_tempo_value(autos[0]["old_value"])
    return {"label": label, "ref": ref, "effective": effective, "count": len(autos)}


def process_file(src_path: str, new_bpm: float, suffix: str,
                 out_dir: Optional[str]) -> str:
    """转换单个 MIDI 文件，返回输出路径。失败时抛出异常。"""
    mid = MidiFile(src_path)
    converted = change_bpm(mid, new_bpm)
    out_path = _resolve_output_path(src_path, suffix, out_dir)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    converted.save(out_path)
    return out_path


def relabel_gp_file(src_path: str, new_bpm: float, suffix: str,
                    out_dir: Optional[str], strict: bool = False) -> tuple[str, dict]:
    """拍单位改写：标签=目标 BPM、有效速度不变（不重排谱面）。

    只替换 Tempo automation 的 ``<Value>`` 文本：标签改为目标 BPM、
    有效四分 BPM 不变（利用 GPIF 拍单位），谱面/技法逐字节保留。
    """
    out_path = _resolve_output_path(src_path, suffix, out_dir, keep_ext=True)

    def transform(xml_text: str) -> str:
        new_text, changes = rewrite_tempo_values_in_text(
            xml_text, new_bpm, only_first=True, strict=strict)
        if not changes:
            raise ValueError("文件中没有 Tempo automation")
        return new_text

    rewrite_gpif_text(src_path, out_path, transform)
    with zipfile.ZipFile(out_path) as z:
        names = set(z.namelist())
        gpif_name = ("Content/score.gpif" if "Content/score.gpif" in names
                     else "score.gpif")
        xml_text = z.read(gpif_name).decode("utf-8")
    changes = find_tempo_automations(xml_text)
    info = parse_tempo_value(changes[0]["old_value"])
    return out_path, {
        "label": info[0], "ref": info[1], "effective": info[2],
        "count": len(changes),
    }


def reengrave_gp_file(src_path: str, new_bpm: float, suffix: str,
                      out_dir: Optional[str]) -> tuple[str, dict]:
    """重排谱面并直改 .gp：tempo 变成真实的目标四分音符 BPM。

    音符/拍时值 ×factor、小节 ×factor、跨小节音符拆开补连音，
    实际播放时长不变；技法等元素原样保留（详见 gpreader.reengrave）。
    """
    root, _ = read_gpif(src_path)
    info = reengrave_tempo(root, new_bpm)
    out_path = _resolve_output_path(src_path, suffix, out_dir, keep_ext=True)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    write_gpif(src_path, out_path, root)
    return out_path, info


def export_gp_to_midi(src_path: str, new_bpm: float, suffix: str,
                      out_dir: Optional[str]) -> str:
    """把 Guitar Pro 文件按目标 BPM 导出为 MIDI（实际时刻不变）。"""
    song = parse_gp(src_path)
    mid = song_to_midi(song, bpm=new_bpm)
    out_path = _resolve_output_path(src_path, suffix, out_dir)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    mid.save(out_path)
    return out_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="midi_bpm_changer",
        description="批量调整 MIDI/GP 文件的 BPM（逻辑提取自 shshouse/MidiBpmChanger）",
    )
    parser.add_argument("target_bpm", nargs="?",
                        help="目标 BPM（正数），例如 150；--detect 模式下可省略")
    parser.add_argument("mid_files", nargs="*", metavar="FILE",
                        help="一个或多个 .mid / .gp / .gpx 文件")
    parser.add_argument("-d", "--detect", action="store_true",
                        help="只检测并打印每个文件的原始 BPM，不做转换")
    parser.add_argument("--relabel", action="store_true",
                        help="GP 改为只改标签（拍单位写法），不重排谱面")
    parser.add_argument("--to-midi", action="store_true",
                        help="GP 输入时导出 MIDI 而非直改 .gp（会丢失技法）")
    parser.add_argument("--strict", action="store_true",
                        help="--relabel 模式下目标比例无法精确表达时失败退出")
    parser.add_argument("--suffix", default="_modified",
                        help="输出文件后缀（默认 _modified）")
    parser.add_argument("-o", "--out-dir", default=None,
                        help="输出目录（默认与原文件同目录）")
    args = parser.parse_args(argv)

    # 位置参数手动判定：兼容 `150 a.mid` 与 `-d a.mid` 两种写法
    bpm: Optional[float] = None
    if args.target_bpm is not None:
        try:
            bpm = float(args.target_bpm)
        except ValueError:
            args.mid_files.insert(0, args.target_bpm)  # 首位置其实是文件名
    if not args.mid_files:
        parser.error("至少需要一个文件")
    if bpm is None and not args.detect:
        parser.error("需要指定目标 BPM（或使用 -d 仅检测）")
    if bpm is not None and bpm <= 0:
        parser.error("目标 BPM 必须为正数")

    failed = 0
    for src_path in args.mid_files:
        if not os.path.isfile(src_path):
            print(f"[错误] 文件不存在: {src_path}", file=sys.stderr)
            failed += 1
            continue
        try:
            if is_gp_path(src_path):
                before = _gp_tempo_label(src_path)
                if args.detect:
                    print(f"{os.path.basename(src_path)}: 标签 {before['label']:g} BPM "
                          f"({REFERENCE_NAME.get(before['ref'], '?')}, 有效 "
                          f"{before['effective']:g} BPM, 共 {before['count']} 处速度)")
                    continue
                if args.to_midi:
                    out_path = export_gp_to_midi(src_path, bpm,
                                                 args.suffix, args.out_dir)
                    print(f"{os.path.basename(src_path)}: 已导出 MIDI "
                          f"({bpm:g} BPM, 实际时刻不变), 已保存: {out_path}")
                    continue
                if args.relabel:
                    out_path, info = relabel_gp_file(src_path, bpm,
                                                     args.suffix, args.out_dir,
                                                     strict=args.strict)
                    if abs(info["effective"] - before["effective"]) < 1e-9:
                        speed_msg = f"实际速度不变 (有效 {info['effective']:g} BPM)"
                    else:
                        speed_msg = (f"⚠ 有效 {before['effective']:g} -> "
                                     f"{info['effective']:g} BPM (非精确比例, 取最近档)")
                    print(f"{os.path.basename(src_path)}: 标签 {before['label']:g} BPM "
                          f"({REFERENCE_NAME.get(before['ref'], '?')}) -> "
                          f"{bpm:g} BPM ({REFERENCE_NAME.get(info['ref'], '?')}), "
                          f"{speed_msg}, 已保存: {out_path}")
                    continue
                out_path, info = reengrave_gp_file(src_path, bpm,
                                                   args.suffix, args.out_dir)
                print(f"{os.path.basename(src_path)}: {before['label']:g} BPM "
                      f"(有效 {before['effective']:g}) -> 真实 {bpm:g} BPM "
                      f"(四分音符), 音符时值×{info['factor']}, "
                      f"小节 {info['bars_in']}→{info['bars_out']}, "
                      f"实际时长不变, 已保存: {out_path}")
                continue
            mid = MidiFile(src_path)
            orig_bpm = detect_bpm(mid)
            if args.detect:
                print(f"{os.path.basename(src_path)}: 原始 BPM = {orig_bpm:g}")
                continue
            out_path = process_file(src_path, bpm, args.suffix, args.out_dir)
            print(f"{os.path.basename(src_path)}: {orig_bpm:g} BPM -> "
                  f"{bpm:g} BPM, 已保存: {out_path}")
        except Exception as e:  # noqa: BLE001 - 逐个文件容错，与原项目 try/except 一致
            print(f"[错误] 处理 {src_path} 失败: {e}", file=sys.stderr)
            failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
