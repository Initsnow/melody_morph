"""Guitar Pro (.gp/.gpx) 解析与和弦自动标注。"""

from gpchords.parser import (
    GPBeat,
    GPChord,
    GPMeasure,
    GPNote,
    GPSong,
    GPTrack,
    GuitarProError,
    detect_format,
    parse_gp,
    select_track,
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
    "parse_gp",
    "select_track",
]
