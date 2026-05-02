import mido
import argparse
import sys
import os

# Ensure we can import counterpoint_generator from the same directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from counterpoint_generator import CounterpointGenerator
except ImportError:
    print("Warning: could not import counterpoint_generator. Counterpoint generation will be skipped.")
    CounterpointGenerator = None

# Pentatonic Scale (Major): C, D, E, G, A
# Maps to MIDI note numbers relative to root
PENTATONIC_INTERVALS = [0, 2, 4, 7, 9] 

class TextToMidiConverter:
    def __init__(self, root_note: int = 60, scale_type: str = 'pentatonic'):
        self.root_note = root_note
        self.scale_type = scale_type
        self.ticks_per_beat = 480
        
        # Build the scale spanning 2 octaves
        self.scale_notes = []
        for octave in range(2):
            base = self.root_note + (octave * 12)
            self.scale_notes.extend([base + interval for interval in PENTATONIC_INTERVALS])
        # Add high root
        self.scale_notes.append(self.root_note + 24)
        
    def text_to_melody(self, text: str) -> mido.MidiFile:
        mid = mido.MidiFile(ticks_per_beat=self.ticks_per_beat)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        
        # Set tempo (120 BPM)
        track.append(mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(120)))
        track.append(mido.MetaMessage('time_signature', numerator=4, denominator=4))
        
        # Instrument: Acoustic Grand Piano
        track.append(mido.Message('program_change', program=0, time=0))
        
        current_time = 0
        
        for char in text:
            # Rhythm Logic
            duration = self.ticks_per_beat  # Default quarter note
            is_rest = False
            
            if char in [' ', '\t']:
                # Space is a short rest
                duration = self.ticks_per_beat // 2
                is_rest = True
            elif char in [',', '，', '、', ';', '；']:
                # Comma is a quarter rest
                duration = self.ticks_per_beat
                is_rest = True
            elif char in ['.', '。', '!', '！', '?', '？', '\n']:
                # Sentence end is a half note rest
                duration = self.ticks_per_beat * 2
                is_rest = True
            else:
                # Normal character -> Note
                is_rest = False
                
            if is_rest:
                # For a rest, we just advance the time of the *next* event
                # But since mido uses delta times, we just accumulate this into a variable
                # or simpler: append a "note_off" or just wait. 
                # Actually, in standard MIDI, rests are just time between Note Ons.
                # However, our loop needs to handle note length.
                # If it's a rest, we don't play a note.
                # If the previous event was a Note Off, we add to its delta time?
                # A simpler way: We treat a rest as a "silence" that adds to the *next* event's wait time.
                current_time += duration
            else:
                # Generate Pitch
                # Use hash to get a deterministic but "random" index
                # We use ord(char) + some mixing to avoid simple linear patterns
                idx = (ord(char) * 7 + 13) % len(self.scale_notes)
                note = self.scale_notes[idx]
                velocity = 80 + (ord(char) % 30) # Dynamic velocity
                
                # Note On
                track.append(mido.Message('note_on', note=note, velocity=velocity, time=current_time))
                current_time = 0 # Reset delta time
                
                # Note Off
                track.append(mido.Message('note_off', note=note, velocity=0, time=duration))
                
        return mid

def main():
    parser = argparse.ArgumentParser(description="Convert text to MIDI melody and generate counterpoint.")
    parser.add_argument("text_input", help="Text string or path to text file")
    parser.add_argument("-o", "--output", default="text_melody.mid", help="Output MIDI file path")
    parser.add_argument("--species", type=int, default=5, choices=[1, 2, 3, 4, 5], help="Counterpoint species (1-5)")
    parser.add_argument("--root", default="C", help="Root note (e.g., C, D#)")
    parser.add_argument("--mode", default="major", choices=["major", "minor"], help="Mode")
    
    args = parser.parse_args()
    
    # 1. Get Text
    if os.path.exists(args.text_input):
        with open(args.text_input, 'r', encoding='utf-8') as f:
            text = f.read()
    else:
        text = args.text_input
        
    print(f"🎵 Converting text to melody ({len(text)} chars)...")
    
    # 2. Convert to MIDI
    converter = TextToMidiConverter(root_note=60) # C4
    mid = converter.text_to_melody(text)
    
    # 3. Save Cantus Firmus (Optional, for debugging or if CP gen fails)
    base_output = args.output
    if base_output.endswith('.mid'):
        cf_output = base_output.replace('.mid', '_cantus.mid')
    else:
        cf_output = base_output + '_cantus.mid'
        
    mid.save(cf_output)
    print(f"✅ Generated base melody: {cf_output}")
    
    # 4. Generate Counterpoint
    # Use subprocess to call the script appropriately, ensuring robust isolation
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'counterpoint_generator.py')
    
    if os.path.exists(script_path):
        print(f"🎹 Generating Species {args.species} Counterpoint...")
        
        # Build command
        # python counterpoint_generator.py input output --species X --root Y --mode Z
        cmd = [
            sys.executable, 
            script_path, 
            cf_output, 
            args.output,
            "--species", str(args.species),
            "--root", args.root,
            "--mode", args.mode
        ]
        
        try:
            import subprocess
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"✨ Done! Final Output: {args.output}")
            else:
                print(f"❌ Counterpoint generation failed with code {result.returncode}")
                print(result.stdout)
                print(result.stderr)
        except Exception as e:
            print(f"❌ Execution failed: {e}")
    else:
        print(f"⚠️ Counterpoint generator script not found at {script_path}")

if __name__ == "__main__":
    main()
