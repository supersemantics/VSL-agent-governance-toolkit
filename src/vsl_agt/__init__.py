"""vsl-agt: VSL gates and the VERBA ledger inside Microsoft's Agent Governance Toolkit.

Register a VSLInterceptor on an Agent Hooks InterceptionEmitter (the
contract AGT's framework adapters emit) and VSL PreNodes and Invariants run
before each named tool call, every decision and every call is recorded in a
VerbaLedger, and VerbaLedger.enforcement(decision_id) shows from record
order whether the gates decided first.
"""

import vsl_core as _vsl_core

from .adapter import AGTAdapter
from .agt_native import TIMEOUT_REASON, UNMEDIATED_PREFIX, VSLToolCallInterceptor
from .interceptor import (
    EVIDENCE_ARTEFACT,
    INTERCEPTION_POINTS,
    REASON_PREFIX,
    VSLInterceptor,
    decision_id_of,
    evaluate,
    hook_link,
)

__version__ = "0.1.0"

# write_action / enforcement arrived in vsl-core 0.4.0.
_MIN_VSL_CORE = (0, 4, 0)
_found = tuple(int(part) for part in _vsl_core.__version__.split(".")[:3])
if _found < _MIN_VSL_CORE:
    raise ImportError(
        f"vsl-agt {__version__} needs vsl-core >= 0.4.0, found {_vsl_core.__version__}. "
        "Upgrade: pip install --upgrade super-semantics-vsl"
    )

__all__ = [
    "AGTAdapter",
    "VSLInterceptor",
    "VSLToolCallInterceptor",
    "TIMEOUT_REASON",
    "UNMEDIATED_PREFIX",
    "INTERCEPTION_POINTS",
    "REASON_PREFIX",
    "EVIDENCE_ARTEFACT",
    "decision_id_of",
    "evaluate",
    "hook_link",
]
