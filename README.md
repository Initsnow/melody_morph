# Melody Morph 🎵

MIDI 旋律处理工具集 - 用于音乐制作中的旋律修改、和弦分配和节奏变换。

## 工具列表

| 脚本 | 功能 | 文档 |
|------|------|------|
| `melody_corrector.py` | 将旋律纠正到和弦内音 | [文档](doc/melody_corrector.md) |
| `chord_to_strings.py` | 将和弦拆分到弦乐声部 | [文档](doc/chord_to_strings.md) |
| `phrase_compressor.py` | 乐句时间压缩/扩展 | [文档](doc/phrase_compressor.md) |
| `counterpoint_generator.py` | 根据旋律生成严格对位法旋律 | [文档](doc/counterpoint_generator.md) |
| `midi_to_ust.py` | MIDI 旋律转调内唱名 UST | [文档](doc/midi_to_ust.md) |
| `gpreader/` | Guitar Pro (.gp/.gpx) 独立读取库（解析 GPIF、文件层写回） | [文档](doc/gp_parser.md) |
| `gpchords/` | 和弦自动标注（`gp-chords`）、调性写入（`gp-key`）等命令 | [文档](doc/gp_parser.md) |
| `gp-clear` | 清除指定轨道的和弦标注与自由文本，重新标注前还原干净状态 | [文档](doc/gp_parser.md) |
| `gp-sections` | 自动分段：多特征检测段落边界、聚类命名并写回 `<Section>` 标记 | [文档](doc/gp_sections.md) |
| `melody_analyzer.py` | 读取 GP 旋律轨，输出乐句结构、重复动机使用位置与音级/节奏分布报告 | 本文件 |

## 快速开始

