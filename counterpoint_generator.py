import argparse
import mido
import sys
import random
from dataclasses import dataclass
from typing import List, Tuple, Optional

# Color codes for terminal output
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"

NOTES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
NOTE_TO_INT = {n: i for i, n in enumerate(NOTES)}
MODES = {
    'major': [0, 2, 4, 5, 7, 9, 11],
    'minor': [0, 2, 3, 5, 7, 8, 10],  # Natural minor
    'ionian': [0, 2, 4, 5, 7, 9, 11],
    'dorian': [0, 2, 3, 5, 7, 9, 10],
    'phrygian': [0, 1, 3, 5, 7, 8, 10],
    'lydian': [0, 2, 4, 6, 7, 9, 11],
    'mixolydian': [0, 2, 4, 5, 7, 9, 10],
    'aeolian': [0, 2, 3, 5, 7, 8, 10],
    'locrian': [0, 1, 3, 5, 6, 8, 10],
}

@dataclass
class Context:
    root: str
    mode: str
    time_sig_num: int
    time_sig_den: int
    scale_obj: 'Scale' = None

class Scale:
    def __init__(self, root: str, mode: str, custom_intervals: Optional[List[int]] = None):
        self.root = root
        self.mode = mode.lower()
        self.root_val = NOTE_TO_INT.get(root.upper(), 0) # Simplify, handle enharmonics later if needed
        
        if custom_intervals:
            self.intervals = custom_intervals
        else:
            self.intervals = MODES.get(self.mode, MODES['major'])
            
        self.scale_notes = [(self.root_val + interval) % 12 for interval in self.intervals]

    def is_diatonic(self, note: int) -> bool:
        return (note % 12) in self.scale_notes

    def get_diatonic_candidate(self, ref_note: int, interval_index: int) -> int:
        """
        Returns a note that is `interval_index` scale steps away from ref_note.
        e.g. interval_index = 2 means a 'third' above (2 steps).
        """
        # Find position of ref_note in scale (or nearest lower)
        ref_pitch_class = ref_note % 12
        octave = ref_note // 12
        
        try:
            current_idx = self.scale_notes.index(ref_pitch_class)
        except ValueError:
            # Chromatic note: Find nearest scale note below
            sorted_scale = sorted(self.scale_notes)
            current_idx = 0
            for i, n in enumerate(sorted_scale):
                if n <= ref_pitch_class:
                    current_idx = i
                else:
                    break
        
        target_idx_raw = current_idx + interval_index # 0-based step index
        degree_count = len(self.scale_notes)
        
        octave_shift = target_idx_raw // degree_count
        final_idx = target_idx_raw % degree_count
        
        final_pitch_class = sorted(self.scale_notes)[final_idx]
        return (octave + octave_shift) * 12 + final_pitch_class

