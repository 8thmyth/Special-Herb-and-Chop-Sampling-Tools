"""METALFNGRZ + STEP VIEW (test remix) -- our main build (metalfngrz) unchanged, with
Slingdinger's STEP VIEW added. HOOKS joins SRC3 KEY and STEP VIEW on the MKI [MIDI]
key (midi_key); STEP VIEW's MKI FUNC + BANK shortcut is left out (SEQ's)."""
import dataclasses

from remix import registry

_BASE = registry.remix("metalfngrz")

REMIX = dataclasses.replace(
    _BASE,
    name="metalfngrz-step",
    image_name="METALFNGRZ",
    proof_note="metalfngrz's gates and verify_step_view under the port; not yet on a unit",
    doc="METALFNGRZ with Slingdinger's STEP VIEW (MIXER + MIDI: a step view and editor for the 8 audio and 8 MIDI tracks).",
    modules=_BASE.modules + ("STEP VIEW",),
)
