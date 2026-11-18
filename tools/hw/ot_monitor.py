#!/usr/bin/env python3
"""Monitor a unit live over USB (v2): sequencer state, LAZY CHOP and its tempo, REMOTE's events, the load meter,
freezes, screenshots and the sound. Every line timestamped; only changes are logged.

    python3 tools/hw/ot_monitor.py LOGFILE            # everything
    python3 tools/hw/ot_monitor.py LOGFILE --quiet    # the load meter only (to measure the monitor's own cost)

Needs an image with USB, REMOTE and LOAD METER (T7/T8), pyusb + libusb-package, and for the sound sounddevice +
numpy (the .command installs them). Captures (screens, freeze traces, audio) go to ot-monitor-captures/ beside
the log. Read-only: PEEK (0xc0/0x58), REMOTE's ring (0xc0/0x5a), LOAD METER (0xc0/0x57), the SCREEN SysEx
request; nothing is written to the unit.

STATE, 10 a second (addresses: pm.s, lazychop.s, the port, 16 Nov 2026):
  pattern 0x800065bd/be; track 0x100b14cc; mutes/solos 0x80000008 (audio) and 0x8000000c (MIDI) bits 8-15 / 0-7;
  PATTERN MUTE's layer 0x100b14dd (absent in T8: reads 0); one-shot words 0x8000184e, 0x46c7ff64, 0x46c803d4,
  0x46c7fe22; each audio track's trigs / one-shots / TRIG MUTE steps in the current pattern; voices
  0x800049d8 + 0xa8 t (+0 active, +68 position).
LAZY CHOP (T8's symbols, checked at start): lc_state (0 off, 1 armed, 2 chopping), lc_track, lc_next, lc_snap,
  lc_live, and the slice record lc_rec (+300 trim start, +304 trim end, +312 64 x {start, end, loop}, +1092
  count). While chopping entry 0 is the whole take and the slices are 1..n. Each change of the slices logs them
  and the tempo (tools/hw/chop_tempo.py: grid+loop, grid, loop, median).
EVENTS: REMOTE's ring (project loads, saves, bank writes, PLAYING, PATTERN ...), each record as it arrives.
LOAD METER, 2 a second: a line every 10 s (frame interrupt share and longest run, DSP core busy, bad frames).
  The DSP frame count is the heartbeat: stopped for 1.5 s -> FREEZE (trace, load meter, screen saved).
  SAFETY VALVE: the frame interrupt's longest run over 420 us (normal on the MKI, playing with USB audio: 310-360 us) ->
  the state polling drops to 2 a second until 30 s pass under 200 us.
SCREEN: a 128x64 PNG on LAZY CHOP changes, pattern changes, freezes, and every 60 s (at most one per 3 s).
EVIDENCE (for the PRs): a session header; LAZY CHOP TEMPO popups and the delay after YES (100 ms polling, coarse);
  LOCK EDIT opens ([TRIG]+[BANK]: the editor opening in grid recording with trigs held); ZOOM KEEP (each page's
  frames/pixel and first frame at a track switch with the editor open); recorder starts with the editor open (SRC3
  KEY on an MKI); ZERO SNAP taps. At exit (Ctrl-C or closing the window) a summary goes to the log and to
  ot-monitor-captures/evidence_<date>.md.
SOUND: every USB input channel's level (SOUND ON/OFF at -50/-60 dBFS, a summary every 2 s while sounding), and
  the main out (channels 17-18 of 20) recorded to 5-minute WAV files, the last 12 kept.
"""
import os
import pathlib
import struct
import sys
import threading
import time
import wave
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import load_meter      # noqa: E402
import ot_events       # noqa: E402

try:
    import chop_tempo  # noqa: E402
except Exception:  # noqa: BLE001
    chop_tempo = None

SR = 44100
# T8 (lazy-chop-mods, personal-baseline-T8 c8e93954): LAZY CHOP's variables, from its runtime.elf
LC_SYMS_T8 = {"lc_state": 0x40B4D650, "lc_rec": 0x40B4D658, "lc_track": 0x40B4D65C, "lc_next": 0x40B4D660,
              "lc_snap": 0x40B4D6B4, "lc_live": 0x40B4D6B8, "lc_ltk": 0x40B4D6BC, "lc_last": 0x40B4D66C}