class KeyDetector:
    def __init__(self):
        # Krumhansl-Schmuckler Profiles
        self.MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
        self.MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

    def correlation(self, profile, duration_profile):
        mean_p = sum(profile) / 12
        mean_d = sum(duration_profile) / 12
        numerator = sum((profile[i] - mean_p) * (duration_profile[i] - mean_d) for i in range(12))
        denominator = (sum((profile[i] - mean_p)**2 for i in range(12)) * 
                       sum((duration_profile[i] - mean_d)**2 for i in range(12))) ** 0.5
        return numerator / denominator if denominator != 0 else 0

    def detect_key(self, notes: List[Tuple[int, int, int]]) -> Tuple[str, str]:
        """
        notes: list of (pitch, velocity, duration)
        Returns: (root_name, mode_name)
        """
        duration_profile = [0.0] * 12
        total_duration = 0
        for p, v, d in notes:
            duration_profile[p % 12] += d
            total_duration += d
            
        if total_duration == 0: return ("C", "major")
        
        best_corr = -2.0
        best_key = ("C", "major")
        
        # Check all 12 roots for Major and Minor
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

    def segmentation_analysis(self, mid: mido.MidiFile, ticks_per_segment=None) -> List[Tuple[int, str, str]]:
        """
        Analyzes the MIDI file in segments to detect key changes.
        Returns list of (tick, root, mode).
        """
        if ticks_per_segment is None:
            ticks_per_segment = mid.ticks_per_beat * 4 # Every 1 bar (4/4 assumed) for finer resolution
            
        # Collect all notes from all tracks with simple duration estimation
        sorted_notes = []
        for track in mid.tracks:
            abs_time = 0
            active = {}
            for msg in track:
                abs_time += msg.time
                if msg.type == 'note_on' and msg.velocity > 0:
                    active[msg.note] = (abs_time, msg.velocity)
                elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                    if msg.note in active:
                        s, v = active.pop(msg.note)
                        sorted_notes.append((s, msg.note, v, abs_time - s)) # (start, pitch, vel, duration)
        
        sorted_notes.sort(key=lambda x: x[0])
        if not sorted_notes: return []
        
        max_time = sorted_notes[-1][0] + sorted_notes[-1][3]
        
        changes = []
        last_key = None
        
        # Sliding window? Or separate segments?
        # Segments are easier.
        
        for t in range(0, max_time, ticks_per_segment):
            segment_end = t + ticks_per_segment
            segment_notes = [(p, v, d) for s, p, v, d in sorted_notes if s >= t and s < segment_end]
            
            # If segment is empty, stretch previous key? or ignore?
            if not segment_notes:
                continue
            
            root, mode = self.detect_key(segment_notes)
            
            # Simple smoothing: Only change if different from last
            if (root, mode) != last_key:
                changes.append((t, root, mode))
                last_key = (root, mode)
                
        return changes

# ... (ContextTracker remains same) ...

    def _save_output(self, notes):
        print(f"{GREEN}Saving output to {self.args.output_file}{RESET}")
        output_mid = mido.MidiFile(ticks_per_beat=self.mid.ticks_per_beat)
        
        track = mido.MidiTrack()
        output_mid.tracks.append(track)
        
        events = []
        # Add Notes
        for start, duration, pitch, velocity in notes:
            events.append((start, 'note_on', pitch, velocity))
            events.append((start + duration, 'note_off', pitch, 0))
            
        # Add Key Signatures from Context Tracker
        # We need to reconstruct the key changes from context_map or re-analysis?
        # ContextTracker has `context_map` which stores exact change points.
        
        for tick in self.context_tracker.sorted_ticks:
            ctx = self.context_tracker.context_map[tick]
            # Convert context to mido KeySignature
            # root is 'C', mode is 'major'. Mido wants 'C' or 'Cm'.
            key_str = ctx.root
            if ctx.mode == 'minor':
                key_str += 'm'
            
            # Create meta message
            # We add it as an event to be sorted.
            # Meta messages have type='key_signature', key='...'
            # Note: ContextTracker stores *derived* contexts. 
            # If we just dump every single context change (which matches every detected segment change), 
            # we might have A LOT of key changes.
            # But that's what the user asked for: "Show me the keys".
            # We can filter consecutive duplicates if needed, but ContextMap logic already does sparse updates usually?
            # Actually ContextMap is populated by `analyze_midi` which iterates events/segments.
            # So it should be sparse enough.
            
            events.append((tick, 'meta_key', key_str))

        # Sort all events
        # Priority at same tick: Meta First, Note Off, Note On?
        # Usually: Meta -> Note Off -> Note On
        def sort_key(x):
            tick = x[0]
            type_score = 0
            if x[1] == 'meta_key': type_score = 0
            elif x[1] == 'note_off': type_score = 1
            elif x[1] == 'note_on': type_score = 2
            return (tick, type_score)

        events.sort(key=sort_key)
        
        last_tick = 0
        for item in events:
            tick = item[0]
            delta = int(max(0, tick - last_tick))
            
            if item[1] == 'meta_key':
                track.append(mido.MetaMessage('key_signature', key=item[2], time=delta))
            else:
                track.append(mido.Message(item[1], note=item[2], velocity=item[3], time=delta))
                
            last_tick = tick
            
        output_mid.save(self.args.output_file)

