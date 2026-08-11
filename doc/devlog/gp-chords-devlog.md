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
gpchords/
├── parser.py        # GP7/8 解析器（纯标准库，库不提供用户命令）
├── info.py          # 内部查看工具（`uv run gp-info`，调试用）
└── annotate.py      # 自动识别 + 写回（`uv run gp-chords`，用户入口）
doc/gp_parser.md     # 格式与实现文档
```

最终效果：172 个分析窗口全部标注，和弦库 34 项，结构（5 轨 / 945 小节 / 8055 拍）与源文件完全一致，Guitar Pro 8 正常打开、正常显示。

两个坑给这个项目的最大教训是：**「文件格式合法」和「目标软件认这个文件」是两回事**。前者靠解析器验证，后者只能靠真机。

---

## 和弦识别改进计划（doc/plan/gp-chords-plan.md）执行记录

### 先建测试集，再动算法

第 1 步先落地了 `tests/test_chords.py`（45 个 pytest 用例）和
`tests/benchmark_chords.py`（可重复运行的基准脚本）。基准实测（改造前）：
《无论如何 - 副本.gp》37 处手工标注，整小节根音 23/37、标注拍到小节末
根音 27/37；合成用例 Am7→C6/A、Dm7/F→F6、C+F→Fmaj9、G5+C→Csus2/G、
小节 53/56→Am9/C 全部判错。

### 三个隐藏的 parser bug

计划里列的是打分、切窗、模板、调性，但动手时先挖出三个 parser 级 bug，
它们直接决定了"真实时值"和"拼写"两项 TODO 的修法：

1. **GPIF 的十六分音符叫 `16th`，不叫 `Sixteenth`**。原 `NOTE_VALUE_QUARTERS`
   只认 `Sixteenth`，于是所有 16 分音符时长解析成 0，再被 `max(dur, 1.0)`
   抬回 1.0——这就是"十六分音符与四分音符同权"的根源。修复后真实时值
   生效，16 分经过音权重只有 0.25，不再污染和弦识别。
2. **GP8 音符的 `<Accidental>` 写的是符号本身（`#`）**，和弦库里却写
   `accidental="Sharp"`。原解析器只认后者，导致所有升号音名显示成
   白键名（midi 54 显示 F4 而非 F#4）。两种写法现在都收。
3. **延音延续（tie destination）音符不重复计权**：起点音符保留真实时长，
   同窗内的延续音符只补时长不加攻击；整窗都是延续（跨小节承接窗）时
   回退为按实际时值计权，避免空权重崩溃（样例文件 125 小节就是这种窗）。

### 打分核心怎么改

原公式 `matched - 0.8×unmatched - 1.0×len(tpl) + 调性加分` 有两个问题：
主音加分按 `matched` 比例放大，任何在 A 大调里的 C 系和弦都被拉成
Am9/C；模板每多一个音固定扣 1.0，扩展和弦要么全赢要么全输。

新公式：`matched - 0.8×unmatched - 1.0×missing - 0.5×len(tpl)`，低音音级
权重 ×2，调性完全不进主分数，只做同分破平。破平顺序
`分数 > 调内/主音 > 低音=根音 > 根音权重 > 模板更简 > 根音编号` 是
在真实文件上反复试出来的：

- 调内/主音必须在低音之前：否则 `Cmaj7/E`（C 大调主音）会被 `Em7/C`
  （低音=根音 E）抢走；
- 低音=根音必须在根音权重之前：否则 `C#m7`（E 被八度加倍）会被
  `E6/C#` 抢走；
- `COMPLEXITY_PENALTY = 0.5` 是 15 个真实样例文件上的经验最优值，
  0.7 不增不减、1.0 会压死扩展和弦、0.0 会让 13/11 模板到处抢戏。

斜杠低音拼写不再按调性取 sharp/flat，而是按和弦品质的度数关系拼写：
C7 的 b7 低音就是 Bb，无论当前调性怎么写。`C7/A# → C7/Bb` 修复。

### auto 切窗的取舍

按拍指纹合并（相同或互为子集），PC 集不再兼容时切分；权重占比 < 20%
的独立组并入相邻组（16 分经过音不切成单独和弦）。这个 20% 阈值决定了
样例小节 51：Em 延长 3.5 拍 + 尾部 0.5 拍的 Dm7 经过，合并成一窗
识别为 G6/9/E（验收要求），而小节 56 的 C#m7（占 33%）正确切开。

