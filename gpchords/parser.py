"""
Guitar Pro (.gp / .gpx) 解析器
==============================

支持 Guitar Pro 7/8 的 ``.gp`` 格式（以及结构相同的 Guitar Pro 6 ``.gpx``）。
这类文件本质上是 zip 压缩包，乐谱数据保存在 ``Content/score.gpif``（XML）。

GP3 / GP4 / GP5 是另一种二进制格式，不在本模块支持范围内。旧格式请使用
PyGuitarPro（``uv add PyGuitarPro``），本模块检测到旧格式时会给出提示。

作为库使用::

    from gpchords import parse_gp
    song = parse_gp("xxx.gp")
    for track in song.tracks:
        print(track.name, len(track.notes))
"""

from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class GuitarProError(Exception):
    """Guitar Pro 文件解析错误。"""


# 音符时值名称 -> 四分音符数
# GPIF 实际写法不统一：GP6/7/8 常见 "Eighth"/"16th"，部分文件也用
# "Sixteenth"/"ThirtySecond" 等描述式写法，这里两种都收。
NOTE_VALUE_QUARTERS = {
    "Long": 16.0,
    "DoubleWhole": 8.0,
    "Whole": 4.0,
    "Half": 2.0,
    "Quarter": 1.0,
    "Eighth": 0.5,
    "8th": 0.5,
    "Sixteenth": 0.25,
    "16th": 0.25,
    "ThirtySecond": 0.125,
    "32nd": 0.125,
    "SixtyFourth": 0.0625,
    "64th": 0.0625,
    "HundredTwentyEighth": 0.03125,
    "128th": 0.03125,
    "TwoHundredFiftySixth": 0.015625,
    "256th": 0.015625,
}

# GPIF 中升降号的写法 -> 常见记法
# 和弦元素用属性写法（accidental="Sharp"），音符 Pitch 里有的文件用
# 元素文本写法（<Accidental>#</Accidental>），两种都收。
ACCIDENTAL_SYMBOL = {
    "": "",
    "Natural": "",
    "Sharp": "#",
    "#": "#",
    "Flat": "b",
    "b": "b",
    "DoubleSharp": "##",
    "##": "##",
    "DoubleFlat": "bb",
    "bb": "bb",
}

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# 五度圈：升号数(0-7) -> 大调根音；降号数 -> 大调根音
_CIRCLE_SHARP = [0, 7, 2, 9, 4, 11, 6, 1]
_CIRCLE_FLAT = [0, 5, 10, 3, 8, 1, 6]
_SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


@dataclass
class GPChord:
    """轨道和弦库（DiagramCollection）中的一项，对应 Guitar Pro 的和弦图。"""

    index: int
    name: str = ""
    key_note: str = ""  # 根音，如 "C"
    bass_note: str = ""  # 低音，如 "F"
    degrees: list[tuple[str, str, bool]] = field(default_factory=list)
    diagram_frets: dict[int, int] = field(default_factory=dict)  # 弦号(1=高音弦) -> 品
    base_fret: int = 0


@dataclass
class GPNote:
    id: str = ""
    midi: int = 0
    pitch_name: str = ""  # 如 "F4"
    octave: Optional[int] = None
    fret: Optional[int] = None
    string: Optional[int] = None  # 1 = 最高音弦
    duration_quarters: float = 0.0  # 以四分音符为单位的时值
    tie_origin: bool = False
    tie_destination: bool = False

    @property
    def pitch_class(self) -> int:
        return self.midi % 12


@dataclass
class GPBeat:
    id: str = ""
    start_quarters: float = 0.0  # 小节内起始位置（四分音符）
    duration_quarters: float = 0.0
    is_rest: bool = False
    chord: Optional[GPChord] = None  # 该拍挂的和弦标注
    notes: list[GPNote] = field(default_factory=list)
    voice_id: str = ""  # 所属声部（GPIF 里 beat 对象可被多处引用，用于定位/克隆）
    position_in_voice: int = -1  # 在声部 Beats 序列中的位置