class ContextTracker:
    def __init__(self, key_root_override=None, key_mode_override=None, custom_scale=None):
        self.key_root_override = key_root_override
        self.key_mode_override = key_mode_override
        self.custom_scale = custom_scale
        self.context_map = {} 
        self.default_context = Context("C", "major", 4, 4)
        self.sorted_ticks = []
        self.key_detector = KeyDetector()

    def analyze_midi(self, mid: mido.MidiFile):
        events = []
        for track in mid.tracks:
            abs_time = 0
            for msg in track:
                abs_time += msg.time
                if msg.type in ['key_signature', 'time_signature']:
                    events.append((abs_time, msg))
        
        events.sort(key=lambda x: x[0])
        
        # Always run smart detection to capture modulation, unless override provided
        # We value dynamic detection over static metadata often found in MIDI
        # But we still respect Time Signatures from metadata.
        
        detected_keys = []
        print(f"{GREEN}Analyzing harmonic content (Smart Key Detection)...{RESET}")
        detected_keys = self.key_detector.segmentation_analysis(mid)
        # Suppressed verbose output of all keys
        if detected_keys:
             print(f"  Detected {len(detected_keys)} local key changes.")

        # If no events and no detection, default
        if not events and not detected_keys:
            self.context_map[0] = self._create_context("C", "major", 4, 4)
            self.sorted_ticks = [0]
            print(f"{YELLOW}Defaulting to C Major, 4/4.{RESET}")
            return
            
        timeline_points = set()
        for t, _ in events: timeline_points.add(t)
        for t, _, _ in detected_keys: timeline_points.add(t)
        
        sorted_times = sorted(list(timeline_points))
        
        evt_idx = 0
        
        curr_ts_num = 4
        curr_ts_den = 4
        
        # Determine initial Key State
        if detected_keys:
            curr_root, curr_mode = detected_keys[0][1], detected_keys[0][2]
        else:
            curr_root, curr_mode = "C", "major"
            
        # Priority: Detection (for Pitch) > Metadata (for Time)
        # Metadata *Key* signatures are largely ignored in favor of Smart Detection 
        # unless Detection failed (empty) and Metadata exists.
        
        use_detection = bool(detected_keys)
        
        # If we have NO detection but we DO have metadata, use metadata
        if not use_detection and any(e[1].type == 'key_signature' for e in events):
             use_detection = False # Redundant but explicit
        elif use_detection:
             # If we have detection, we ignore metadata keys
             pass

        for t in sorted_times:
            # 1. Process Metadata Events up to this time
            while evt_idx < len(events) and events[evt_idx][0] <= t:
                msg = events[evt_idx][1]
                if msg.type == 'time_signature':
                     curr_ts_num = msg.numerator
                     curr_ts_den = msg.denominator
                     print(f"  [Tick {t}] Time Sig: {curr_ts_num}/{curr_ts_den}")
                elif msg.type == 'key_signature' and not use_detection:
                    k = msg.key
                    if k.endswith('m'):
                        curr_root = k[:-1]
                        curr_mode = 'minor'
                    else:
                        curr_root = k
                        curr_mode = 'major'
                    print(f"  [Tick {t}] Metadata Key: {curr_root} {curr_mode}")
                evt_idx += 1
            
            # 2. Process Detected Key at this exact time (if any)
            if use_detection:
                dk = next((k for k in detected_keys if k[0] == t), None)
                if dk:
                    curr_root, curr_mode = dk[1], dk[2]

            self.context_map[t] = self._create_context(curr_root, curr_mode, curr_ts_num, curr_ts_den)
            
        self.sorted_ticks = sorted(self.context_map.keys())

    def _create_context(self, root, mode, ts_num, ts_den):
        final_root = self.key_root_override if self.key_root_override else root
        final_mode = self.key_mode_override if self.key_mode_override else mode
        
        custom_intervals = None
        if self.custom_scale:
            try:
                custom_intervals = [int(x) for x in self.custom_scale.split()]
            except ValueError:
                print(f"{YELLOW}Warning: Invalid custom scale format. Using mode {final_mode}.{RESET}")
        
        scale = Scale(final_root, final_mode, custom_intervals)
        return Context(final_root, final_mode, ts_num, ts_den, scale_obj=scale)

    def get_context(self, tick: int) -> Context:
        if not self.sorted_ticks:
            return self._create_context("C", "major", 4, 4)
        
        import bisect
        idx = bisect.bisect_right(self.sorted_ticks, tick)
        if idx == 0:
            return self.context_map[self.sorted_ticks[0]]
        return self.context_map[self.sorted_ticks[idx-1]]

