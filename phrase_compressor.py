"""
MIDI Phrase Compressor - 将MIDI乐句进行时间压缩（等比例缩放）

功能：
1. 将多个小节的内容压缩到更少的小节中
2. 支持指定压缩比例（如 4:1 表示4小节压缩为1小节）
3. 保持音符之间的相对时间关系
4. 可选择保持或调整音符时值
"""

import mido
from dataclasses import dataclass
import os


# 音名映射
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def pitch_to_name(pitch: int) -> str:
    """将MIDI音高转换为音名"""
    octave = pitch // 12 - 1
    note = NOTE_NAMES[pitch % 12]
    return f"{note}{octave}"


@dataclass
class NoteEvent:
    """表示一个MIDI音符事件"""
    pitch: int  # MIDI音高 (0-127)
    velocity: int  # 力度 (0-127)
    start_tick: int  # 开始时间（tick）
    duration_ticks: int  # 持续时间（tick）
    
    def __repr__(self):
        return f"NoteEvent({pitch_to_name(self.pitch)}, start={self.start_tick}, dur={self.duration_ticks})"


class PhraseCompressor:
    """MIDI乐句压缩器"""
    
    def __init__(
        self,
        compression_ratio: float = 2.0,
        preserve_duration_ratio: bool = True,
        min_duration_ticks: int = 1,
    ):
        """
        初始化压缩器
        
        Args:
            compression_ratio: 压缩比例，如2.0表示时间缩短为原来的1/2（2小节变1小节）
            preserve_duration_ratio: 是否按比例压缩音符时值，False则保持原时值
            min_duration_ticks: 压缩后的最小音符时值（tick）
        """
        if compression_ratio <= 0:
            raise ValueError("压缩比例必须大于0")
        
        self.compression_ratio = compression_ratio
        self.preserve_duration_ratio = preserve_duration_ratio
        self.min_duration_ticks = min_duration_ticks
    
    def extract_notes(self, track: mido.MidiTrack) -> list[NoteEvent]:
        """
        从MIDI轨道中提取音符事件
        
        Args:
            track: MIDI轨道
            
        Returns:
            音符事件列表
        """
        notes = []
        active_notes = {}  # {pitch: (start_tick, velocity)}
        current_tick = 0
        
        for msg in track:
            current_tick += msg.time
            
            if msg.type == 'note_on' and msg.velocity > 0:
                active_notes[msg.note] = (current_tick, msg.velocity)
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                if msg.note in active_notes:
                    start_tick, velocity = active_notes.pop(msg.note)
                    duration = current_tick - start_tick
                    if duration > 0:
                        notes.append(NoteEvent(
                            pitch=msg.note,
                            velocity=velocity,
                            start_tick=start_tick,
                            duration_ticks=duration
                        ))
        
        # 处理未关闭的音符
        for pitch, (start_tick, velocity) in active_notes.items():
            notes.append(NoteEvent(
                pitch=pitch,
                velocity=velocity,
                start_tick=start_tick,
                duration_ticks=480  # 默认1拍时值
            ))
        
        return sorted(notes, key=lambda n: n.start_tick)
    
    def compress_notes(self, notes: list[NoteEvent]) -> list[NoteEvent]:
        """
        压缩音符事件的时间
        
        Args:
            notes: 原始音符事件列表
            
        Returns:
            压缩后的音符事件列表
        """
        if not notes:
            return []
        
        compressed = []
        for note in notes:
            # 压缩开始时间
            new_start = int(note.start_tick / self.compression_ratio)
            
            # 压缩时值
            if self.preserve_duration_ratio:
                new_duration = int(note.duration_ticks / self.compression_ratio)
                new_duration = max(self.min_duration_ticks, new_duration)
            else:
                new_duration = note.duration_ticks
            
            compressed.append(NoteEvent(
                pitch=note.pitch,
                velocity=note.velocity,
                start_tick=new_start,
                duration_ticks=new_duration
            ))
        
        return compressed
    
    def notes_to_track(
        self, 
        notes: list[NoteEvent], 
        original_track: mido.MidiTrack
    ) -> mido.MidiTrack:
        """
        将音符事件转换回MIDI轨道
        
        Args:
            notes: 音符事件列表
            original_track: 原始轨道（用于保留非音符事件）
            
        Returns:
            新的MIDI轨道
        """
        new_track = mido.MidiTrack()
        
        # 收集原轨道中的非音符事件（如乐器设置、控制器等）
        for msg in original_track:
            if msg.type not in ('note_on', 'note_off'):
                new_track.append(msg.copy())
                break  # 只取第一个非音符消息（通常是轨道名或乐器设置）
        
        # 收集开头的meta和控制消息
        for msg in original_track:
            if msg.is_meta or msg.type in ('program_change', 'control_change'):
                if msg not in [m for m in new_track]:
                    new_track.append(msg.copy())
            elif msg.type in ('note_on', 'note_off'):
                break
        
        # 创建音符事件列表（note_on和note_off）
        events = []
        for note in notes:
            events.append((note.start_tick, 'note_on', note.pitch, note.velocity))
            events.append((note.start_tick + note.duration_ticks, 'note_off', note.pitch, 0))
        
        # 按时间排序
        events.sort(key=lambda e: (e[0], 0 if e[1] == 'note_off' else 1))
        
        # 转换为MIDI消息
        current_tick = 0
        for tick, msg_type, pitch, velocity in events:
            delta = tick - current_tick
            if msg_type == 'note_on':
                new_track.append(mido.Message(
                    'note_on', 
                    note=pitch, 
                    velocity=velocity, 
                    time=delta
                ))
            else:
                new_track.append(mido.Message(
                    'note_off', 
                    note=pitch, 
                    velocity=0, 
                    time=delta
                ))
            current_tick = tick
        
        # 添加轨道结束标记
        new_track.append(mido.MetaMessage('end_of_track', time=0))
        
        return new_track
    
    def compress(
        self,
        input_path: str,
        output_path: str,
        source_track: int = 0,
    ) -> dict:
        """
        压缩MIDI文件
        
        Args:
            input_path: 输入MIDI文件路径
            output_path: 输出MIDI文件路径
            source_track: 源轨道索引
            
        Returns:
            压缩统计信息
        """
        mid = mido.MidiFile(input_path)
        
        if source_track >= len(mid.tracks):
            raise ValueError(f"轨道索引 {source_track} 超出范围（共 {len(mid.tracks)} 个轨道）")
        
        track = mid.tracks[source_track]
        
        # 提取音符
        original_notes = self.extract_notes(track)
        
        if not original_notes:
            raise ValueError(f"轨道 {source_track} 中没有找到音符")
        
        # 计算原始时间范围
        original_duration = max(n.start_tick + n.duration_ticks for n in original_notes)
        
        # 压缩音符
        compressed_notes = self.compress_notes(original_notes)
        
        # 计算压缩后时间范围
        compressed_duration = max(n.start_tick + n.duration_ticks for n in compressed_notes)
        
        # 转换回轨道
        new_track = self.notes_to_track(compressed_notes, track)
        
        # 替换轨道
        mid.tracks[source_track] = new_track
        mid.save(output_path)
        
        # 计算统计信息
        ticks_per_beat = mid.ticks_per_beat
        original_beats = original_duration / ticks_per_beat
        compressed_beats = compressed_duration / ticks_per_beat
        
        stats = {
            'note_count': len(original_notes),
            'compression_ratio': self.compression_ratio,
            'original_duration_ticks': original_duration,
            'compressed_duration_ticks': compressed_duration,
            'original_beats': original_beats,
            'compressed_beats': compressed_beats,
            'ticks_per_beat': ticks_per_beat,
        }
        
        return stats