@dataclass
class GPMeasure:
    index: int  # 从 1 开始
    time_signature: Optional[tuple[int, int]] = None
    key_signature: Optional[str] = None  # 如 "C" / "Am"
    section: Optional[str] = None  # 如 "A:Intro 1"
    beats: list[GPBeat] = field(default_factory=list)


@dataclass
class GPTrack:
    id: int
    name: str = ""
    short_name: str = ""
    program: str = ""  # 音色名，如 "Distortion Guitar"
    midi_program: Optional[int] = None
    tuning: list[int] = field(default_factory=list)  # 空弦 MIDI，低 -> 高
    chords: list[GPChord] = field(default_factory=list)  # 轨道和弦库
    measures: list[GPMeasure] = field(default_factory=list)
    notes: list[GPNote] = field(default_factory=list)  # 按小节/拍展开


@dataclass
class GPSong:
    gp_version: str = ""
    title: str = ""
    subtitle: str = ""
    artist: str = ""
    album: str = ""
    tracks: list[GPTrack] = field(default_factory=list)


def detect_format(path: str | Path) -> tuple[str, str]:
    """
    识别 Guitar Pro 文件格式。

    返回 ``(格式, 版本)``，格式为 ``gp3``/``gp4``/``gp5``/``gp``/``gpx``/
    ``zip``/``unknown`` 之一。
    """
    p = Path(path)
    if not p.exists():
        raise GuitarProError(f"文件不存在: {p}")
    with p.open("rb") as f:
        head = f.read(24)
    if head.startswith(b"FICHIER GUITAR PRO v"):
        version = head[len(b"FICHIER GUITAR PRO v"):].decode("ascii", "replace").strip()
        return "gp" + version[:1], version
    if head.startswith(b"BCFZ"):
        # GP4/5 时代的部分二进制文件用 BCFZ 魔数开头（含一些误命名为 .gpx 的文件）
        return "gp5", ""
    if not head.startswith(b"PK\x03\x04"):
        return "unknown", ""
    try:
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
            if "Content/score.gpif" not in names and "score.gpif" not in names:
                return "zip", ""
            version = ""
            if "VERSION" in names:
                version = z.read("VERSION").decode("utf-8", "replace").strip()
            return ("gp", version) if version else ("gpx", "")
    except zipfile.BadZipFile:
        return "zip", ""


def select_track(song: "GPSong", selector: str) -> Optional["GPTrack"]:
    """按索引或名称（大小写不敏感、子串匹配）选择轨道，找不到时抛错。"""
    if selector.strip().isdigit():
        idx = int(selector.strip())
        if 0 <= idx < len(song.tracks):
            return song.tracks[idx]
        raise GuitarProError(f"轨道索引 {idx} 超出范围（共 {len(song.tracks)} 条轨道）")
    lowered = selector.strip().lower()
    for track in song.tracks:
        if track.name.lower() == lowered or track.short_name.lower() == lowered:
            return track
    for track in song.tracks:
        if lowered in track.name.lower() or lowered in track.short_name.lower():
            return track
    raise GuitarProError(
        f"找不到轨道 {selector!r}。可用轨道: {[t.name for t in song.tracks]}"
    )


def select_tracks(song: "GPSong", selectors: "str | list[str]") -> list["GPTrack"]:
    """
    按索引/名称选择多个轨道（顺序保留、去重）。

    - 单个 selector 可逗号分隔（``--track 0,2``、``--track "Lead,Rhythm"``）；
    - 关键字 ``all`` 选择所有有音符且非鼓组（MIDI Program 0）的轨道，
      避免打击乐污染和弦识别；
    - 空选择抛 :class:`GuitarProError`。
    """
    if isinstance(selectors, str):
        selectors = [selectors]
    tracks: list[GPTrack] = []
    seen: set[int] = set()
    for selector in selectors:
        for part in selector.split(","):
            part = part.strip()
            if not part:
                continue
            if part.lower() == "all":
                for t in song.tracks:
                    if not t.notes or t.midi_program == 0:
                        continue
                    if t.id not in seen:
                        seen.add(t.id)
                        tracks.append(t)
                continue
            t = select_track(song, part)
            if t.id not in seen:
                seen.add(t.id)
                tracks.append(t)
    if not tracks:
        raise GuitarProError("没有选择任何轨道")
    return tracks