class VoiceLeading:
    def __init__(self):
        self.PERFECT_CONSONANCES = {0, 7, 12, 19, 24, 31, 36}
        self.IMPERFECT_CONSONANCES = {3, 4, 8, 9, 15, 16, 20, 21, 27, 28}
        self.DISSONANCES = {1, 2, 5, 6, 10, 11, 13, 14, 17, 18, 22, 23}
        
    def is_consonant(self, interval: int) -> bool:
        iv = abs(interval)
        return (iv in self.PERFECT_CONSONANCES) or (iv in self.IMPERFECT_CONSONANCES)

    def is_perfect_consonance(self, interval: int) -> bool:
        return abs(interval) in self.PERFECT_CONSONANCES

    def get_motion(self, p1: int, n1: int, p2: int, n2: int) -> str:
        if p1 == n1 and p2 == n2: return "static"
        if p1 == n1 or p2 == n2: return "oblique"
        
        dir1 = 1 if n1 > p1 else -1
        dir2 = 1 if n2 > p2 else -1
        
        if dir1 != dir2: return "contrary"
        
        iv1 = n1 - p1
        iv2 = n2 - p2
        return "parallel" if iv1 == iv2 else "similar"

    def evaluate(self, cf_note: int, cp_note: int, 
                 prev_cf: Optional[int], prev_cp: Optional[int], 
                 context: Context, is_strong_beat: bool = True, species: int = 1) -> float:
        interval = cp_note - cf_note
        abs_interval = abs(interval)
        
        if species == 1 or (species > 1 and is_strong_beat):
            if not self.is_consonant(abs_interval):
                return -100.0 
        
        if prev_cf is not None and prev_cp is not None:
            motion = self.get_motion(prev_cf, cf_note, prev_cp, cp_note)
            
            if motion == "parallel" and self.is_perfect_consonance(abs_interval):
                if abs_interval % 12 in {0, 7}: 
                   return -100.0
            
            if motion == "similar" and self.is_perfect_consonance(abs_interval):
                return -50.0

            if motion == "contrary":
                score = 10.0
            elif motion == "oblique":
                score = 5.0
            elif motion == "parallel":
                if abs_interval in self.IMPERFECT_CONSONANCES:
                    score = 8.0
                else: 
                    score = -5.0
            else: 
                score = 2.0
        else:
            score = 10.0 if self.is_perfect_consonance(abs_interval) else 5.0

        if self.is_perfect_consonance(abs_interval) and prev_cf is not None:
            score -= 2.0 
        if abs_interval in self.IMPERFECT_CONSONANCES:
            score += 2.0
            
        if prev_cp is not None:
            leap = abs(cp_note - prev_cp)
            if leap > 12: score -= 20.0 
            if leap > 2 and leap != 12: score -= leap * 0.5 
            if leap > 0 and leap <= 2: score += 5.0 
            
        return score

