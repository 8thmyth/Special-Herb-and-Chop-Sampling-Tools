#!/usr/bin/env python3
"""STEP VIEW in 8thmyth's build, under the ColdFire port (the [MIDI] key it shares with SRC3 KEY, modules/hooks midi_key):
  stepview  MKI: MIXER + MIDI opens STEP VIEW outside the audio editor, NO leaves it; [MIDI] alone is stock's;
            in the editor [MIDI] is SRC3 KEY's and STEP VIEW stays shut
  stepmkii  MKII: MIXER + MIDI opens STEP VIEW (the MKII's [MIDI] is STEP VIEW's alone), [MIDI] alone is stock's

    python3 tools/verify/verify_step_view.py [REMIX] [scenario ...]

STEP VIEW's own screens are its author's (github.com/Slingdinger/octatrack-step-view); this checks only the keys
this build changed. The harness, project and cards are verify_lazy_chop.py's.
"""
import os, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
os.environ.setdefault("LAZY_CHOP_OUT", "out/step_view_verify")
import verify_lazy_chop  # noqa: E402

if __name__ == "__main__":
    sys.exit(verify_lazy_chop.main(sys.argv[1:], gate="step_view"))
