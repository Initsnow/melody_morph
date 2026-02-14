"""
MIDI Key Transposer - 按音阶位置转调工具

核心功能：
1. 自动检测 MIDI 文件的调号（使用 Krumhansl-Schmuckler 算法）
2. 按音阶位置（scale degree）转调，而非简单的半音移调
3. 例如：C 大调的 E (第3级) → F 大调的 A (也是第3级)

与传统移调的区别：
- 传统移调：所有音符移动固定半音数
- 音阶位置转调：保持音在调式中的"位置感"，只改变调号
"""

import argparse
import mido
import sys
from typing import List, Tuple, Optional
from dataclasses import dataclass

# 终端颜色
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"

# 音名映射
NOTES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
NOTE_TO_INT = {
    'C': 0, 'C#': 1, 'DB': 1, 'D': 2, 'D#': 3, 'EB': 3,
    'E': 4, 'FB': 4, 'E#': 5, 'F': 5, 'F#': 6, 'GB': 6,
    'G': 7, 'G#': 8, 'AB': 8, 'A': 9, 'A#': 10, 'BB': 10,
    'B': 11, 'CB': 11, 'B#': 0
}

# 音阶模式
MODES = {
    'major': [0, 2, 4, 5, 7, 9, 11],  # 大调音阶：全全半全全全半
    'minor': [0, 2, 3, 5, 7, 8, 10],  # 自然小调：全半全全半全全
}


@dataclass
class Note:
    """表示一个 MIDI 音符"""
    pitch: int
    velocity: int
    start_time: int  # 开始时间 (ticks)
    duration: int    # 持续时间 (ticks)


class KeyDetector:
    """调号检测器 - 使用 Krumhansl-Schmuckler 算法"""
    
    def __init__(self):
        # Krumhansl-Schmuckler 音高分布模板
        self.MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
        self.MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

    def correlation(self, profile: List[float], duration_profile: List[float]) -> float:
        """计算两个分布的皮尔逊相关系数"""
        mean_p = sum(profile) / 12
        mean_d = sum(duration_profile) / 12
        numerator = sum((profile[i] - mean_p) * (duration_profile[i] - mean_d) for i in range(12))
        denominator = (sum((profile[i] - mean_p)**2 for i in range(12)) * 
                       sum((duration_profile[i] - mean_d)**2 for i in range(12))) ** 0.5
        return numerator / denominator if denominator != 0 else 0

    def detect_key(self, notes: List[Tuple[int, int, int]]) -> Tuple[str, str]:
        """
        检测调号
        
        Args:
            notes: 音符列表 [(pitch, velocity, duration), ...]
            
        Returns:
            (根音名, 调式) 如 ("C", "major")
        """
        duration_profile = [0.0] * 12
        total_duration = 0
        for p, v, d in notes:
            duration_profile[p % 12] += d
            total_duration += d
            
        if total_duration == 0:
            return ("C", "major")
        
        best_corr = -2.0
        best_key = ("C", "major")
        
        # 检查所有 12 个根音的大调和小调
        for root in range(12):
            shifted_durations = duration_profile[root:] + duration_profile[:root]
            
            corr_major = self.correlation(self.MAJOR_PROFILE, shifted_durations)
            if corr_major > best_corr:
                best_corr = corr_major
                best_key = (NOTES[root], "major")
                
            corr_minor = self.correlation(self.MINOR_PROFILE, shifted_durations)
            if corr_minor > best_corr:
                best_corr = corr_minor
                best_key = (NOTES[root], "minor")
                
        return best_key
    
    def detect_from_midi(self, mid: mido.MidiFile) -> Tuple[str, str]:
        """从 MIDI 文件检测调号"""
        notes = []
        for track in mid.tracks:
            abs_time = 0
            active = {}
            for msg in track:
                abs_time += msg.time
                if msg.type == 'note_on' and msg.velocity > 0:
                    active[msg.note] = (abs_time, msg.velocity)
                elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                    if msg.note in active:
                        start, vel = active.pop(msg.note)
                        notes.append((msg.note, vel, abs_time - start))
        
        return self.detect_key(notes)


