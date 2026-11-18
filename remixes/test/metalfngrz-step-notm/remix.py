"""A/B scratch: metalfngrz-step without TRACK TRIG MODE."""
import dataclasses
from remix import registry
_BASE = registry.remix("metalfngrz-step")
REMIX = dataclasses.replace(
    _BASE,
    name="metalfngrz-step-notm",
    doc="metalfngrz-step with TRACK TRIG MODE removed (garble A/B only)",
    modules=tuple(m for m in _BASE.modules if m != "TRACK TRIG MODE"),
)
