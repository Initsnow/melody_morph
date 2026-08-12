"""
循环和弦进行检测
================

给 gp-chords 的 ``--progressions`` 提供检测核心：从逐小节和弦序列里找出
重复的循环进行（loop family），供写回 ``P1: I-IV-V-vi`` 式自由注解。

方法（两级容差）：

1. **块链接（周期运行）**：对每个候选周期 p（2..16 小节），把序列切成
   p 小节一块，相邻两块按"罗马度数一致比例"比较；比例不低于
   ``--min-ratio``（默认 0.6）就链成同一遍循环，允许变体重复（Verse 1
   和 Verse 2 换掉几个和弦仍算同一循环）。左极大且至少两遍的运行才保留。
2. **模式聚类**：同一周期的运行按模式 LCS 相似度（默认 0.7）归成
   loop family——分散在曲中各处的同名循环（副歌 1 / 副歌 2）共享一个
   family。**逐轮选择**：每轮取覆盖最大的 family，选中后把它占用的区从
   其余 family 移除并重算覆盖——长周期 family 不能靠"之后会被丢弃的区"
   虚增覆盖抢先（16 小节变体句不会挤掉更准的 8 小节循环）。P 编号按
   首次出现顺序，谱面上先遇到的循环是 P1。

两个防错位机制：

- **周期约简**：块的度数是更短周期的精确重复时（6 小节 V-I-V-I-V-I
  本质是 2 小节循环重复 3 遍），按短周期重新链接。否则同一段和声会被
  长周期平移窗口切走，循环起点跟着偏（如 intro 的 V-I 循环被标成从
  第 3 小节开始的 6 小节窗口）。跨小节休止打断短周期重链时保留原周期。
- **同周期去重优先更大窗口**：重叠的运行若 span 更大且质量没有明显更差
  （差 ≤0.1），用更大窗口替换——大窗口通常从乐段边界起并覆盖变体遍，
  避免循环起点被"对齐更好"的平移小窗挤到乐段中间。

过滤：纯空小节构成的"循环"和只有一种度数的静态模式（[V,V]、[I,I,I]
这类持续音/踏板）都不是进行，不报告。

标注粒度：freetext 写在每个**连续运行区**（region）的起点，而不是循环的
每一遍——Intro 的 2 小节循环重复 4 遍只标一处，不会刷屏。标注内容是
**该 region 第一遍循环的完整罗马数字**（含品质，如 ``P1: I-IV-V7-vi``）；
同一 family 的不同 region 各标各的，变体直接在谱面上可见（如 Verse 1 的
region 标 ``V7``、Verse 2 的 region 标 ``V``），不再被 family 汇总模式
抹平。quality 对和声分析很重要，所以汇总模式只用于归族/聚类，展示层
按 region 保留完整品质。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from gpchords.roman import chord_to_roman

# 块链接：相邻两块按罗马度数一致比例至少达到该值才视为同一次循环
DEFAULT_MIN_RATIO = 0.6
# 模式聚类：两个运行的模式 LCS 相似度至少达到该值才归入同一 family
DEFAULT_PATTERN_SIMILARITY = 0.7
# 同周期去重：span 更大的运行质量不比已保留的低超过该值时，替换为
# 更大窗口（通常从乐段边界起、覆盖变体遍），避免只留"对齐最好"的
# 平移小窗而把循环起点挤到乐段中间。0.1 是实测折中：轨道 1 的 8 小节
# 循环平移 1 小节只掉 0.06 质量（值得换），轨道 0 副歌区横跨过渡段的
# 大窗口掉 0.15（不换，保留 q=1.0 的干净窗口）。
DEDUP_QUALITY_TOLERANCE = 0.1
# 运行匹配时 None（无和弦小节）视为弱通配：与任何 token 固定给该相似度
_WILDCARD_SIM = 0.5


@dataclass
class LoopFamily:
    """一组共享同一循环模式的连续运行区。"""

    id: str  # P1 / P2 ...
    pattern: list[str]  # family 归族/汇总用度数模式，如 ["I", "IV", "V", "vi"]；
    # 展示层由 annotate 按 region 生成含品质的完整罗马数字，不直接用它
    period: int  # 循环长度（小节）
    occurrences: list[tuple[int, int]] = field(default_factory=list)  # 1 起闭区间
    copies: int = 0  # 所有运行区的循环遍数总和
    coverage: int = 0  # 净覆盖小节数 = Σ(遍数-1)*period


def _roman_degree(roman: str) -> str:
    """罗马数字字符串 -> 度数部分（去掉品质/斜杠）：'Isus2/F#' -> 'I'。"""
    main = roman.split("/")[0].strip()
    for i, ch in enumerate(main):
        if ch in "IV" or ch in "iv":
            j = i + 1
            while j < len(main) and main[j] in "IViv":
                j += 1
            return main[:j]
    return main


def _quality_family(quality: str) -> str:
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


def chord_token(
    chord: dict, key_root: int, key_mode: str = "Major"
) -> tuple[str, str]:
    """detect_chord 结果 -> 匹配用 token (罗马度数, 品质家族)。"""
    roman = chord_to_roman(chord, key_root, key_mode)
    return _roman_degree(roman), _quality_family(chord.get("quality", "maj"))


def token_sim(a: Optional[tuple[str, str]], b: Optional[tuple[str, str]]) -> float:
    """两个 token 的相似度：度数不同即不同；同度数不同品质家族算 0.6。"""
    if a is None or b is None:
        return _WILDCARD_SIM
    if a[0] != b[0]:
        return 0.0
    return 1.0 if a[1] == b[1] else 0.6


def _block_degree_sim(
    tokens: list[Optional[tuple[str, str]]], a: int, b: int, period: int
) -> float:
    """两块 [a, a+p) 与 [b, b+p) 的罗马度数一致比例（0..1）。"""
    hits = sum(
        1
        for k in range(period)
        if tokens[a + k] is not None
        and tokens[b + k] is not None
        and tokens[a + k][0] == tokens[b + k][0]
    )
    return hits / period


def _primitive_period(
    block: list[Optional[tuple[str, str]]]
) -> int:
    """块的最小真周期：位置 k 与 k-d 的度数一致（None 视为通配）。

    返回能整除块长的最小 d；没有则返回块长。用于识别"周期是短循环
    整数倍的重复切片"（如 6 小节的 V-I-V-I-V-I 本质是 2 小节循环
    重复 3 遍），把这些切片还原成短循环，避免循环起点被长周期
    平移窗口带偏。
    """
    n = len(block)
    for d in range(1, n):
        if n % d:
            continue
        ok = True
        for k in range(d, n):
            a, b = block[k], block[k - d]
            if a is not None and b is not None and a[0] != b[0]:
                ok = False
                break
        if ok:
            return d
    return n


def _lcs_sim(a: list[str], b: list[str]) -> float:
    """两条度数序列的 LCS 相似度（长度可能因 None 通配而不同，按较长者归一化）。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    dp = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            dp[i][j] = (
                dp[i - 1][j - 1] + 1
                if a[i - 1] == b[j - 1]
                else max(dp[i - 1][j], dp[i][j - 1])
            )
    return dp[la][lb] / max(la, lb)


