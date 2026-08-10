"""Guitar Pro (.gp / .gpx) 独立读取库。

只负责把 GPIF 文件解析成数据模型，并提供文件层的写回工具；
和弦识别、调性估计等音乐分析在 gpchords 包里。
"""

from gpreader.parser import (
    GPBeat,
    GPChord,
    GPMeasure,
    GPNote,
    GPSong,
    GPTrack,
    GuitarProError,
    detect_format,
    key_name,
    key_signature,
    parse_gp,
    select_track,
    select_tracks,
)

__all__ = [
    "GPBeat",
    "GPChord",
    "GPMeasure",
    "GPNote",
    "GPSong",
    "GPTrack",
    "GuitarProError",
    "detect_format",
    "key_name",
    "key_signature",
    "parse_gp",
    "select_track",
    "select_tracks",
]