def parse_gp(path: str | Path) -> GPSong:
    """解析 Guitar Pro 文件并返回 :class:`GPSong`。"""
    fmt, version = detect_format(path)
    if fmt in ("gp3", "gp4", "gp5"):
        raise GuitarProError(
            f"检测到旧版 Guitar Pro {fmt.upper()} 二进制格式（版本 {version}）。\n"
            "本模块只支持 GP6 (.gpx) / GP7 / GP8 (.gp)。旧格式请安装 PyGuitarPro：\n"
            "    uv add PyGuitarPro\n"
            "    from guitarpro import parse; song = parse('xxx.gp5')"
        )
    if fmt not in ("gp", "gpx"):
        raise GuitarProError(f"不是可识别的 Guitar Pro 文件（检测到格式: {fmt!r}）。")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        gpif_name = "Content/score.gpif" if "Content/score.gpif" in names else "score.gpif"
        xml_bytes = z.read(gpif_name)
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise GuitarProError(f"score.gpif XML 解析失败: {e}") from e
    if root.tag != "GPIF":
        raise GuitarProError(f"score.gpif 根元素异常: {root.tag!r}")
    return _parse_gpif(root, version)


def _parse_gpif(root: ET.Element, version: str) -> GPSong:
    # GPVersion（XML 内）是实际保存程序的版本，VERSION 文件只是格式类别
    song = GPSong(gp_version=(root.findtext("GPVersion") or version).strip())

    score = root.find("Score")
    if score is not None:
        song.title = (score.findtext("Title") or "").strip()
        song.subtitle = (score.findtext("SubTitle") or "").strip()
        song.artist = (score.findtext("Artist") or "").strip()
        song.album = (score.findtext("Album") or "").strip()

    bars = {b.get("id"): b for b in root.findall("Bars/Bar")}
    voices = {v.get("id"): v for v in root.findall("Voices/Voice")}
    beats = {b.get("id"): b for b in root.findall("Beats/Beat")}
    notes = {n.get("id"): n for n in root.findall("Notes/Note")}
    rhythms = {r.get("id"): r for r in root.findall("Rhythms/Rhythm")}
    master_bars = root.findall("MasterBars/MasterBar")

    track_els = root.findall("Tracks/Track")
    track_order = [t.get("id", str(i)) for i, t in enumerate(track_els)]

    # MasterBar.<Bars> 中的顺序即轨道顺序：第 i 个 bar id 属于第 i 个轨道
    track_measure_bars: dict[str, list[tuple[int, str]]] = {tid: [] for tid in track_order}
    for mbi, mb in enumerate(master_bars):
        ids = (mb.findtext("Bars") or "").split()
        for pos, bid in enumerate(ids):
            if pos < len(track_order):
                track_measure_bars[track_order[pos]].append((mbi, bid))

    for tel in track_els:
        tid = tel.get("id", "0")
        track = GPTrack(
            id=int(tid) if tid.isdigit() else len(song.tracks),
            name=(tel.findtext("Name") or "").strip(),
            short_name=(tel.findtext("ShortName") or "").strip(),
        )
        sound_name = tel.findtext("Sounds/Sound/Name")
        track.program = (sound_name or "").strip()
        midi_program = tel.findtext("Sounds/Sound/MIDI/Program")
        track.midi_program = int(midi_program) if midi_program and midi_program.strip().isdigit() else None

        staff = tel.find("Staves/Staff")
        if staff is not None:
            staff_props = staff.find("Properties")
            for prop in list(staff_props) if staff_props is not None else []:
                prop_name = prop.get("name")
                if prop_name == "Tuning":
                    pitches = prop.findtext("Pitches")
                    if pitches:
                        track.tuning = [int(x) for x in pitches.split()]
                elif prop_name in ("DiagramCollection", "ChordCollection"):
                    parsed = _parse_chord_collection(prop)
                    if parsed or not track.chords:
                        track.chords = parsed

        for mbi, bid in track_measure_bars.get(tid, []):
            bar = bars.get(bid)
            if bar is None:
                continue
            mb = master_bars[mbi] if mbi < len(master_bars) else None
            measure = GPMeasure(
                index=mbi + 1,
                time_signature=_parse_time(mb),
                key_signature=_key_name(mb),
                section=_section_name(mb),
            )
            beats_in: list[GPBeat] = []
            for vid in (bar.findtext("Voices") or "").split():
                if vid == "-1":
                    continue
                voice = voices.get(vid)
                if voice is None:
                    continue
                tick = 0.0
                voice_pos = 0
                for bid_ in (voice.findtext("Beats") or "").split():
                    if bid_ == "-1":  # 占位休止
                        voice_pos += 1
                        continue
                    beat_el = beats.get(bid_)
                    if beat_el is None:
                        voice_pos += 1
                        continue
                    dur = _beat_duration(beat_el, rhythms)
                    beat = GPBeat(
                        id=bid_,
                        start_quarters=tick,
                        duration_quarters=dur,
                        voice_id=vid,
                        position_in_voice=voice_pos,
                    )
                    tick += dur

                    chord_ref = beat_el.findtext("Chord")
                    if chord_ref and chord_ref.strip().isdigit():
                        idx = int(chord_ref.strip())
                        if 0 <= idx < len(track.chords):
                            beat.chord = track.chords[idx]

                    note_ids = (beat_el.findtext("Notes") or "").split()
                    beat.is_rest = not note_ids or all(nid == "-1" for nid in note_ids)
                    for nid in note_ids:
                        if nid == "-1":
                            continue
                        note_el = notes.get(nid)
                        if note_el is None:
                            continue
                        note = _parse_note(note_el)
                        note.duration_quarters = dur
                        beat.notes.append(note)
                    beats_in.append(beat)
                    voice_pos += 1
            beats_in.sort(key=lambda b: b.start_quarters)
            measure.beats = beats_in
            track.measures.append(measure)

        for m in track.measures:
            for b in m.beats:
                track.notes.extend(b.notes)
        song.tracks.append(track)

    return song


