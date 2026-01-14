# Text to Melody Converter (文本转旋律)

`text_to_midi.py` 是一个实验性工具，用于将任意文本（中文、英文等）转换为 MIDI 旋律，并自动为其生成严格对位法的伴奏。

## 功能特点

*   **文本映射 (Text Mapping)**: 使用哈希算法将字符映射到特定的音阶上。
*   **五声音阶 (Pentatonic Scale)**: 默认使用大调五声音阶（宫商角徵羽），保证生成的旋律听起来和谐、悦耳，带有中国风。
*   **节奏映射 (Rhythm Mapping)**: 根据标点符号自动插入休止符和长音，模仿朗读的语感。
    *   空格：短休止
    *   逗号：四分休止
    *   句号/感叹号/问号/换行：二分休止
*   **伴奏生成 (Auto-Accompaniment)**: 自动调用 `counterpoint_generator.py`，为生成的“文章旋律”添加符合严格对位法规则的伴奏声部，使其成为一首完整的二声部乐曲。

## 使用方法

### 命令行

```bash
# 基本用法：将一段文字转换为 MIDI
uv run python text_to_midi.py "我好想做嘉然小姐的狗啊。可惜嘉然小姐喜欢的是猫，我哭了" -o output.mid

# 指定文本文件作为输入
uv run python text_to_midi.py poem.txt -o poem.mid

# 高级选项：指定对位种类和调式
uv run python text_to_midi.py "Hello World" -o hello.mid --species 5 --root C --mode major
```

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `text_input` | 输入的文本字符串或文本文件路径 | (必填) |
| `-o`, `--output` | 输出 MIDI 文件路径 | `text_melody.mid` |
| `--species` | 对位法种类 (1-5) | 5 (花样对位) |
| `--root` | 根音 (Root Note) | C |
| `--mode` | 调式 (Major/Minor) | major |

## 原理

1.  **旋律生成**:
    *   程序遍历输入文本的每一个字符。
    *   计算字符的特征值（Hash），取模映射到 C 大调五声音阶 (C, D, E, G, A) 的两个八度范围内。
    *   根据字符类型（普通字符 vs 标点）决定音符时值。
2.  **伴奏生成**:
    *   也就是将文本生成的旋律作为 **定式旋律 (Cantus Firmus)**。
    *   调用 `counterpoint_generator`，根据 Fux 的对位法规则（如协和音程优先、反向运动等）生成第二条旋律。

## 示例

**输入**: 我好想做嘉然小姐的狗啊。可惜嘉然小姐喜欢的是猫，我哭了

**输出**:
*   生成的旋律会带有五声调式的韵味。
*   逗号和句号处会有自然的停顿。
*   伴奏声部会通过对位法填充空隙，增加音乐的厚度和流动感。
