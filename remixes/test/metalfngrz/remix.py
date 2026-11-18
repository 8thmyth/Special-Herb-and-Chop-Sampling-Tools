"""METALFNGRZ (test remix) -- 8thmyth's firmware: octabam-lazy-chop (LAZY CHOP, LOCK EDIT,
ZOOM KEEP, SRC3 KEY on octabam-verbs) unchanged, with LAZY CHOP TEMPO and Hteah's TRACK TRIG MODE,
PAGE LOOP and TRIG MUTE added. Each stays its own module; this remix only composes them."""
import dataclasses

from remix import registry

_BASE = registry.remix("octabam-lazy-chop")

REMIX = dataclasses.replace(
    _BASE,
    name="METALFNGRZ",
    image_name="METALFNGRZ",
    proof_note="octabam-lazy-chop's, plus verify_track_trig_mode, verify_page_loop, verify_trig_mute and verify_chop_tempo under the port; not yet on a unit",
    doc="octabam-lazy-chop with LAZY CHOP TEMPO (the BPM popup after LAZY CHOP's slice count), TRACK TRIG MODE (each audio track keeps its CHROMATIC / SLOTS / SLICES mode, saved with the project), PAGE LOOP (loop one page of one track) and TRIG MUTE (mute a trig without deleting it); without PATTERN MUTE (it shares the one-shot mask's byte).",
    # PATTERN MUTE is left out (16 Nov 2026): it keeps its per-pattern mute in bit 0/1 of byte 31 of each
    # track's pattern record, which is the one-shot mask's steps 1 and 2 (bytes 24-31 = one-shots). A
    # one-shot on step 1 muted its track at the next mute write, and a PATTERN-layer mute edit deleted or
    # created step-1 one-shots on every track (owner's MKI over REMOTE, 4 captures; reproduced under the port).
    modules=tuple(m for m in _BASE.modules if m != "PATTERN MUTE") + ("TRACK TRIG MODE", "PAGE LOOP", "TRIG MUTE", "LAZY CHOP TEMPO", "DARK MODE"),
)
