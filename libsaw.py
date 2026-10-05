"""
SAW - SWZL Audio Wave codec (.saw)
==================================

file spec:
- header: "SAW", flags (version<<6, base_rate_idx<<3, channels-1), total samples (u32)
- table: one bit-packed 3-bit rate index per frame (index 7 is silent frame so no payload)
- payload: continuous stream of 4-bit ADPCM codes (sample-major, channel-interleaved)

library api:
- encode(input_path, output_path) -> EncodeResult
- decode(input_path, output_path) -> DecodeResult
- play(input_path, on_start=None, on_rate=None)

internal:
- read_wav, write_wav, read_saw_header, iter_saw_frames, open_pcm_stream
"""

import math
import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from collections import Counter
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path

# --- Format ---

RATES = [4000, 8000, 12000, 16000, 32000, 40000, 44100]
SILENT = 7
MAGIC, VERSION = b"SAW", 2
HEADER = struct.Struct("<3sBI")
FRAME_MS = 50


# --- Encoder configuration ---

LOST_ENERGY_RATIO = 1e-3
LOST_ABS_LEVEL = 0.001
SILENCE_LEVEL = 0.0005

MIN_PLAYBACK_RATE, PLAYBACK_FALLBACK_RATE = 8000, 44100
PREFETCH_FRAMES, PREBUFFER_FRAMES = 40, 4

STEPS = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60,
    66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371,
    408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707,
    1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132,
    7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623,
    27086, 29794, 32767,
]
INDEX_DELTA = [-1, -1, -1, -1, 2, 4, 6, 8]


# --- result types ---
@dataclass
class EncodeResult:
    base_rate: int
    frames: int
    size: int # in bytes
    raw_size: int # equivalent 16-bit pcm size
    rate_usage: Counter = field(default_factory=Counter)

    @property
    def ratio(self):
        return self.size / max(1, self.raw_size)

@dataclass
class DecodeResult:
    base_rate: int
    channels: int

@dataclass
class PlaybackInfo:
    channels: int
    play_rate: int    # rate actually sent to the audio device
    source_rate: int  # rate of the source file
    duration: float # in secs


# --- IMA ADPCM stuff ---

def adpcm_decode(code, state):
    predictor, index = state
    step = STEPS[index]
    diff = step >> 3
    if code & 4:
        diff += step
    if code & 2:
        diff += step >> 1
    if code & 1:
        diff += step >> 2
    predictor = max(-32768, min(32767, predictor - diff if code & 8 else predictor + diff))
    state[0] = predictor
    state[1] = max(0, min(88, index + INDEX_DELTA[code & 7]))
    return predictor


def adpcm_encode(sample, state):
    step = STEPS[state[1]]
    diff = round(sample) - state[0]
    code = 8 if diff < 0 else 0
    diff = abs(diff)
    for bit, shift in ((4, 0), (2, 1), (1, 2)):
        if diff >= step >> shift:
            code |= bit
            diff -= step >> shift
    adpcm_decode(code, state)
    return code


# --- Internal utilities ---

def rms(samples):
    return math.sqrt(sum(x * x for x in samples) / len(samples)) if samples else 0.0


def resample(samples, out_len):
    if not samples:
        return [0] * out_len
    if len(samples) == out_len:
        return samples[:]
    scale, last, out = len(samples) / out_len, len(samples) - 1, []
    for j in range(out_len):
        pos = j * scale
        i = int(pos)
        out.append(samples[last] if i >= last else samples[i] + (samples[i + 1] - samples[i]) * (pos - i))
    return out


def scaled_len(length, src_rate, dst_rate):
    return max(1, round(length * dst_rate / src_rate))


def frame_length(rate):
    return round(rate * FRAME_MS / 1000)


def pcm_bytes(channels):
    count = min(len(c) for c in channels)
    values = [max(-32768, min(32767, round(c[i]))) for i in range(count) for c in channels]
    return struct.pack(f"<{len(values)}h", *values)


