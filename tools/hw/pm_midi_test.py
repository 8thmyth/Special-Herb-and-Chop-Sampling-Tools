#!/usr/bin/env python3
"""PATTERN MUTE x one-shot test on a unit, over USB MIDI (macOS CoreMIDI, ot_midi). Run beside ot_monitor.

    python3 tools/hw/pm_midi_test.py [LOGFILE]

Audio track channels 1-8. Writes a "TEST:" line into LOGFILE (the monitor's log) before each step, so the
monitor's lines can be read against what was sent. MIDI map: docs/firmware/MIDI.md (CC 49 mute, CC 52 arm).
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ot_midi  # noqa: E402
import struct  # noqa: E402

try:                                              # the unit's state after each step, over REMOTE's PEEK
    import load_meter
    import ot_events
    _pk = ot_events.unit_peek(load_meter.libusb_device())
except BaseException as e:  # noqa: BLE001
    _pk = None
    print(f"(no PEEK: {e})")


def state():
    if _pk is None:
        return "?"
    rd = lambda a, n: ot_events.peek(_pk, a, n)   # noqa: E731
    lg = lambda a: struct.unpack(">I", rd(a, 4))[0]   # noqa: E731
    wa, wm = lg(0x80000008), lg(0x8000000C)
    base = lg(0x46C82456) + rd(0x80000004, 1)[0] * 0x8ED8
    os_ = []
    for t in range(4):
        v = int.from_bytes(rd(base + 2330 * t + 24, 8), "big")
        os_.append(f"T{t + 1}:" + (",".join(str(s + 1) for s in range(64) if v >> s & 1) or "-"))
    mu = lambda w: "".join(str(t + 1) for t in range(8) if w >> (8 + t) & 1) or "-"   # noqa: E731
    lay = "PATTERN" if rd(0x100B14DD, 1)[0] & 1 else "GLOBAL"
    return f"audio mutes {mu(wa)}  MIDI mutes {mu(wm)}  layer {lay}  one-shots {' '.join(os_)}"

LOG = open(sys.argv[1], "a") if len(sys.argv) > 1 else None


def mark(s, show=True):
    if show:
        try:
            s += "   ->  " + state()
        except Exception as e:  # noqa: BLE001
            s += f"   ->  (read failed: {e})"
    line = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}  TEST: {s}"
    print(line, flush=True)
    if LOG:
        LOG.write(line + "\n")
        LOG.flush()


dsts = [n for n, _ in ot_midi.endpoints("dst")]
name = next((n for n in dsts if "octatrack" in n.lower()), None) or next((n for n in dsts if "elektron" in n.lower()), None)
if not name:
    sys.exit(f"no Octatrack MIDI destination; have {dsts}")
out = ot_midi.Out(name)
mark(f"MIDI to {name!r}", False)


def cc(ch, num, val, what):
    mark(f"{what}  (ch{ch} CC{num}={val})", False)
    out.send([0xB0 | (ch - 1), num, val])


mark("before")
mark("START", False); out.send([0xFA]); time.sleep(4); mark("4 s playing")
cc(6, 49, 127, "mute T6"); time.sleep(3); mark("3 s after mute T6")
cc(6, 49, 0, "unmute T6"); time.sleep(3); mark("3 s after unmute T6")
for t in (1, 2, 3, 4):
    cc(t, 52, 127, f"re-arm T{t}'s one-shots"); time.sleep(0.3)
time.sleep(5); mark("5 s after re-arm")
cc(6, 49, 127, "mute T6"); time.sleep(3); mark("3 s after mute T6")
cc(6, 49, 0, "unmute T6"); time.sleep(3); mark("3 s after unmute T6")
mark("STOP", False); out.send([0xFC]); time.sleep(0.5)
mark("done")
