# Counterpoint Generator 文档

MIDI 对位法生成器 - 基于严格对位法规则（Fuxian Species Counterpoint）为输入的 MIDI 旋律生成独立的对位声部。

## 功能特性

- **支持五种对位种类 (Species)**：从简单的音对音到复杂的华彩混合节奏。
- **智能调性检测 (Smart Key Detection)**：基于 Krumhansl-Schmuckler 算法，自动分析乐曲的局部调性变化（转调）。
- **动态和声支持**：完全支持大小调转调、自定义音阶。
- **复音处理**：能够从多声部（如钢琴）轨道中智能提取出顶层主旋律（Cantus Firmus）。
- **符合 MIDI 标准**：自动处理异名同音（如将 D# 大调转换为 Eb 大调），输出完整的调号及拍号信息。

## 快速开始

```bash
# 最简单的用法：为输入文件生成第五类（华彩）对位
uv run counterpoint_generator.py input.mid output.mid --species 5

# 指定定旋律 (Cantus Firmus) 所在的轨道（例如第 1 轨）
uv run counterpoint_generator.py input.mid output.mid --cantus_firmus_track 1

# 强制指定调性（忽略智能检测）
uv run counterpoint_generator.py input.mid output_dorian.mid --root D --mode dorian
```

## 核心算法介绍

本工具并非基于深度学习，而是基于**规则系统**和**启发式搜索算法**，以确保生成的旋律严格符合古典对位法的理论要求。

### 1. 智能调性检测 (Smart Key Detection)
为了处理现代音乐中复杂的转调，脚本内置了 **Krumhansl-Schmuckler 调性检测算法**：
- **原理**：将乐曲切分为短小的片段（如每小节），统计片段内 12 个音高的时值分布（Pitch Class Profile）。
- **匹配**：将统计出的分布与预设的“大调轮廓”和“小调轮廓”进行相关性计算（Pearson Correlation）。
- **结果**：相关性最高的调性即为该片段的预测调性。这使得生成器能够跟随原曲的转调（例如从 C 小调转到 F 大调）调整其和声策略。

### 2. 声部牵引打分系统 (Voice Leading Scoring)
生成核心是一个**贪心搜索算法**，它在每一步为所有候选音打分，选择分值最高的音符。评分标准包括：
- **协和性 (Consonance)**：
    - **完全协和 (Perfect)**：同度、五度、八度（分值较低，避免空洞）。
    - **不完全协和 (Imperfect)**：三度、六度（分值最高，色彩丰富）。
    - **不协和 (Dissonance)**：二度、四度、七度（分值极低，除特定种类外禁止）。
- **声部进行 (Motion)**：
    - **反向进行 (Contrary Motion)**：最高分，鼓励声部独立性。
    - **平行进行 (Parallel Motion)**：若形成平行五度/八度，直接判为非法（-100分）。
- **旋律跳进 (Leaps)**：鼓励级进 (Stepwise)，惩罚过大的跳进（>八度）。

### 3. 五种对位模式 (Species Logic)

| 种类 | 节奏比 | 算法策略 |
|------|-------|---------|
| **Species 1** | 1:1 | **严格协和**。每一步只搜索三度/六度/五度/八度，严禁不协和音。 |
| **Species 2** | 2:1 | **经过音 (Passing Tones)**。强拍必须协和；弱拍允许不协和，但必须以级进方式填补两个协和音的空隙。 |
| **Species 3** | 4:1 | **邻音与换音 (Cambiatas)**。允许更复杂的装饰音型，搜索空间包含双邻音和换音模式。 |
| **Species 4** | 切分 | **挂留音 (Suspenison)**。优先搜索能否形成 7-6 或 4-3 挂留（强拍不协和，弱拍解决）。 |
| **Species 5** | 混合 | **华彩 (Florid)**。基于概率模型混合上述四种模式，优先生成具有切分节奏的长线条。 |

## 完整参数列表

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `input_file` | 输入 MIDI 文件路径 | (必选) |
| `output_file` | 输出 MIDI 文件路径 | `[input]_counterpoint.mid` |
| `--species` | 对位种类 (1-5) | `1` |
| `--cantus_firmus_track` | 定旋律 (CF) 所在轨道索引 | `0` |
| `--root` | 强制调性根音 (如 C, F#) | (自动检测) |
| `--mode` | 强制调式 (major, minor, dorian...) | (自动检测) |
| `--custom_scale` | 自定义音阶 (半音间隔常用, 如 `0 2 4 6 8 10`) | - |

## 开发接口 (Python API)

您也可以在代码中直接调用核心类：

```python
from counterpoint_generator import CounterpointGenerator, ContextTracker, VoiceLeading
import mido

# 加载 MIDI
mid = mido.MidiFile("input.mid")

# 配置 (支持类似 argparse 的 namespace 对象)
class Args:
    species = 5
    cantus_firmus_track = 1
    output_file = "out.mid"
    root = None
    mode = None
    custom_scale = None

# 初始化并运行
generator = CounterpointGenerator(mid, Args())
generator.run()
```
