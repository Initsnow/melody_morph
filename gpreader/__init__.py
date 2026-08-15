"""Guitar Pro (.gp / .gpx) 独立读取库。

只负责把 GPIF 文件解析成数据模型、文件层写回与 MIDI 导出；
和弦识别、调性估计等音乐分析在 gpchords 包里。
"""

from gpreader.midi import (
    detect_song_bpm,
    song_real_duration,
    song_tempos,
    song_to_midi,
)
from gpreader.parser import (
    GPBeat,
    GPChord,
    GPMeasure,
    GPNote,
    GPSong,
    GPTrack,
    GuitarProError,
    TEMPO_REFERENCE_FACTOR,
    detect_format,
    key_name,
    key_signature,
    parse_gp,
    select_track,
    select_tracks,
    tempo_effective_bpm,
)
from gpreader.reengrave import reengrave_tempo
from gpreader.tempo import (
    find_tempo_automations,
    format_tempo_value,
    parse_tempo_value,
    relabel_ref,
    relabel_tempo_value,
    rewrite_tempo_values_in_text,
)

__all__ = [
    "GPBeat",
    "GPChord",
    "GPMeasure",
    "GPNote",
    "GPSong",
    "GPTrack",
    "GuitarProError",
    "TEMPO_REFERENCE_FACTOR",
    "detect_format",
    "detect_song_bpm",
    "find_tempo_automations",
    "format_tempo_value",
    "key_name",
    "key_signature",
    "parse_gp",
    "parse_tempo_value",
    "reengrave_tempo",
    "relabel_ref",
    "relabel_tempo_value",
    "rewrite_tempo_values_in_text",
    "select_track",
    "select_tracks",
    "song_real_duration",
    "song_tempos",
    "song_to_midi",
    "tempo_effective_bpm",
]
