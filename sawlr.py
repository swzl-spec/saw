#!/usr/bin/env python3
import argparse
from pathlib import Path
import libsaw as saw


class RateDisplay:
    def __init__(self):
        self.last = object()
        self.shown = False

    def show(self, label):
        if label == self.last:
            return
        self.last = label
        self.shown = True
        text = "silent" if label is None else f"{label} Hz"
        print(f"\r  Now playing at: {text:<12}", end="", flush=True)


def cmd_encode(args):
    result = saw.encode(Path(args.input), Path(args.output))
    print(f"[encode] {args.input} -> {args.output}")
    print(f"Base rate: {result.base_rate} Hz, frames: {result.frames}")
    print(f"Size: {result.size} B ({result.ratio:.1%} of {result.raw_size} B PCM)")
    print("Rate usage:")
    for index, n in sorted(result.rate_usage.items()):
        print(f"  Index {index}: {saw.rate_label(index)} ({n} frames)")


def cmd_decode(args):
    result = saw.decode(Path(args.input), Path(args.output))
    print(f"[decode] {args.input} -> {args.output}")
    print(f"output: {result.base_rate} Hz, {result.channels} channel(s)")


def cmd_play(args):
    display = RateDisplay()

    def on_start(info):
        print(f"[playback] {args.input} ({info.source_rate} Hz, {info.channels} channel(s), {info.duration:.2f}s)")
        print("Press Ctrl+C to stop...")

    try:
        saw.play(Path(args.input), on_start=on_start, on_rate=display.show)
    except KeyboardInterrupt:
        print("\nstopped")
        return
    if display.shown:
        print()


def build_parser():
    parser = argparse.ArgumentParser(prog="sawlr", description="Sawler - The complete SAW CLI toolkit")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, func, help_text, in_help, out_help in (
        ("encode", cmd_encode, "Encode a 16-bit PCM wave into SAW", "input 16-bit PCM WAV", "output SAW"),
        ("decode", cmd_decode, "Decode SAW back into PCM wave", "input SAW", "output WAV"),
        ("play", cmd_play, "Stream-decode and play a SAW (or WAV) file", "input SAW or 16-bit PCM WAV", None),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("input", help=in_help)
        if out_help:
            sub.add_argument("output", help=out_help)
        sub.set_defaults(func=func)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
