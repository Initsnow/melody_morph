"""
MIDI Melody Corrector - 将MIDI乐句中的音符纠正到指定的目标音（如和弦内音）

支持多种纠正算法：
1. nearest - 最近音纠正（选择最近的目标音）
2. weighted_random - 加权随机（距离越近概率越高）
3. direction_bias - 方向偏向（根据旋律走向选择）
4. random - 完全随机（随机选择一个目标音）
"""

import random
from typing import Literal
from dataclasses import dataclass
from enum import Enum
import mido


class CorrectionMethod(Enum):
    """纠正算法枚举"""
    NEAREST = "nearest"  # 最近音
    WEIGHTED_RANDOM = "weighted_random"  # 加权随机
    DIRECTION_BIAS = "direction_bias"  # 方向偏向
    RANDOM = "random"  # 完全随机


@dataclass
class Note:
    """表示一个MIDI音符"""
    pitch: int  # MIDI音高 (0-127)
    velocity: int = 100  # 力度 (0-127)
    start_time: float = 0.0  # 开始时间（秒或tick）
    duration: float = 1.0  # 持续时间

    def __repr__(self):
        return f"Note(pitch={self.pitch}, name={pitch_to_name(self.pitch)})"


# 音名映射
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def pitch_to_name(pitch: int) -> str:
    """将MIDI音高转换为音名"""
    octave = pitch // 12 - 1
    note = NOTE_NAMES[pitch % 12]
    return f"{note}{octave}"


def name_to_pitch(name: str) -> int:
    """将音名转换为MIDI音高（如 'C4' -> 60）"""
    # 处理升降号
    if len(name) >= 2 and name[1] in '#b':
        note_part = name[:2]
        octave_part = name[2:]
    else:
        note_part = name[0]
        octave_part = name[1:]
    
    # 处理降号
    if 'b' in note_part:
        base_note = note_part[0]
        idx = NOTE_NAMES.index(base_note) - 1
        if idx < 0:
            idx = 11
        note_part = NOTE_NAMES[idx]
    
    note_idx = NOTE_NAMES.index(note_part.replace('b', '').upper())
    octave = int(octave_part)
    return (octave + 1) * 12 + note_idx


def expand_target_notes_to_all_octaves(target_notes: list[int]) -> list[int]:
    """
    将目标音扩展到所有八度（0-127范围内）
    
    Args:
        target_notes: 目标音的音高列表（可以是任意八度的音）
    
    Returns:
        扩展到所有八度的音高列表
    """
    # 提取音级（0-11）
    pitch_classes = set(note % 12 for note in target_notes)
    
    # 扩展到所有八度
    all_pitches = []
    for octave in range(11):  # MIDI 0-127 覆盖约10.5个八度
        for pc in pitch_classes:
            pitch = octave * 12 + pc
            if 0 <= pitch <= 127:
                all_pitches.append(pitch)
    
    return sorted(all_pitches)


def find_nearest_target(pitch: int, targets: list[int]) -> int:
    """找到最近的目标音"""
    return min(targets, key=lambda t: abs(t - pitch))


def find_nearest_targets_in_direction(
    pitch: int, 
    targets: list[int], 
    prefer_up: bool
) -> tuple[int | None, int | None]:
    """
    找到上下方向最近的目标音
    
    Returns:
        (上方最近音, 下方最近音)
    """
    upper = [t for t in targets if t >= pitch]
    lower = [t for t in targets if t <= pitch]
    
    nearest_up = min(upper) if upper else None
    nearest_down = max(lower) if lower else None
    
    return nearest_up, nearest_down


