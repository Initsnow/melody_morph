"""GP 分析和弦识别共用的音乐理论小工具。

把音名拼写、调名解析、罗马度数提取、品质家族归类集中在这里，
避免 annotate / roman / progression / sections 各自复制一份。
"""

from __future__ import annotations

from typing import Optional


SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
FLAT_KEYS = {5, 10, 3, 8, 1, 6}  # F Bb Eb Ab Db Gb 用降号；B 大调（11）用升号


def pc_name(pc: int, key_root: Optional[int] = None) -> str:
    """音级 -> 音名（按调性选择升/降号记法）。"""
    names = FLAT_NAMES if key_root in FLAT_KEYS else SHARP_NAMES
    return names[pc % 12]


def parse_key_name(text: str) -> tuple[int, str]:
    """解析调名（C / Am / F#m / Bb ...）-> (根音音级, Major|Minor)。"""
    s = text.strip()
    if not s:
        raise ValueError("空调名")
    minor = s.endswith("m") and not s.endswith("maj")
    core = s[:-1] if minor else s
    for names in (SHARP_NAMES, FLAT_NAMES):
        if core in names:
            return names.index(core), ("Minor" if minor else "Major")
    raise ValueError(f"无法解析调名: {text!r}")


def quality_family(quality: str) -> str:
    """模板品质 -> 粗家族（匹配时容忍 Isus2/Iadd9 这类装饰差异）。"""
    if "dim" in quality or "ø" in quality:
        return "dim"
    if "aug" in quality:
        return "aug"
    if "sus" in quality:
        return "sus"
    if "maj" in quality:
        return "maj"
    if "m" in quality:
        return "min"
    if "7" in quality:
        return "dom"
    return "maj"


def roman_degree(roman: str) -> str:
    """罗马数字字符串 -> 度数部分（去掉品质/斜杠）：'Isus2/F#' -> 'I'。"""
    main = roman.split("/")[0].strip()
    for i, ch in enumerate(main):
        if ch in "IViv":
            j = i + 1
            while j < len(main) and main[j] in "IViv":
                j += 1
            return main[:j]
    return main
