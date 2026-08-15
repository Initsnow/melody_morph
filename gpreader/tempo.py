"""
GPIF tempo automation 的拍单位换算与直改
========================================

GPIF 的 ``<Automation Type="Tempo">`` 里 ``<Value>X ref</Value>`` 的
**第二个数字是拍单位**（alphaTab 与 TuxGuitar 的 GPX 解析器一致）：

=====  ===========  ================
ref    拍单位        四分音符倍率
=====  ===========  ================
1      八分音符      0.5
2      四分音符      1.0  (GP8 默认)
3      附点四分音符  1.5
4      二分音符      2.0
5      附点二分音符  3.0
=====  ===========  ================

有效四分音符 BPM = 标签 X × 倍率。

由此得到一个“改 BPM 标签、不动实际速度、不碰谱面”的写法：把
``<Value>73 2</Value>`` 改写成 ``<Value>146 1</Value>`` —— 标签变成
146，但有效速度仍是 146×0.5 = 73 四分音符/分钟，与原来完全一致。
Guitar Pro 打开后显示新标签，播放速度不变；谱面（技法、和弦、歌词、
记谱）一个字节都不改，因为只替换了 MasterTrack 里的这一小段文本。

注意：拍单位只能表达 {0.5, 1.0, 1.5, 2.0, 3.0} 这几档倍率，所以“标签
改成 T 且速度保持”只有在 ``T / 有效BPM ∈ {2, 1, 2/3, 1/2, 1/3}`` 时
才精确可行；其余比例只能取最近档（速度会略有偏差）或改记谱时值。

示例::

    from gpreader.tempo import parse_tempo_value, relabel_ref, rewrite_tempo_values_in_text

    label, ref, effective = parse_tempo_value("73 2")   # (73.0, 2, 73.0)
    ref2, exact = relabel_ref(effective=73.0, target=146.0)  # (1, True)
    new_text = rewrite_tempo_values_in_text(gpif_xml, 146.0)
"""

from __future__ import annotations

import re
from typing import Optional

from gpreader.parser import TEMPO_REFERENCE_FACTOR

# ref -> 倍率（与 parser.TEMPO_REFERENCE_FACTOR 同表，语义见模块 docstring）
REFERENCE_FACTOR = dict(TEMPO_REFERENCE_FACTOR)

# ref -> 中文名（用于提示信息）
REFERENCE_NAME = {
    1: "八分音符",
    2: "四分音符",
    3: "附点四分音符",
    4: "二分音符",
    5: "附点二分音符",
}

_AUTOMATION_RE = re.compile(r"<Automation[^>]*>.*?</Automation>", re.S)
_VALUE_RE = re.compile(r"<Value>([^<]*)</Value>")


def parse_tempo_value(value_text: str) -> tuple[float, int, float]:
    """解析 ``<Value>`` 文本，返回 (标签, ref, 有效四分BPM)。

    无法识别时按 GP 惯例回退到 ref=2（四分音符）。
    """
    parts = value_text.split()
    if not parts:
        raise ValueError(f"空的 tempo Value: {value_text!r}")
    label = float(parts[0])
    ref = int(parts[1]) if len(parts) > 1 else 2
    if ref not in REFERENCE_FACTOR:
        ref = 2
    return label, ref, label * REFERENCE_FACTOR[ref]


def format_tempo_value(target_bpm: float, ref: int) -> str:
    """按标签 + 拍单位生成 ``<Value>`` 文本，如 ``"146 1"``。"""
    return f"{target_bpm:g} {ref}"


def relabel_ref(effective_bpm: float, target_bpm: float) -> tuple[int, bool]:
    """找拍单位 ref，使 ``target × factor(ref)`` 尽量等于有效 BPM。

    返回 ``(ref, 是否精确)``。精确当且仅当
    ``target / effective ∈ {2, 1, 2/3, 1/2, 1/3}``（取最近档）。
    """
    needed = effective_bpm / target_bpm
    best_ref, best_err = 2, abs(REFERENCE_FACTOR[2] - needed)
    for ref, factor in REFERENCE_FACTOR.items():
        err = abs(factor - needed)
        if err < best_err:
            best_ref, best_err = ref, err
    return best_ref, best_err < 1e-9


def relabel_tempo_value(value_text: str, target_bpm: float) -> tuple[str, dict]:
    """把一条 tempo ``<Value>`` 文本改写成标签=target 且尽量保持有效速度。

    返回 ``(新Value文本, 信息dict)``。信息 dict 含 label/ref/effective
    的前后值与 exact 标志，供 CLI 提示用。
    """
    label, ref, effective = parse_tempo_value(value_text)
    new_ref, exact = relabel_ref(effective, target_bpm)
    return format_tempo_value(target_bpm, new_ref), {
        "label": label,
        "ref": ref,
        "effective": effective,
        "new_ref": new_ref,
        "new_effective": target_bpm * REFERENCE_FACTOR[new_ref],
        "exact": exact,
    }


def find_tempo_automations(xml_text: str) -> list[dict]:
    """定位 GPIF 文本里所有 Tempo automation 的 ``<Value>``。

    返回按出现顺序的 dict 列表：``{bar, value_start, value_end, old_value}``
    （``bar`` 缺失时为 None）。``value_start/end`` 是 ``<Value>`` 元素在
    文本中的起止偏移（含标签）。
    """
    found: list[dict] = []
    for m in _AUTOMATION_RE.finditer(xml_text):
        block = m.group(0)
        if "<Type>Tempo</Type>" not in block:
            continue
        bar_m = re.search(r"<Bar>\s*(-?\d+)\s*</Bar>", block)
        val_m = _VALUE_RE.search(block)
        if val_m is None:
            continue
        found.append({
            "bar": int(bar_m.group(1)) if bar_m else None,
            "value_start": m.start() + val_m.start(),
            "value_end": m.start() + val_m.end(),
            "old_value": val_m.group(1).strip(),
        })
    return found


def rewrite_tempo_values_in_text(
    xml_text: str,
    target_bpm: float,
    only_first: bool = True,
    strict: bool = False,
) -> tuple[str, list[dict]]:
    """把 Tempo automation 的 ``<Value>`` 改写为标签=target、速度尽量不变。

    :param only_first: 只改第一条（基础速度）；其余 automation 保持原样
        （各自的有效速度不变，相对速度结构完整保留）。
    :param strict: 目标比例无法精确表达时抛 :class:`ValueError`；
        否则取最近档并在返回的 changes 里标记 ``exact=False``。
    :return: ``(新文本, changes)``，changes 为 find_tempo_automations
        的结果外加 ``new_value`` / ``exact`` / ``new_effective`` 字段。
    """
    automations = find_tempo_automations(xml_text)
    changes: list[dict] = []
    if not automations:
        return xml_text, changes

    targets = automations[:1] if only_first else automations
    replacements = [(a["value_start"], a["value_end"]) for a in targets]
    # 从后往前替换，偏移不受影响
    for a, (start, end) in zip(targets, replacements):
        new_value, info = relabel_tempo_value(a["old_value"], target_bpm)
        if not info["exact"] and strict:
            raise ValueError(
                f"目标 {target_bpm:g} BPM 与有效 {info['effective']:g} BPM 的比例 "
                f"无法用 GP 拍单位精确表达（可表达比例: 2, 1, 2/3, 1/2, 1/3）"
            )
        a.update(info)
        a["new_value"] = new_value
        changes.append(a)

    # 从后往前替换
    out = xml_text
    for a in reversed(changes):
        start, end = a["value_start"], a["value_end"]
        out = out[:start] + f"<Value>{a['new_value']}</Value>" + out[end:]
    return out, changes