# T9 (lazy-chop-mods + LAZY CHOP TEMPO, BUILD=T9, version METALFNGRZ): from out/platform/runtime/runtime.map
# (lc_track / lc_last are local symbols: lc_state + 0x0c / + 0x1c, the same layout as T8)
LC_SYMS_T9 = {"lc_state": 0x40B4EB50, "lc_rec": 0x40B4EB58, "lc_track": 0x40B4EB5C, "lc_next": 0x40B4EB60,
              "lc_snap": 0x40B4EBB4, "lc_live": 0x40B4EBB8, "lc_ltk": 0x40B4EBBC, "lc_last": 0x40B4EB6C,
              # ZERO SNAP debug words (no behaviour change): the last tap before / after the snap, taps since power-on
              "lc_dbg_tap": 0x40B4EBD0, "lc_dbg_snap": 0x40B4EBD4, "lc_dbg_n": 0x40B4EBD8,
              # LAZY CHOP TEMPO: popup state (0 none, 1 waiting for the count popup, 2 up, 3 gate), the line, c / bracket
              "ct_state": 0x40B50BE8, "ct_c": 0x40B50C04, "ct_br": 0x40B50C08, "ct_buf": 0x40B50CA8}
# TA (METALFNGRZ remix, BUILD=TA, version METALFNGRZ): from the 17 Nov rebuild's out/platform/runtime/runtime.map
# (lc_track / lc_last are local symbols: lc_state + 0x0c / + 0x1c, the same layout as T9/T8)
LC_SYMS_TA = {"lc_state": 0x40B4EB70, "lc_rec": 0x40B4EB78, "lc_track": 0x40B4EB7C, "lc_next": 0x40B4EB80,
              "lc_snap": 0x40B4EBD4, "lc_live": 0x40B4EBD8, "lc_ltk": 0x40B4EBDC, "lc_last": 0x40B4EB8C,
              # ZERO SNAP debug words (no behaviour change): the last tap before / after the snap, taps since power-on
              "lc_dbg_tap": 0x40B4EBF0, "lc_dbg_snap": 0x40B4EBF4, "lc_dbg_n": 0x40B4EBF8,
              # LAZY CHOP TEMPO: popup state (0 none, 1 waiting for the count popup, 2 up, 3 gate), the line, c / bracket
              "ct_state": 0x40B50C08, "ct_c": 0x40B50C24, "ct_br": 0x40B50C28, "ct_buf": 0x40B50CC8}
# EVIDENCE (for the PRs, 17 Nov): stock addresses the modules read (lock-edit / src3-key / zoom-keep sources)
AED_WIN = 0x460E5E4C          # the audio editor's window, 0 when shut
GRID_REC = 0x460D1736         # grid recording on
HELD_TRIGS = 0x460D174A       # u16, a bit a held trig key
HELD_PAGE = 0x460D174C        # u16, the step page's first step
AED_VIEWS = {"TRIM": 0x400CE724, "SLICE": 0x400CEB14, "EDIT": 0x400CEF60}   # +0x10 frames/pixel, +0x18 first frame
EVID = {"start": time.strftime("%Y-%m-%d %H:%M:%S"), "image": "?", "chops": 0, "tempo": [], "snap_taps": 0,
        "snap_moved": 0, "lockedit": [], "zoom_changes": {k: 0 for k in AED_VIEWS}, "zoom_kept": 0, "zoom_reset": 0,
        "src3": [], "rec_starts": 0, "yes_ms": [], "freezes": 0}
CT_STATES = {0: "none", 1: "waiting (slice count up)", 2: "UP", 3: "gate request"}
LC_STATES = {0: "off", 1: "armed", 2: "chopping"}

LOG = None
LOCK = threading.Lock()
CAP = None
# the last RING_S seconds of the main out, mono, for the tempo reading's audio cross-check
RING_S = 180
RING = {"buf": None, "sr": 0, "pos": 0, "filled": 0, "t_end": 0.0}
RING_LOCK = threading.Lock()


def audio_since(t0, dur, least=8.0):
    """The main out from wall time t0 for dur seconds (or as much of it as has played, at least `least` s),
    as (mono float32 samples, rate); None when the sound is not monitored or it is not in the ring."""
    with RING_LOCK:
        buf, sr = RING["buf"], RING["sr"]
        if buf is None:
            return None
        import numpy as np
        n = len(buf)
        ago = RING["t_end"] - t0                     # seconds from t0 to the newest sample
        if ago <= least or ago * sr > RING["filled"] or ago * sr > n:
            return None
        take = int(min(dur, ago) * sr)
        start = (RING["pos"] - int(ago * sr)) % n
        idx = (start + np.arange(take)) % n
        return buf[idx].copy(), sr


def stamp():
    return time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"


def say(s):
    line = f"{stamp()}  {s}"
    with LOCK:
        print(line, flush=True)
        if LOG:
            LOG.write(line + "\n")
            LOG.flush()


