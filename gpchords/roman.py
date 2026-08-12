"""
罗马和弦记号（Roman numeral）转换
================================

把 :func:`gpchords.annotate.detect_chord` 的和弦结果转换成罗马数字标记，
供 gp-chords 写回 Guitar Pro 时作为拍上的自由文本（``<FreeText>``）注解，
如 B 大调下 Bsus2 -> ``Isus2``（与《春日影.gp》里的手工标注同款）。

记法约定（与常见罗马数字分析一致，兼顾和弦名可读性）：

- 小调**默认按关系大调记度数**：A 小调视作 C 大调，Am -> ``vi``、
  Dm -> ``ii``、Em -> ``iii``（流行和弦表常用的度数记法，便于直接对照
  I-V-vi-IV 这类进行）；需要主音小调记法（Am -> ``i``）时传
  ``minor_as_tonic=True``，对应 ``--roman-tonic-minor``。
- 度数按当前调性的自然音阶拼写。根音音级正好落在调内音级时直接取该级
  （拼写为 Gb 的根音在 B 大调里与 F# 同音 -> ``V``，功能优先于字母拼写）；
  调外根音按根音字母对应的音级加升降号（C 在 B 大调 -> ``bII``，
  D# 在 C 大调 -> ``#II``）。
- 大三和弦与挂留/强力和弦大写（``I``、``IV``、``Isus2``、``V5``）；
  小三/减/半减家族小写，且小写度数已隐含小调性，后缀省略开头的 ``m``
  （C#m / C#m7 在 B 大调 -> ``ii`` / ``ii7``，vii°、iiø7 同理）。
- 品质后缀保留（``V7``、``Imaj7``、``IV6/9``），只做符号替换：
  ``dim -> °``、``dim7 -> °7``、``m7b5 -> ø7``、``aug -> +``。
- 斜杠低音保留音名写法（``Isus2/F#``），不强行转成转位标记。
"""

from __future__ import annotations

from typing import Optional

from gpchords.theory import pc_name as _pc_name

_ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII")
# C=0, D=1, ..., B=6：字母 -> 音级序数
_LETTERS = ("C", "D", "E", "F", "G", "A", "B")

# 调式音阶步进（与 annotate 的 _diatonic_pcs 一致：小调取自然小调）
_STEPS = {
    "Major": (0, 2, 4, 5, 7, 9, 11),
    "Minor": (0, 2, 3, 5, 7, 8, 10),
}

# 需要小写度数、且做符号替换的品质
_DIM_SUFFIX = {"dim": "°", "dim7": "°7"}
_HALF_DIM_SUFFIX = {"m7b5": "ø7"}
_AUG_SUFFIX = {"aug": "+"}

_MINOR_QUALITIES = {
    "min",
    "m",
    "m6",
    "m7",
    "m7(no3)",
    "m7(no5)",
    "m9",
    "m11",
    "mmaj7",
    "madd4",
    "madd9",
    "m6/9",
    "m(no5)",
    *_DIM_SUFFIX,
    *_HALF_DIM_SUFFIX,
}

def _accidental(diff: int) -> str:
    """半音差 -> 升降号前缀（最多双升/双降）。"""
    if diff == 0:
        return ""
    if diff > 0:
        return "#" * min(diff, 2)
    return "b" * min(-diff, 2)


def _degree_of(
    root: int, key_root: int, key_mode: str, scale_pcs: list[int]
) -> tuple[int, str]:
    """根音音级 -> (度数下标 0..6, 升降号前缀)。"""
    if root in scale_pcs:
        return scale_pcs.index(root), ""
    # 调外根音：先按字母对应调内音级（C 在 B 大调 -> II 降半音 -> bII）
    root_letter = _LETTERS.index(_pc_name(root, key_root)[0])
    tonic_letter = _LETTERS.index(_pc_name(key_root, key_root)[0])
    degree = (root_letter - tonic_letter) % 7
    diff = (root - scale_pcs[degree]) % 12
    if diff > 6:
        diff -= 12
    if abs(diff) <= 2:
        return degree, _accidental(diff)
    # 极罕见：字母对应音级离得太远（增四度附近），退回最近的音级
    best = min(
        range(7),
        key=lambda i: min(
            (root - scale_pcs[i]) % 12, (scale_pcs[i] - root) % 12
        ),
    )
    diff = (root - scale_pcs[best]) % 12
    if diff > 6:
        diff -= 12
    return best, _accidental(diff)


def _is_lowercase(quality: str) -> bool:
    return quality in _MINOR_QUALITIES


def _suffix(quality: str, chord: dict) -> str:
    """品质 -> 罗马数字后缀（符号替换 + 小调后缀省略开头 m）。"""
    if quality in _DIM_SUFFIX:
        return _DIM_SUFFIX[quality]
    if quality in _HALF_DIM_SUFFIX:
        return _HALF_DIM_SUFFIX[quality]
    if quality in _AUG_SUFFIX:
        return _AUG_SUFFIX[quality]
    suffix = chord.get("suffix", quality)
    # 小写度数已表示小调性：Am7 -> vi7（而不是 vim7）、Dmadd9 -> iiadd9
    if quality in _MINOR_QUALITIES and suffix.startswith("m"):
        suffix = suffix[1:]
    return suffix


def chord_to_roman(
    chord: dict,
    key_root: int,
    key_mode: str = "Major",
    minor_as_tonic: bool = False,
) -> str:
    """
    把和弦识别结果转换成罗马数字标记。

    ``chord`` 需要包含 ``root``（根音音级）、``quality``（模板品质）、
    ``bass_pc``（低音音级）与可选的 ``suffix``（和弦名后缀）。

    示例（B 大调）：Bsus2 -> ``Isus2``；C#m7 -> ``ii7``；
    B/F# -> ``I/F#``；C -> ``bII``。

    小调默认按关系大调记（A 小调 Am -> ``vi``）；``minor_as_tonic=True``
    时按主音小调记（Am -> ``i``、G#dim -> ``#vii°``）。
    """
    if key_root is None:
        raise ValueError("罗马数字需要调性根音（key_root）")
    if key_mode == "Minor" and not minor_as_tonic:
        # 关系大调与小调共享调号，拼写一致：A 小调 -> C 大调
        key_root = (key_root + 3) % 12
        key_mode = "Major"
    root = chord["root"] % 12
    quality = chord["quality"]
    scale_pcs = [(key_root + step) % 12 for step in _STEPS[key_mode]]

    degree, acc = _degree_of(root, key_root, key_mode, scale_pcs)
    numeral = _ROMAN[degree]
    if _is_lowercase(quality):
        numeral = numeral.lower()
    out = f"{acc}{numeral}{_suffix(quality, chord)}"

    bass = chord.get("bass_pc", root)
    if bass != root:
        out += f"/{_pc_name(bass, key_root)}"
    return out