class Scale:
    """音阶类 - 处理音阶位置计算"""
    
    def __init__(self, root: str, mode: str = "major"):
        self.root = root.upper()
        self.mode = mode.lower()
        self.root_val = NOTE_TO_INT.get(self.root, 0)
        self.intervals = MODES.get(self.mode, MODES['major'])
        
        # 生成音阶中所有音的 pitch class (0-11)
        self.scale_notes = [(self.root_val + interval) % 12 for interval in self.intervals]
    
    def get_scale_degree(self, pitch: int) -> Tuple[int, int]:
        """
        获取音符在音阶中的位置
        
        Args:
            pitch: MIDI 音高
            
        Returns:
            (scale_degree, chromatic_offset)
            scale_degree: 1-7 表示音阶级数
            chromatic_offset: 相对于该级的半音偏移 (-1=降, 0=自然, 1=升)
        """
        pitch_class = pitch % 12
        
        # 首先检查是否是调内音
        if pitch_class in self.scale_notes:
            degree = self.scale_notes.index(pitch_class) + 1  # 1-based
            return (degree, 0)
        
        # 非调内音：找最近的调内音
        # 检查是升还是降
        for i, scale_note in enumerate(self.scale_notes):
            if (scale_note + 1) % 12 == pitch_class:
                # 是某个调内音的升音
                return (i + 1, 1)
            if (scale_note - 1) % 12 == pitch_class:
                # 是某个调内音的降音
                return (i + 1, -1)
        
        # 如果还是找不到（理论上不应该发生），返回最近的
        min_dist = 12
        nearest_degree = 1
        for i, scale_note in enumerate(self.scale_notes):
            dist = min(abs(pitch_class - scale_note), 12 - abs(pitch_class - scale_note))
            if dist < min_dist:
                min_dist = dist
                nearest_degree = i + 1
        
        # 判断是升还是降
        target_pc = self.scale_notes[nearest_degree - 1]
        diff = (pitch_class - target_pc + 12) % 12
        if diff <= 6:
            return (nearest_degree, diff)
        else:
            return (nearest_degree, diff - 12)
    
    def get_pitch_from_degree(self, degree: int, chromatic_offset: int, octave: int) -> int:
        """
        根据音阶级数获取音高
        
        Args:
            degree: 音阶级数 (1-7)
            chromatic_offset: 半音偏移
            octave: 八度
            
        Returns:
            MIDI 音高
        """
        # degree 是 1-based，转换为 0-based 索引
        scale_idx = (degree - 1) % 7
        base_pitch_class = self.scale_notes[scale_idx]
        pitch_class = (base_pitch_class + chromatic_offset) % 12
        return octave * 12 + pitch_class


class KeyTransposer:
    """调号转换器"""
    
    def __init__(self):
        self.detector = KeyDetector()
    
    def transpose_pitch(self, pitch: int, source_scale: Scale, target_scale: Scale) -> int:
        """
        按音阶位置转换单个音高
        
        Args:
            pitch: 原始 MIDI 音高
            source_scale: 原调音阶
            target_scale: 目标调音阶
            
        Returns:
            转换后的 MIDI 音高
        """
        octave = pitch // 12
        
        # 获取在原调中的音阶位置
        degree, offset = source_scale.get_scale_degree(pitch)
        
        # 在目标调中获取相同位置的音高
        new_pitch = target_scale.get_pitch_from_degree(degree, offset, octave)
        
        # 确保在有效范围内
        return max(0, min(127, new_pitch))
    
    def transpose_midi(
        self,
        mid: mido.MidiFile,
        target_key: str,
        target_mode: str = "major",
        source_key: Optional[str] = None,
        source_mode: Optional[str] = None
    ) -> mido.MidiFile:
        """
        转换整个 MIDI 文件的调号
        
        Args:
            mid: 输入的 MIDI 文件
            target_key: 目标调根音 (如 "G", "F#")
            target_mode: 目标调式 ("major" 或 "minor")
            source_key: 原调根音（可选，不指定则自动检测）
            source_mode: 原调调式（可选）
            
        Returns:
            转换后的 MIDI 文件
        """
        # 检测或使用指定的原调
        if source_key is None:
            detected_key, detected_mode = self.detector.detect_from_midi(mid)
            source_key = detected_key
            source_mode = detected_mode if source_mode is None else source_mode
            print(f"{CYAN}检测到原调：{source_key} {source_mode}{RESET}")
        
        if source_mode is None:
            source_mode = "major"
        
        source_scale = Scale(source_key, source_mode)
        target_scale = Scale(target_key, target_mode)
        
        print(f"{YELLOW}转调：{source_key} {source_mode} → {target_key} {target_mode}{RESET}")
        
        # 创建新的 MIDI 文件
        output_mid = mido.MidiFile(ticks_per_beat=mid.ticks_per_beat)
        
        for track in mid.tracks:
            new_track = mido.MidiTrack()
            output_mid.tracks.append(new_track)
            
            for msg in track:
                if msg.type in ('note_on', 'note_off'):
                    # 转换音高
                    new_pitch = self.transpose_pitch(msg.note, source_scale, target_scale)
                    new_msg = msg.copy(note=new_pitch)
                    new_track.append(new_msg)
                else:
                    # 其他消息直接复制
                    new_track.append(msg.copy())
        
        return output_mid