真实 GP 谱里常见的"整和弦一拍 + 单音琶音尾"能被正确合并；逐音琶音
（C-G-B-E 每拍一个音）原来会被切分成多窗——这个问题在后续的
"单音/双音证据门槛"修复中一并解决，见下文。

### 模板与调性

参照 pychord 的 `DEFAULT_QUALITIES` 新增 18 种（7b5/7#5/7b9/7#9/9sus4/
7#11/9#11/maj7#11/maj7#5/maj7sus2/add11/madd4/mmaj7/m6/9/11/m11/13/maj13），
13 和 maj13 按 pychord 原样含 11 音——这也避免了小节 51 的 G13 抢走
G6/9/E（窗口里没有 C 这个 11 音，G13 缺失扣分）。`DEGREES` 同步补全，
写回 .gp 已实测可解析。

调性默认改为逐小节调号（样例文件 50 小节转 C、57 小节转回 A），
`--key-per-section` 供没有逐小节调号的文件按段落回退；段落调内覆盖率
低于 0.65 时才尝试 K-K 重估（实测桥段 47-49 覆盖率 0.90，不会误触发）。

### 结果

- `uv run pytest tests`：45 passed；
- 合成场景根音/名称 30/30，auto 切窗 3/3；
- 样例文件：整小节根音 23→27、名称 14→17；标注拍到小节末根音 27→31；
  auto 窗口根音 31/37；验收小节 51 → G6/9/E、53/56 → Cmaj7；
- 全量 15 个带手工标注的真实文件：整小节根音 92→103、尾窗根音 105→113，
  无测试集回退；个别爵士向文件（Lost、对iyowa）的整小节读数被新 13/11
  模板带偏，auto 窗口整体更准，属计划预期的"模板变多增加歧义"。

---

## 单音/双音证据门槛与逐音琶音合并（计划后续修复）

实测发现两个误识别：

- 单音 C/D/E/G 被标成 C5/D5/E5/C5/G——旋律单音被写成强力和弦；
- 双音 E+G → C/E、C+Bb → Fsus4/C、C+D → Csus2——和弦碎片被硬猜成
  完整和弦；
- 逐音琶音 C-G-B-E 每拍一个音时，auto 切成 4 窗标成 C5、C5/G、B5、E5。

修复分两层：

1. **`detect_chord` 加证据门槛**：窗口只有 1 个音级时返回 None（无法确定
   和弦）；只有 2 个音级时仅当是纯五度（强力和弦，两个方向都算）才识别，
   其余双音一律返回 None。写回时 None 窗口自动跳过，不再产生误导性符号。
   代价是单音小节不再自动标注——这是刻意的保守：宁可留空也不瞎猜。
2. **auto 切窗加逐音琶音合并**：单音/双音碎片在满足"相对上一拍是跳进
   （非级进）且并集能落在某个模板和弦内"时并入当前组。这样
   C-G-B-E / C-E-G-B 合成一窗 Cmaj7；音阶跑动 C-D-E-F-G 因为级进
   不合并（每音单独成窗且不标注）；C 和弦后接 G5 双音这类真实变化
   也不误并（双音碎片只并入尚未成形的组，或单音碎片并入完整组）。

测试新增 16 个用例（`uv run pytest tests` 61 passed），全量真实文件
根音准确率 116/236（整小节）、148/236（尾窗）、158/236（auto），
与此前持平、无回退。

---

## 先现音（anticipation）处理（ヒグレギ 谱面修复）

真实谱例《ヒグレギ.gp》暴露了一个系统性误判：小节末的 8 分音符和弦
（如 A6/9 小节第 3.5 拍的 B-F#-B）写成 tie 进下一小节——这是下一小节
和弦（B5）的先现/抢拍。原算法把它吸收进本小节窗口，导致 8 个
A6/9 → B5 交替小节全部被误判成 B7sus4/A（B7sus4 恰好完整覆盖
A-E + B-F# 四个音，A6/9 反而缺 C# 扣分）。

