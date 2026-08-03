# MIDI → UST（调内唱名）文档

将 MIDI 旋律转换为以首调唱名（movable-do solfège）为歌词的 UST 文件，
适用于把一段旋律快速变成 UTAU/OpenUTAU 可播放的"唱名音轨"。

## 功能特性

- **调内唱名**：音符音高保持不变，歌词反映该音在指定调内的唱名
  （`ドレミファソラシ`、`do re mi...` 或 `多来咪...`）
- **调性自动识别**：默认读取 MIDI 自带的调号（`key_signature` 元事件），
  读不到时按音符时值分布估计，也可手动指定
- **变化音处理**：调外音符自动使用标准变化唱名
  （`di ri fi si li` / `ra me se le te`，或中文 `升多 升来...` / `降来 降咪...`）
- **小调支持**：采用首调 la 唱法（即简谱小调记法，小调主音唱 `la`）
- **节奏还原**：按 MIDI 时值换算为 UST tick，支持变速（tempo map）、
  休止符自动插入、结尾自动补休止
- **多轨选择**：自动选择音符最多的非打击乐轨，也可指定轨道/通道或合并
- **重叠处理**：和弦/复调文件默认提取最高音旋律线，也可保留、截短或丢弃

## 快速开始

```bash
# 基础用法：C 大调，日文假名唱名（默认）
uv run python midi_to_ust.py input.mid -o output.ust --key C

# 英文 / 中文唱名
uv run python midi_to_ust.py input.mid -o output.ust --key C --lyrics en
uv run python midi_to_ust.py input.mid -o output.ust --key Am --lyrics zh

# 自动识别调性（默认）：优先读 MIDI 调号，读不到则估计
uv run python midi_to_ust.py input.mid -o output.ust

# 指定轨道，变化音用降号拼写
uv run python midi_to_ust.py input.mid -o output.ust --key "Bb minor" --track 2 --chromatic flat

# 运行演示
uv run python midi_to_ust.py --demo
```

## 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `input` | - | 输入 MIDI 文件路径 |
| `-o, --output` | 输入同名 `.ust` | 输出 UST 路径 |
| `-k, --key` | `auto` | 调名，如 `C`、`D#`、`Am`、`Bb minor`；`auto`=读 MIDI 调号/估计 |
| `--mode` | 由 key 推断 | `major` / `minor` |
| `--lyrics` | `ja` | `ja`=ドレミファソラシ（默认）、`en`=do re mi、`zh`=多来咪 |
| `--chromatic` | `sharp` | 变化音拼写：`sharp`（升）或 `flat`（降） |
| `--track` | 自动 | 指定 MIDI 轨道索引 |
| `--channel` | 自动 | 指定 MIDI 通道（0-15） |
| `--merge` | 关 | 合并所有非打击乐通道 |
| `--overlap` | `top` | 重叠音符：`top` 取最高音（默认）、`keep` 保留/警告、`cut` 截短、`drop` 丢弃 |
| `--encoding` | `auto` | UST 编码，`auto` 按歌词自动选择 |
| `--no-final-rest` | 关 | 不自动在结尾追加休止符 |
| `--debug` | 关 | 打印逐音符转换明细（调试用） |

## 唱名规则

### 大调

主音唱 `do`，按大调音阶依次为 `do re mi fa sol la si`。

### 小调（la 唱法）

小调主音唱 `la`，使用关系大调的唱名框架（简谱记法）：

```text
C 大调： C  D  E  F  G  A  B
         do re mi fa sol la si
A 小调： A  B  C  D  E  F  G
         la si do re mi fa sol
```

### 变化音

调外音符使用标准变化唱名。以 C 大调为例：

| 相对主音 | 升号拼写（默认） | 降号拼写 |
|----------|------------------|----------|
| 1 | di（升多） | ra（降来） |
| 3 | ri（升来） | me（降咪） |
| 6 | fi（升发） | se（降索） |
| 8 | si（升索） | le（降拉） |
| 10 | li（升拉） | te（降西） |

变化音唱名本身是记谱约定，不反映和声功能，可用 `--chromatic` 切换。

### 日文假名（ja）

日语声库的 oto.ini 别名通常是假名，`--lyrics ja` 直接输出假名唱名：

| 音级 | do | re | mi | fa | sol | la | si |
|------|----|----|----|----|-----|----|----|
| 假名 | ド | レ | ミ | ファ | ソ | ラ | シ |

两个注意事项：

- 变化音不单独造音节，按日本习惯**借相邻音级唱**：升号变化音借下方音级、
  降号变化音借上方音级。例如 C 大调中 F# 唱 ファ（借 fa）、Bb 唱 ラ（借 la）
- 因此变化音与相邻自然音可能同字（如 Bb 和 A 都是 ラ），音高以 `NoteNum`、
  度数以 `--debug` 的度数列区分
- `ファ` 属于外来音节（扩展假名），部分 CV 声库没有收录，
  遇到唱不了的假名请到音符下拉框里选声库实际存在的别名

## 调性识别

`--key auto`（默认）按以下顺序确定调性：

1. 读取 MIDI 的 `key_signature` 元事件（如 `F#m`、`Bb`）。这是 MIDI 规范
   自带的可选信息，标准导出文件通常带在轨道 0 开头
2. 若没有调号（或调号无法解析），按音符时值加权的音高分布匹配
   Krumhansl-Kessler 调性轮廓进行估计
3. 输出会标明来源（`MIDI 调号` / `估计`），估计结果仅供参考，
   重要场合请用 `--key` 手动指定

需要注意：**很多 MIDI 不带调号**（DAW 导出、在线转换常见），
且调号只反映谱面升降号，不一定与实际音高一致，因此读取结果也可能与实际不符；
这是 MIDI 格式本身的限制，不是脚本缺陷。旋律终止感弱或变化音多时估计容易歧义
（如 C 大调与 A 小调共享音阶），请结合 `--key` 使用。

## 音符与轨道选择

- 默认自动选择**音符最多的非打击乐轨道**，适合大多数旋律 MIDI
- 复调/和弦文件默认取**最高音旋律线**（`--overlap top`，即旋律提取常用的
  soprano 启发式）；若旋律在低声部或内声部，请用 `--track` 指定旋律轨，
  或改用 `--overlap keep/cut/drop`
- MIDI 打击乐通道（通道 9）默认排除

## 编码说明

UST 的编码与 UTAU 版本有关：

- `--encoding auto`（默认）：ASCII（英文）或日文假名（ja）歌词用 `cp932`
  （经典 UTAU 兼容）；中文（zh）自动用 `utf-8`（OpenUTAU 标准）
- 中文唱名 + 经典 UTAU 需确认编辑器编码支持，否则请改用 `--lyrics ja/en`

## 实现说明

脚本基于 `mido` 读取 MIDI、`utaupy` 生成 UST：

- 时值按 MIDI tempo map 换算为秒，再按 UST 全局速度（480 tick/四分音符）换算，
  因此变速 MIDI 的绝对时值依然准确
- 力度（velocity）映射为 UST `Intensity`（0-100）
- 休止符统一为 `Lyric=R`、`NoteNum=0`，结尾自动补一个休止
