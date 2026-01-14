# Melody Corrector 文档

MIDI 旋律纠正器 - 将 MIDI 中的音符纠正到指定的目标音（如和弦内音）。

## 功能特性

- **多种纠正算法**：最近音、加权随机、方向偏向、完全随机
- **周期性纠正**：按节拍周期循环使用目标音
- **和弦进行**：不同周期使用不同的和弦

## 快速开始

```bash
# 最简单的用法：纠正到 C 大调和弦
uv run python melody_corrector.py input.mid -o output.mid -n C,E,G

# 运行演示
uv run python melody_corrector.py --demo
```

## 纠正方法

### 1. nearest（默认）- 最近音纠正
每个音符纠正到最近的目标音。

```bash
uv run python melody_corrector.py input.mid -o output.mid -n C,E,G -m nearest
```

### 2. weighted_random - 加权随机
距离越近的目标音被选中概率越高，增加随机性。

```bash
uv run python melody_corrector.py input.mid -o output.mid -n C,E,G -m weighted_random -r 0.5
```
- `-r 0.5`：随机性参数（0-1），值越大随机性越强

### 3. direction_bias - 方向偏向
根据旋律走向选择目标音，保持旋律的上行/下行趋势。

```bash
uv run python melody_corrector.py input.mid -o output.mid -n C,E,G -m direction_bias -d 0.7
```
- `-d 0.7`：方向偏向强度（0-1）

### 4. random - 完全随机
从附近的目标音中随机选择。

```bash
uv run python melody_corrector.py input.mid -o output.mid -n C,E,G -m random
```

### 5. periodic - 周期性纠正
按节拍周期循环使用目标音序列中的单个音。

```bash
uv run python melody_corrector.py input.mid -o output.mid -n C,E,G -m periodic --period 2.0 --pattern cycle
```

**周期模式（`--pattern`）：**
| 模式 | 说明 |
|------|------|
| `cycle` | 按 C→E→G→C... 顺序循环 |
| `hold` | 每周期随机选一个音保持 |
| `nearest_hold` | 首音最近纠正，后续保持 |

### 6. chord_progression - 和弦进行 ⭐
**最强大的功能！** 不同周期使用不同的和弦。

```bash
# 前4拍用 Cm，接下来4拍用 Fm，再4拍用 Cmaj7
uv run python melody_corrector.py input.mid -o output.mid \
  -m chord_progression \
  --chords "C,Eb,G;F,Ab,C;C,E,G,B" \
  --period 4.0
```

**参数：**
- `--chords "和弦1;和弦2;和弦3"`：用分号分隔每个和弦
- `--period 4.0`：每个和弦持续的拍数
- 无需 `-n` 参数，自动从第一个和弦推断

**示例和弦进行：**
```bash
# I-IV-V-I 进行（C大调）
--chords "C,E,G;F,A,C;G,B,D;C,E,G"

# i-iv-V-i 进行（C小调）
--chords "C,Eb,G;F,Ab,C;G,B,D;C,Eb,G"

# Jazz ii-V-I
--chords "D,F,A;G,B,D,F;C,E,G,B"
```

## 完整参数列表

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `input` | 输入 MIDI 文件路径 | - |
| `-o, --output` | 输出文件路径 | `{input}_corrected.mid` |
| `-n, --notes` | 目标音列表 | - |
| `-m, --method` | 纠正算法 | `nearest` |
| `-t, --track` | 轨道索引 | `0` |
| `-r, --randomness` | 随机性（weighted_random） | `0.3` |
| `-d, --direction-strength` | 方向强度（direction_bias） | `0.7` |
| `--period` | 周期长度（拍数） | `2.0` |
| `--pattern` | 周期模式 | `cycle` |
| `--chords` | 和弦进行 | - |
| `--demo` | 运行演示 | - |

## 音符格式

目标音支持多种格式：
- **音名**：`C,E,G` 或 `C4,E4,G4`
- **MIDI 数值**：`60,64,67`
- **混合**：`C4,64,G4`
- **升降号**：`C#,Eb,F#`

## Python API

```python
from melody_corrector import MelodyCorrector, CorrectionMethod, Note

# 基础用法
corrector = MelodyCorrector(
    target_notes=[60, 64, 67],  # C, E, G
    method=CorrectionMethod.NEAREST
)

# 和弦进行用法
corrector = MelodyCorrector(
    target_notes=[60, 64, 67],
    method=CorrectionMethod.CHORD_PROGRESSION,
    period_beats=4.0,
    ticks_per_beat=480,
    chord_progression=[[60, 64, 67], [69, 72, 76]]  # C大调 → A小调
)

# 纠正旋律
notes = [Note(pitch=62), Note(pitch=66)]
corrected = corrector.correct_melody(notes)
```

## 实用技巧

1. **处理指定轨道**：使用 `-t` 参数选择轨道
   ```bash
   uv run python melody_corrector.py input.mid -o out.mid -n C,E,G -t 1
   ```

2. **每拍一个和弦**：设置 `--period 1.0`

3. **每小节一个和弦**：设置 `--period 4.0`（4/4拍）

4. **查看帮助**：
   ```bash
   uv run python melody_corrector.py --help
   ```
