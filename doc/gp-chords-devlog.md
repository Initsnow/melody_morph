# 从零解析 .gp 到自动标注和弦：一个踩坑纪实

> 本文记录 `melody_morph` 项目中「自动识别并写回 Guitar Pro 和弦」这个功能的完整开发过程：格式逆向、算法设计、写回实现，以及两个只有真机才能暴露的坑。全部代码都在仓库里，纯 Python 标准库，零第三方依赖。

## 背景

用户有一份 Guitar Pro 谱（`NERDNEKO - CURE feat.初音ミク.gp`），Lead Guitar 轨手标了 4 个和弦，想知道能不能写个脚本自动把整轨的和弦都标上——而且结果必须能写回 `.gp`，让 Guitar Pro 8 正常打开、正常显示。

第一反应自然是「找个现成库」。但现实是：

- `PyGuitarPro` 只支持 GP3–5 旧格式；
- GP6/7/8 的 `.gp` 是另一套完全不同的结构；
- 网上对 GPIF 的文档零散且不成体系。

结论：没有可用的库，那就自己写。这反而成了整个项目最扎实的部分——**格式是你亲手摸出来的，后面所有坑都是在这套认知上定位的**。

## 第一步：.gp 到底是什么

把 `.gp` 当 zip 打开，立刻真相大白：

```text
Content/
  Assets/xxx.wav          # 内嵌音频
  BinaryStylesheet
  LayoutConfiguration
  PartConfiguration
  Preferences.json
  ScoreViews/0.gpsv       # 视图缓存（protobuf）
  Stylesheets/
  score.gpif              # 真正的乐谱，XML
VERSION                   # 7.0
meta.json
```

`score.gpif` 是核心，内部写着 `<GPVersion>8.1.4</GPVersion>`。整个乐谱（轨道、小节、拍、音符、和弦库、调号）都是 XML，结构大概是这样：

```xml
<GPIF>
  <Score>…</Score>
  <MasterBars>…</MasterBars>
  <Tracks>
    <Track id="0">
      <Name><![CDATA[Lead Guitar]]></Name>
      <Staves><Staff><Properties>
        <Property name="Tuning">…</Property>
        <Property name="DiagramCollection"><Items>…</Items></Property>
      </Properties></Staff></Staves>
    </Track>
  </Tracks>
  <Voices>…</Voices>
  <Beats>…</Beats>
  <Rhythms>…</Rhythms>
  <Notes>…</Notes>
</GPIF>
```

几个关键点：

- **拍（Beat）是全局对象**：`<Beats>` 里存所有拍，每个 `<Voice>` 用一串 id 引用它们。同一个 riff 拍会被几十上百个位置复用，而不是复制。这直接决定了后面写回时的克隆策略。
- **和弦是两套东西**：轨道级「和弦库」`DiagramCollection`（每个 Item 含指法图 + 和弦构成），和拍级引用 `<Chord><![CDATA[i]]></Chord>`（i 是库内 Item 的 id）。
- **文本普遍用 CDATA 包裹**：标题、轨道名、段落记号，包括拍上的和弦引用。

于是有了 `gp_parser.py`：`zipfile` + `xml.etree` 两个标准库模块，解析出 `GPSong → GPTrack → GPMeasure → GPBeat → GPNote` 的完整对象模型，顺手提供 `gp_info.py` 当查看工具。写解析器的过程本身也是第一轮验证——能把自己的谱读回来、和 GP8 里看到的信息对上，格式认知才算数。

## 第二步：和弦识别算法

识别逻辑在 `annotate_chords.py`，思路不复杂，但细节决定成败：

1. **分析窗口**：默认整小节，可选半小节、逐拍。
2. **收集音级**：窗口内所有音符按**时值加权**映射到 12 个音级（pitch class）。
3. **打分**：对 21 种和弦模板（maj/min/dim/aug/sus2/sus4/5/6/7/maj7/m7/m7b5/dim7/add9/9/maj9/m9/7sus4/6-9）逐一计算：

```python
score = 0
for pc, weight in window_pitch_classes.items():
    if pc in chord_pcs:
        score += weight            # 命中和弦音
    else:
        score -= weight            # 非和弦音扣分
for missing in chord_pcs - set(window_pitch_classes):
    score -= missing_penalty       # 缺了和弦音也扣分
if root == key_root:
    score += 1                     # 调内根音小加成
if bass == root:
    score += 1                     # 低音等于根音小加成
```

4. **调性**：优先读文件调号；没有调号时用 Krumhansl-Kessler 键感轮廓估计。
5. **吉他风格收敛**：没有三音就收敛成强力和弦（`C5`），低音不是根音就写成斜杠和弦（`C5/G`）——这是 GP 谱里最常见的记法。

算法上线后和用户的手标做了对照：4 处手动标注里「标注拍到小节末」窗口下名称、根音全部一致。产品层面也按用户要求迭代了三次：

- 不指定 `--track` 时交互选择轨道；
- 默认自动写 `<原名>_chords.gp`；
- 逐小节明细只在 `--debug` 时打印。

## 第三步：写回 .gp

写回是真正的雷区，两个大坑都在这。

### 坑前传：共享拍的克隆

GPIF 的 beat 是复用的。给一个被 104 处复用的 riff 拍挂上和弦，会把和弦泄漏到整个 riff。正确做法：目标拍若被多处引用，就 `deepcopy` 一份、分配新 id、追加到 `<Beats>`，然后把**当前这一处**的引用替换掉：

```python
if len(usage[current_id]) > 1:
    new_id = str(next_beat_id); next_beat_id += 1
    new_beat = copy.deepcopy(beat_el)
    new_beat.set("id", new_id)
    beats_container.append(new_beat)
    beats_tokens[pos] = new_id      # 只替换这一处
```

