# Key Transposer - 按音阶位置转调工具

按音阶位置（scale degree）而非简单半音移调的 MIDI 调号转换工具。

## 核心概念

**传统移调** vs **音阶位置移调**：

| 方式 | C大调 E (第3级) → F大调 |
|------|-------------------------|
| 传统移调 | A# (第#4级) |
| 音阶位置移调 | A (第3级) ✓ |

音阶位置移调保持旋律在调式中的"感觉"不变。

## 快速开始

```bash
# 检测调号
uv run python key_transposer.py input.mid --detect

# 转到 G 大调（自动检测原调）
uv run python key_transposer.py input.mid -o output.mid --to G

# 转到 G 小调
uv run python key_transposer.py input.mid -o output.mid --to Gm

# 指定原调
uv run python key_transposer.py input.mid -o output.mid --from C --to G

# 运行演示
uv run python key_transposer.py --demo
```

## 参数说明

| 参数 | 说明 |
|------|------|
| `input` | 输入 MIDI 文件 |
| `-o, --output` | 输出文件路径 |
| `--to` | 目标调号（如 `G`, `Gm`, `F# minor`） |
| `--from` | 原调号（可选，不指定则自动检测） |
| `--detect` | 仅检测调号，不转换 |
| `--demo` | 运行演示 |

## 调号格式

支持多种格式：
- `G` - G 大调
- `Gm` - G 小调
- `G major` - G 大调
- `G minor` - G 小调
- `F#` - F# 大调
- `F#m` - F# 小调

## 算法说明

1. **调号检测**：使用 Krumhansl-Schmuckler 算法分析音高分布
2. **音阶映射**：将每个音符映射到其在原调中的音阶级数（1-7）和变化音偏移
3. **转调输出**：根据音阶位置，映射到目标调的对应音

## 示例

```
原调 C 大调：C4 D4 E4 F4 G4 A4 B4 C5
转调 G 大调：G4 A4 B4 C5 D5 E5 F#5 G5
转调 F 大调：F4 G4 A4 Bb4 C5 D5 E5 F5

注意：F4 → Bb4（不是 A#4），保持了自然大调的感觉
```