def find_loop_families(
    tokens: list[Optional[tuple[str, str]]],
    min_period: int = 2,
    max_period: int = 16,
    min_ratio: float = DEFAULT_MIN_RATIO,
    similarity: float = DEFAULT_PATTERN_SIMILARITY,
    min_coverage: int = 4,
) -> list[LoopFamily]:
    """从逐小节 token 序列检测循环进行 family。

    返回互不重叠、按首次出现位置排序的 loop family（P1 是谱面上先遇到的
    循环）；每处 occurrence 是一个连续运行区（1 起闭区间），``copies``
    是区内循环遍数。
    """
    n = len(tokens)
    runs: list[tuple[int, int, int, float]] = []  # (period, start0, copies, quality)
    for p in range(min_period, max_period + 1):
        for i in range(n - 2 * p + 1):
            copies = 1
            while i + (copies + 1) * p <= n and _block_degree_sim(
                tokens, i + (copies - 1) * p, i + copies * p, p
            ) >= min_ratio:
                copies += 1
            if copies < 2:
                continue
            # 左极大：上一块还能接上的运行会被 i-p 处更长的运行覆盖
            if i >= p and _block_degree_sim(tokens, i - p, i, p) >= min_ratio:
                continue
            # 运行质量：逐对块相似度的均值（阻止 50% 边缘匹配被链成长循环）
            quality = sum(
                _block_degree_sim(tokens, i + k * p, i + (k + 1) * p, p)
                for k in range(copies - 1)
            ) / (copies - 1)
            if quality >= min_ratio:
                runs.append((p, i, copies, quality))

    # 周期约简：块的度数是更短周期的精确重复时（如 6 小节 V-I-V-I-V-I
    # 本质是 2 小节循环重复 3 遍），按短周期重新链接。消除"同一段和声
    # 被长周期平移切片"的错位——如 intro 的 V-I 循环被切成从第 3 小节
    # 开始的 6 小节窗口。重链失败（如跨小节休止打断短周期）则保留原周期。
    reduced: list[tuple[int, int, int, float]] = []
    for p, i, copies, quality in runs:
        d = _primitive_period(tokens[i : i + p])
        if d < p and d >= min_period:
            copies_d = 1
            while (
                i + (copies_d + 1) * d <= n
                and _block_degree_sim(
                    tokens,
                    i + (copies_d - 1) * d,
                    i + copies_d * d,
                    d,
                )
                >= min_ratio
            ):
                copies_d += 1
            if copies_d >= 2:
                # 左极大同样适用于约简后的短周期
                if i >= d and _block_degree_sim(tokens, i - d, i, d) >= min_ratio:
                    continue
                quality_d = sum(
                    _block_degree_sim(tokens, i + k * d, i + (k + 1) * d, d)
                    for k in range(copies_d - 1)
                ) / (copies_d - 1)
                if quality_d >= min_ratio:
                    reduced.append((d, i, copies_d, quality_d))
                    continue
        reduced.append((p, i, copies, quality))
    runs = reduced

    # 同一周期内：滑动错位会产生大量互相重叠（≥2 小节）的同款运行，
    # 默认保留质量最高的一条（对齐最好）；但若另一条的 span 更大且质量
    # 没有明显更差，用更大窗口替换——大窗口通常从乐段边界起并覆盖变体遍
    # （如 8 小节循环从第 3 小节起 ×4，而不是从第 4 小节起 ×3），
    # 避免循环起点被"对齐更好"的平移小窗挤到乐段中间。
    deduped: list[tuple[int, int, int, float]] = []
    for p in sorted({r[0] for r in runs}):
        kept: list[tuple[int, int, int, float]] = []
        for r in sorted(
            (x for x in runs if x[0] == p),
            key=lambda x: (-x[3], -x[2], x[1]),
        ):
            s0, copies = r[1], r[2]
            conflict = next(
                (
                    k
                    for k in kept
                    if max(
                        0,
                        min(s0 + copies * p, k[1] + k[2] * p)
                        - max(s0 + 1, k[1] + 1)
                        + 1,
                    )
                    >= 2
                ),
                None,
            )
            if conflict is None:
                kept.append(r)
                continue
            if (
                copies * p > conflict[2] * p
                and r[3] >= conflict[3] - DEDUP_QUALITY_TOLERANCE
            ):
                kept.remove(conflict)
                kept.append(r)
        deduped.extend(kept)

    # 同一周期内按模式相似度聚类（分散的同名循环归一族）
    families: list[LoopFamily] = []
    for p, i, copies, quality in sorted(
        deduped, key=lambda r: (r[0], r[1])
    ):
        pattern = [tokens[i + k][0] for k in range(p) if tokens[i + k] is not None]
        family = None
        for cand in families:
            if cand.period != p:
                continue
            if _lcs_sim(pattern, cand.pattern) >= similarity:
                family = cand
                break
        if family is None:
            family = LoopFamily(
                id="",
                pattern=pattern,
                period=p,
            )
            families.append(family)
        start, end = i + 1, i + copies * p
        family.occurrences.append((start, end))
        family.copies += copies
        family.coverage += (copies - 1) * p

    # 丢弃纯空小节构成"循环"（pattern 里没有真实度数）
    families = [f for f in families if any(d is not None for d in f.pattern)]
    # 丢弃静态模式：pattern 只有一种度数（如 [V,V]、[I,I,I]）是持续音/
    # 踏板，不是和弦"进行"，标出来只会刷屏
    families = [f for f in families if len(set(f.pattern)) >= 2]
    # 去包含：同一 family 内被更长运行区覆盖的区只保留外层
    for f in families:
        kept: list[tuple[int, int]] = []
        for occ in sorted(f.occurrences, key=lambda o: (o[0], -o[1])):
            if not any(
                k[0] <= occ[0] and occ[1] <= k[1] and k != occ for k in kept
            ):
                kept.append(occ)
        f.occurrences = kept

    # 逐轮选择：每轮取覆盖最大的 family，选中后把它占用的区从其余
    # family 里移除并重算覆盖。长周期 family 不能靠"之后会被丢弃的区"
    # 虚增覆盖抢先——否则 16 小节变体句会挤掉更准的 8 小节循环。
    pending = list(families)
    selected: list[LoopFamily] = []
    while pending:
        pending.sort(key=lambda f: (-f.coverage, f.period))
        f = pending.pop(0)
        kept_occ = [
            o
            for o in f.occurrences
            if not any(
                _overlap(o, o2) >= 1
                for g in selected
                for o2 in g.occurrences
            )
        ]
        if not kept_occ:
            continue
        f.occurrences = kept_occ
        f.copies = sum((e - s + 1) // f.period for s, e in kept_occ)
        f.coverage = sum(
            ((e - s + 1) // f.period - 1) * f.period for s, e in kept_occ
        )
        if f.coverage < min_coverage:
            continue
        selected.append(f)
        for g in pending:
            g.occurrences = [
                o
                for o in g.occurrences
                if not any(
                    _overlap(o, o2) >= 1
                    for o2 in f.occurrences
                )
            ]
            g.copies = sum(
                (e - s + 1) // g.period for s, e in g.occurrences
            )
            g.coverage = sum(
                ((e - s + 1) // g.period - 1) * g.period
                for s, e in g.occurrences
            )

    # P 编号按首次出现顺序（谱面上先遇到的循环是 P1），不是按覆盖量
    selected.sort(key=lambda f: min(s for s, _ in f.occurrences))
    for idx, f in enumerate(selected, start=1):
        f.id = f"P{idx}"
    return selected


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]) + 1)


def loop_label(family: LoopFamily) -> str:
    """freetext 展示文本：'P1: I-IV-V-vi'。"""
    return f"{family.id}: {'-'.join(family.pattern)}"