这也是为什么最终 172 处标注里克隆了 145 个拍。

同时向 `DiagramCollection` 追加新的和弦 Item：`<KeyNote>/<BassNote>/<Degree>` 描述构成，`<Diagram>` 描述指法图。

## 大坑一：GP8 打不开文件

第一次写回，`zipfile` 校验通过、XML 可解析、alphaTab（开源的 GPIF 解析器）也能读——但 **Guitar Pro 8 就是拒绝打开**。

这个「第三方能读、真身拒开」的差异，只能从 GP8 自己的产物里找答案。三条证据链：

1. **扫用户机器上 123 个 GP8 写的 `.gp`**：462 个和弦图全部是 `fretCount="5"`，我们写的却五花八门（2/3/5/6）。
2. **在 GPCore.dll 的字符串里挖到一条断言**：
   `fret == InvalidFret || fret == 0 || (fret > base_fret && fret <= base_fret + spanLimit())`
3. **对比 GP8 自己写的 `<Diagram>` 结构**：元素顺序是 `Fret* → Fingering → Property*`，且 Fingering 要列全 6 根弦。

结论：GP8 的指板图是一个**固定 5 品窗口**，所有按弦品必须落在 `(baseFret, baseFret+5]` 内。我们之前的指法生成器会给出 6 品甚至更高的按法，还漏了 `<Fingering>`、把 `<Property>` 包进了错误的 `<Properties>` 容器——违反的每一项都会让 GP8 拒开。

修复：

- 按弦只搜低把位 0–5，`fretCount="5"`、`barsStates="1 1 1 1 1"`；
- `baseFret` 按 `max(0, max_fret - 5)` 选取；
- `<Fingering>` 补全（按下的弦给指法，没按的给 `finger="None" fret="4294967295"`）；
- 三个 `Show*` Property 直接挂在 `<Diagram>` 下；
- 顺手修正了弦号映射：GP8 是 **0 起从低到高**（0=6 弦）；
- zip 逐项保留原压缩方式，避免容器层面的任何差异。

这一轮修完，文件能开了。

## 大坑二：打开了，但所有和弦都不显示

用户反馈：**能打开，但一个和弦都没有——连原来手动标的那 4 个都不见了。**

手动标注的拍和库 Item 我们明明原样保留了，这太诡异了。数据自查（XML 可解析、alphaTab 能读到 172 个和弦引用）全部通过，说明问题在 GP8 的读取路径上，普通解析器测不出来。

于是换思路：**用真机做最小化对照实验**。把改动拆成四个样本，逐个用 GP8 打开：

| 样本 | 改动 | GP8 显示 |
|---|---|---|
| V2 | 原文件仅重打包（gpif 字节不变） | 和弦正常 |
| V3 | 原 gpif 仅做 XML 序列化往返 | **所有和弦消失** |
| V3b | V3 + 把拍上 `<Chord>` 恢复成 CDATA | 和弦恢复 |
| V4 | 完整写回 + CDATA 恢复 | 172 处全部显示 |

真凶水落石出：**ElementTree 序列化会把 CDATA 降级成普通文本**，而 GP8 的解析器只认 `<Chord><![CDATA[0]]></Chord>` 这种写法。普通文本会被静默忽略——于是整个文件的和弦符号全军覆没，包括原本手标的。

修复：写回时先收集原文件的 (标签, 文本) CDATA 对，序列化后按对恢复；新增的和弦引用统一写成 CDATA：

```python
xml_text = re.sub(
    r"<Chord>(\d+)</Chord>",
    r"<Chord><![CDATA[\1]]></Chord>",
    xml_text,
)
```

最终验证：直接启动 GP8 打开成品文件，用 PowerShell + Windows.Media.Ocr 截屏 OCR，肉眼（机器）确认 C/F、C5/G、G5 和新标注的 Am9、Fmaj7 等全部显示。

## 方法论：几个可复用的调试套路

1. **格式认知要来自一手样本**。别只看文档，把目标软件自己写的文件当语料扫一遍——统计规律往往比文档更可靠（123 个文件、462 个和弦图全是 5 品窗口，这就是铁证）。

2. **二进制里也有契约**。目标软件是原生程序时，`strings` 一把梭经常能挖到断言、校验、字段名（那条 `fret <= base_fret + spanLimit()` 就是直接决定了修复方向）。

3. **独立解析器只能当「下限」**。alphaTab、TuxGuitar 都能读 ≠ GP8 能读。它们宽容，GP8 严格；越接近真实软件行为，验证才越有效。

4. **最小化对照实验**。遇到「改了 A 也改了 B」的模糊局面，把改动拆成 V2/V3/V3b 一组递进样本，一次只动一个变量，真凶会自己跳出来。

5. **GUI 也能自动化测试**。没有 CLI 的桌面软件，可以用 `Start-Process` 打开文件 + `SendKeys` 导航/缩放 + 系统 OCR 读屏，把「肉眼验证」变成可重复的机器验证。

## 最终成果

```text
gp_parser.py         # GP7/8 解析器（纯标准库）
gp_info.py           # 查看工具
annotate_chords.py   # 自动识别 + 写回
doc/gp_parser.md     # 格式与实现文档
```

最终效果：172 个分析窗口全部标注，和弦库 34 项，结构（5 轨 / 945 小节 / 8055 拍）与源文件完全一致，Guitar Pro 8 正常打开、正常显示。

两个坑给这个项目的最大教训是：**「文件格式合法」和「目标软件认这个文件」是两回事**。前者靠解析器验证，后者只能靠真机。
