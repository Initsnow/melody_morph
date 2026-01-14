# Chord to Strings 文档

将吉他扫弦或钢琴和弦拆分为弦乐声部（Violin, Viola, Cello）。

## 功能特性

- **和弦检测**：自动检测 MIDI 中的和弦事件
- **智能分配**：将和弦音分配给不同弦乐器
- **自定义配置**：支持自定义各弦乐器数量
- **音域调整**：自动调整音符到各乐器的合理音域

## 快速开始

```bash
# 基本用法（1 violin, 1 viola, 1 cello）
uv run python chord_to_strings.py input.mid -o output.mid

# 运行演示
uv run python chord_to_strings.py --demo
```

## 使用示例

```bash
# 自定义弦乐配置（2小提琴，1中提琴，1大提琴）
uv run python chord_to_strings.py input.mid -o output.mid --violins 2 --violas 1 --cellos 1

# 弦乐四重奏配置
uv run python chord_to_strings.py input.mid -o output.mid --violins 2 --violas 1 --cellos 1

# 处理第2轨道
uv run python chord_to_strings.py input.mid -o output.mid -t 1

# 设置和弦检测阈值（更大的值可合并接近的音符）
uv run python chord_to_strings.py input.mid -o output.mid --threshold 20

# 禁用八度自动调整
uv run python chord_to_strings.py input.mid -o output.mid --no-adjust
```

## 参数列表

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `input` | 输入 MIDI 文件路径 | - |
| `-o, --output` | 输出文件路径 | `{input}_strings.mid` |
| `-t, --track` | 源轨道索引 | `0` |
| `--violins` | 小提琴数量 | `1` |
| `--violas` | 中提琴数量 | `1` |
| `--cellos` | 大提琴数量 | `1` |
| `--threshold` | 和弦检测阈值（tick） | `10` |
| `--no-adjust` | 禁用八度自动调整 | - |
| `--demo` | 运行演示 | - |

## 弦乐器音域

| 乐器 | 音域范围 | GM 编号 |
|------|----------|---------|
| Violin | G3 - E7 | 40 |
| Viola | C3 - A6 | 41 |
| Cello | C2 - A5 | 42 |

## 分配策略

- 和弦音从低到高排列后分配给各乐器
- Cello 获得最低音，Violin 获得最高音
- 当和弦音数量少于乐器数量时，某些乐器可能齐奏同一音
- 当和弦音数量多于乐器数量时，音符会合理分布

## Python API

```python
from chord_to_strings import ChordToStringsConverter, StringsConfig

# 创建配置
config = StringsConfig(violins=2, violas=1, cellos=1)

# 创建转换器
converter = ChordToStringsConverter(
    config=config,
    chord_threshold_ticks=10,
    adjust_octave=True
)

# 执行转换
stats = converter.convert(
    input_path="input.mid",
    output_path="output.mid",
    source_track=0
)
```
