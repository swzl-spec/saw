# SWZL Audio Wave (SAW)
A new lossy codec and file format for audio that leverages both variable **sample rate** (yes, sample rate itself varies!) and IMA ADPCM compression to achieve files that are 10-16% the size of raw signed 16-bit PCM WAV and roughly 50% the size of standard IMA ADPCM with reasonably clear quality. It achieves this by basically analyzing small chunks of the audio, getting the "audible" frequency range maximums of each, and choosing a suitable Nyquist sample rate for each chunk from a table of ones supported by the codec.

The standard file extension for this format is `.saw`.

## File Spec
- header: "SAW", flags (version<<6, base_rate_idx<<3, channels-1), total samples (u32)
- table: one bit-packed 3-bit rate index per frame (index 7 is silent frame so no payload)
- payload: continuous stream of 4-bit ADPCM codes (sample-major, channel-interleaved)

### Chunk Sample Rate Table
- 4000Hz (Level 0)
- 8000Hz (Level 1)
- 12000Hz (Level 2)
- 16000Hz (Level 3)
- 32000Hz (Level 4)
- 40000Hz (Level 5)
- 44100Hz (Level 6)
- Silent chunk (Level 7)

## How To Use
On Unix-like:
```
./install.sh
```
This repo includes two things:
- `libsaw`: The library itself so you can use it anywhere you want.
- `sawlr` (Sawler): A CLI for encoding/decoding SAW audio.

### CLI Usage
```
# Encode/convert 16-bit PCM WAV to SAW
sawlr encode audio.wav audio.saw

# Decode SAW to 16-bit PCM WAV
sawlr decode audio.saw audio.saw.wav

# Play a SAW file
sawlr play audio.saw
```

## Library implementations
### libsaw.py
Main SAW Library. Supports:
- `.encode`: Yes
- `.decode`: Yes
- `.play`: Yes