def read_wav(path):
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("Only supports 16-bit PCM WAV")
        count, rate = wav.getnchannels(), wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    values = struct.unpack(f"<{len(raw) // 2}h", raw)
    return [list(values[i::count]) for i in range(count)], rate


def write_wav(path, channels, rate):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(len(channels))
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm_bytes(channels))


try:
    import numpy as _np
except ImportError:
    _np = None
def _fft(values):
    n = len(values)
    out = [values[int(format(i, f"0{n.bit_length() - 1}b")[::-1], 2)] for i in range(n)] if n > 1 else values[:]
    size = 2
    while size <= n:
        half = size // 2
        twiddle = [complex(math.cos(-2 * math.pi * k / size), math.sin(-2 * math.pi * k / size)) for k in range(half)]
        for base in range(0, n, size):
            for k in range(half):
                a, b = out[base + k], out[base + k + half] * twiddle[k]
                out[base + k], out[base + k + half] = a + b, a - b
        size *= 2
    return out


def power_spectrum(samples, size):
    count = len(samples)
    if _np is not None:
        x = _np.asarray(samples, dtype=float)
        x = (x - x.mean()) * _np.hanning(count + 2)[1:-1]
        return (_np.abs(_np.fft.rfft(x, size)) ** 2).tolist()
    mean = sum(samples) / count
    windowed = [(v - mean) * (0.5 - 0.5 * math.cos(2 * math.pi * (i + 1) / (count + 1))) for i, v in enumerate(samples)]
    windowed += [0.0] * (size - count)
    spectrum = _fft([complex(v) for v in windowed])
    return [abs(spectrum[k]) ** 2 for k in range(size // 2 + 1)]


def required_rate_index(spectrum, base_rate, base_index, level):
    total = sum(spectrum)
    if total <= 0:
        return 0
    bin_hz = base_rate / 2 / (len(spectrum) - 1)
    above, k = 0.0, len(spectrum) - 1
    for index in range(base_index, 0, -1):
        # first check whether cutting at its Nyquist is audible
        cutoff = RATES[index - 1] / 2
        while k >= 0 and k * bin_hz > cutoff:
            above += spectrum[k]
            k -= 1
        fraction = above / total
        if fraction >= LOST_ENERGY_RATIO and level * math.sqrt(fraction) >= LOST_ABS_LEVEL:
            return index
    return 0


def choose_rate_indices(channels, base_index, frame_len):
    """
    Pick each frame's rate from its frequency content.
    A frame gets the lowest rate whose Nyquist frequency covers the highest frequency that
    makes up an audible share of the frame (content ~16 kHz -> 32 kHz, <8 kHz -> 16 kHz,
    etc). Higher rates are only chosen when the high band is deemed actually audible.
    """
    total, indices, base_rate = len(channels[0]), [], RATES[base_index]
    size = 1 << max(1, (frame_len - 1).bit_length())
    for start in range(0, total, frame_len):
        end = min(start + frame_len, total)
        mono = [sum(c[i] for c in channels) / len(channels) for i in range(start, end)]
        level = rms(mono) / 32768.0
        if level < SILENCE_LEVEL:
            indices.append(SILENT)
            continue
        spectrum = None
        for c in channels:
            part = power_spectrum(c[start:end], size)
            spectrum = part if spectrum is None else [a + b for a, b in zip(spectrum, part)]
        indices.append(required_rate_index(spectrum, base_rate, base_index, level))
    return indices


def rate_label(rate_index):
    return "silent" if rate_index == SILENT else f"{RATES[rate_index]} Hz"


# --- Encoder ---

def encode(input_path, output_path):
    channels, input_rate = read_wav(input_path)
    base_index = min(range(len(RATES)), key=lambda i: abs(RATES[i] - input_rate))
    base_rate = RATES[base_index]
    if base_rate != input_rate:
        channels = [resample(c, scaled_len(len(c), input_rate, base_rate)) for c in channels]

    count, total, frame_len = len(channels), len(channels[0]), frame_length(base_rate)
    if count > 8:
        raise ValueError("SAW supports up to 8 channels")
    indices = choose_rate_indices(channels, base_index, frame_len)
    states = [[0, 0] for _ in channels]
    nibbles = []

    for number, rate_index in enumerate(indices):
        if rate_index == SILENT:
            continue
        start = number * frame_len
        frame = [c[start:start + frame_len] for c in channels]
        size = scaled_len(len(frame[0]), base_rate, RATES[rate_index])
        coded = [[adpcm_encode(s, states[c]) for s in resample(frame[c], size)] for c in range(count)]
        nibbles.extend(code for group in zip(*coded) for code in group)

    if len(nibbles) % 2:
        nibbles.append(0)
    payload = bytes(hi << 4 | lo for hi, lo in zip(nibbles[::2], nibbles[1::2]))

    pad = -3 * len(indices) % 8
    packed = 0
    for r in indices:
        packed = packed << 3 | r
    table = (packed << pad).to_bytes((3 * len(indices) + pad) // 8, "big")

    with open(output_path, "wb") as out:
        out.write(HEADER.pack(MAGIC, VERSION << 6 | base_index << 3 | count - 1, total))
        out.write(table)
        out.write(payload)

    return EncodeResult(
        base_rate=base_rate,
        frames=len(indices),
        size=HEADER.size + len(table) + len(payload),
        raw_size=total * count * 2,
        rate_usage=Counter(indices),
    )


def read_saw_header(f):
    magic, flags, total = HEADER.unpack(f.read(HEADER.size))
    if magic != MAGIC:
        raise ValueError("not a SAW file")
    if flags >> 6 != VERSION:
        raise ValueError(f"unsupported SAW version: {flags >> 6}")
    base_index = flags >> 3 & 7
    if base_index >= len(RATES):
        raise ValueError("corrupt SAW header")
    base_rate = RATES[base_index]
    frames = -(-total // frame_length(base_rate))
    pad = -3 * frames % 8
    packed = int.from_bytes(f.read((3 * frames + pad) // 8), "big") >> pad
    indices = [packed >> 3 * (frames - 1 - k) & 7 for k in range(frames)]
    return (flags & 7) + 1, base_rate, total, indices


def _nibbles(f):
    while chunk := f.read(65536):
        for byte in chunk:
            yield byte >> 4
            yield byte & 15


def iter_saw_frames(f, count, base_rate, total, indices):
    frame_len, codes = frame_length(base_rate), _nibbles(f)
    states = [[0, 0] for _ in range(count)]
    for number, rate_index in enumerate(indices):
        length = min(frame_len, total - number * frame_len)
        if rate_index == SILENT:
            yield rate_index, [[0] * length for _ in range(count)]
            continue
        coded = list(islice(codes, scaled_len(length, base_rate, RATES[rate_index]) * count))
        yield rate_index, [
            resample([adpcm_decode(c, states[ch]) for c in coded[ch::count]], length) for ch in range(count)
        ]


# --- Decode ---

def decode(input_path, output_path):
    with open(input_path, "rb") as f:
        count, base_rate, total, indices = read_saw_header(f)
        out = [[] for _ in range(count)]
        for _, frame in iter_saw_frames(f, count, base_rate, total, indices):
            for c in range(count):
                out[c].extend(frame[c])
    write_wav(output_path, out, base_rate)
    return DecodeResult(base_rate=base_rate, channels=count)


# --- Stream playback ---

def open_pcm_stream(input_path):
    input_path = Path(input_path)
    if input_path.suffix.lower() == ".wav":
        data, rate = read_wav(input_path)
        total, play_rate = len(data[0]), rate if rate >= MIN_PLAYBACK_RATE else PLAYBACK_FALLBACK_RATE
        step = max(1, frame_length(rate))

        def chunks():
            for start in range(0, total, step):
                piece = [c[start:start + step] for c in data]
                yield pcm_bytes([resample(c, scaled_len(len(c), rate, play_rate)) for c in piece]), rate

        return len(data), play_rate, rate, total / rate, chunks()

    f = open(input_path, "rb")
    try:
        count, base_rate, total, indices = read_saw_header(f)
    except Exception:
        f.close()
        raise
    play_rate = base_rate if base_rate >= MIN_PLAYBACK_RATE else PLAYBACK_FALLBACK_RATE  # devices often reject low rates

    def chunks():
        with f:
            for rate_index, frame in iter_saw_frames(f, count, base_rate, total, indices):
                label = None if rate_index == SILENT else RATES[rate_index]
                yield pcm_bytes([resample(c, scaled_len(len(c), base_rate, play_rate)) for c in frame]), label

    return count, play_rate, base_rate, total / base_rate, chunks()


def _prefetch(chunks, depth=PREFETCH_FRAMES):
    buffer, stop = queue.Queue(maxsize=depth), threading.Event()

    def put(item):
        while not stop.is_set():
            try:
                return buffer.put(item, timeout=0.1)
            except queue.Full:
                pass

    def worker():
        try:
            for chunk in chunks:
                if stop.is_set():
                    return
                put(chunk)
            put(None)
        except Exception as exc:
            put(exc)

    threading.Thread(target=worker, daemon=True).start()
    try:
        while (item := buffer.get()) is not None:
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop.set()


def _play_sounddevice(chunks, channels, rate):
    try:
        import sounddevice as sd
        stream = sd.RawOutputStream(samplerate=rate, channels=channels, dtype="int16")
    except Exception:
        return False
    first = list(islice(chunks, PREBUFFER_FRAMES))
    with stream:
        for chunk in first:
            stream.write(chunk)
        for chunk in chunks:
            stream.write(chunk)
        stream.stop()
    return True


def _piped_players(channels, rate):
    return [
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-f", "s16le",
         "-ar", str(rate), "-ac", str(channels), "-i", "-"],
        ["paplay", "--raw", "--format=s16le", f"--rate={rate}", f"--channels={channels}"],
        ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(rate), "-c", str(channels)],
        ["play", "-q", "-t", "raw", "-r", str(rate), "-e", "signed", "-b", "16", "-c", str(channels), "-"],
    ]


def _play_piped(chunks, channels, rate):
    for command in _piped_players(channels, rate):
        if not shutil.which(command[0]):
            continue
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
        try:
            for chunk in chunks:
                process.stdin.write(chunk)
            process.stdin.close()
            process.wait()
        except BrokenPipeError:
            pass
        finally:
            if process.poll() is None:
                process.terminate()
        return True
    return False


def _play_via_file(chunks, channels, rate):
    # afplay and winsound need a complete file
    if sys.platform == "win32":
        import winsound
        player = None
    elif sys.platform == "darwin" and shutil.which("afplay"):
        player = ["afplay"]
    else:
        return False

    fd, tmp_name = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with wave.open(tmp_name, "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            for chunk in chunks:
                wav.writeframes(chunk)
        if player is None:
            winsound.PlaySound(tmp_name, winsound.SND_FILENAME)
        else:
            subprocess.run(player + [tmp_name], check=True)
    finally:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
    return True


def play(input_path, on_start=None, on_rate=None):
    channels, play_rate, rate, duration, raw_chunks = open_pcm_stream(input_path)
    if on_start:
        on_start(PlaybackInfo(channels, play_rate, rate, duration))

    prefetched = _prefetch(raw_chunks)
    live = [True]

    def announce():
        for pcm, label in prefetched:
            if on_rate and live[0]:
                on_rate(label)
            yield pcm

    chunks = announce()
    try:
        for player in (_play_sounddevice, _play_piped, _play_via_file):
            live[0] = player is not _play_via_file
            if player(chunks, channels, play_rate):
                return
        raise RuntimeError(
            "No playback backend found. Install sounddevice (pip install sounddevice), "
            "or one of the following: ffplay, paplay, aplay, sox"
        )
    finally:
        chunks.close()
        prefetched.close()
        raw_chunks.close()
