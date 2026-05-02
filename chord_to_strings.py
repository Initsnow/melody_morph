"""
Chord to Strings Converter - 将和弦音拆分为弦乐声部

功能：
1. 读取MIDI文件中的吉他扫弦或钢琴和弦
2. 将和弦中的音符分配给不同的弦乐器（Violin, Viola, Cello）
3. 支持自定义各弦乐器的数量
4. 输出多轨道MIDI文件

分配策略：
- 从低到高排列和弦音
- Cello 负责最低音
- Viola 负责中音
- Violin 负责最高音
- 当和弦音数量超过乐器数量时，一个乐器可能演奏多个音（双音技法）
- 当和弦音数量少于乐器数量时，某些乐器可能演奏相同的音（齐奏）
"""

import mido
from dataclasses import dataclass
from enum import Enum
import os


class StringInstrument(Enum):
    """弦乐器类型"""
    VIOLIN = "violin"  # 小提琴 音域: G3(55) - E7(100)
    VIOLA = "viola"    # 中提琴 音域: C3(48) - A6(93)
    CELLO = "cello"    # 大提琴 音域: C2(36) - A5(81)


# 弦乐器音域范围 (MIDI音高)
INSTRUMENT_RANGES = {
    StringInstrument.VIOLIN: (55, 100),  # G3 - E7
    StringInstrument.VIOLA: (48, 93),     # C3 - A6
    StringInstrument.CELLO: (36, 81),     # C2 - A5
}

# General MIDI 乐器编号
GM_INSTRUMENTS = {
    StringInstrument.VIOLIN: 40,  # Violin
    StringInstrument.VIOLA: 41,   # Viola
    StringInstrument.CELLO: 42,   # Cello
}

# 音名映射
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def pitch_to_name(pitch: int) -> str:
    """将MIDI音高转换为音名"""
    octave = pitch // 12 - 1
    note = NOTE_NAMES[pitch % 12]
    return f"{note}{octave}"


@dataclass
class ChordEvent:
    """和弦事件"""
    pitches: list[int]  # 和弦中的音高列表（从低到高排序）
    velocities: list[int]  # 对应的力度列表
    start_tick: int  # 开始时间（tick）
    duration_ticks: int  # 持续时间（tick）
    
    def __post_init__(self):
        # 确保按音高排序
        if self.pitches:
            pairs = sorted(zip(self.pitches, self.velocities))
            self.pitches, self.velocities = list(zip(*pairs)) if pairs else ([], [])
            self.pitches = list(self.pitches)
            self.velocities = list(self.velocities)


@dataclass
class StringsConfig:
    """弦乐配置"""
    violins: int = 1       # 小提琴数量
    violas: int = 1        # 中提琴数量
    cellos: int = 1        # 大提琴数量
    
    @property
    def total(self) -> int:
        """总乐器数量"""
        return self.violins + self.violas + self.cellos
    
    def get_instrument_list(self) -> list[StringInstrument]:
        """
        获取从低到高排列的乐器列表
        Cello -> Viola -> Violin
        """
        instruments = []
        instruments.extend([StringInstrument.CELLO] * self.cellos)
        instruments.extend([StringInstrument.VIOLA] * self.violas)
        instruments.extend([StringInstrument.VIOLIN] * self.violins)
        return instruments