def demo():
    """演示转调功能"""
    print(f"\n{GREEN}{'='*60}")
    print("Key Transposer 演示 - 按音阶位置转调")
    print(f"{'='*60}{RESET}\n")
    
    # 创建一个 C 大调的示例旋律：C D E F G A B C
    print(f"{CYAN}创建 C 大调音阶旋律：C4 D4 E4 F4 G4 A4 B4 C5{RESET}")
    
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    
    # C 大调音阶
    c_major_scale = [60, 62, 64, 65, 67, 69, 71, 72]  # C D E F G A B C
    
    current_time = 0
    for pitch in c_major_scale:
        track.append(mido.Message('note_on', note=pitch, velocity=100, time=current_time))
        track.append(mido.Message('note_off', note=pitch, velocity=0, time=480))
        current_time = 0
    
    transposer = KeyTransposer()
    
    # 转到 G 大调
    print(f"\n{YELLOW}转换到 G 大调：{RESET}")
    g_major_mid = transposer.transpose_midi(mid, "G", "major", "C", "major")
    
    # 显示结果
    print("原调 C 大调：", end="")
    for p in c_major_scale:
        print(f"{NOTES[p % 12]}{p // 12 - 1} ", end="")
    print()
    
    g_major_scale = []
    for track in g_major_mid.tracks:
        for msg in track:
            if msg.type == 'note_on' and msg.velocity > 0:
                g_major_scale.append(msg.note)
    
    print("转调 G 大调：", end="")
    for p in g_major_scale[:8]:
        print(f"{NOTES[p % 12]}{p // 12 - 1} ", end="")
    print()
    
    # 说明
    print(f"\n{GREEN}说明：{RESET}")
    print("  C 大调的第1级 C → G 大调的第1级 G")
    print("  C 大调的第2级 D → G 大调的第2级 A")
    print("  C 大调的第3级 E → G 大调的第3级 B")
    print("  C 大调的第4级 F → G 大调的第4级 C")
    print("  ...")
    
    # 转到 F 大调
    print(f"\n{YELLOW}转换到 F 大调：{RESET}")
    f_major_mid = transposer.transpose_midi(mid, "F", "major", "C", "major")
    
    f_major_scale = []
    for track in f_major_mid.tracks:
        for msg in track:
            if msg.type == 'note_on' and msg.velocity > 0:
                f_major_scale.append(msg.note)
    
    print("原调 C 大调：", end="")
    for p in c_major_scale:
        print(f"{NOTES[p % 12]}{p // 12 - 1} ", end="")
    print()
    
    print("转调 F 大调：", end="")
    for p in f_major_scale[:8]:
        print(f"{NOTES[p % 12]}{p // 12 - 1} ", end="")
    print()
    
    # 与传统移调对比
    print(f"\n{GREEN}与传统移调对比：{RESET}")
    print("  传统移调（+5半音）：C→F, D→G, E→A, F→A#, G→C, A→D, B→E")
    print("  音阶位置移调：      C→F, D→G, E→A, F→Bb, G→C, A→D, B→E")
    print("  注意：F→Bb 而非 F→A#，保持了自然音阶的感觉")
    
    # 保存演示文件
    output_file = "demo_key_transposed.mid"
    g_major_mid.save(output_file)
    print(f"\n{GREEN}已保存演示文件：{output_file}{RESET}")