def _parse_note(note_el: ET.Element) -> GPNote:
    note = GPNote(id=note_el.get("id", ""))
    note_props = note_el.find("Properties")
    for prop in list(note_props) if note_props is not None else []:
        name = prop.get("name")
        if name == "Midi":
            number = prop.findtext("Number")
            if number and number.strip().isdigit():
                note.midi = int(number.strip())
        elif name == "ConcertPitch":
            pitch = prop.find("Pitch")
            if pitch is not None:
                step = pitch.findtext("Step") or ""
                acc = ACCIDENTAL_SYMBOL.get(pitch.findtext("Accidental") or "", "")
                octv = pitch.findtext("Octave")
                note.pitch_name = f"{step}{acc}{octv or ''}"
                note.octave = int(octv) if octv and octv.strip().isdigit() else None
        elif name == "Fret":
            fret = prop.findtext("Fret")
            note.fret = int(fret) if fret and fret.strip().isdigit() else None
        elif name == "String":
            string = prop.findtext("String")
            note.string = int(string) if string and string.strip().isdigit() else None
    tie = note_el.find("Tie")
    if tie is not None:
        note.tie_origin = tie.get("origin") == "true"
        note.tie_destination = tie.get("destination") == "true"
    return note


def _beat_duration(beat_el: ET.Element, rhythms: dict[str, ET.Element]) -> float:
    rhythm_el = beat_el.find("Rhythm")
    if rhythm_el is None:
        return 0.0
    rhythm = rhythms.get(rhythm_el.get("ref", ""))
    if rhythm is None:
        return 0.0
    dur = NOTE_VALUE_QUARTERS.get(rhythm.findtext("NoteValue") or "", 0.0)
    dot = rhythm.find("AugmentationDot")
    if dot is not None:
        count = int(dot.get("count", "1") or "1")
        dur *= 1.5**count
    tuplet = rhythm.find("PrimaryTuplet")
    if tuplet is not None:
        num = int(tuplet.findtext("Num") or "1")
        den = int(tuplet.findtext("Den") or "1")
        dur *= den / num
    return dur