```bash
# 旋律纠正：将音符纠正到 C 大调和弦
uv run python melody_corrector.py input.mid -o output.mid -n C,E,G

# 和弦进行：不同周期使用不同和弦
uv run python melody_corrector.py input.mid -o output.mid -m chord_progression --chords "C,E,G;A,C,E" --period 4

# 和弦拆分：将和弦分配给弦乐四重奏
uv run python chord_to_strings.py input.mid -o output.mid --violins 2 --violas 1 --cellos 1

# 乐句压缩：4小节压缩为1小节
uv run python phrase_compressor.py input.mid -o output.mid -r 4

# 对位生成：为旋律生成五类对位（花样对位）
uv run python counterpoint_generator.py input.mid -o output.mid --species 5

# MIDI 转调内唱名 UST（C 大调，日文假名唱名，默认）
uv run python midi_to_ust.py input.mid -o output.ust --key C

# 英文 / 中文唱名
uv run python midi_to_ust.py input.mid -o output.ust --key C --lyrics en
uv run python midi_to_ust.py input.mid -o output.ust --key Am --lyrics zh

# 默认自动识别调性（读 MIDI 调号，读不到则估计）
uv run python midi_to_ust.py input.mid -o output.ust

# 自动标注和弦：交互选择轨道 -> 识别 -> 自动写回 <原名>_chords.gp（原文件不变）
uv run gp-chords "song.gp"

# 额外检测循环和弦进行：在每次循环起点写该处实际进行的自由注解
# （第一行该拍单和弦罗马数字、第二行含品质的完整罗马数字，
# 如 P1: I-IV-V7-vi，即进行放在罗马记号下面；同一进行不同循环区
# 各标各的变体；--overwrite 时整体替换旧注解；即使小节已有手工
# 和弦也照写）
uv run gp-chords "song.gp" --track "Rhythm Guitar" --progressions

# 自动判断调性并写入全部小节 -> <原名>_key.gp（原文件不变）
uv run gp-key "song.gp"

# 清除指定轨道的和弦与自由文本 -> <原名>_cleared.gp（原文件不变），
# 重新标注前把轨道还原成干净状态（和弦库一并清空）
uv run gp-clear "song.gp" --track "Rhythm Guitar"
uv run gp-clear "song.gp" --track all --no-write

# 按段落估计（转调谱）/ 强制指定调性
uv run gp-key "song.gp" --per-section
uv run gp-key "song.gp" --key Am

# 指定轨道；默认按和弦变化自动切窗（--window auto），--no-write 不写回
uv run gp-chords "song.gp" --track "Lead Guitar" --no-write --debug

# 写回时默认在每拍和弦旁加罗马数字自由注解（如 B 大调下 Bsus2 -> Isus2，
# 与 GP 的"自由文本"注解同机制；--no-roman 可关闭）；调性按各小节调号计算，
# 小调按关系大调记（A 小调 Am -> vi，--roman-tonic-minor 可切回主音小调）
uv run gp-chords "song.gp" --track "Rhythm Guitar" --overwrite

# 多轨道：每轨单独分析、单独标注（可逗号分隔或重复 --track，all=全部非鼓轨道）
uv run gp-chords "song.gp" --track "Lead Guitar,Rhythm Guitar"
uv run gp-chords "song.gp" --track all --no-write

# 合并多轨音符识别（和弦拆在两轨 / 需要贝斯补低音），默认写回第一轨；
# --write-tracks all 写回全部分析轨道
uv run gp-chords "song.gp" --track "Lead Guitar,Electric Bass" --merge
uv run gp-chords "song.gp" --track "Lead Guitar,Rhythm Guitar" --merge --write-tracks all

# 固定按小节/半小节/节拍切窗；转调谱默认逐小节读调号，也可按段落调性
uv run gp-chords "song.gp" --window measure
uv run gp-chords "song.gp" --key-per-section

# 查看文件内部结构（调试用）
uv run gp-info "song.gp" --track "Lead Guitar"

# 自动分段：多特征 novelty + 重复起点检测边界，段落聚类命名后写回
# <原名>_sections.gp（所有段都带字母；已有段落的小节默认跳过）
uv run gp-sections "song.gp"

# 只预览候选边界和触发证据（推荐先看再写回）；--name 可自定义段落文本
uv run gp-sections "song.gp" --no-write
uv run gp-sections "song.gp" --name "A=Verse 1" --out sections.json

# 旋律分析拆解：输出乐句、动机使用位置和分布统计的 Markdown 报告
uv run python melody_analyzer.py "song.gp" --track "Vocals" --report vocal_report.md
uv run python melody_analyzer.py "song.gp" --track "Lead Guitar" --report lead_report.md

# 动机发现是在整条旋律上做的（跨乐句/跨段落），不是单乐句内部：
# 支持逐字反复、移调序列、八度/音级变体、轮廓反复、纯节奏型六种匹配，
# 输出按显著性排序的去冗余动机家族，并标注每次出现的移调与变体类型。
# 常用参数：
#   --min-motif-notes 3    最短动机长度（音符数）
#   --max-motif-notes 24   最长动机长度
#   --min-occurrences 2    至少非重叠出现次数
#   --motif-gap 1.0        超过该四分音符长度的休止视为动机匹配的旋律段边界
#   --max-motifs 40        报告中最多保留的动机数量（0 = 全部）
uv run python melody_analyzer.py "song.gp" --track "Vocals" --max-motifs 20
```

## 演示

每个脚本都支持 `--demo` 参数查看演示：

```bash
uv run python melody_corrector.py --demo
uv run python chord_to_strings.py --demo
uv run python phrase_compressor.py --demo
uv run gp-chords --demo
```

## 帮助

```bash
uv run python melody_corrector.py --help
uv run python chord_to_strings.py --help
uv run python phrase_compressor.py --help
uv run python counterpoint_generator.py --help
uv run python midi_to_ust.py --help
uv run gp-chords --help
uv run gp-key --help
```

## 说明

本项目的所有代码及文档均由 AI 协助编写。
> Human Prompted, AI Generated. 🎵
