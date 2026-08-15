"""
GPIF 谱面重排版：把 tempo 改成真实的目标四分音符 BPM
====================================================

midi_bpm_changer 的语义是“改 BPM 标签、实际播放时长不变”。对 Guitar
Pro 谱面（.gp/.gpx）来说，要做到**标签是真实目标值**（如 ``73 2`` ->
``146 2``，即真正的 146 四分音符/分钟），必须重排整个谱面：

1. 所有音符/拍的时值 × factor（四分音符变成二分、八分变四分……）；
2. 每个旧小节拆成 factor 个新小节（保持拍号，如 4/4 -> 4/4×2 小节）；
3. 跨新小节的音符拆成若干小节内音符并补上连音（Tie origin/destination）；
4. 音符的技法（滑音、泛音、推弦……）、和弦、歌词等元素全部原样保留，
   只改时值结构；时长换算按乐理附点（2 - 0.5^count）。

实际播放时长不变：旧时长 = 旧四分音符数 × 60/旧BPM；新时长 =
（旧四分音符数 × factor）× 60/(旧BPM × factor)，二者相等。

约束：``factor = 目标 / 有效BPM`` 必须为**正整数**（新小节边界才能与
旧小节边界对齐）；含 tuplet（三连音等）且跨小节的拍暂不支持（不跨小节
的 tuplet 保持 tuplet 结构翻倍）。这些情况下会抛 :class:`GuitarProError`。

示例::

    from gpreader.writer import read_gpif, write_gpif
    from gpreader.reengrave import reengrave_tempo

    root, _ = read_gpif("song.gp")
    info = reengrave_tempo(root, 146.0)
    write_gpif("song.gp", "song_146.gp", root)
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from copy import deepcopy
from typing import Optional

from gpreader.parser import NOTE_VALUE_QUARTERS, GuitarProError
from gpreader.tempo import parse_tempo_value

_QUARTERS_TO_VALUE = {q: name for name, q in NOTE_VALUE_QUARTERS.items()}

# GP8 规范写法（NOTE_VALUE_QUARTERS 里 Eighth/8th、Sixteenth/16th 等
# 是重复值，倒查表时优先用 GP8 常见的名字，便于复用现有节奏池）
_PREFERRED_VALUE_NAMES = {
    16.0: "Long", 8.0: "DoubleWhole", 4.0: "Whole", 2.0: "Half",
    1.0: "Quarter", 0.5: "Eighth", 0.25: "16th", 0.125: "32nd",
    0.0625: "64th", 0.03125: "128th", 0.015625: "256th",
}
_QUARTERS_TO_VALUE.update(_PREFERRED_VALUE_NAMES)


def _dot_factor(count: int) -> float:
    """乐理附点倍率：1 点 ×1.5、2 点 ×1.75、3 点 ×1.875。"""
    return 2.0 - 0.5 ** count


def _expressible_values(max_dots: int = 3) -> list[tuple[float, str, int]]:
    vals: list[tuple[float, str, int]] = []
    for q in NOTE_VALUE_QUARTERS.values():
        for dots in range(max_dots + 1):
            vals.append((q * _dot_factor(dots), _QUARTERS_TO_VALUE[q], dots))
    return sorted(vals, key=lambda x: x[0], reverse=True)


_EXPRESSIBLE = _expressible_values()


def express_units(duration: float) -> list[tuple[str, int]]:
    """把时长（四分音符）拆成 ``[(NoteValue, dots), ...]`` 序列。

    精确可表达时用单个值；否则贪心取最大可表达值并拆成多个单位
    （单位之间由调用方补连音）。
    """
    rem = duration
    units: list[tuple[str, int]] = []
    for _ in range(64):
        if rem <= 1e-9:
            return units
        for vq, nv, dots in _EXPRESSIBLE:
            if vq <= rem + 1e-9:
                units.append((nv, dots))
                rem -= vq
                break
        else:
            raise GuitarProError(
                f"无法用标准音符时值表达 {duration:g} 四分音符（含附点）"
            )
    raise GuitarProError(f"时值 {duration:g} 分解失败（超出 64 个拆分单位）")


class _IdGen:
    """按容器生成不重复的整数 id 字符串。"""

    def __init__(self, elements) -> None:
        nums = [int(e.get("id")) for e in elements
                if (e.get("id") or "").isdigit()]
        self._next = (max(nums) if nums else -1) + 1

    def __call__(self) -> str:
        i = self._next
        self._next += 1
        return str(i)


class _Ctx:
    def __init__(self, root: ET.Element) -> None:
        self.root = root
        self.mbs_root = root.find("MasterBars")
        self.bars_root = root.find("Bars")
        self.voices_root = root.find("Voices")
        self.beats_root = root.find("Beats")
        self.notes_root = root.find("Notes")
        self.rhythms_root = root.find("Rhythms")
        for name in ("mbs_root", "bars_root", "voices_root", "beats_root",
                     "notes_root", "rhythms_root"):
            if getattr(self, name) is None:
                raise GuitarProError(f"GPIF 缺少 <{name[:-5]}> 容器")

        self.bar_ids = _IdGen(self.bars_root.findall("Bar"))
        self.voice_ids = _IdGen(self.voices_root.findall("Voice"))
        self.beat_ids = _IdGen(self.beats_root.findall("Beat"))
        self.note_ids = _IdGen(self.notes_root.findall("Note"))
        self.rhythm_ids = _IdGen(self.rhythms_root.findall("Rhythm"))

        self.old_bars = list(self.bars_root.findall("Bar"))
        self.old_voices = list(self.voices_root.findall("Voice"))
        self.old_beats = list(self.beats_root.findall("Beat"))
        self.old_notes = list(self.notes_root.findall("Note"))

        self.bar_by_id = {b.get("id"): b for b in self.old_bars}
        self.voice_by_id = {v.get("id"): v for v in self.old_voices}
        self.beat_by_id = {b.get("id"): b for b in self.old_beats}
        self.note_by_id = {n.get("id"): n for n in self.old_notes}

        self.rhythm_pool: dict[tuple, str] = {}
        for r in self.rhythms_root.findall("Rhythm"):
            self.rhythm_pool[self._rhythm_key(r)] = r.get("id")

        # 拆分缓存：GPIF 的 Beat/Note 元素会被多个声部/小节共享引用，
        # 同一 (beat, 小节内位置, 拍号) 只拆一次，产出元素复用（与原文件一致）
        self.split_cache: dict[tuple, list[tuple[int, ET.Element]]] = {}

    @staticmethod
    def _rhythm_key(r: ET.Element) -> tuple:
        nv = r.findtext("NoteValue") or ""
        dot = r.find("AugmentationDot")
        dots = int(dot.get("count", "1") or "1") if dot is not None else 0
        t = r.find("PrimaryTuplet")
        tuplet = None
        if t is not None:
            tuplet = (int(t.findtext("Num") or "1"),
                      int(t.findtext("Den") or "1"))
        return (nv, dots, tuplet)

    def rhythm_id(self, key: tuple) -> str:
        """按 (NoteValue, dots, tuplet) 取节奏池 id，没有则新建。"""
        rid = self.rhythm_pool.get(key)
        if rid is not None:
            return rid
        nv, dots, tuplet = key
        el = ET.SubElement(self.rhythms_root, "Rhythm")
        el.set("id", self.rhythm_ids())
        ET.SubElement(el, "NoteValue").text = nv
        if dots:
            d = ET.SubElement(el, "AugmentationDot")
            d.set("count", str(dots))
        if tuplet:
            t = ET.SubElement(el, "PrimaryTuplet")
            ET.SubElement(t, "Num").text = str(tuplet[0])
            ET.SubElement(t, "Den").text = str(tuplet[1])
        self.rhythm_pool[key] = el.get("id")
        return el.get("id")

    def beat_duration(self, b: ET.Element) -> float:
        """拍时长（四分音符），按乐理附点 + tuplet 计算。"""
        ref = b.find("Rhythm")
        if ref is None:
            return 0.0
        r = self.rhythms_root.find(f"Rhythm[@id='{ref.get('ref')}']")
        if r is None:
            return 0.0
        dur = NOTE_VALUE_QUARTERS.get(r.findtext("NoteValue") or "", 0.0)
        dot = r.find("AugmentationDot")
        if dot is not None:
            dur *= _dot_factor(int(dot.get("count", "1") or "1"))
        t = r.find("PrimaryTuplet")
        if t is not None:
            dur *= int(t.findtext("Den") or "1") / int(t.findtext("Num") or "1")
        return dur

    def clone_note(self, nid: str, origin: bool, dest: bool) -> str:
        """深拷贝音符并设置连音标记，返回新 id（原音符不动）。"""
        src = self.note_by_id[nid]
        el = deepcopy(src)
        el.set("id", self.note_ids())
        for t in el.findall("Tie"):
            el.remove(t)
        if origin or dest:
            tie = ET.SubElement(el, "Tie")
            tie.set("origin", "true" if origin else "false")
            tie.set("destination", "true" if dest else "false")
        self.notes_root.append(el)
        return el.get("id")

    def orig_tie(self, nid: str) -> tuple[bool, bool]:
        t = self.note_by_id[nid].find("Tie")
        if t is None:
            return False, False
        return (t.get("origin") == "true", t.get("destination") == "true")


def _make_beat(ctx: _Ctx, b_el: ET.Element, nv: str, dots: int,
               tuplet: Optional[tuple[int, int]], note_ids: list[str],
               extras: bool) -> ET.Element:
    """生成新 Beat 元素（深拷贝原拍，替换 Rhythm/Notes）。"""
    nb = deepcopy(b_el)
    nb.set("id", ctx.beat_ids())
    for r in nb.findall("Rhythm"):
        nb.remove(r)
    r = ET.SubElement(nb, "Rhythm")
    r.set("ref", ctx.rhythm_id((nv, dots, tuplet)))
    for nn in nb.findall("Notes"):
        nb.remove(nn)
    if note_ids:
        nn = ET.SubElement(nb, "Notes")
        nn.text = " ".join(note_ids)
    if not extras:
        for child in list(nb):
            if child.tag not in ("Rhythm", "Notes"):
                nb.remove(child)
    ctx.beats_root.append(nb)
    return nb


def _split_beat(ctx: _Ctx, b_el: ET.Element, bar_abs_q: float, pos: float,
                dur: float, q_per: float, factor: int,
                stats: dict) -> list[tuple[int, ET.Element]]:
    """把一个旧拍展开成新拍列表，返回 ``[(新小节相对序号k, Beat元素)]``。

    同一 (beat id, 小节内位置, 拍号) 的结果会被缓存复用——GPIF 里同一
    Beat 元素常被多个声部/小节共享引用，拆分产物同样按引用共享。
    """
    stats["beats_in"] += 1
    cache_key = (b_el.get("id"), round(pos, 6), round(q_per, 6))
    cached = ctx.split_cache.get(cache_key)
    if cached is not None:
        return cached

    s = (bar_abs_q + pos) * factor
    e = s + dur * factor
    k0 = int(math.floor(s / q_per))
    k1 = int(math.ceil(e / q_per - 1e-9))
    pieces = []
    for k in range(k0, k1):
        ps = max(s, k * q_per)
        pe = min(e, (k + 1) * q_per)
        if pe - ps > 1e-9:
            pieces.append((k, ps, pe))
    base_k = int(round(bar_abs_q * factor / q_per))

    # 原拍节奏信息（NoteValue / 附点 / tuplet）
    ref = b_el.find("Rhythm")
    rhythm_el = None
    if ref is not None:
        rhythm_el = ctx.rhythms_root.find(f"Rhythm[@id='{ref.get('ref')}']")
    nv_orig = (rhythm_el.findtext("NoteValue") or "Quarter") \
        if rhythm_el is not None else "Quarter"
    dot_el = rhythm_el.find("AugmentationDot") if rhythm_el is not None else None
    dots_orig = int(dot_el.get("count", "1") or "1") if dot_el is not None else 0
    t_el = rhythm_el.find("PrimaryTuplet") if rhythm_el is not None else None
    tuplet = None
    if t_el is not None:
        tuplet = (int(t_el.findtext("Num") or "1"),
                  int(t_el.findtext("Den") or "1"))

    unit_list: list[tuple[int, str, int, Optional[tuple[int, int]]]] = []
    if tuplet is not None:
        if len(pieces) > 1:
            raise GuitarProError(
                "跨小节的三连音拍暂不支持重排（请先手动处理该拍）"
            )
        doubled = NOTE_VALUE_QUARTERS.get(nv_orig, 0.0) * 2
        nv2 = _QUARTERS_TO_VALUE.get(doubled)
        if nv2 is None:
            raise GuitarProError("音符时值翻倍后超出 GP 支持范围")
        unit_list = [(pieces[0][0] - base_k, nv2, dots_orig, tuplet)]
    else:
        for k, ps, pe in pieces:
            for nv, dots in express_units(pe - ps):
                unit_list.append((k - base_k, nv, dots, None))

    orig_note_ids = []
    notes_el = b_el.find("Notes")
    if notes_el is not None:
        orig_note_ids = notes_el.text.split()

    n = len(unit_list)
    stats["beats_out"] += n
    if n > 1:
        stats["splits"] += 1
    out: list[tuple[int, ET.Element]] = []
    for i, (k_local, nv, dots, tup) in enumerate(unit_list):
        if n == 1:
            note_ids = orig_note_ids
            extras = True
        else:
            extras = i == 0
            first = i == 0
            last = i == n - 1
            note_ids = []
            for nid in orig_note_ids:
                o, d = ctx.orig_tie(nid)
                origin = (not last) or o
                dest = (not first) or d
                note_ids.append(ctx.clone_note(nid, origin, dest))
            stats["notes_cloned"] += len(orig_note_ids)  # 每个单位克隆一次
        unit = _make_beat(ctx, b_el, nv, dots, tup, note_ids, extras)
        out.append((k_local, unit))
    ctx.split_cache[cache_key] = out
    return out


def _transform_bar(ctx: _Ctx, bar_el: ET.Element, bar_abs_q: float,
                   q_per: float, factor: int, stats: dict) -> list[str]:
    """把一个轨道的旧小节拆成 factor 个新小节，返回新 Bar id 列表。"""
    voices_el = bar_el.find("Voices")
    voices = (voices_el.text or "").split() if voices_el is not None else []
    if not voices:
        voices = ["-1", "-1", "-1", "-1"]

    per_voice_units: dict[str, dict[int, list[ET.Element]]] = {}
    for vid in voices:
        if vid == "-1":
            continue
        v_el = ctx.voice_by_id.get(vid)
        if v_el is None:
            continue
        beats_el = v_el.find("Beats")
        bids = (beats_el.text or "").split() if beats_el is not None else []
        per_voice_units[vid] = {k: [] for k in range(factor)}
        pos = 0.0
        for bid in bids:
            b_el = ctx.beat_by_id.get(bid)
            if b_el is None:
                pos += 0.0
                continue
            dur = ctx.beat_duration(b_el)
            for k, unit in _split_beat(ctx, b_el, bar_abs_q, pos, dur,
                                       q_per, factor, stats):
                per_voice_units[vid][k].append(unit)
            pos += dur

    new_ids: list[str] = []
    for k in range(factor):
        nb = deepcopy(bar_el)
        nb.set("id", ctx.bar_ids())
        nv_ids: list[str] = []
        for vid in voices:
            if vid == "-1":
                nv_ids.append("-1")
                continue
            nv = deepcopy(ctx.voice_by_id[vid])
            nv.set("id", ctx.voice_ids())
            beats_el = nv.find("Beats")
            if beats_el is None:
                beats_el = ET.SubElement(nv, "Beats")
            beats_el.text = " ".join(
                u.get("id") for u in per_voice_units.get(vid, {}).get(k, [])
            )
            ctx.voices_root.append(nv)
            nv_ids.append(nv.get("id"))
        nb.find("Voices").text = " ".join(nv_ids)
        ctx.bars_root.append(nb)
        new_ids.append(nb.get("id"))
    return new_ids


def reengrave_tempo(root: ET.Element, target_bpm: float) -> dict:
    """原地重排 GPIF：tempo 改为真实 ``target_bpm`` 四分音符/分钟。

    返回信息 dict（factor、小节/拍前后数量、拆分数、克隆音符数等）。
    """
    master_track = root.find("MasterTrack")
    if master_track is None:
        raise GuitarProError("GPIF 缺少 <MasterTrack>")
    autos = master_track.findall("Automations/Automation")
    tempo_autos = [a for a in autos
                   if (a.findtext("Type") or "").strip() == "Tempo"]
    if not tempo_autos:
        raise GuitarProError("文件中没有 Tempo automation")
    if target_bpm <= 0:
        raise ValueError(f"目标 BPM 必须为正数，收到: {target_bpm!r}")

    first = tempo_autos[0]
    label, ref, effective = parse_tempo_value(
        (first.findtext("Value") or "").strip())
    f = target_bpm / effective
    if abs(f - round(f)) > 1e-9:
        raise GuitarProError(
            f"目标 {target_bpm:g} / 有效 {effective:g} BPM = {f:.4g} 不是整数倍，"
            "谱面重排版仅支持整数倍（如 73 -> 146）。"
        )
    factor = int(round(f))
    if factor < 1:
        raise GuitarProError(
            "目标 BPM 低于当前有效 BPM，无法用“拆音加小节”保持时长（仅支持提速）"
        )
    if factor == 1:
        return {"factor": 1, "bars_in": len(root.findall("MasterBars/MasterBar")),
                "bars_out": len(root.findall("MasterBars/MasterBar")),
                "beats_in": 0, "beats_out": 0, "splits": 0, "notes_cloned": 0,
                "tempo_before": label, "tempo_after": target_bpm}

    ctx = _Ctx(root)

    def time_quarters(mb: ET.Element) -> float:
        t = (mb.findtext("Time") or "4/4").strip()
        if "/" in t:
            num, _, den = t.partition("/")
            return int(num) * 4 / int(den)
        return 4.0

    old_mbs = list(ctx.mbs_root.findall("MasterBar"))
    stats = {"beats_in": 0, "beats_out": 0, "splits": 0, "notes_cloned": 0}
    abs_q = 0.0
    new_master_bars: list[ET.Element] = []
    for mb in old_mbs:
        q = time_quarters(mb)
        track_bars = (mb.findtext("Bars") or "").split()
        per_track: list[list[str]] = []
        for tb in track_bars:
            bar_el = ctx.bar_by_id.get(tb)
            if bar_el is None:
                per_track.append(["-1"] * factor)
                continue
            per_track.append(
                _transform_bar(ctx, bar_el, abs_q, q, factor, stats))
        for k in range(factor):
            nmb = deepcopy(mb)
            bars_el = nmb.find("Bars")
            if bars_el is not None:
                bars_el.text = " ".join(
                    ids[k] for ids in per_track)
            if k > 0:  # 段落标记只保留在第一个新小节
                for sec in nmb.findall("Section"):
                    nmb.remove(sec)
            new_master_bars.append(nmb)
        abs_q += q

    # 移除被替换的旧元素（音符池保留，可能仍被共享）
    for el in old_mbs:
        ctx.mbs_root.remove(el)
    for el in ctx.old_bars:
        ctx.bars_root.remove(el)
    for el in ctx.old_voices:
        ctx.voices_root.remove(el)
    for el in ctx.old_beats:
        ctx.beats_root.remove(el)
    for el in new_master_bars:
        ctx.mbs_root.append(el)

    # 速度 automation：标签 ×factor（ref=2），Bar 序号 ×factor；其余 automation 的 Bar 也缩放
    for a in autos:
        bar_el = a.find("Bar")
        if bar_el is not None and (bar_el.text or "").strip().lstrip("-").isdigit():
            bar_el.text = str(int(bar_el.text) * factor)
        if (a.findtext("Type") or "").strip() == "Tempo":
            lab, ref2, eff = parse_tempo_value((a.findtext("Value") or "").strip())
            a.find("Value").text = f"{eff * factor:g} 2"

    return {
        "factor": factor,
        "bars_in": len(old_mbs),
        "bars_out": len(new_master_bars),
        "beats_in": stats["beats_in"],
        "beats_out": stats["beats_out"],
        "splits": stats["splits"],
        "notes_cloned": stats["notes_cloned"],
        "tempo_before": label,
        "tempo_after": target_bpm,
    }