def _parse_chord_collection(prop: ET.Element) -> list[GPChord]:
    chords: list[GPChord] = []
    items = prop.find("Items")
    if items is None:
        return chords
    for item in items:
        if item.tag != "Item":
            continue
        chord = GPChord(
            index=int(item.get("id", str(len(chords))) or len(chords)),
            name=(item.get("name") or "").strip(),
        )
        chord_el = item.find("Chord")
        if chord_el is not None:
            for tag, attr in (("KeyNote", "key_note"), ("BassNote", "bass_note")):
                el = chord_el.find(tag)
                if el is not None:
                    step = el.get("step") or ""
                    acc = ACCIDENTAL_SYMBOL.get(el.get("accidental") or "", "")
                    setattr(chord, attr, f"{step}{acc}")
            for degree in chord_el.findall("Degree"):
                chord.degrees.append(
                    (
                        degree.get("interval") or "",
                        degree.get("alteration") or "",
                        degree.get("omitted") == "true",
                    )
                )
        diagram = item.find("Diagram")
        if diagram is not None:
            base = diagram.get("baseFret") or "0"
            chord.base_fret = int(base) if base.isdigit() else 0
            for fret in diagram.findall("Fret"):
                string = fret.get("string")
                value = fret.get("fret")
                if string and string.isdigit() and value and value.isdigit():
                    chord.diagram_frets[int(string)] = int(value)
        chords.append(chord)
    return chords


def _parse_time(mb: Optional[ET.Element]) -> Optional[tuple[int, int]]:
    if mb is None:
        return None
    text = (mb.findtext("Time") or "").strip()
    if "/" in text:
        num, _, den = text.partition("/")
        if num.isdigit() and den.isdigit():
            return int(num), int(den)
    return None


def _key_name(mb: Optional[ET.Element]) -> Optional[str]:
    """根据调号推算出调名，如 "C" / "Am" / "Bb" / "F#m"。"""
    if mb is None:
        return None
    key = mb.find("Key")
    if key is None:
        return None
    count_text = key.findtext("AccidentalCount")
    if count_text is None:
        return None
    count = int(count_text) if count_text.strip().lstrip("-").isdigit() else 0
    mode = (key.findtext("Mode") or "Major").strip()
    if count >= 0:
        major_pc = _CIRCLE_SHARP[count] if count < len(_CIRCLE_SHARP) else _CIRCLE_SHARP[-1]
    else:
        flat_idx = min(-count, len(_CIRCLE_FLAT) - 1)
        major_pc = _CIRCLE_FLAT[flat_idx]
    pc = (major_pc + 9) % 12 if mode == "Minor" else major_pc
    names = _FLAT_NAMES if count < 0 else _SHARP_NAMES
    return names[pc] + ("m" if mode == "Minor" else "")


def _section_name(mb: Optional[ET.Element]) -> Optional[str]:
    if mb is None:
        return None
    section = mb.find("Section")
    if section is None:
        return None
    letter = (section.findtext("Letter") or "").strip()
    text = (section.findtext("Text") or "").strip()
    if not letter and not text:
        return None
    return f"{letter}:{text}" if letter and text else (letter or text)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="查看 Guitar Pro (.gp/.gpx) 文件概览")
    parser.add_argument("file", help=".gp / .gpx 文件路径")
    args = parser.parse_args()

    fmt, version = detect_format(args.file)
    song = parse_gp(args.file)
    print(f"格式: {fmt}  版本: {song.gp_version or version}")
    print(f"标题: {song.title or '(无)'}  艺术家: {song.artist or '(无)'}")
    print(f"轨道数: {len(song.tracks)}")
    for track in song.tracks:
        chord_names = ", ".join(c.name for c in track.chords) or "(无)"
        print(
            f"  [{track.id}] {track.name} | 音色: {track.program or '(未设置)'} | "
            f"小节: {len(track.measures)} | 音符: {len(track.notes)} | 和弦库: {chord_names}"
        )


if __name__ == "__main__":
    main()
