#!/usr/bin/env python3
"""Watch a unit's one-shot trig state over REMOTE's PEEK (USB) and log every change.

    python3 tools/hw/oneshot_watch.py [LOGFILE]

The image must carry USB and REMOTE (T7 does). Polls every 50 ms and prints a line whenever one of these
changes (addresses from the port, 16 Nov 2026; out/svcost/oneshot.py):
  track    0x80000004 (byte)   the current audio track
  masks    the current track's four trig masks in its pattern record (base = [0x46c82456] + track * 0x8ed8):
           +0x00 trigs, +0x08, +0x10, +0x18 one-shots (64 steps each, step 1 = bit 0 of the last byte)
  muted    steps whose condition byte (base + 0x89a + 2*step + 1, low 7 bits) is > 64: TRIG MUTE's mute
  spent    0x8000184e (word)      one-shot spent, a bit a track (bit 0 = T1, bit 8 = M1)
  armreq   0x46c803d4 (word)      arm request, a bit a track (consumed each frame at 0x4000ac44: spent &= ~req)
  disarm   0x46c7fe22 (2 bytes)   disarm request
  hold     0x46c7ff64 (word)      set with the arm bit by YES (0x4005e2e6); the frame path 0x4000b934 fires a
                                  one-shot when its track's spent bit is clear or its hold bit set
  playing  0x800065bb (byte)      1 while the sequencer plays
"""
import os
import pathlib
import struct
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import load_meter      # noqa: E402
import ot_events       # noqa: E402


def main():
    log = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None
    dev = load_meter.libusb_device()
    pk = ot_events.unit_peek(dev)

    def rd(a, n):
        return ot_events.peek(pk, a, n)

    def long(a):
        return struct.unpack(">I", rd(a, 4))[0]

    out = open(log, "a") if log else None

    def say(s):
        line = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}  " + s
        print(line, flush=True)
        if out:
            out.write(line + "\n")
            out.flush()

    say("oneshot_watch: connected")
    prev = {}
    vprev = [None] * 8
    while True:
        try:
            ui = rd(0x100B14CC, 1)[0] & 7                  # the track shown (UI mirror of 0x80000000)
            trk = int(os.environ.get("OS_TRACK", ui + 1)) - 1
            pat = rd(0x80000004, 1)[0]                      # the pattern (PATTERN MUTE's PATNO)
            base = long(0x46C82456) + pat * 0x8ED8 + trk * 2330   # its track record (pm.s PAT_STRIDE, REC_STRIDE)
            m = rd(base, 0x20)
            conds = rd(base + 0x89A, 129)          # never a multiple of 64 (TRAPS trap-ep0-dma)
            st = {
                "track": f"T{trk + 1}",
                "trigs": m[0:8].hex(), "m08": m[8:16].hex(), "m10": m[16:24].hex(), "oneshot": m[24:32].hex(),
                "muted": ",".join(str(s + 1) for s in range(64) if (conds[2 * s + 1] & 127) > 64) or "-",
                "cond1": conds[1] & 127,
                "spent": rd(0x8000184E, 2).hex(),
                "armreq": rd(0x46C803D4, 2).hex(),
                "disarm": rd(0x46C7FE22, 2).hex(),
                "hold": rd(0x46C7FF64, 2).hex(),
                "playing": rd(0x800065BB, 1)[0],
                "mutes": "".join(str(t + 1) for t in range(8) if long(0x80000008) >> (8 + t) & 1) or "-",
                "mutelayer": "PATTERN" if rd(0x100B14DD, 1)[0] & 1 else "GLOBAL",   # PATTERN MUTE (pm.s LAYER_B)
                "solos": "".join(str(t + 1) for t in range(8) if long(0x80000008) >> t & 1) or "-",
            }
            # every audio voice: 0x800049d8 + 0xa8 t; +0 active, +68 play position (lazy-chop README)
            vraw = rd(0x800049D8, 0xA8 * 8 - 1)
        except Exception as e:  # noqa: BLE001
            say(f"read failed: {e}")
            time.sleep(1)
            continue
        ch = {k: v for k, v in st.items() if prev.get(k) != v}
        if ch:
            say("  ".join(f"{k}={v}" for k, v in (st.items() if not prev else ch.items())))
        prev = st
        for t in range(8):
            o = 0xA8 * t
            act = vraw[o]
            pos = struct.unpack(">I", vraw[o + 68:o + 72])[0]
            pv = vprev[t]
            if pv is not None:
                pact, ppos = pv
                if act and not pact:
                    say(f"voice T{t + 1} START pos={pos}")
                elif act and pos + 2000 < ppos:
                    say(f"voice T{t + 1} RESTART pos={pos} (was {ppos})")
                elif pact and not act:
                    say(f"voice T{t + 1} END pos={ppos}")
            vprev[t] = (act, pos)
        time.sleep(0.02)


if __name__ == "__main__":
    main()