修复：auto 切窗的"小权重吸收"环节跳过先现音组——先现音保留为
自己的窗口（B5 / G#5），不再并入主窗口。判定分两层：

1. **tie 信号**：组内音符全部是延音起点（tie_origin），跨小节延续；
2. **跨小节和声一致**：不延音但小节末 1/4 内的短尾组（<20% 权重）
   与下一小节首组和弦同根音，同样视为先现音。

第 2 层正是"不延音也是 anticipation"的落地：需要把下一小节传入
切窗器（`segment_auto(measure, next_measure)`），先对下一小节做首组
指纹分组再比对根音。代价是《无论如何》小节 51 的 16 分尾音
D-A-F（与 52 小节 Dm 同根音）从"并入主窗口"改为独立成窗：
auto 结果 [Em, Dm]（整小节视图仍是 G6/9/E）。这比原来的 G6/9/E
更贴近实际弹奏——尾音本来就是 Dm 的先现。

修复后《ヒグレギ.gp》L 轨 auto 根音一致 151/173 → 162/173：
- 小节 43/47/51/55/107/111/115/119：B7sus4/A → [A5, B5]（抢拍独立成窗）；
- 小节 45：G#sus4（吸收 D# 抢拍）→ [C#5, G#5]（G#5 抢拍独立成窗）。

另外两点核查结论：
- R 轨小节 75 的单个 G# 音符：修复后不再标成 G#5（返回 None，不写回）；
- R 轨小节 43 的 Eadd9 是对的：音符 E+G#+F#（E 大三度 + 9 音，5 音 B
  未弹但属常规省略），与谱面原标一致。

全量 15 个真实文件：整小节 116/236、尾窗 148/236 持平无回退，
auto 窗口 158 → 167/236（`uv run pytest tests` 64 passed）。

---

## 先导单音并入成形和弦（ヒグレギ 158 小节 Fsus2 修复）

158 小节 L 轨音符是 G-F-C-F 四个单音（Fsus2 琶音），谱面原标 Fsus2。
此前 auto 窗口把开头的 G 单音切走（级进不合并），剩下 F-C-F 识别成
F5，与谱面原标 Fsus2 并排出现——同一小节两个和弦。

修复：吸收环节新增"先导单音并入相邻成形和弦"规则——相邻组明显更重
（已成形），且并集能识别出包含该单音的三音以上和弦时并入
（`_single_note_fits_neighbor_chord`）。音阶跑动里相邻单音等权，
不会触发。修复后 158/159 小节 auto 整窗 = Fsus2。

`uv run pytest tests` 65 passed；《ヒグレギ》R 轨 auto 根音一致
75 → 81/141。

注意：原谱《ヒグレギ.gp》L 轨 173/173 小节、R 轨所有有音符的小节
都已有吉他手自己的和弦标注，脚本默认跳过已标注小节；用户文件中出现
的 B7sus4/A 是把原标 A6/9 覆盖（--overwrite）产生的。重新生成应基于
原文件且不加 --overwrite，原标会被保留。

---

## 复杂和声谱的节奏吉他误判（ルサンチマン「きっとそう」修复）

《きっとそう.gp》的 Rhythm Guitar 轨是复杂和声谱（借用和弦、副属和弦、
b2 低音经过音密集），全轨 172 个 auto 窗口里暴露出三类系统性误判，
全部来自打分/破平规则本身，而不是切窗：

1. **m7 缺五音没有模板**：D-F-C（实际是 Dm7 缺 A）被硬判成 F6/D——
   F6 模板里 A 根本不在场，只因为缺 1 音 + 根音 C/F 在调内就赢了。
   新增 `m7(no5)` 模板（Dm7(no5) = D-F-C、Gm7(no5) = G-Bb-F、
   Bbm7(no5) = Bb-Db-Ab），`DEGREES` 同步补全（写回时五度标记
   omitted=true）。受影响：39/40/81/82/89/90/105 小节 Dm7(no5)、
   16/20/121/125/126 小节 Gm7(no5)、122 小节 Bbm7(no5)/Cm7(no5)。