class MelodyCorrector:
    """MIDI旋律纠正器"""
    
    def __init__(
        self,
        target_notes: list[int],
        method: CorrectionMethod = CorrectionMethod.NEAREST,
        randomness: float = 0.3,
        direction_strength: float = 0.7,
    ):
        """
        初始化纠正器
        
        Args:
            target_notes: 目标音列表（会自动扩展到所有八度）
            method: 纠正算法
            randomness: 随机性参数（0-1），用于 weighted_random 方法
            direction_strength: 方向偏向强度（0-1），用于 direction_bias 方法
        """
        self.target_notes = expand_target_notes_to_all_octaves(target_notes)
        self.method = method
        self.randomness = randomness
        self.direction_strength = direction_strength
        self._previous_pitch: int | None = None
    
    def correct_note(self, note: Note) -> Note:
        """
        纠正单个音符
        
        Args:
            note: 原始音符
        
        Returns:
            纠正后的音符（新对象）
        """
        # 如果已经是目标音，直接返回
        if note.pitch in self.target_notes:
            new_note = Note(
                pitch=note.pitch,
                velocity=note.velocity,
                start_time=note.start_time,
                duration=note.duration
            )
            self._previous_pitch = note.pitch
            return new_note
        
        # 根据算法选择目标音
        if self.method == CorrectionMethod.NEAREST:
            new_pitch = self._correct_nearest(note.pitch)
        elif self.method == CorrectionMethod.WEIGHTED_RANDOM:
            new_pitch = self._correct_weighted_random(note.pitch)
        elif self.method == CorrectionMethod.DIRECTION_BIAS:
            new_pitch = self._correct_direction_bias(note.pitch)
        elif self.method == CorrectionMethod.RANDOM:
            new_pitch = self._correct_random(note.pitch)
        else:
            new_pitch = self._correct_nearest(note.pitch)
        
        self._previous_pitch = note.pitch
        
        return Note(
            pitch=new_pitch,
            velocity=note.velocity,
            start_time=note.start_time,
            duration=note.duration
        )
    
    def correct_melody(self, notes: list[Note]) -> list[Note]:
        """
        纠正整个旋律
        
        Args:
            notes: 音符列表
        
        Returns:
            纠正后的音符列表
        """
        self._previous_pitch = None
        return [self.correct_note(note) for note in notes]
    
    def _correct_nearest(self, pitch: int) -> int:
        """最近音纠正"""
        return find_nearest_target(pitch, self.target_notes)
    
    def _correct_weighted_random(self, pitch: int) -> int:
        """
        加权随机纠正
        距离越近的目标音被选中的概率越高
        """
        # 计算每个目标音的权重（距离的倒数）
        candidates = []
        weights = []
        
        for target in self.target_notes:
            distance = abs(target - pitch)
            if distance <= 12:  # 只考虑一个八度内的音
                candidates.append(target)
                # 使用指数衰减，randomness越高，远处的音权重越大
                weight = 1.0 / (1 + distance * (1 - self.randomness))
                weights.append(weight)
        
        if not candidates:
            return find_nearest_target(pitch, self.target_notes)
        
        # 归一化权重
        total = sum(weights)
        weights = [w / total for w in weights]
        
        return random.choices(candidates, weights=weights, k=1)[0]
    
    def _correct_direction_bias(self, pitch: int) -> int:
        """
        方向偏向纠正
        根据前一个音符判断旋律走向，倾向于选择顺向的目标音
        """
        nearest_up, nearest_down = find_nearest_targets_in_direction(
            pitch, self.target_notes, True
        )
        
        # 如果只有一个方向有音，选那个
        if nearest_up is None:
            return nearest_down  # type: ignore
        if nearest_down is None:
            return nearest_up
        
        # 判断旋律方向
        if self._previous_pitch is not None:
            going_up = pitch > self._previous_pitch
            going_down = pitch < self._previous_pitch
        else:
            going_up = going_down = False
        
        # 计算选择上方音的概率
        if going_up:
            prob_up = 0.5 + self.direction_strength * 0.5
        elif going_down:
            prob_up = 0.5 - self.direction_strength * 0.5
        else:
            prob_up = 0.5
        
        # 加入距离因素
        dist_up = nearest_up - pitch
        dist_down = pitch - nearest_down
        
        # 调整概率：距离更近的选项获得加成
        if dist_up < dist_down:
            prob_up = min(1.0, prob_up + 0.1)
        elif dist_down < dist_up:
            prob_up = max(0.0, prob_up - 0.1)
        
        return nearest_up if random.random() < prob_up else nearest_down
    
    def _correct_random(self, pitch: int) -> int:
        """
        完全随机纠正
        从附近的目标音中随机选择
        """
        # 限制在2个八度范围内
        candidates = [
            t for t in self.target_notes 
            if abs(t - pitch) <= 24
        ]
        
        if not candidates:
            return find_nearest_target(pitch, self.target_notes)
        
        return random.choice(candidates)