class CounterpointGenerator:
    def __init__(self, mid: mido.MidiFile, args):
        self.mid = mid
        self.args = args
        self.context_tracker = ContextTracker(args.root, args.mode, args.custom_scale)
        self.voice_leading = VoiceLeading()

    def run(self):
        print(f"{GREEN}Analyzing MIDI structure...{RESET}")
        self.context_tracker.analyze_midi(self.mid)
        
        print(f"{GREEN}Extracting Cantus Firmus from track {self.args.cantus_firmus_track}...{RESET}")
        cantus_firmus_raw = self._extract_cantus_firmus()
        if not cantus_firmus_raw:
            print(f"{YELLOW}Warning: No notes found in Track {self.args.cantus_firmus_track}.{RESET}")
            print(f"{GREEN}Scanning other tracks for notes...{RESET}")
            
            # Scan all tracks
            for i, track in enumerate(self.mid.tracks):
                note_count = sum(1 for msg in track if msg.type == 'note_on' and msg.velocity > 0)
                if note_count > 0:
                    print(f"  - Track {i}: {track.name.strip() or '(No Name)'} | {note_count} notes")
            
            print(f"{YELLOW}Please rerun with --cantus_firmus_track <TRACK_ID>{RESET}")
            
            # Optional: Auto-select first valid track if user used default 0
            if self.args.cantus_firmus_track == 0:
                 for i, track in enumerate(self.mid.tracks):
                    val_notes = [msg for msg in track if msg.type == 'note_on' and msg.velocity > 0]
                    if val_notes:
                        print(f"{GREEN}Auto-selecting Track {i} as Cantus Firmus...{RESET}")
                        self.args.cantus_firmus_track = i
                        cantus_firmus_raw = self._extract_cantus_firmus()
                        break
            
            if not cantus_firmus_raw:
                return
            
        cantus_firmus = self._flatten_polyphony(cantus_firmus_raw)
        print(f"{GREEN}Detected {len(cantus_firmus)} notes in melody line.{RESET}")

        print(f"{GREEN}Generating Species {self.args.species} Counterpoint...{RESET}")
        counterpoint_notes = self._generate(cantus_firmus)
        
        self._save_output(counterpoint_notes)

    def _extract_cantus_firmus(self) -> List[Tuple[int, int, int, int]]:
        track_idx = self.args.cantus_firmus_track
        if track_idx >= len(self.mid.tracks):
             print(f"{RED}Error: Track index {track_idx} out of range.{RESET}")
             return []
        
        track = self.mid.tracks[track_idx]
        abs_time = 0
        finished_notes = []
        active_notes = {}
        
        for msg in track:
            abs_time += msg.time
            if msg.type == 'note_on' and msg.velocity > 0:
                active_notes[msg.note] = (abs_time, msg.velocity)
            elif (msg.type == 'note_off') or (msg.type == 'note_on' and msg.velocity == 0):
                if msg.note in active_notes:
                    start_time, velocity = active_notes.pop(msg.note)
                    finished_notes.append((start_time, abs_time, msg.note, velocity))
        
        finished_notes.sort(key=lambda x: x[0])
        return finished_notes

    def _flatten_polyphony(self, notes: List[Tuple[int, int, int, int]]) -> List[Tuple[int, int, int, int]]:
        if not notes:
            return []
            
        grouped_notes = []
        current_group = []
        group_start = -1
        TOLERANCE = self.mid.ticks_per_beat / 8
        
        for n in notes:
            start, end, pitch, vel = n
            if group_start == -1:
                group_start = start
                current_group.append(n)
            elif abs(start - group_start) < TOLERANCE:
                current_group.append(n)
            else:
                best_note = max(current_group, key=lambda x: x[2])
                grouped_notes.append(best_note)
                current_group = [n]
                group_start = start
                
        if current_group:
            best_note = max(current_group, key=lambda x: x[2])
            grouped_notes.append(best_note)
            
        return grouped_notes

    def _generate(self, cantus_firmus):
        species = self.args.species
        if species == 1:
            return self._species_1(cantus_firmus)
        elif species == 2:
            return self._species_2(cantus_firmus)
        elif species == 3:
            return self._species_3(cantus_firmus)
        elif species == 4:
            return self._species_4(cantus_firmus)
        elif species == 5:
            return self._species_5(cantus_firmus)
        else:
            raise ValueError("Invalid Species")

    def _save_output(self, notes):
        print(f"{GREEN}Saving output to {self.args.output_file}{RESET}")
        output_mid = mido.MidiFile(ticks_per_beat=self.mid.ticks_per_beat)
        
        track = mido.MidiTrack()
        output_mid.tracks.append(track)
        
        events = []
        # Add Notes
        for start, duration, pitch, velocity in notes:
            events.append((start, 'note_on', pitch, velocity))
            events.append((start + duration, 'note_off', pitch, 0))
            
        # Add Key Signatures from Context Tracker
        filtered_ticks = []
        last_key_tuple = None
        
        for tick in self.context_tracker.sorted_ticks:
            ctx = self.context_tracker.context_map[tick]
            valid_key = self._get_valid_midi_key(ctx.root, ctx.mode)
            
            current_key_tuple = (valid_key)
            if current_key_tuple != last_key_tuple:
                events.append((tick, 'meta_key', valid_key))
                last_key_tuple = current_key_tuple

        # Add Time Signatures and Tempo from Input MIDI
        # We need to scan the original MIDI for these meta events
        for track_idx, input_track in enumerate(self.mid.tracks):
             abs_time = 0
             for msg in input_track:
                 abs_time += msg.time
                 if msg.type == 'set_tempo':
                     events.append((abs_time, 'meta_tempo', msg.tempo))
                 elif msg.type == 'time_signature':
                     # We might have handled TimeSig in metrics, but preserving them in output is good
                     events.append((abs_time, 'meta_timesig', (msg.numerator, msg.denominator, msg.clocks_per_click, msg.notated_32nd_notes_per_beat)))

        # Sort all events
        def sort_key(x):
            tick = x[0]
            type_low = 3
            if x[1].startswith('meta'): type_low = 0
            elif x[1] == 'note_off': type_low = 1
            elif x[1] == 'note_on': type_low = 2
            return (tick, type_low)

        events.sort(key=sort_key)
        
        # Deduplication for Metas (Key/Time/Tempo might be duplicated across tracks)
        # We should accept the first one at a given tick
        
        unique_events = []
        seen_metas = set() # (tick, type, value)
        
        for ev in events:
            if ev[1].startswith('meta'):
                # Key: (tick, type) is enough? No, we might have multiple meta types at same tick.
                # Key: (tick, type, value) to filter exact duplicates.
                sig = (ev[0], ev[1], ev[2])
                if sig in seen_metas: continue
                seen_metas.add(sig)
            unique_events.append(ev)
            
        events = unique_events

        last_tick = 0
        for item in events:
            tick = item[0]
            delta = int(max(0, tick - last_tick))
            
            if item[1] == 'meta_key':
                try:
                    track.append(mido.MetaMessage('key_signature', key=item[2], time=delta))
                except ValueError:
                    pass
            elif item[1] == 'meta_tempo':
                track.append(mido.MetaMessage('set_tempo', tempo=item[2], time=delta))
            elif item[1] == 'meta_timesig':
                num, den, cpc, n32 = item[2]
                track.append(mido.MetaMessage('time_signature', numerator=num, denominator=den, clocks_per_click=cpc, notated_32nd_notes_per_beat=n32, time=delta))
            else:
                track.append(mido.Message(item[1], note=item[2], velocity=item[3], time=delta))
                
            last_tick = tick
            
        output_mid.save(self.args.output_file)

    def _get_valid_midi_key(self, root: str, mode: str) -> str:
        # Map sharp roots to flat equivalents for Major keys that exceed 7 sharps
        # D# Major (9#) -> Eb Major (3b)
        # G# Major (8#) -> Ab Major (4b)
        # A# Major (10#) -> Bb Major (2b)
        # E# Major (11#) -> F Major (1b)
        # B# Major (12#) -> C Major
        
        # Mido expects 'Cm', 'C' format.
        
        enharmonics_major = {
            'D#': 'Eb',
            'G#': 'Ab',
            'A#': 'Bb',
            'E#': 'F',
            'B#': 'C',
            'C#': 'Db' # C# (7#) is valid, but Db (5b) often preferred. Let's stick to valid ones.
            # F# (6#) vs Gb (6b) - both valid.
        }
        
        # For Minor keys:
        # D#m (6#) -> valid
        # G#m (5#) -> valid
        # A#m (7#) -> valid
        # E#m? -> Fm
        
        r = root
        m = mode.lower()
        
        if m == 'major':
            if r in enharmonics_major:
                r = enharmonics_major[r]
        elif m == 'minor':
             if r == 'E#': r = 'F'
             if r == 'B#': r = 'C'
             # D#m is valid (6 sharps). Eb minor (6 flats) is also valid.
             # If detections generated A#m (7 sharps), valid.
        
        suffix = 'm' if m == 'minor' else ''
        return r + suffix

    def _species_1(self, cf_notes):
        output_notes = []
        prev_cp_pitch = None
        prev_cf_pitch = None
        
        for i, (start, end, cf_pitch, vel) in enumerate(cf_notes):
            duration = end - start
            ctx = self.context_tracker.get_context(start)
            
            candidates = []
            search_start = cf_pitch + 1
            search_end = cf_pitch + 20
            
            high_score = -999.0
            best_pitch = None
            
            for p in range(search_start, search_end):
                if not ctx.scale_obj.is_diatonic(p):
                    continue
                
                score = self.voice_leading.evaluate(
                    cf_note=cf_pitch, cp_note=p,
                    prev_cf=prev_cf_pitch, prev_cp=prev_cp_pitch,
                    context=ctx, species=1
                )
                
                if score > high_score:
                    high_score = score
                    best_pitch = p
                    
            if best_pitch is not None:
                output_notes.append((start, duration, best_pitch, vel))
                prev_cp_pitch = best_pitch
                prev_cf_pitch = cf_pitch
            else:
                output_notes.append((start, duration, cf_pitch + 12, vel))
                prev_cp_pitch = cf_pitch + 12
                prev_cf_pitch = cf_pitch
                
        return output_notes

    def _generate_species_generic(self, cf_notes, subdivision_ratio, allow_passing=True, allow_neighbor=True):
        output_notes = []
        prev_cp_pitch = None
        
        for i, (start, end, cf_pitch, vel) in enumerate(cf_notes):
            duration = end - start
            step_duration = duration // subdivision_ratio
            
            for step in range(subdivision_ratio):
                sub_start = start + step * step_duration
                sub_dur = step_duration if step < subdivision_ratio - 1 else (end - sub_start)
                
                ctx = self.context_tracker.get_context(sub_start)
                is_strong = (step == 0)
                
                search_start = cf_pitch + 1
                search_end = cf_pitch + 20
                
                high_score = -999.0
                best_pitch = None
                
                for p in range(search_start, search_end):
                    if not ctx.scale_obj.is_diatonic(p):
                        continue
                    
                    score = self.voice_leading.evaluate(
                        cf_note=cf_pitch, cp_note=p,
                        prev_cf=cf_pitch, prev_cp=prev_cp_pitch,
                        context=ctx, is_strong_beat=is_strong, species=2 if subdivision_ratio==2 else 3
                    )
                    
                    interval = abs(p - cf_pitch)
                    is_consonant = self.voice_leading.is_consonant(interval)
                    
                    if not is_consonant:
                        if is_strong:
                            score -= 200.0
                        else:
                            if prev_cp_pitch is None:
                                score -= 100.0
                            else:
                                diff_prev = p - prev_cp_pitch
                                if abs(diff_prev) > 2:
                                    score -= 100.0
                                else:
                                    score += 5.0 
                    else:
                        score += 5.0

                    if score > high_score:
                        high_score = score
                        best_pitch = p
                
                if best_pitch is None:
                    best_pitch = cf_pitch + 12
                    
                output_notes.append((sub_start, sub_dur, best_pitch, vel))
                prev_cp_pitch = best_pitch
                
        return output_notes

    def _species_2(self, cf_notes):
        return self._generate_species_generic(cf_notes, subdivision_ratio=2)
    
    def _species_3(self, cf_notes):
        return self._generate_species_generic(cf_notes, subdivision_ratio=4)

    def _species_4(self, cf_notes):
        output_notes = []
        prev_cp_pitch = None
        
        for i, (start, end, cf_pitch, vel) in enumerate(cf_notes):
            duration = end - start
            half_dur = duration // 2
            
            note_start = start + half_dur
            
            ctx = self.context_tracker.get_context(note_start)
            next_cf_valid = (i + 1 < len(cf_notes))
            next_cf_pitch = cf_notes[i+1][2] if next_cf_valid else None
            
            search_start = cf_pitch + 1
            search_end = cf_pitch + 20
            
            high_score = -999.0
            best_pitch = None
            
            for p in range(search_start, search_end):
                if not ctx.scale_obj.is_diatonic(p): continue
                
                score = 0
                
                iv_weak = abs(p - cf_pitch)
                if not self.voice_leading.is_consonant(iv_weak):
                    score -= 1000.0
                else:
                    score += 10.0
                    
                if next_cf_valid:
                    iv_strong = abs(p - next_cf_pitch)
                    is_cons_strong = self.voice_leading.is_consonant(iv_strong)
                    
                    if is_cons_strong:
                        score += 5.0
                    else:
                        resolution_pitch = ctx.scale_obj.get_diatonic_candidate(p, -1)
                        iv_res = abs(resolution_pitch - next_cf_pitch)
                        if self.voice_leading.is_consonant(iv_res):
                            score += 20.0 
                        else:
                            score -= 1000.0 
                            
                if prev_cp_pitch is not None:
                     leap = abs(p - prev_cp_pitch)
                     if leap > 4: score -= 10.0
                     if leap == 0: score -= 5.0 
                     
                if score > high_score:
                    high_score = score
                    best_pitch = p
            
            if best_pitch is not None:
                final_dur = duration
                if i == len(cf_notes):
                    final_dur = half_dur
                    
                output_notes.append((note_start, final_dur, best_pitch, vel))
                prev_cp_pitch = best_pitch
                
        return output_notes

    def _species_5(self, cf_notes):
        output_notes = []
        prev_cp_pitch = None
        
        for i, (start, end, cf_pitch, vel) in enumerate(cf_notes):
            duration = end - start
            choices = ['sp1', 'sp2', 'sp3', 'sp4']
            weights = [0.1, 0.4, 0.3, 0.2]
            
            pattern = random.choices(choices, weights)[0]
            
            single_cf_note_list = [(start, end, cf_pitch, vel)]
            generated = []
            
            if pattern == 'sp4':
                generated = self._species_4(single_cf_note_list)
                if not generated:
                    pattern = 'sp2'
                    
            if pattern == 'sp2':
                generated = self._generate_species_generic(single_cf_note_list, subdivision_ratio=2)
            elif pattern == 'sp3':
                 generated = self._generate_species_generic(single_cf_note_list, subdivision_ratio=4)
            elif pattern == 'sp1':
                 generated = self._species_1(single_cf_note_list)
            
            output_notes.extend(generated)
            if generated:
                prev_cp_pitch = generated[-1][2]
                
        return output_notes