def parse_key(key_str: str) -> Tuple[str, str]:
    """
    解析调号字符串
    
    Args:
        key_str: 如 "G", "Gm", "G major", "G minor", "F#", "F#m"
        
    Returns:
        (根音, 调式)
    """
    key_str = key_str.strip()
    
    # 检查是否有调式标记
    mode = "major"
    
    if key_str.lower().endswith(" minor"):
        mode = "minor"
        key_str = key_str[:-6].strip()
    elif key_str.lower().endswith(" major"):
        mode = "major"
        key_str = key_str[:-6].strip()
    elif key_str.lower().endswith("m"):
        # 检查是不是以 m 结尾（小调）
        potential_root = key_str[:-1].upper()
        if potential_root in NOTE_TO_INT:
            mode = "minor"
            key_str = key_str[:-1]
    
    root = key_str.upper()
    if root not in NOTE_TO_INT:
        raise ValueError(f"无效的调号：{key_str}")
    
    return (root, mode)


def main():
    parser = argparse.ArgumentParser(
        description="MIDI 调号转换器 - 按音阶位置转调",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s input.mid --detect                    # 仅检测调号
  %(prog)s input.mid -o output.mid --to G        # 转到 G 大调
  %(prog)s input.mid -o output.mid --to Gm       # 转到 G 小调
  %(prog)s input.mid -o output.mid --to "F# minor"
  %(prog)s input.mid -o output.mid --from C --to G  # 指定原调
  %(prog)s --demo                                # 运行演示
        """
    )
    
    parser.add_argument("input", nargs="?", help="输入 MIDI 文件")
    parser.add_argument("-o", "--output", help="输出 MIDI 文件")
    parser.add_argument("--to", dest="target_key", help="目标调号 (如 G, Gm, 'F# minor')")
    parser.add_argument("--from", dest="source_key", help="原调号（可选，不指定则自动检测）")
    parser.add_argument("--detect", action="store_true", help="仅检测调号，不转换")
    parser.add_argument("--demo", action="store_true", help="运行演示")
    
    args = parser.parse_args()
    
    if args.demo:
        demo()
        return
    
    if not args.input:
        parser.print_help()
        sys.exit(1)
    
    # 读取 MIDI 文件
    try:
        mid = mido.MidiFile(args.input)
    except Exception as e:
        print(f"{RED}无法读取 MIDI 文件：{e}{RESET}")
        sys.exit(1)
    
    transposer = KeyTransposer()
    
    # 仅检测调号
    if args.detect:
        key, mode = transposer.detector.detect_from_midi(mid)
        print(f"检测到调号：{GREEN}{key} {mode}{RESET}")
        return
    
    # 转调
    if not args.target_key:
        print(f"{RED}请指定目标调号 (--to){RESET}")
        parser.print_help()
        sys.exit(1)
    
    try:
        target_root, target_mode = parse_key(args.target_key)
    except ValueError as e:
        print(f"{RED}{e}{RESET}")
        sys.exit(1)
    
    source_root, source_mode = None, None
    if args.source_key:
        try:
            source_root, source_mode = parse_key(args.source_key)
        except ValueError as e:
            print(f"{RED}{e}{RESET}")
            sys.exit(1)
    
    # 执行转调
    output_mid = transposer.transpose_midi(
        mid,
        target_root,
        target_mode,
        source_root,
        source_mode
    )
    
    # 保存输出
    output_file = args.output or args.input.replace(".mid", f"_{target_root}{target_mode}.mid")
    output_mid.save(output_file)
    print(f"{GREEN}已保存到：{output_file}{RESET}")


if __name__ == "__main__":
    main()