def demo():
    """演示用法"""
    print("=" * 60)
    print("MIDI Melody Corrector 演示")
    print("=" * 60)
    
    # 定义一个C大调和弦 (C-E-G)
    # 使用C4, E4, G4作为基准，会自动扩展到所有八度
    chord_notes = [60, 64, 67]  # C4, E4, G4
    
    print(f"\n目标和弦音: {[pitch_to_name(p) for p in chord_notes]}")
    print("(将自动扩展到所有八度)")
    
    # 创建一个测试旋律
    test_melody = [
        Note(pitch=62, start_time=0.0, duration=0.5),   # D4 - 不在和弦内
        Note(pitch=64, start_time=0.5, duration=0.5),   # E4 - 在和弦内
        Note(pitch=66, start_time=1.0, duration=0.5),   # F#4 - 不在和弦内
        Note(pitch=68, start_time=1.5, duration=0.5),   # G#4 - 不在和弦内
        Note(pitch=69, start_time=2.0, duration=0.5),   # A4 - 不在和弦内
        Note(pitch=71, start_time=2.5, duration=0.5),   # B4 - 不在和弦内
        Note(pitch=72, start_time=3.0, duration=0.5),   # C5 - 在和弦内
    ]
    
    print(f"\n原始旋律: {[pitch_to_name(n.pitch) for n in test_melody]}")
    
    # 测试不同的纠正方法
    methods = [
        (CorrectionMethod.NEAREST, "最近音"),
        (CorrectionMethod.WEIGHTED_RANDOM, "加权随机"),
        (CorrectionMethod.DIRECTION_BIAS, "方向偏向"),
        (CorrectionMethod.RANDOM, "完全随机"),
    ]
    
    for method, name in methods:
        print(f"\n--- {name} 纠正 ({method.value}) ---")
        corrector = MelodyCorrector(
            target_notes=chord_notes,
            method=method,
            randomness=0.5,
            direction_strength=0.7
        )
        
        # 运行3次展示随机性
        for i in range(3):
            corrected = corrector.correct_melody(test_melody)
            result = [pitch_to_name(n.pitch) for n in corrected]
            print(f"  第{i+1}次: {result}")





def parse_target_notes(notes_str: str) -> list[int]:
    """
    解析目标音字符串
    
    支持格式:
        - 音名: "C4,E4,G4" 或 "C,E,G"（默认第4八度）
        - MIDI数值: "60,64,67"
        - 混合: "C4,64,G4"
    """
    notes = []
    for part in notes_str.split(','):
        part = part.strip()
        if not part:
            continue
        
        # 尝试解析为数字
        try:
            notes.append(int(part))
        except ValueError:
            # 尝试解析为音名
            if part[-1].isdigit():
                notes.append(name_to_pitch(part))
            else:
                # 没有八度号，默认第4八度
                notes.append(name_to_pitch(part + "4"))
    
    return notes


def cli():
    """命令行接口"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="MIDI旋律纠正器 - 将MIDI中的音符纠正到指定的目标音",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 纠正到C大调和弦 (C-E-G)，使用最近音算法
  python melody_corrector.py input.mid -o output.mid -n C,E,G
  
  # 使用加权随机算法，随机性0.5
  python melody_corrector.py input.mid -o output.mid -n C4,E4,G4 -m weighted_random -r 0.5
  
  # 使用方向偏向算法
  python melody_corrector.py input.mid -o output.mid -n 60,64,67 -m direction_bias
  
  # 只处理第2轨道
  python melody_corrector.py input.mid -o output.mid -n C,E,G,B -t 1
  
  # 运行演示
  python melody_corrector.py --demo
"""
    )
    
    parser.add_argument(
        "input",
        nargs="?",
        help="输入MIDI文件路径"
    )
    parser.add_argument(
        "-o", "--output",
        help="输出MIDI文件路径（默认: 输入文件名_corrected.mid）"
    )
    parser.add_argument(
        "-n", "--notes",
        help="目标音列表，逗号分隔。支持音名(C4,E4,G4)或MIDI数值(60,64,67)"
    )
    parser.add_argument(
        "-m", "--method",
        choices=["nearest", "weighted_random", "direction_bias", "random"],
        default="nearest",
        help="纠正算法 (默认: nearest)"
    )
    parser.add_argument(
        "-t", "--track",
        type=int,
        default=0,
        help="要纠正的轨道索引 (默认: 0)"
    )
    parser.add_argument(
        "-r", "--randomness",
        type=float,
        default=0.3,
        help="随机性参数，用于weighted_random方法 (0-1, 默认: 0.3)"
    )
    parser.add_argument(
        "-d", "--direction-strength",
        type=float,
        default=0.7,
        help="方向偏向强度，用于direction_bias方法 (0-1, 默认: 0.7)"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="运行演示"
    )
    
    args = parser.parse_args()
    
    if args.demo:
        demo()
        return
    
    if not args.input:
        parser.error("请提供输入MIDI文件路径，或使用 --demo 运行演示")
    
    if not args.notes:
        parser.error("请使用 -n/--notes 指定目标音")
    
    # 解析目标音
    target_notes = parse_target_notes(args.notes)
    print(f"目标音: {[pitch_to_name(p) for p in target_notes]}")
    
    # 确定输出路径
    output_path = args.output
    if not output_path:
        import os
        base, ext = os.path.splitext(args.input)
        output_path = f"{base}_corrected{ext}"
    
    # 解析纠正方法
    method = CorrectionMethod(args.method)
    
    print(f"输入文件: {args.input}")
    print(f"输出文件: {output_path}")
    print(f"纠正方法: {method.value}")
    print(f"轨道: {args.track}")
    
    # 执行纠正
    correct_midi_file(
        input_path=args.input,
        output_path=output_path,
        target_notes=target_notes,
        method=method,
        track=args.track,
        randomness=args.randomness,
        direction_strength=args.direction_strength,
    )