class ChordToStringsConverter:
    """和弦到弦乐转换器"""
    
    def __init__(
        self,
        config: StringsConfig = None,
        chord_threshold_ticks: int = 10,
        min_note_duration_ticks: int = 1,
        adjust_octave: bool = True,
    ):
        """
        初始化转换器
        
        Args:
            config: 弦乐配置
            chord_threshold_ticks: 和弦检测阈值（tick），
                                   同时或接近同时的音符会被视为和弦
            min_note_duration_ticks: 最小音符持续时间
            adjust_octave: 是否自动调整八度以适应乐器音域
        """
        self.config = config or StringsConfig()
        self.chord_threshold = chord_threshold_ticks
        self.min_note_duration = min_note_duration_ticks
        self.adjust_octave = adjust_octave
    
    def extract_chords(self, track: mido.MidiTrack) -> list[ChordEvent]:
        """
        从MIDI轨道中提取和弦事件
        
        Args:
            track: MIDI轨道
            
        Returns:
            和弦事件列表
        """
        # 首先提取所有音符及其时间信息
        notes = []  # (start_tick, end_tick, pitch, velocity)
        current_tick = 0
        active_notes = {}  # pitch -> (start_tick, velocity)
        
        for msg in track:
            current_tick += msg.time
            
            if msg.type == 'note_on' and msg.velocity > 0:
                active_notes[msg.note] = (current_tick, msg.velocity)
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                if msg.note in active_notes:
                    start, vel = active_notes.pop(msg.note)
                    notes.append((start, current_tick, msg.note, vel))
        
        # 按开始时间排序
        notes.sort(key=lambda x: x[0])
        
        if not notes:
            return []
        
        # 将接近同时的音符组合为和弦
        chords = []
        current_chord_start = notes[0][0]
        current_chord_notes = []
        
        for start, end, pitch, vel in notes:
            if start - current_chord_start <= self.chord_threshold:
                # 属于同一个和弦
                current_chord_notes.append((start, end, pitch, vel))
            else:
                # 新和弦开始，保存之前的和弦
                if current_chord_notes:
                    chord = self._create_chord_event(current_chord_notes)
                    chords.append(chord)
                
                current_chord_start = start
                current_chord_notes = [(start, end, pitch, vel)]
        
        # 保存最后一个和弦
        if current_chord_notes:
            chord = self._create_chord_event(current_chord_notes)
            chords.append(chord)
        
        return chords
    
    def _create_chord_event(self, notes: list[tuple]) -> ChordEvent:
        """从音符列表创建和弦事件"""
        pitches = [n[2] for n in notes]
        velocities = [n[3] for n in notes]
        start_tick = min(n[0] for n in notes)
        end_tick = max(n[1] for n in notes)
        duration = max(end_tick - start_tick, self.min_note_duration)
        
        return ChordEvent(
            pitches=pitches,
            velocities=velocities,
            start_tick=start_tick,
            duration_ticks=duration
        )
    
    def assign_to_instruments(
        self, 
        chord: ChordEvent
    ) -> dict[int, list[tuple[int, int]]]:
        """
        将和弦音分配给各个乐器
        
        Args:
            chord: 和弦事件
            
        Returns:
            字典 {乐器索引: [(音高, 力度), ...]}
            乐器索引0是最低的cello，最高索引是最高的violin
        """
        instruments = self.config.get_instrument_list()
        num_instruments = len(instruments)
        num_notes = len(chord.pitches)
        
        # 分配字典
        assignments: dict[int, list[tuple[int, int]]] = {
            i: [] for i in range(num_instruments)
        }
        
        if num_notes == 0:
            return assignments
        
        if num_notes >= num_instruments:
            # 音符数量 >= 乐器数量：均匀分配
            notes_per_instrument = num_notes // num_instruments
            extra_notes = num_notes % num_instruments
            
            note_idx = 0
            for inst_idx in range(num_instruments):
                # 前几个乐器多分配一个音符
                count = notes_per_instrument + (1 if inst_idx < extra_notes else 0)
                for _ in range(count):
                    if note_idx < num_notes:
                        pitch = chord.pitches[note_idx]
                        vel = chord.velocities[note_idx]
                        
                        # 调整八度以适应乐器音域
                        if self.adjust_octave:
                            pitch = self._adjust_to_range(pitch, instruments[inst_idx])
                        
                        assignments[inst_idx].append((pitch, vel))
                        note_idx += 1
        else:
            # 音符数量 < 乐器数量：某些乐器共享音符
            # 策略：均匀分布，优先填充最高和最低的位置
            
            # 首先分配给分散的位置
            positions = self._distribute_notes(num_notes, num_instruments)
            
            for i, note_idx in enumerate(range(num_notes)):
                inst_idx = positions[i]
                pitch = chord.pitches[note_idx]
                vel = chord.velocities[note_idx]
                
                if self.adjust_octave:
                    pitch = self._adjust_to_range(pitch, instruments[inst_idx])
                
                assignments[inst_idx].append((pitch, vel))
        
        return assignments
    
    def _distribute_notes(self, num_notes: int, num_positions: int) -> list[int]:
        """
        将音符分布到指定数量的位置
        
        策略：尽量均匀分布
        """
        if num_notes >= num_positions:
            return list(range(num_positions))
        
        positions = []
        if num_notes == 1:
            # 单音：放在中间偏低的位置（更像bass line）
            positions = [0]
        elif num_notes == 2:
            # 两个音：最低和最高
            positions = [0, num_positions - 1]
        else:
            # 多个音：尽量均匀分布
            step = (num_positions - 1) / (num_notes - 1) if num_notes > 1 else 0
            for i in range(num_notes):
                pos = int(round(i * step))
                positions.append(min(pos, num_positions - 1))
        
        return positions
    
    def _adjust_to_range(self, pitch: int, instrument: StringInstrument) -> int:
        """调整音高到乐器音域范围内"""
        low, high = INSTRUMENT_RANGES[instrument]
        
        # 如果已经在范围内，直接返回
        if low <= pitch <= high:
            return pitch
        
        # 调整八度
        if pitch < low:
            while pitch < low:
                pitch += 12
        elif pitch > high:
            while pitch > high:
                pitch -= 12
        
        # 确保在范围内（处理极端情况）
        return max(low, min(pitch, high))
    
    def convert(
        self, 
        input_path: str, 
        output_path: str,
        source_track: int = 0,
    ) -> dict:
        """
        转换MIDI文件
        
        Args:
            input_path: 输入MIDI文件路径
            output_path: 输出MIDI文件路径
            source_track: 源轨道索引
            
        Returns:
            转换统计信息
        """
        # 读取MIDI文件
        mid = mido.MidiFile(input_path)
        
        if source_track >= len(mid.tracks):
            raise ValueError(
                f"轨道索引 {source_track} 超出范围（共 {len(mid.tracks)} 个轨道）"
            )
        
        # 提取和弦
        chords = self.extract_chords(mid.tracks[source_track])
        
        # 创建新的MIDI文件
        new_mid = mido.MidiFile(ticks_per_beat=mid.ticks_per_beat)
        
        # 复制元信息轨道（如果存在）
        # 通常第一个轨道包含tempo等元信息
        if mid.tracks:
            meta_track = mido.MidiTrack()
            for msg in mid.tracks[0]:
                if msg.type in ('set_tempo', 'time_signature', 'key_signature', 
                               'track_name', 'text'):
                    meta_track.append(msg.copy())
            if meta_track:
                new_mid.tracks.append(meta_track)
        
        # 为每个乐器创建轨道
        instruments = self.config.get_instrument_list()
        instrument_tracks: list[list[tuple]] = [[] for _ in instruments]
        # 每个轨道存储 (absolute_tick, msg_type, pitch, velocity, duration)
        
        # 分配和弦到各个乐器
        for chord in chords:
            assignments = self.assign_to_instruments(chord)
            
            for inst_idx, notes in assignments.items():
                for pitch, vel in notes:
                    instrument_tracks[inst_idx].append((
                        chord.start_tick,
                        pitch,
                        vel,
                        chord.duration_ticks
                    ))
        
        # 创建MIDI轨道
        for inst_idx, instrument in enumerate(instruments):
            track = mido.MidiTrack()
            
            # 设置轨道名称
            track_name = f"{instrument.value.capitalize()} {inst_idx + 1}"
            track.append(mido.MetaMessage('track_name', name=track_name))
            
            # 设置乐器音色（使用channel）
            channel = inst_idx % 16  # MIDI最多16个通道
            program = GM_INSTRUMENTS[instrument]
            track.append(mido.Message('program_change', 
                                     channel=channel, 
                                     program=program, 
                                     time=0))
            
            # 转换音符事件为MIDI消息
            events = []  # (tick, is_note_on, pitch, vel, channel)
            for start, pitch, vel, duration in instrument_tracks[inst_idx]:
                events.append((start, True, pitch, vel, channel))
                events.append((start + duration, False, pitch, 0, channel))
            
            # 按时间排序
            events.sort(key=lambda x: (x[0], not x[1]))  # note_off 在 note_on 之前
            
            # 转换为相对时间并添加到轨道
            last_tick = 0
            for tick, is_note_on, pitch, vel, ch in events:
                delta = tick - last_tick
                msg_type = 'note_on' if is_note_on else 'note_off'
                track.append(mido.Message(msg_type, 
                                         note=pitch, 
                                         velocity=vel if is_note_on else 0,
                                         channel=ch,
                                         time=delta))
                last_tick = tick
            
            # 添加轨道结束标记
            track.append(mido.MetaMessage('end_of_track', time=0))
            new_mid.tracks.append(track)
        
        # 保存MIDI文件
        new_mid.save(output_path)
        
        # 返回统计信息
        return {
            'input_file': input_path,
            'output_file': output_path,
            'num_chords': len(chords),
            'num_tracks': len(instruments),
            'instruments': [inst.value for inst in instruments],
            'notes_per_track': [len(t) for t in instrument_tracks],
        }


