"""efaktura-radar — hvem kan motta EHF-faktura?

Teknisk spike for e-fakturaplikten som trer i kraft 1. januar 2027
(bokføringsloven § 10 annet ledd, lov 19. juni 2026 nr. 39).
"""

from .check import CheckResult, Status, check, check_many
from .orgnr import InvalidOrgnr, is_valid, normalise
from .sml import SmlResult, naptr_hostname, participant_id, resolve_smp

__version__ = "0.1.0"
__all__ = [
    "CheckResult", "InvalidOrgnr", "SmlResult", "Status",
    "check", "check_many", "is_valid", "naptr_hostname",
    "normalise", "participant_id", "resolve_smp",
]