def main():
    parser = argparse.ArgumentParser(description="Generate counterpoint melody for a MIDI file.")
    parser.add_argument("input_file", help="Path to input MIDI file")
    parser.add_argument("output_file", nargs='?', help="Path to output MIDI file (optional)")
    parser.add_argument("--species", type=int, choices=[1, 2, 3, 4, 5], default=1, help="Counterpoint species (1-5)")
    parser.add_argument("--cantus_firmus_track", type=int, default=0, help="Track index for Cantus Firmus")
    parser.add_argument("--root", type=str, help="Override key root (e.g. C, F#, Bb)")
    parser.add_argument("--mode", type=str, help="Override key mode (major, minor, dorian, etc.)")
    parser.add_argument("--custom_scale", type=str, help="Custom scale intervals (space separated numbers)")
    
    args = parser.parse_args()

    # Determine output file if not provided
    if not args.output_file:
        import os
        base, ext = os.path.splitext(args.input_file)
        args.output_file = f"{base}_counterpoint{ext}"
        print(f"{YELLOW}Output file not specified. Defaulting to: {args.output_file}{RESET}")

    try:
        mid = mido.MidiFile(args.input_file)
    except Exception as e:
        print(f"{RED}Error opening MIDI file: {e}{RESET}")
        return

    generator = CounterpointGenerator(mid, args)
    generator.run()

if __name__ == "__main__":
    main()