2. **m7/6 同音集时被调性破平带偏**：D-F-A-C 既是 Dm7 也是 F6，原破平
   顺序"调内/主音 > 低音=根音"在 F 大调下永远选主音 F6/D；但吉他谱
   习惯（也是已有测试的意图：Am7 不得写成 C6/A、Dm7/F 不得写成 F6）
   是这类同音集一律取 m7 读法。破平顺序改为
   `分数 > m7/6 家族偏好 > 调内/主音 > 低音=根音 > 根音权重 > ...`，
   家族偏好只作用于 m7/6 这一对（其余 no3/no5 变体靠分数区分），
   避免抢走 C-Eb-F 的 Cmadd11(no5) 这类低音=根音判定。受影响：
   2/6/10/14/47/48/65/69/73/97/98/106 小节 Dm7（原 F6/D）。
3. **七和弦缺 7 音没有额外惩罚**：C-C#-E-G（低音 C#）被判成 C7b9/Db，
   但它根本没有 b7(Bb)；同音集更合理的读法是 A7#9/C#（A7#9 的
   7 音 G 在场，只是根音 A 省略——吉他声部省略根音很常见）。
   新增 `MISSING_SEVENTH_PENALTY = 0.5`：七和弦模板缺 7 音时额外扣分。
   受影响：87/88 小节 C7b9/Db → A7#9/C#。

结果：节奏吉他 172 个窗口改动 29 处，全部落在这三类修复内；
主音吉他 6 处连带变化（D-F-C 琶音片段 F5 → Dm7(no5)/F 等，
属于同一修复的自然延伸，未发现回退）。`uv run pytest tests`
121 passed（新增 4 个回归用例）。新生成文件：
`ルサンチマン-きっとそう-04-07-2026_key_chords_v2.gp`。

---

## 罗马数字自由注解（参照《春日影.gp》的 Isus2）

用户希望 gp-chords 在写回和弦时，顺带用 GP 的"自由文本"注解写罗马数字，
参照《春日影.gp》里 Beat 上的手工标注：`<FreeText><![CDATA[Isus2]]></FreeText>`
紧跟在 `<Chord>` 前——B 大调下 Bsus2 的罗马数字就是 Isus2。

实现分四块：

1. **`gpchords/roman.py`**：`chord_to_roman(chord, key_root, key_mode)` 把
   识别结果转罗马数字。度数优先按音级落位（B 大调里拼写为 Gb 的根音
   与 F# 同音 -> V，功能优先于字母拼写），调外根音按字母对应音级加
   升降号（B 大调里 C -> bII）；大小写按品质（I/IV/Isus2/V5 大写，
   ii7/vii°/iiø7 小写），小写度数已隐含小调性，后缀省略开头的 m
   （C#m7 -> ii7）；斜杠低音保留音名（Isus2/F#）。
2. **写回**：`write_chords_to_gp` 默认在挂和弦的拍上同时写
   `<FreeText>`，调性取该窗口所在小节的调号（支持中途转调）；
   位置与 GP8 原生文件一致（Chord 前），CDATA 由 `restore_cdata`
   统一补成 `<FreeText><![CDATA[...]]></FreeText>`。已存在的自由文本
   默认保留，`--overwrite` 才替换；`--no-roman` 整体关闭。
   小调默认按**关系大调**记度数（A 小调 Am -> vi、Dm -> ii、Em -> iii，
   流行和弦表常用，便于直接对照 I-V-vi-IV），`--roman-tonic-minor`
   切回主音小调（Am -> i、G#dim -> #vii°）。
3. **解析器**：`GPBeat.free_text` 读出拍上的自由文本，写回后自检
   与 `gp-info` 都能看到罗马数字。
4. **顺带修一个拼写 bug**：`_FLAT_KEYS` 里含 11 导致 B 大调（5 个升号）
   被按 Cb 拼写——C#m 识别成 Dbm、F# 低音写成 Gb。B 大调按规范用升号，
   从表里去掉 11；对《春日影.gp》实测 C#m(no5)/F#/G#m 等全部恢复
   升号拼写，罗马数字随之正确（Isus2/F# 而不是 Isus2/Gb）。

实测《春日影.gp》Rhythm Guitar 轨 `--overwrite`：143 处和弦全部带罗马
数字自由注解，Bsus2 -> Isus2 与参照完全一致；`uv run pytest tests`
135 passed（新增 14 个罗马数字用例，含真实文件写回验证）。