def demo():
    """演示用法"""
    print("=" * 60)
    print("Chord to Strings Converter 演示")
    print("=" * 60)
    
    # 创建配置
    config = StringsConfig(violins=2, violas=1, cellos=1)
    print("\n弦乐配置:")
    print(f"  Violins: {config.violins}")
    print(f"  Violas: {config.violas}")
    print(f"  Cellos: {config.cellos}")
    print(f"  总计: {config.total} 个声部")
    
    # 显示乐器列表
    instruments = config.get_instrument_list()
    print("\n乐器排列（从低到高）:")
    for i, inst in enumerate(instruments):
        low, high = INSTRUMENT_RANGES[inst]
        print(f"  {i}: {inst.value} ({pitch_to_name(low)} - {pitch_to_name(high)})")
    
    # 创建一个示例和弦
    chord = ChordEvent(
        pitches=[48, 55, 60, 64, 67],  # C3, G3, C4, E4, G4 (Cmaj)
        velocities=[80, 80, 80, 80, 80],
        start_tick=0,
        duration_ticks=480
    )
    
    print(f"\n示例和弦: {[pitch_to_name(p) for p in chord.pitches]}")
    
    # 分配到乐器
    converter = ChordToStringsConverter(config)
    assignments = converter.assign_to_instruments(chord)
    
    print("\n分配结果:")
    for inst_idx, notes in assignments.items():
        inst = instruments[inst_idx]
        if notes:
            note_names = [f"{pitch_to_name(p)}(v{v})" for p, v in notes]
            print(f"  {inst.value} {inst_idx}: {', '.join(note_names)}")
        else:
            print(f"  {inst.value} {inst_idx}: (无)")
    
    print("\n" + "=" * 60)
    print("要处理实际MIDI文件，请使用命令行接口")
    print("示例: python chord_to_strings.py input.mid -o output.mid")
    print("=" * 60)


