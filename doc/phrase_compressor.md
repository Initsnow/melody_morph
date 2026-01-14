# Phrase Compressor 文档

MIDI 乐句压缩器 - 将 MIDI 乐句进行等比例时间缩放。

## 功能特性

- **时间压缩**：将多个小节压缩到更少的小节
- **时间扩展**：将乐句拉长到更多小节
- **保持相对时间**：音符之间的相对时间关系保持不变
- **可选时值压缩**：可选择是否同比例压缩音符时值

## 快速开始

```bash
# 压缩为 1/2 时长（2小节变1小节）
uv run python phrase_compressor.py input.mid -o output.mid -r 2

# 运行演示
uv run python phrase_compressor.py --demo
```

## 使用示例

```bash
# 压缩为 1/4 时长（4小节变1小节）
uv run python phrase_compressor.py input.mid -o output.mid -r 4

# 扩展为 2 倍时长（1小节变2小节）
uv run python phrase_compressor.py input.mid -o output.mid -r 0.5

# 压缩但保持原音符时值不变
uv run python phrase_compressor.py input.mid -o output.mid -r 2 --no-preserve-duration

# 处理第2轨道
uv run python phrase_compressor.py input.mid -o output.mid -r 2 -t 1
```

## 参数列表

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `input` | 输入 MIDI 文件路径 | - |
| `-o, --output` | 输出文件路径 | `{input}_compressed.mid` |
| `-r, --ratio` | 压缩比例 | `2.0` |
| `-t, --track` | 轨道索引 | `0` |
| `--no-preserve-duration` | 不压缩音符时值 | - |
| `--min-duration` | 最小音符时值（tick） | `1` |
| `--demo` | 运行演示 | - |

## 压缩比例说明

| 比例 | 效果 | 说明 |
|------|------|------|
| `2.0` | 压缩 | 2小节 → 1小节 |
| `4.0` | 压缩 | 4小节 → 1小节 |
| `0.5` | 扩展 | 1小节 → 2小节 |
| `0.25` | 扩展 | 1小节 → 4小节 |

## 时值处理

**默认行为（`--preserve-duration`）**：
- 音符开始时间和时值都按比例压缩
- 适合制作更紧凑的伴奏

**使用 `--no-preserve-duration`**：
- 只压缩音符开始时间，保持原始时值
- 可能导致音符重叠

## Python API

```python
from phrase_compressor import PhraseCompressor

# 创建压缩器
compressor = PhraseCompressor(
    compression_ratio=2.0,           # 2x 压缩
    preserve_duration_ratio=True,    # 同比例压缩时值
    min_duration_ticks=1             # 最小时值
)

# 执行压缩
stats = compressor.compress(
    input_path="input.mid",
    output_path="output.mid",
    source_track=0
)

print(f"原始时长: {stats['original_beats']} 拍")
print(f"压缩后: {stats['compressed_beats']} 拍")
```
