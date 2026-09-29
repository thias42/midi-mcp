# midi-mcp 🎹

**Give your AI agent ears and fingers.** An MCP server that lets Claude (or any MCP client) listen to what you play on a MIDI keyboard and play back into your synth: harmonize your melody, loop a backing track while you solo, or just guess the tune you're playing (results may vary).

It works at the plain MIDI level, so there's no DAW and no plugin: any controller, any synth.

```
you:     *plays a folk tune on the keyboard*
claude:  "C major pentatonic, rising to a long A, settling home on C...
          my best guess is Auld Lang Syne?"
you:     "Down by the Salley Gardens."
claude:  "Close! Want me to harmonize it?"  *plays it back with chords*
```

## Install

Requires [uv](https://docs.astral.sh/uv/). Tested on macOS; other platforms should work with a virtual MIDI port (e.g. loopMIDI on Windows) but are untested.

1. **Create a virtual MIDI bus** so the agent can reach your synth: *Audio MIDI Setup → Window → Show MIDI Studio → IAC Driver → Device is online*.
2. **Let your synth listen to it**: enable *IAC Driver Bus 1* as a MIDI input (in Arturia Analog Lab: ☰ → Audio MIDI Settings → MIDI Devices).
3. **Add the server** to Claude Code:

   ```sh
   claude mcp add midi --scope user -- uvx --from git+https://github.com/thias42/midi-mcp midi-mcp
   ```

   For other MCP clients, run `uvx --from git+https://github.com/thias42/midi-mcp midi-mcp` as a stdio server.

Output goes to the first IAC bus and input comes from the first non-IAC port (your controller). Override with the `MIDI_OUT` / `MIDI_IN` environment variables (substring match) or the `port` argument on any tool.

## Things to try

- "Listen to what I play and tell me what key it's in."
- "Harmonize what I just played, then do it again in A minor with an arpeggio."
- "Loop Am, F, C, G as an arpeggio at 80 bpm while I improvise, then tell me what I played."
- "Play back my take a fifth higher and half as fast."
- "Save that as salley.mid."

## Tools

| Tool | What it does |
|---|---|
| `list_ports` | Show MIDI inputs/outputs and the defaults |
| `listen` | Capture what you play (notes, timing, velocity, CCs); stops after a pause |
| `play_notes` | Play a note sequence in beats at a given tempo |
| `play_chords` | Play a progression (`Am`, `G7`, `Cmaj7`, `F/A`, ...) as block chords or arpeggios |
| `replay` | Play back the last take, optionally transposed or at a different speed |
| `stop` | Stop background playback and silence all notes |
| `send_control` / `program_change` | Send a CC or program change |
| `save_take` / `play_midi_file` | Write the last take to `.mid` / play a `.mid` file |

`play_notes`, `play_chords`, `replay` and `play_midi_file` take `wait=false` to play in the background, and the note and chord tools take `loops=0` to repeat until `stop`, so the agent can `listen` while a backing track loops.

Note names use C4 = MIDI 60.

## How it started

With an Arturia Minilab3 and Analog Lab, I asked Claude Code "can you access my keyboard?" One session later it had guessed a tune (wrongly), harmonized it (nicely), researched whether any DAW already does this (none quite do), and written this server.

## License

MIT