def correct_midi_file(
    input_path: str,
    output_path: str,
    target_notes: list[int],
    method: CorrectionMethod = CorrectionMethod.NEAREST,
    track: int = 0,
    randomness: float = 0.3,
    direction_strength: float = 0.7,
):
    """
    纠正MIDI文件中的音符
    
    需要安装 mido 库: pip install mido
    
    Args:
        input_path: 输入MIDI文件路径
        output_path: 输出MIDI文件路径
        target_notes: 目标音列表
        method: 纠正算法
        track: 要纠正的轨道索引
        randomness: 随机性参数
        direction_strength: 方向偏向强度
    """
    mid = mido.MidiFile(input_path)
    
    if track >= len(mid.tracks):
        print(f"错误: 轨道索引 {track} 超出范围（共 {len(mid.tracks)} 个轨道）")
        return
    
    corrector = MelodyCorrector(
        target_notes=target_notes,
        method=method,
        randomness=randomness,
        direction_strength=direction_strength,
    )
    
    # 统计
    original_notes = []
    corrected_notes = []
    
    # 跟踪每个原始音高对应的纠正后音高
    # 使用字典存储: {原始音高: [纠正后音高列表]}（支持同一音高重叠）
    pitch_mapping: dict[int, list[int]] = {}
    
    # 创建新轨道
    target_track = mid.tracks[track]
    new_track = mido.MidiTrack()
    
    for msg in target_track:
        if msg.type == 'note_on' and msg.velocity > 0:
            original_notes.append(msg.note)
            # 纠正音高
            note = Note(pitch=msg.note, velocity=msg.velocity)
            corrected = corrector.correct_note(note)
            corrected_notes.append(corrected.pitch)
            
            # 记录原始音高到纠正后音高的映射
            if msg.note not in pitch_mapping:
                pitch_mapping[msg.note] = []
            pitch_mapping[msg.note].append(corrected.pitch)
            
            new_track.append(msg.copy(note=corrected.pitch))
        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
            # note_off 事件：根据原始音高找到对应的纠正后音高
            original_pitch = msg.note
            if original_pitch in pitch_mapping and pitch_mapping[original_pitch]:
                # 使用FIFO方式匹配（先开始的音符先结束）
                corrected_pitch = pitch_mapping[original_pitch].pop(0)
                new_track.append(msg.copy(note=corrected_pitch))
            else:
                # 如果找不到映射，保持原样
                new_track.append(msg.copy())
        else:
            new_track.append(msg.copy())
    
    # 替换轨道
    mid.tracks[track] = new_track
    mid.save(output_path)
    
    # 打印统计
    changed = sum(1 for o, c in zip(original_notes, corrected_notes) if o != c)
    print(f"\n纠正完成!")
    print(f"  总音符数: {len(original_notes)}")
    print(f"  已纠正: {changed}")
    print(f"  未改变: {len(original_notes) - changed}")
    print(f"\n已保存到: {output_path}")


if __name__ == "__main__":
    cli()