def demo():
    """演示用法"""
    print("=" * 60)
    print("MIDI Phrase Compressor 演示")
    print("=" * 60)
    
    print("\n此工具用于将MIDI乐句进行时间压缩")
    print("例如：将4小节的旋律压缩到1小节")
    print("\n压缩比例说明：")
    print("  2.0 = 2小节 -> 1小节（时间缩短一半）")
    print("  4.0 = 4小节 -> 1小节（时间缩短为1/4）")
    print("  0.5 = 1小节 -> 2小节（时间延长一倍）")
    
    print("\n示例音符压缩：")
    
    # 创建一些示例音符
    test_notes = [
        NoteEvent(pitch=60, velocity=100, start_tick=0, duration_ticks=480),      # C4 at beat 1
        NoteEvent(pitch=64, velocity=100, start_tick=480, duration_ticks=480),    # E4 at beat 2
        NoteEvent(pitch=67, velocity=100, start_tick=960, duration_ticks=480),    # G4 at beat 3
        NoteEvent(pitch=72, velocity=100, start_tick=1440, duration_ticks=480),   # C5 at beat 4
    ]
    
    print("\n原始音符（假设 480 ticks/beat）:")
    for note in test_notes:
        beat = note.start_tick / 480 + 1
        print(f"  {pitch_to_name(note.pitch)}: 第{beat:.0f}拍, 时值={note.duration_ticks}ticks")
    
    # 2倍压缩
    compressor = PhraseCompressor(compression_ratio=2.0)
    compressed = compressor.compress_notes(test_notes)
    
    print("\n2倍压缩后:")
    for note in compressed:
        beat = note.start_tick / 480 + 1
        print(f"  {pitch_to_name(note.pitch)}: 第{beat:.1f}拍, 时值={note.duration_ticks}ticks")
    
    # 4倍压缩
    compressor = PhraseCompressor(compression_ratio=4.0)
    compressed = compressor.compress_notes(test_notes)
    
    print("\n4倍压缩后:")
    for note in compressed:
        beat = note.start_tick / 480 + 1
        print(f"  {pitch_to_name(note.pitch)}: 第{beat:.2f}拍, 时值={note.duration_ticks}ticks")