def cli():
    """命令行接口"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="将吉他/钢琴和弦拆分为弦乐声部",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 基本用法：使用默认配置（1 violin, 1 viola, 1 cello）
  python chord_to_strings.py input.mid -o output.mid

  # 自定义弦乐配置（2小提琴，1中提琴，1大提琴）
  python chord_to_strings.py input.mid -o output.mid --violins 2 --violas 1 --cellos 1

  # 指定源轨道
  python chord_to_strings.py input.mid -o output.mid -t 1

  # 设置和弦检测阈值
  python chord_to_strings.py input.mid -o output.mid --threshold 20

  # 运行演示
  python chord_to_strings.py --demo
"""
    )
    
    parser.add_argument(
        "input",
        nargs="?",
        help="输入MIDI文件路径"
    )
    parser.add_argument(
        "-o", "--output",
        help="输出MIDI文件路径（默认: 输入文件名_strings.mid）"
    )
    parser.add_argument(
        "-t", "--track",
        type=int,
        default=0,
        help="源轨道索引 (默认: 0)"
    )
    parser.add_argument(
        "--violins",
        type=int,
        default=1,
        help="小提琴数量 (默认: 1)"
    )
    parser.add_argument(
        "--violas",
        type=int,
        default=1,
        help="中提琴数量 (默认: 1)"
    )
    parser.add_argument(
        "--cellos",
        type=int,
        default=1,
        help="大提琴数量 (默认: 1)"
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=10,
        help="和弦检测阈值（tick）(默认: 10)"
    )
    parser.add_argument(
        "--no-adjust",
        action="store_true",
        help="禁用八度自动调整"
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
    
    # 确定输出路径
    output_path = args.output
    if not output_path:
        base, ext = os.path.splitext(args.input)
        output_path = f"{base}_strings{ext}"
    
    # 创建配置
    config = StringsConfig(
        violins=args.violins,
        violas=args.violas,
        cellos=args.cellos
    )
    
    print(f"输入文件: {args.input}")
    print(f"输出文件: {output_path}")
    print(f"源轨道: {args.track}")
    print("\n弦乐配置:")
    print(f"  Violins: {config.violins}")
    print(f"  Violas: {config.violas}")
    print(f"  Cellos: {config.cellos}")
    
    # 创建转换器并执行
    converter = ChordToStringsConverter(
        config=config,
        chord_threshold_ticks=args.threshold,
        adjust_octave=not args.no_adjust
    )
    
    try:
        stats = converter.convert(
            input_path=args.input,
            output_path=output_path,
            source_track=args.track
        )
        
        print("\n转换完成!")
        print(f"  处理和弦数: {stats['num_chords']}")
        print(f"  输出轨道数: {stats['num_tracks']}")
        print(f"  各轨道音符数: {dict(zip(stats['instruments'], stats['notes_per_track']))}")
        print(f"\n已保存到: {output_path}")
        
    except Exception as e:
        print(f"\n错误: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    cli()
