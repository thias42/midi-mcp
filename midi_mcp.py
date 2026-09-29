"""MIDI MCP server: lets an agent listen to, play and record MIDI on local ports.

Ports are picked by substring match. Defaults can be set with MIDI_IN / MIDI_OUT;
otherwise output prefers an IAC bus and input prefers the first non-IAC port.
"""

import asyncio
import os
import re
import threading
import time
from pathlib import Path

import mido
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer(
    "midi",
    instructions=(
        "Listen to and play MIDI on the user's local instruments. Note names use C4 = MIDI 60. "
        "Use `listen` to hear what the user plays on their keyboard, `play_notes` / `play_chords` "
        "to perform, and `replay` to play back the last take. For a backing loop the user can play "
        "over, call `play_chords` with wait=false and loops=0, then `listen`, and `stop` when done."
    ),
)

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
PITCH_CLASS = {n: i for i, n in enumerate(NOTE_NAMES)} | {"Db": 1, "Eb": 3, "Gb": 6, "Ab": 8, "Bb": 10}

CHORD_INTERVALS = {
    "": [0, 4, 7], "m": [0, 3, 7], "7": [0, 4, 7, 10], "maj7": [0, 4, 7, 11], "m7": [0, 3, 7, 10],
    "m7b5": [0, 3, 6, 10], "dim": [0, 3, 6], "dim7": [0, 3, 6, 9], "aug": [0, 4, 8],
    "sus2": [0, 2, 7], "sus4": [0, 5, 7], "add9": [0, 4, 7, 14], "madd9": [0, 3, 7, 14],
    "6": [0, 4, 7, 9], "m6": [0, 3, 7, 9], "9": [0, 4, 7, 10, 14], "maj9": [0, 4, 7, 11, 14],
    "m9": [0, 3, 7, 10, 14],
}
_quality = "|".join(sorted((re.escape(q) for q in CHORD_INTERVALS if q), key=len, reverse=True))
CHORD_RE = re.compile(rf"^([A-G][#b]?)({_quality})?(?:/([A-G][#b]?))?$")
NOTE_RE = re.compile(r"^([A-Ga-g][#b]?)(-?\d)$")


def note_name(n: int) -> str:
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def parse_note(x: int | str) -> int:
    if isinstance(x, int) or str(x).isdigit():
        return int(x)
    m = NOTE_RE.match(str(x).strip())
    if not m:
        raise ToolError(f"Can't parse note {x!r}; use a MIDI number or a name like 'C4', 'F#3', 'Bb2'")
    name = m.group(1)[0].upper() + m.group(1)[1:]
    return PITCH_CLASS[name] + (int(m.group(2)) + 1) * 12


def parse_chord(symbol: str, octave: int) -> tuple[int, list[int]]:
    """Returns (bass note one octave below, chord tones)."""
    m = CHORD_RE.match(symbol.strip())
    if not m:
        raise ToolError(f"Can't parse chord {symbol!r}; supported qualities: {sorted(CHORD_INTERVALS)}")
    root = PITCH_CLASS[m.group(1)] + (octave + 1) * 12
    tones = [root + i for i in CHORD_INTERVALS[m.group(2) or ""]]
    bass_pc = PITCH_CLASS[m.group(3)] if m.group(3) else PITCH_CLASS[m.group(1)]
    return bass_pc + octave * 12, tones


def resolve_port(kind: str, name: str | None) -> str:
    names = mido.get_output_names() if kind == "out" else mido.get_input_names()
    if not names:
        raise ToolError(f"No MIDI {kind}put ports found")
    wanted = name or os.environ.get("MIDI_OUT" if kind == "out" else "MIDI_IN")
    if wanted:
        for n in names:
            if wanted.lower() in n.lower():
                return n
        raise ToolError(f"No {kind}put port matching {wanted!r}; available: {names}")
    if kind == "out":
        return next((n for n in names if "IAC" in n), names[0])
    return next((n for n in names if "IAC" not in n), names[0])


