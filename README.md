# Melody Morph 🎵

MIDI 旋律处理工具集 - 用于音乐制作中的旋律修改、和弦分配和节奏变换。

## 工具列表

| 脚本 | 功能 | 文档 |
|------|------|------|
| `melody_corrector.py` | 将旋律纠正到和弦内音 | [文档](doc/melody_corrector.md) |
| `chord_to_strings.py` | 将和弦拆分到弦乐声部 | [文档](doc/chord_to_strings.md) |
| `phrase_compressor.py` | 乐句时间压缩/扩展 | [文档](doc/phrase_compressor.md) |

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
```
