# Guitar Pro 解析与自动和弦标注

## 有没有现成的库？

**GP3 / GP4 / GP5**（旧二进制格式）：有，[PyGuitarPro](https://pypi.org/project/PyGuitarPro/)，
可以解析、修改、另存这三种格式。安装：`uv add PyGuitarPro`。

**GP6（`.gpx`）、GP7 / GP8（`.gp`）**：目前**没有维护良好的 Python 库**。
PyGuitarPro 明确只支持 GP3-5；AlphaTab（C#/JS）虽支持更多格式但不是 Python。
因此对 `.gp` 这类格式，自写解析器是合理选择——它的数据不是晦涩的二进制，
而是 **zip 压缩包 + 一份 XML（`Content/score.gpif`）**，解析门槛很低。

本仓库的 `gpreader` 就是针对这一格式的独立读取库（GP6 的 `.gpx` 结构相同，也可解析）。
检测到 GP3-5 时它会给出提示并建议使用 PyGuitarPro，而不是报一个莫名其妙的错。

它只负责"读"，不掺和弦识别、调性估计等音乐判断——那些在 `gpchords` 包里
（`gp-chords` 标注和弦、`gp-key` 写入调号）。GP 文件的文件层写回（替换
`score.gpif` 并保留 zip 元数据）也收在 `gpreader.writer` 里。

## 格式原理

一个 `.gp` 文件本质是 zip：

```
Content/score.gpif   # 乐谱主数据（XML）
Content/Assets/...   # 音频、图片等资源
VERSION              # 如 "7.0"
```

`score.gpif` 里的核心结构：

```
GPIF
├─ Score            # 标题、艺术家
├─ Tracks           # 轨道（名称、音色、调弦、和弦库）
├─ MasterBars       # 小节：拍号、调号、段落；Bars 列表按轨道顺序给出各轨小节 id
├─ Bars             # 小节 -> 声部 id 列表
├─ Voices           # 声部 -> 拍 id 列表
├─ Beats            # 拍 -> 节奏、音符 id 列表、和弦引用
├─ Notes            # 音符（MIDI 编号、品、弦）
└─ Rhythms          # 时值（四分/八分…附点、三连音）
```

和弦标注有两层：

- **轨道和弦库**：`Track > Staves > Staff > Properties > DiagramCollection`，
  每项是一个 `Item id="N" name="C/F"`，内含和弦构成（根音/低音/音级）与指板图。
- **拍上的引用**：`Beat > Chord` 是 CDATA 数字，指向该轨道和弦库的第 N 项。

`gpreader` 把这两层都解出来：`GPTrack.chords` 是和弦库，
`GPBeat.chord` 是具体某一拍挂的和弦。

## 解析器 API

```python
from gpreader import parse_gp, select_track, detect_format

fmt, version = detect_format("song.gp")      # ("gp", "7.0")
song = parse_gp("song.gp")

track = select_track(song, "Lead Guitar")    # 名称/索引都可以
for measure in track.measures:
    for beat in measure.beats:
        print(measure.index, beat.start_quarters,
              beat.chord.name if beat.chord else None,
              [n.pitch_name for n in beat.notes])
```

数据模型：

- `GPSong`：版本、标题、艺术家、`tracks`
- `GPTrack`：`name`、`program`（音色）、`tuning`（调弦 MIDI）、`chords`、`measures`、`notes`
- `GPMeasure`：`index`（从 1 起）、`time_signature`、`key_signature`、`section`、`beats`
- `GPBeat`：`start_quarters`（小节内位置）、`duration_quarters`、`chord`、`notes`
- `GPNote`：`midi`、`pitch_name`（如 `F4`）、`fret`、`string`、`duration_quarters`

## 命令行

`gpreader` 是纯读取库（`from gpreader import parse_gp`），不提供用户命令；
`gpchords` 包在其上提供三个命令：`gp-info`（查看内部结构，调试用）、
`gp-chords`（自动标注和弦，用户入口）和 `gp-key`（自动判断调性并写入调号）。

### 查看轨道内容

```bash
# 轨道一览
uv run gp-info "song.gp"

# 查看 Lead Guitar 每个小节的音符与和弦
uv run gp-info "song.gp" --track "Lead Guitar"

# 只看有和弦标注的小节
uv run gp-info "song.gp" --track "Lead Guitar" --chords
```

### 自动标注和弦

```bash
# 默认：交互选择轨道 -> 按小节识别 -> 自动写回 <原名>_chords.gp（原文件不变）
uv run gp-chords "song.gp"

# 指定轨道
uv run gp-chords "song.gp" --track "Lead Guitar"

# 多轨道：每轨单独分析、单独标注（--write-tracks 可指定写回哪些分析轨道）
uv run gp-chords "song.gp" --track "Lead Guitar,Rhythm Guitar"
uv run gp-chords "song.gp" --track all --no-write
uv run gp-chords "song.gp" --track "Lead Guitar,Rhythm Guitar" --write-tracks "Lead Guitar"

# 合并多轨音符识别：和弦拆在两轨、或需要贝斯补低音时；
# 默认写回第一个分析轨道，--write-tracks all 写回全部分析轨道
uv run gp-chords "song.gp" --track "Lead Guitar,Electric Bass" --merge
uv run gp-chords "song.gp" --track all --merge --write-tracks all

# 按节拍识别，结果存 JSON
uv run gp-chords "song.gp" --track 0 --window beat --out chords.json

# 指定写回路径；或只看分析结果、不写回
uv run gp-chords "song.gp" --track "Lead Guitar" --write out.gp
uv run gp-chords "song.gp" --track "Lead Guitar" --no-write

# 输出每个小节的识别明细（默认不打印）
uv run gp-chords "song.gp" --track "Lead Guitar" --no-write --debug

# 已有手工标注的小节默认保留；--overwrite 时也写入/替换
uv run gp-chords "song.gp" --track "Lead Guitar" --overwrite --write

# 写回时默认在每拍和弦旁同时写罗马数字自由注解（如 B 大调下 Bsus2 -> Isus2，
# 与 GP 的"自由文本"注解同机制，--no-roman 可关闭）；调性按各小节调号计算，
# 小调按关系大调记（A 小调 Am -> vi，--roman-tonic-minor 切回主音小调）
uv run gp-chords "song.gp" --track "Rhythm Guitar"

# 指定调性 / 理论风格（不做强力/斜杠收敛）
uv run gp-chords "song.gp" --key "Am" --style theory

# 不依赖文件，看算法演示
uv run gp-chords --demo
```

### 自动判断调性并写入调号

```bash
# 估计全局调性并写入全部小节 -> <原名>_key.gp（原文件不变）
uv run gp-key "song.gp"

# 按段落估计 / 强制指定调性
uv run gp-key "song.gp" --per-section
uv run gp-key "song.gp" --key Am

# 只用指定轨道估计；只看结果不写回
uv run gp-key "song.gp" --track "Lead Guitar"
uv run gp-key "song.gp" --no-write
```

原理与限制见 [gp_key.md](gp_key.md)。

## 和弦识别算法

1. **确定调性**：优先读 GP 调号；没有调号时用 Krumhansl-Kessler
   键感轮廓对全轨音符做相关估计。
2. **窗口内音级加权**：每个音符按时值加权（短于四分音符的至少计 1）。
3. **模板打分**：对 12 个根音 × 21 种和弦模板（大三/小三/属七/大七/小七/
   挂留/强力和弦/六/九和弦等）计算
   `得分 = 命中音权重 − 0.8 × 非和弦音权重 − 模板音数`
   并加“主音优先、调内根音其次、低音等于根音”的少量先验。
4. **风格收敛**：`--style guitar` 时，若窗口内没有三音则收敛成强力和弦（5），
   低音不是根音时写成斜杠和弦（如 `C5/G`），贴近 Guitar Pro 常见记法。

## 写回 `.gp` 的原理与安全措施

运行时会自动把识别结果写成一个**新的** `.gp` 文件（默认 `<原名>_chords.gp`，
`--write 路径` 可指定位置，`--no-write` 关闭），原文件不动。写入分三步：

1. **和弦库**：向目标轨道的 `DiagramCollection` 追加缺失的和弦项
   （`<KeyNote>/<BassNote>/<Degree>` + 指板图），已有同名和弦直接复用。
   指板图由贪心算法从低音弦往高音弦生成，只为显示；和弦符号以名称为准。
2. **挂拍**：在目标拍的 `<Beat>` 里写 `<Chord>CDATA[i]</Chord>`，
   `i` 是和弦库索引。
3. **罗马数字自由注解**：默认在同一个 `<Beat>` 里再写一个
   `<FreeText>CDATA[Isus2]</FreeText>`（GP 的"自由文本"注解，显示在谱表上方，
   与《春日影.gp》里的手工注解 Isus2 同款）。记号按该窗口所在小节的调号
   计算：大三/挂留/强力和弦大写（I、Isus2、V5），小三/减/半减小写且省略
   后缀开头的 m（ii7、vii°、iiø7），调外根音加升降号（B 大调里 C -> bII），
   斜杠低音保留音名（Isus2/F#）。小调默认按**关系大调**记度数
   （A 小调 Am -> vi、Dm -> ii、Em -> iii，便于直接对照 I-V-vi-IV 进行），
   `--roman-tonic-minor` 可切回主音小调记法（Am -> i）。
   `--no-roman` 关闭；已存在的自由文本默认保留用户原文，只有 `--overwrite`
   才替换。
4. **避开 beat 复用陷阱**：GPIF 里同一个 beat 对象会被几十上百个位置复用
   （例如同一 riff 的 G4 拍），而带和弦的 beat 从不复用。如果目标拍被共享，
   脚本会**深拷贝一个新 beat**、分配新 id、并把该声部该位置的引用替换过去，
   保证和弦精确落在目标小节，不会泄漏到 riff 的其他位置。

默认行为：已有手工标注的小节整段跳过（保留你的 C/F、C5/G、G5、C）；
`--overwrite` 才会写入/替换这些小节。

写回完成后脚本会用解析器重新读一遍输出文件做自检：
所有声部引用存在、beat id 无重复、和弦索引不越界。
写回的结构与 Guitar Pro 8 自身的保存方式一致，但**首次使用请用 GP8 打开
新文件确认一次显示效果**。

## 对照实验（本仓库示例文件）

`NERDNEKO-CURE feat.初音ミク.gp` 的 Lead Guitar 轨道里手工标了 4 个和弦，
分别挂在琶音/和弦的首个低音上：

| 小节 | 手动 | 整小节识别 | 标注拍→小节末识别 |
|------|------|-----------|------------------|
| 9    | C/F  | C/F       | C/F              |
| 10   | C5/G | C5/G      | C5/G             |
| 12   | G5   | Csus2/G   | G5               |
| 13   | C    | C         | C                |

结论：手动标注的 4 处全部可以被算法复现。第 12 小节整小节看是
`C - D - G` 的分解和弦，会判成 Csus2/G；只看标注拍起到小节末（`G D`）
则稳定得到 G5——这也解释了为什么对照要同时给两种窗口。

## 已知限制与下一步

- **旋律性乐句**（单音 riff）理论上和弦不唯一：同样的音级既可能是
  Am7 也可能是带经过音的 Am。算法只能给出概率最高的解释。
- **指板图是近似生成**：`--write` 生成的和弦指板图只是用于显示，可能不是
  你最顺手的按法；和弦名称与构成是准确的，可在 GP8 里直接改指法。
- **指板图必须符合 GP8 的格式约束**：GP8 固定使用 `fretCount="5"` 的窗口，
  所有按弦品必须落在 `(baseFret, baseFret+5]` 内，且 `<Diagram>` 需按
  `Fret* → Fingering → Property*` 的顺序书写（`<Fingering>` 含全部 6 根弦的
  `<Position>`，未按弦用 `finger="None" fret="4294967295"`）。生成器已按此约束
  输出，并把按弦限制在低把位（品 0-5）以保证窗口放得下。
- **拍上的和弦引用必须写成 CDATA**：GP8 的 GPIFReader 只认
  `<Chord><![CDATA[i]]></Chord>` 这种形式；写成普通文本 `<Chord>i</Chord>`
  会被静默忽略（整个文件的和弦符号都不显示）。写回时按原文件的
  (标签, 文本) 对恢复 CDATA，新增的 `<Chord>` 数字引用也统一写成 CDATA。
- **自由文本注解同样必须写成 CDATA**：新增的 `<FreeText>` 若写成普通文本，
  GP8 会静默丢弃（参照文件里 Isus2 就是 CDATA 形式），写回时统一补成
  `<FreeText><![CDATA[...]]></FreeText>`，并按 GP8 顺序放在 `<Chord>` 前。
- 如果还有 GP3-5 旧格式的谱子，接 PyGuitarPro 做统一入口即可。