# ---- the screen (SCREEN module, over USB MIDI) ---------------------------------------------------------------
class Screen:
    REQ = bytes.fromhex("F0 00 20 3C 10 00 00 00 01 00 00 04 F7")   # modules/screen/README.md

    def __init__(self):
        self.ok = False
        self.buf = bytearray()
        self.frames = []
        self.last = 0.0
        try:
            import ctypes
            import ot_midi

            def walk(pktlist, aligned):
                """ot_midi._walk with SysEx packets up to 64 KB: the screen reply (1,194 bytes) can arrive as one
                packet, which ot_midi's 256-byte sanity limit drops with the whole list."""
                n = ctypes.cast(pktlist, ctypes.POINTER(ctypes.c_uint32))[0]
                p, out, sysex = pktlist + 4, [], False
                for _ in range(n):
                    length = ctypes.cast(p + 8, ctypes.POINTER(ctypes.c_uint16))[0]
                    if not 0 < length <= 65535 or (ctypes.string_at(p + 10, 1)[0] < 0x80 and not sysex):
                        return None
                    for b in ctypes.string_at(p + 10, length):
                        if b == 0xF0 or b == 0xF7:
                            sysex = b == 0xF0
                    out.append((p, length))
                    end = p + 10 + length
                    p = (end + 3) & ~3 if aligned else end
                return out
            ot_midi._walk = walk
            src = next((n for n, _ in ot_midi.endpoints("src") if "octatrack" in n.lower()), None)
            dst = next((n for n, _ in ot_midi.endpoints("dst") if "octatrack" in n.lower()), None)
            if not (src and dst):
                say("screen: no Octatrack MIDI port (not captured)")
                return
            self.out = ot_midi.Out(dst)
            self.inp = ot_midi.In(src, self._bytes)
            self.ok = True
            say(f"screen: SCREEN over '{dst}'")
        except BaseException as e:  # noqa: BLE001
            say(f"screen: not captured ({e})")

    def _bytes(self, data):
        for b in data:
            if b == 0xF0:
                self.buf = bytearray([b])
            elif self.buf:
                self.buf.append(b)
                if b == 0xF7:
                    if len(self.buf) > 1000:
                        self.frames.append(bytes(self.buf))
                    self.buf = bytearray()

    @staticmethod
    def unpack7(data):
        out = bytearray()
        for i in range(0, len(data), 8):
            chunk = data[i:i + 8]
            msb = chunk[0]
            for j, b in enumerate(chunk[1:]):
                out.append(b | (0x80 if msb & (1 << (6 - j)) else 0))
        return bytes(out)

    def grab(self, tag, wait=1.5):
        if not self.ok or time.time() - self.last < 3:
            return None
        self.last = time.time()
        self.frames.clear()
        self.out.send(self.REQ)
        end = time.time() + wait
        while time.time() < end and not self.frames:
            time.sleep(0.02)
        if not self.frames:
            say(f"screen: no reply ({tag})")
            return None
        body = self.unpack7(self.frames[-1][6:-1])
        pix = body[14:14 + 1024]                    # msgId, replyTo, cmd, ver, w, h, len: 14 bytes
        if len(pix) < 1024:
            say(f"screen: short reply ({len(body)} bytes)")
            return None
        rows = []
        for y in range(64):
            row = bytearray()
            for x in range(128):
                w = struct.unpack(">I", pix[8 * x + 4 * (y // 32):8 * x + 4 * (y // 32) + 4])[0]
                lit = (w >> (y % 32)) & 1
                row += bytes([20 if lit else 225]) * 4
            rows += [bytes(row)] * 4
        path = CAP / f"screen_{time.strftime('%H%M%S')}_{tag}.png"
        png(path, 512, 256, rows)
        say(f"screen: {path.name}")
        return path


def png(path, w, h, rows):
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


# ---- the sound -----------------------------------------------------------------------------------------------
def sound():
    try:
        import numpy as np
        import sounddevice as sd
    except Exception as e:  # noqa: BLE001
        say(f"sound: not monitored ({e})")
        return
    devs = [(i, d) for i, d in enumerate(sd.query_devices()) if "octatrack" in d["name"].lower() and d["max_input_channels"] > 0]
    if not devs:
        say("sound: no Octatrack audio input on this Mac (USB audio off?)")
        return
    idx, dev = devs[0]
    nch = dev["max_input_channels"]
    sr = int(dev["default_samplerate"]) or SR
    main = (16, 17) if nch >= 18 else (0, 1)
    adir = CAP / "audio"
    adir.mkdir(exist_ok=True)
    say(f"sound: '{dev['name']}', {nch} channels at {sr} Hz; main out ch{main[0] + 1}-{main[1] + 1} recorded to {adir.name}/")
    on = [False] * nch
    last_sum = [0.0]
    peak2s = np.full(nch, -120.0)
    seg = {"w": None, "t": 0.0}
    with RING_LOCK:
        RING.update(buf=np.zeros(int(RING_S * sr), dtype=np.float32), sr=sr, pos=0, filled=0)

    def rotate():
        if seg["w"]:
            seg["w"].close()
        files = sorted(adir.glob("main_*.wav"))
        for f in files[:-11]:
            try:
                f.unlink()
            except OSError:
                pass
        p = adir / f"main_{time.strftime('%Y%m%d_%H%M%S')}.wav"
        w = wave.open(str(p), "wb")
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        seg["w"], seg["t"] = w, time.time()

    def cb(indata, frames, t, status):
        pk = np.max(np.abs(indata), axis=0)
        db = 20 * np.log10(np.maximum(pk, 1e-6))
        for c in range(nch):
            peak2s[c] = max(peak2s[c], db[c])
            if db[c] > -50 and not on[c]:
                on[c] = True
                say(f"SOUND ON  ch{c + 1} {db[c]:.0f} dB")
            elif db[c] < -60 and on[c]:
                on[c] = False
                say(f"SOUND OFF ch{c + 1}")
        now = time.time()
        if now - last_sum[0] > 2:
            last_sum[0] = now
            if any(on):
                say("levels " + " ".join(f"ch{c + 1}={peak2s[c]:.0f}" for c in range(nch) if peak2s[c] > -70))
            peak2s[:] = -120.0
        if seg["w"] is None or now - seg["t"] > 300:
            rotate()
        st = np.clip(indata[:, list(main)], -1, 1)
        seg["w"].writeframes((st * 32767).astype("<i2").tobytes())
        mono = st.mean(axis=1).astype(np.float32)
        with RING_LOCK:
            buf, n, p = RING["buf"], len(RING["buf"]), RING["pos"]
            k = len(mono)
            if p + k <= n:
                buf[p:p + k] = mono
            else:
                buf[p:] = mono[:n - p]
                buf[:k - (n - p)] = mono[n - p:]
            RING["pos"] = (p + k) % n
            RING["filled"] = min(n, RING["filled"] + k)
            RING["t_end"] = now

    try:
        with sd.InputStream(device=idx, channels=nch, samplerate=sr, blocksize=int(sr / 10), callback=cb):
            while True:
                time.sleep(1)
    except Exception as e:  # noqa: BLE001
        say(f"sound: stopped ({e})")


def bits(b8):
    v = int.from_bytes(b8, "big")
    return ",".join(str(s + 1) for s in range(64) if v >> s & 1) or "-"


def tracks(w):
    return "".join(str(t + 1) for t in range(8) if w >> (8 + t) & 1) or "-"


def solos(w):
    return "".join(str(t + 1) for t in range(8) if w >> t & 1) or "-"


def tempo_line(starts, length, audio=None):
    """The chops' tempo: the READING first, both octaves rounded to whole BPM ('82 (or 163)') with the decimals
    beside it; with the take's audio (main out while it played under LAZY CHOP) the chops are checked against the
    music's beat and the confidence is a word (high / medium / low). The other methods follow."""
    if not chop_tempo or len(starts) < 3:
        return None
    e = chop_tempo.estimate(SR, starts, length)
    gg = e.get("gapgrid")
    parts = []
    rd = e.get("reading")
    cb = None
    if audio is not None and hasattr(chop_tempo, "combined"):
        try:
            cb = chop_tempo.combined(SR, starts, length, audio[0], audio[1], rd, e.get("gapgrid-noloop"))
        except Exception as ex:  # noqa: BLE001
            parts.append(f"(audio check failed: {ex})")
    if cb:
        au = f"audio {cb['audio'][0]} (support {cb['audio'][1]})" if cb["audio"] else "audio: no beat"
        parts.append(f"READING {chop_tempo.shown(cb)}  confidence {cb['conf'].upper()}, {cb['how']}"
                     f"  [{cb['bpm']} / {cb['or']}; chops {cb['chops']}; {au}]")
    if rd:
        lead = "chops" if cb else "READING " + (chop_tempo.shown(rd) if hasattr(chop_tempo, "shown") else str(rd["bpm"]))
        parts.append(f"{lead} [{rd['bpm']} / {rd['or']}; conf {rd['conf']}; {rd['how']}]")
    if gg:
        how = f"loop = {gg['loop_bars']} bars -> {gg['loop_bpm']}" if gg["loop_bars"] else "spacing only (length is not whole bars)"
        parts.append(f"{gg['bpm']} BPM (or {gg['or']}; conf {gg['conf']}; runner-up {gg['runner_up']}; {how})")
    if hasattr(chop_tempo, "unit_display"):
        try:
            ud = chop_tempo.unit_display(SR, starts, length)
            if ud:
                parts.append(f"UNIT SCREEN [{ud['line']}]")
        except Exception as ex:  # noqa: BLE001
            parts.append(f"(unit reading failed: {ex})")
    gl, g = e.get("grid+loop"), e.get("grid")
    if gl:
        parts.append(f"grid+loop {gl['bpm']}")
    if e.get("median"):
        parts.append(f"median {e['median']:.2f}")
    return "TEMPO " + " | ".join(parts)


def evidence_summary(logp):
    """At exit: what this session exercised, per module, for the PRs' Hardware sections."""
    e = EVID
    lines = [f"# Evidence: {e['start']} -> {time.strftime('%Y-%m-%d %H:%M:%S')}, Octatrack MKI (8thmyth), image {e['image']}",
             f"- LAZY CHOP: {e['chops']} chops closed with YES; ZERO SNAP debug: {e['snap_taps']} taps, {e['snap_moved']} moved by the snap",
             f"- LAZY CHOP TEMPO: {len(e['tempo'])} popups" + (f"; YES -> popup ready {min(e['yes_ms'])}-{max(e['yes_ms'])} ms (100 ms polling)" if e['yes_ms'] else ""),
             *[f"    {x}" for x in e["tempo"]],
             f"- LOCK EDIT: {len(e['lockedit'])} [TRIG]+[BANK] opens", *[f"    {x}" for x in e["lockedit"]],
             f"- ZOOM KEEP: per-page zooms kept across track switches {e['zoom_kept']}, reset {e['zoom_reset']}; zoom edits "
             + ", ".join(f"{k} {v}" for k, v in e["zoom_changes"].items()),
             f"- SRC3 KEY: {len(e['src3'])} recordings started with the editor open (of {e['rec_starts']} recorder starts)",
             *[f"    {x}" for x in e["src3"]],
             f"- FREEZES: {e['freezes']}"]
    try:
        p = logp.parent / "ot-monitor-captures" / f"evidence_{time.strftime('%Y%m%d_%H%M%S')}.md"
        p.write_text("\n".join(lines) + "\n")
        say("EVIDENCE summary -> " + p.name)
        for ln in lines:
            say(ln)
    except Exception:  # noqa: BLE001
        pass


def shot(scr, tag):
    """A screenshot without stopping the state polling (the reply takes up to 1.5 s)."""
    if scr:
        threading.Thread(target=scr.grab, args=(tag,), daemon=True).start()


def main():
    global LOG, CAP
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    quiet = "--quiet" in sys.argv
    logp = pathlib.Path(args[0]) if args else pathlib.Path("ot-monitor.log")
    LOG = open(logp, "a")
    CAP = logp.parent / "ot-monitor-captures"
    CAP.mkdir(exist_ok=True)
    dev = load_meter.libusb_device()
    pk = ot_events.unit_peek(dev)
    ctrl = ot_events.unit_ctrl(dev)

    def rd(a, n):
        return ot_events.peek(pk, a, n)

    def long(a):
        return struct.unpack(">I", rd(a, 4))[0]

    say(f"ot_monitor v2: connected{' (QUIET: load meter only)' if quiet else ''}")
    try:
        ev = ot_events.Events(ctrl)
    except Exception as e:  # noqa: BLE001
        ev = None
        say(f"events: REMOTE ring not read ({e})")
    lc = None
    for tag, syms in (("TA", LC_SYMS_TA), ("T9", LC_SYMS_T9), ("T8", LC_SYMS_T8)):
        try:
            if long(syms["lc_state"]) > 2 or long(syms["lc_snap"]) > 1 or long(syms["lc_live"]) > 1:
                continue
            if "ct_state" in syms and long(syms["ct_state"]) > 3:
                continue
            lc = dict(syms)
            EVID["image"] = tag + (" (METALFNGRZ)" if tag in ("TA", "T9") else "")
            say(f"lazy chop: {tag} addresses")
            break
        except Exception:  # noqa: BLE001
            continue
    if lc is None:
        say("lazy chop: this image's variables are not at TA's, T9's or T8's addresses: LAZY CHOP not monitored")
    say(f"EVIDENCE session: {EVID['start']}  unit MKI (8thmyth)  image {EVID['image']}  log {logp.name}")
    import atexit
    import signal
    atexit.register(evidence_summary, logp)
    for sg in (signal.SIGHUP, signal.SIGTERM):      # closing the Terminal window still writes the summary
        signal.signal(sg, lambda *_: sys.exit(0))
    scr = Screen() if not quiet else None
    if not quiet:
        threading.Thread(target=sound, daemon=True).start()

    prev, vprev = {}, [None] * 8
    lcprev, lcslices = None, None
    dbgprev, ctprev = None, None    # T9: ZERO SNAP debug tap count, tempo popup state
    aedprev, viewprev, trkprev, recprev = 0, {}, None, [0] * 8
    yes_t = None            # wall time a chop closed (YES): the tempo popup's delay is measured from it
    chop_t0 = None          # wall time LAZY CHOP started playing the take (state -> chopping)
    lm_last, lm_line, lm_seq, lm_seq_t, frozen = 0.0, 0.0, None, time.time(), False
    backoff_until, interval = 0.0, (0.5 if quiet else 0.1)
    fails, last_screen = 0, time.time()

    def dump_trace(tag):
        try:
            tr = ot_events.trace(ctrl, pk)
            p = CAP / f"trace_{time.strftime('%H%M%S')}_{tag}.txt"
            p.write_text("".join(f"{n} {pc:08x} {st}\n" for n, pc, st in tr))
            say(f"trace: {p.name} ({len(tr)} frame interrupts)")
        except Exception as e:  # noqa: BLE001
            say(f"trace: not read ({e})")

    while True:
        t0 = time.time()
        # ---- the load meter: heartbeat, safety valve, a line every 10 s ----
        if t0 - lm_last >= 0.5:  # (SAFETY VALVE below: the unit's normal longest run playing with USB audio is 310-360 us)
            lm_last = t0
            try:
                r = load_meter.read_fast(dev)
                cf, dsp = r["cf"], r["dsp"]
                if lm_seq is None or dsp["seq"] != lm_seq:
                    if frozen:
                        say("RECOVERED: the DSP frame count moves again")
                        frozen = False
                    lm_seq, lm_seq_t = dsp["seq"], t0
                elif t0 - lm_seq_t > 1.5 and not frozen:
                    frozen = True
                    say("FREEZE: the DSP frame count stopped (audio engine), USB still answers")
                    EVID["freezes"] += 1
                    dump_trace("freeze")
                    try:
                        full = load_meter.read(dev)
                        (CAP / f"loadmeter_{time.strftime('%H%M%S')}_freeze.txt").write_text(repr(full))
                    except Exception:  # noqa: BLE001
                        pass
                    shot(scr, "freeze")
                longest = cf["longest_us"] or 0.0
                if longest > 420 and t0 > backoff_until:
                    say(f"SAFETY VALVE: frame interrupt's longest run {longest:.0f} us: polling slowed to 2/s for 30 s")
                    backoff_until = t0 + 30
                if t0 - lm_line >= 10:
                    lm_line = t0
                    c0 = load_meter.ratio(dsp["core0"]) or 0.0
                    c1 = load_meter.ratio(dsp["core1"]) or 0.0
                    say(f"LOAD cf {100 * (cf['share'] or 0):.1f}%  longest {longest:.0f} us  irq {cf['irq_per_s'] or 0:.0f}/s"
                        f"  DSP A {100 * c0:.0f}%  B {100 * c1:.0f}%  bad {dsp['bad']}")
            except Exception as e:  # noqa: BLE001
                if getattr(e, "errno", None) != 32:
                    pass
        if quiet:
            time.sleep(max(0.0, interval - (time.time() - t0)))
            continue
        # ---- events ----
        try:
            if ev:
                for rec in ev.poll():
                    say(f"EVENT {rec['name']} {rec['arg']}")
            # ---- the sequencer ----
            pat = rd(0x80000004, 1)[0]
            bank = long(0x46C82456) + pat * 0x8ED8
            wa, wm = long(0x80000008), long(0x8000000C)
            bp = rd(0x800065BD, 2)
            st = {
                "pattern": f"{'ABCDEFGHIJKLMNOP'[bp[0] & 15]}{bp[1] + 1:02d}",
                "playing": rd(0x800065BB, 1)[0],
                "track": f"T{(rd(0x100B14CC, 1)[0] & 7) + 1}",
                "mutes": tracks(wa), "solos": solos(wa), "midimutes": tracks(wm), "midisolos": solos(wm),
                "mutelayer": "PATTERN" if rd(0x100B14DD, 1)[0] & 1 else "GLOBAL",
                "spent": rd(0x8000184E, 2).hex(), "hold": rd(0x46C7FF64, 2).hex(),
                "armreq": rd(0x46C803D4, 2).hex(), "disarm": rd(0x46C7FE22, 2).hex(),
            }
            for t in range(8):
                rec = bank + 2330 * t
                m = rd(rec, 0x20)
                cond = rd(rec + 0x89A, 129)          # never a multiple of 64 (TRAPS trap-ep0-dma)
                st[f"T{t + 1}trigs"] = bits(m[0:8])
                st[f"T{t + 1}oneshot"] = bits(m[24:32])
                st[f"T{t + 1}tmute"] = ",".join(str(s + 1) for s in range(64) if (cond[2 * s + 1] & 127) > 64) or "-"
            vraw = rd(0x800049D8, 0xA8 * 8 - 1)
            # ---- LAZY CHOP ----
            lcs = None
            if lc:
                state = long(lc["lc_state"])
                lcs = {"state": LC_STATES.get(state, state), "track": f"T{long(lc['lc_track']) + 1}",
                       "next": long(lc["lc_next"]), "snap": "ON" if long(lc["lc_snap"]) else "off",
                       "mode": "LIVE" if long(lc["lc_live"]) else "STUDIO"}
                if state == 2 and (lcprev is None or lcprev.get("state") != "chopping"):
                    chop_t0 = time.time()
                recp = long(lc["lc_rec"])
                okrec = 0x40000000 <= recp < 0x48000000 or 0x10000000 <= recp < 0x100FF000   # SDRAM or CS1
                # read while armed/chopping, and once more after it closes (YES has written the final slices)
                final = (not state) and lcprev is not None and lcprev.get("state") != "off"
                if final:
                    yes_t = time.time()
                    EVID["chops"] += 1
                first = lcprev is None
                if "lc_dbg_n" in lc:
                    n_tap = long(lc["lc_dbg_n"])
                    if dbgprev is not None and n_tap != dbgprev:
                        tap, snp = long(lc["lc_dbg_tap"]), long(lc["lc_dbg_snap"])
                        mv = (snp - tap) if snp >= tap else -(tap - snp)
                        EVID["snap_taps"] += 1
                        EVID["snap_moved"] += 1 if mv else 0
                        say(f"ZERO SNAP debug: tap {n_tap}: frame {tap} -> {snp} (moved {mv:+d} frames, {mv / SR * 1000:+.1f} ms)"
                            f"{'' if lcs['snap'] == 'ON' else ' (snap off)'}")
                    dbgprev = n_tap
                if "ct_state" in lc:
                    cst = long(lc["ct_state"])
                    if cst != ctprev:
                        if cst in (1, 2):
                            txt = rd(lc["ct_buf"], 24).split(b"\0")[0].decode("ascii", "replace")
                            if cst == 1:
                                dly = f"  {1000 * (time.time() - yes_t):.0f} ms after the chop closed (poll ~100 ms: coarse)" if yes_t else ""
                                if yes_t:
                                    EVID["yes_ms"].append(round(1000 * (time.time() - yes_t)))
                                EVID["tempo"].append(f"{stamp()} {txt}")
                                say(f"EVIDENCE tempo: {txt}{dly}")
                            say(f"TEMPO POPUP {CT_STATES.get(cst, cst)}: [{txt}]  (c {long(lc['ct_c'])}, bracket {long(lc['ct_br'])})")
                        elif ctprev in (1, 2):
                            say("TEMPO POPUP closed")
                        ctprev = cst
                if okrec and (state or final or first):
                    head = rd(recp + 300, 8)
                    ts, te = struct.unpack(">II", head)
                    cnt = struct.unpack(">I", rd(recp + 1092, 4))[0]
                    cnt = min(cnt, 64)
                    raw = rd(recp + 312, max(12 * cnt, 12) + (1 if (12 * cnt) % 64 == 0 else 0))
                    starts = [struct.unpack(">I", raw[12 * i:12 * i + 4])[0] for i in range(cnt)]
                    if state == 2 and starts:
                        starts = starts[1:]            # entry 0 is the whole take while chopping
                    sl = (ts, te, tuple(starts))
                    if sl != lcslices:
                        lcslices = sl
                        say(f"LC slices{' (final)' if final else ' (in record)' if first and not state else ''} {len(starts)} (trim {ts}-{te}, {(te - ts) / SR:.3f} s): "
                            + " ".join(str(s) for s in starts))
                        aud = audio_since(chop_t0, (te - ts) / SR) if chop_t0 else None
                        line = tempo_line(list(starts), te - ts, aud)
                        if line:
                            say(line)
            # ---- EVIDENCE: the editor, LOCK EDIT, ZOOM KEEP, SRC3 KEY ----
            aed = long(AED_WIN)
            trk = rd(0x100B14CC, 1)[0] & 7
            if aed and not aedprev:
                held = struct.unpack(">H", rd(HELD_TRIGS, 2))[0]
                if rd(GRID_REC, 1)[0] and held:
                    page = struct.unpack(">H", rd(HELD_PAGE, 2))[0]
                    steps = [page + i + 1 for i in range(16) if held >> i & 1]
                    EVID["lockedit"].append(f"{stamp()} T{trk + 1} step {steps}")
                    say(f"EVIDENCE LOCK EDIT: [TRIG]+[BANK] opened the editor on T{trk + 1} step {steps}")
                else:
                    say(f"EDITOR opened on T{trk + 1}")
            elif aedprev and not aed:
                say("EDITOR closed")
            if aed:
                views = {}
                for name, va in AED_VIEWS.items():
                    fpp, _, first = struct.unpack(">III", rd(va + 0x10, 12))
                    views[name] = (fpp, first)
                if trkprev is not None and trk != trkprev and viewprev:
                    kept = [n for n in views if views[n][0] == viewprev[n][0]]
                    EVID["zoom_kept"] += len(kept)
                    EVID["zoom_reset"] += len(views) - len(kept)
                    say(f"EVIDENCE ZOOM KEEP: track T{trkprev + 1} -> T{trk + 1}: "
                        + "  ".join(f"{n} zoom {viewprev[n][0]}->{views[n][0]} first {viewprev[n][1]}->{views[n][1]}" for n in views)
                        + f"  ({len(kept)}/3 zooms kept)")
                elif viewprev:
                    for n in views:
                        if views[n][0] != viewprev[n][0]:
                            EVID["zoom_changes"][n] += 1
                            say(f"ZOOM {n} on T{trk + 1}: {viewprev[n][0]} -> {views[n][0]} frames/pixel (first {views[n][1]})")
                viewprev = views
            trkprev, aedprev = trk, aed
            for t in range(8):
                r = vraw[0xA8 * t + 1]
                if r and not recprev[t]:
                    EVID["rec_starts"] += 1
                    if aed:
                        EVID["src3"].append(f"{stamp()} T{t + 1}")
                        say(f"EVIDENCE SRC3 KEY?: recorder T{t + 1} started with the editor open (an [MIDI] press on an MKI with MIDI = SRC3)")
                    else:
                        say(f"recorder T{t + 1} started")
                elif recprev[t] and not r:
                    say(f"recorder T{t + 1} stopped")
                recprev[t] = r
            if fails >= 3:
                say("UNIT ANSWERS AGAIN")
                dump_trace("after-silence")
            fails = 0
        except Exception as e:  # noqa: BLE001
            fails += 1
            if fails == 3:
                say(f"UNIT NOT ANSWERING ({e})")
            time.sleep(1 if fails >= 3 else 0.2)
            continue
        ch = {k: v for k, v in st.items() if prev.get(k) != v}
        if ch:
            say("  ".join(f"{k}={v}" for k, v in (st.items() if not prev else ch.items())))
        if lcs and lcs != lcprev:
            say("LC " + "  ".join(f"{k}={v}" for k, v in lcs.items()))
            if lcprev is not None:
                shot(scr, "lazychop")
        if "pattern" in ch and prev:
            shot(scr, "pattern")
        prev, lcprev = st, lcs
        for t in range(8):
            o = 0xA8 * t
            act, pos = vraw[o], struct.unpack(">I", vraw[o + 68:o + 72])[0]
            if vprev[t] is not None:
                pact, ppos = vprev[t]
                if act and not pact:
                    say(f"voice T{t + 1} START pos={pos}")
                elif act and pos + 2000 < ppos:
                    say(f"voice T{t + 1} RESTART pos={pos}")
                elif pact and not act:
                    say(f"voice T{t + 1} END pos={ppos}")
            vprev[t] = (act, pos)
        if scr and time.time() - last_screen > 60:
            last_screen = time.time()
            shot(scr, "periodic")
        iv = 0.5 if time.time() < backoff_until else interval
        time.sleep(max(0.0, iv - (time.time() - t0)))


if __name__ == "__main__":
    main()
