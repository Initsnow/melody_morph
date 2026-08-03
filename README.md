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
```

## 演示

每个脚本都支持 `--demo` 参数查看演示：

```bash
uv run python melody_corrector.py --demo
uv run python chord_to_strings.py --demo
uv run python phrase_compressor.py --demo
```

## 帮助

```bash
uv run python melody_corrector.py --help
uv run python chord_to_strings.py --help
uv run python phrase_compressor.py --help
uv run python counterpoint_generator.py --help
uv run python midi_to_ust.py --help
```

## 说明

本项目的所有代码及文档均由 AI 协助编写。
> Human Prompted, AI Generated. 🎵