def build_events(notes: list[tuple[float, float, int, int, int]]) -> list[tuple[float, mido.Message]]:
    """notes: (start_s, duration_s, midi, velocity, channel0) -> time-sorted note on/off events."""
    events = []
    for start, dur, n, vel, ch in notes:
        events.append((start, mido.Message("note_on", note=n, velocity=vel, channel=ch)))
        events.append((start + max(dur, 0.02) - 0.005, mido.Message("note_off", note=n, velocity=0, channel=ch)))
    # note_offs first at equal times so repeated notes retrigger cleanly
    events.sort(key=lambda e: (round(e[0], 4), e[1].type == "note_on"))
    return events


class Player:
    """Plays event lists on a background thread so the agent can keep listening meanwhile."""

    def __init__(self):
        self._out = None
        self._out_name = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def output(self, port: str | None = None) -> mido.ports.BaseOutput:
        name = resolve_port("out", port)
        if self._out is None or self._out_name != name:
            if self._out is not None:
                self._out.close()
            self._out, self._out_name = mido.open_output(name), name
        return self._out

    def start(self, events, length: float, loops: int, port: str | None) -> str:
        self.stop()
        out = self.output(port)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(out, events, length, loops, self._stop), daemon=True)
        self._thread.start()
        return self._out_name

    @staticmethod
    def _run(out, events, length, loops, stop):
        sounding = set()
        begin = time.monotonic()
        i = 0
        try:
            while loops == 0 or i < loops:
                base = begin + i * length
                for t, msg in events:
                    if stop.wait(max(0.0, base + t - time.monotonic())):
                        return
                    out.send(msg)
                    if msg.type == "note_on" and msg.velocity > 0:
                        sounding.add((msg.channel, msg.note))
                    elif msg.type in ("note_on", "note_off"):
                        sounding.discard((msg.channel, msg.note))
                i += 1
        finally:
            for ch, n in sounding:
                out.send(mido.Message("note_off", note=n, velocity=0, channel=ch))

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._stop.set()
            self._thread.join(timeout=2)

    def is_playing(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    async def wait(self):
        if self._thread:
            await asyncio.to_thread(self._thread.join)


player = Player()
last_take: list[dict] = []


async def _perform(notes, length: float, loops: int, wait: bool, port: str | None) -> str:
    if loops == 0 and wait:
        raise ToolError("loops=0 (loop forever) requires wait=false; call stop to end it")
    name = player.start(build_events(notes), length, loops, port)
    if wait:
        await player.wait()
        return f"Played on {name}."
    return f"Playing on {name} in the background" + (" (looping until stop)." if loops == 0 else ".")


@mcp.tool()
def list_ports() -> dict:
    """List MIDI input and output ports and which ones are used by default."""
    ins, outs = mido.get_input_names(), mido.get_output_names()
    return {
        "inputs": ins,
        "outputs": outs,
        "default_input": resolve_port("in", None) if ins else None,
        "default_output": resolve_port("out", None) if outs else None,
    }


def _listen(seconds: float, silence: float, port: str) -> dict:
    notes, controls, held = [], [], {}
    first = last = None
    with mido.open_input(port) as inp:
        begin = time.monotonic()
        while True:
            now = time.monotonic()
            if now - begin > seconds:
                break
            if first is not None and silence and not held and now - last > silence:
                break
            for msg in inp.iter_pending():
                t = time.monotonic()
                last = t
                if msg.type == "note_on" and msg.velocity > 0:
                    if first is None:
                        first = t
                    held[(msg.channel, msg.note)] = (t, msg.velocity)
                elif msg.type in ("note_off", "note_on") and (msg.channel, msg.note) in held:
                    s, v = held.pop((msg.channel, msg.note))
                    notes.append((s, t - s, msg.note, v, msg.channel))
                elif msg.type in ("control_change", "pitchwheel"):
                    controls.append((t, msg))
            time.sleep(0.002)
        end = time.monotonic()
        for (ch, n), (s, v) in held.items():
            notes.append((s, end - s, n, v, ch))

    if first is None:
        return {"port": port, "notes": [], "summary": "No notes were played."}
    notes.sort()
    take = [
        {"note": note_name(n), "midi": n, "start": round(s - first, 3), "duration": round(d, 3),
         "velocity": v, "channel": ch + 1}
        for s, d, n, v, ch in notes
    ]
    last_take[:] = take
    counts: dict[str, int] = {}
    for n in take:
        pc = n["note"].rstrip("-0123456789")
        counts[pc] = counts.get(pc, 0) + 1
    mids = [n["midi"] for n in take]
    ctrl = [
        {"time": round(t - first, 3), "type": m.type,
         **({"control": m.control, "value": m.value} if m.type == "control_change" else {"pitch": m.pitch})}
        for t, m in controls
    ]
    return {
        "port": port,
        "notes": take,
        "controls": ctrl[:200],
        "summary": {
            "note_count": len(take),
            "length_seconds": round(max(n["start"] + n["duration"] for n in take), 2),
            "range": f"{note_name(min(mids))}-{note_name(max(mids))}",
            "pitch_classes_by_count": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "average_velocity": round(sum(n["velocity"] for n in take) / len(take)),
        },
    }


@mcp.tool()
async def listen(seconds: float = 20, stop_after_silence: float = 3, port: str | None = None) -> dict:
    """Listen to what the user plays and return the notes with timing and velocity.

    Tell the user to start playing before calling this. Stops after `seconds`, or earlier once
    `stop_after_silence` seconds pass with no input after they started playing (0 disables that).
    Times are in seconds from the first note. The result becomes the "last take" for `replay`
    and `save_take`. Works while a background loop is playing.
    """
    return await asyncio.to_thread(_listen, seconds, stop_after_silence, resolve_port("in", port))


@mcp.tool()
async def play_notes(
    notes: list[dict], bpm: float = 90, loops: int = 1, wait: bool = True, port: str | None = None
) -> str:
    """Play a sequence of notes.

    Each note: {"note": "C4" or 60, "start": beat, "duration": beats, "velocity": 1-127 (default 90),
    "channel": 1-16 (default 1)}. Beats are quarter notes at `bpm`. Use loops=0 with wait=false to
    loop until `stop`.
    """
    spb = 60 / bpm
    seq = [
        (float(n["start"]) * spb, float(n.get("duration", 1)) * spb, parse_note(n["note"]),
         int(n.get("velocity", 90)), int(n.get("channel", 1)) - 1)
        for n in notes
    ]
    end_beats = max(float(n["start"]) + float(n.get("duration", 1)) for n in notes)
    length = -(-end_beats // 1) * spb  # round loop length up to a whole beat
    return await _perform(seq, length, loops, wait, port)


@mcp.tool()
async def play_chords(
    chords: list[str],
    beats_per_chord: float = 4,
    bpm: float = 90,
    style: str = "block",
    octave: int = 3,
    velocity: int = 70,
    loops: int = 1,
    wait: bool = True,
    port: str | None = None,
) -> str:
    """Play a chord progression, e.g. ["Am", "Dm", "G7", "Cmaj7", "F/A"].

    style: "block" (held chords with a bass note) or "arpeggio" (flowing eighth notes).
    octave sets the chord register (3 = around C3); the bass sits an octave lower.
    Use loops=0 with wait=false for a backing loop the user can play over, then `stop`.
    """
    if style not in ("block", "arpeggio"):
        raise ToolError("style must be 'block' or 'arpeggio'")
    spb = 60 / bpm
    seq = []
    for i, sym in enumerate(chords):
        start, span = i * beats_per_chord * spb, beats_per_chord * spb
        bass, tones = parse_chord(sym, octave)
        if style == "block":
            seq.append((start, span * 0.97, bass, min(127, velocity + 10), 0))
            seq += [(start, span * 0.97, n, velocity, 0) for n in tones]
        else:
            # bass, then up and back down through the chord tones, one eighth note each
            cycle = [bass] + tones + tones[-2:0:-1]
            eighth = spb / 2
            for k in range(int(beats_per_chord * 2)):
                n = cycle[k % len(cycle)]
                dur = min(eighth * 1.9, span - k * eighth)
                vel = min(127, velocity + 10) if k % len(cycle) == 0 else velocity - 8
                seq.append((start + k * eighth, dur, n, vel, 0))
    return await _perform(seq, len(chords) * beats_per_chord * spb, loops, wait, port)


@mcp.tool()
async def replay(transpose: int = 0, speed: float = 1.0, wait: bool = True, port: str | None = None) -> str:
    """Play back the last take captured by `listen`, optionally transposed (semitones) or at a different speed."""
    if not last_take:
        raise ToolError("Nothing recorded yet; call listen first")
    seq = [
        (n["start"] / speed, n["duration"] / speed, max(0, min(127, n["midi"] + transpose)), n["velocity"],
         n["channel"] - 1)
        for n in last_take
    ]
    length = max(s + d for s, d, *_ in seq) + 0.1
    return await _perform(seq, length, 1, wait, port)


@mcp.tool()
def stop() -> str:
    """Stop any background playback and silence all notes."""
    was_playing = player.is_playing()
    player.stop()
    if player._out is not None:
        for ch in range(16):
            player._out.send(mido.Message("control_change", control=123, value=0, channel=ch))
    return "Stopped." if was_playing else "Nothing was playing; sent all-notes-off anyway."


@mcp.tool()
def send_control(control: int, value: int, channel: int = 1, port: str | None = None) -> str:
    """Send a MIDI CC, e.g. 1 = mod wheel, 64 = sustain pedal, 74 = filter cutoff (on many synths)."""
    player.output(port).send(mido.Message("control_change", control=control, value=value, channel=channel - 1))
    return f"CC{control}={value} on channel {channel}."


@mcp.tool()
def program_change(program: int, channel: int = 1, port: str | None = None) -> str:
    """Send a program change (0-127) to switch presets on synths that support it."""
    player.output(port).send(mido.Message("program_change", program=program, channel=channel - 1))
    return f"Program {program} on channel {channel}."


@mcp.tool()
def save_take(path: str, bpm: float = 120) -> str:
    """Save the last take from `listen` as a Standard MIDI File (.mid). `bpm` only sets the file's tempo grid."""
    if not last_take:
        raise ToolError("Nothing recorded yet; call listen first")
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    tempo = mido.bpm2tempo(bpm)
    track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    seq = [(n["start"], n["duration"], n["midi"], n["velocity"], n["channel"] - 1) for n in last_take]
    now = 0
    for t, msg in build_events(seq):
        tick = int(round(mido.second2tick(t, 480, tempo)))
        track.append(msg.copy(time=max(0, tick - now)))
        now = max(now, tick)
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    mid.save(out)
    return f"Saved {len(last_take)} notes to {out}."


@mcp.tool()
async def play_midi_file(path: str, wait: bool = True, port: str | None = None) -> str:
    """Play a Standard MIDI File (.mid) to the output port."""
    file = Path(path).expanduser()
    if not file.is_file():
        raise ToolError(f"No such file: {file}")
    events, t = [], 0.0
    for msg in mido.MidiFile(file):
        t += msg.time
        if not msg.is_meta and msg.type != "sysex":
            events.append((t, msg))
    name = player.start(events, t + 0.1, 1, port)
    if wait:
        await player.wait()
        return f"Played {path} on {name}."
    return f"Playing {path} on {name} in the background."


def main():
    mcp.run()


if __name__ == "__main__":
    main()