def cli():
    """命令行接口"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="MIDI乐句压缩器 - 将MIDI乐句等比例时间压缩",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 将乐句压缩为原来的1/2时长（2小节变1小节）
  python phrase_compressor.py input.mid -o output.mid -r 2
  
  # 将乐句压缩为原来的1/4时长（4小节变1小节）
  python phrase_compressor.py input.mid -o output.mid -r 4
  
  # 压缩但保持原音符时值不变
  python phrase_compressor.py input.mid -o output.mid -r 2 --no-preserve-duration
  
  # 处理第2轨道
  python phrase_compressor.py input.mid -o output.mid -r 2 -t 1
  
  # 将乐句延长为原来的2倍时长
  python phrase_compressor.py input.mid -o output.mid -r 0.5
  
  # 运行演示
  python phrase_compressor.py --demo

压缩比例说明:
  比例 > 1: 压缩（时间变短）
    2.0 = 2小节 -> 1小节
    4.0 = 4小节 -> 1小节
  
  比例 < 1: 扩展（时间变长）
    0.5 = 1小节 -> 2小节
    0.25 = 1小节 -> 4小节
"""
    )
    
    parser.add_argument(
        "input",
        nargs="?",
        help="输入MIDI文件路径"
    )
    parser.add_argument(
        "-o", "--output",
        help="输出MIDI文件路径（默认: 输入文件名_compressed.mid）"
    )
    parser.add_argument(
        "-r", "--ratio",
        type=float,
        default=2.0,
        help="压缩比例，大于1压缩，小于1扩展 (默认: 2.0)"
    )
    parser.add_argument(
        "-t", "--track",
        type=int,
        default=0,
        help="要处理的轨道索引 (默认: 0)"
    )
    parser.add_argument(
        "--no-preserve-duration",
        action="store_true",
        help="不按比例压缩音符时值（保持原始时值）"
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=1,
        help="压缩后的最小音符时值（tick）(默认: 1)"
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
    
    if args.ratio <= 0:
        parser.error("压缩比例必须大于0")
    
    # 确定输出路径
    output_path = args.output
    if not output_path:
        base, ext = os.path.splitext(args.input)
        if args.ratio > 1:
            output_path = f"{base}_compressed{ext}"
        else:
            output_path = f"{base}_expanded{ext}"
    
    print(f"输入文件: {args.input}")
    print(f"输出文件: {output_path}")
    print(f"压缩比例: {args.ratio}x")
    print(f"轨道: {args.track}")
    print(f"压缩音符时值: {'否' if args.no_preserve_duration else '是'}")
    
    # 创建压缩器
    compressor = PhraseCompressor(
        compression_ratio=args.ratio,
        preserve_duration_ratio=not args.no_preserve_duration,
        min_duration_ticks=args.min_duration,
    )
    
    try:
        stats = compressor.compress(
            input_path=args.input,
            output_path=output_path,
            source_track=args.track,
        )
        
        print("\n压缩完成!")
        print(f"  总音符数: {stats['note_count']}")
        print(f"  原始时长: {stats['original_beats']:.1f} 拍 ({stats['original_duration_ticks']} ticks)")
        print(f"  压缩后时长: {stats['compressed_beats']:.1f} 拍 ({stats['compressed_duration_ticks']} ticks)")
        print(f"\n已保存到: {output_path}")
        
    except Exception as e:
        print(f"\n错误: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    cli()
