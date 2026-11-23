"""The NoC: flit format, resident ``CalReg``, passive forwarding, and delivery."""

from wse_model.noc.calreg import CalRegBank, CalRegImage, CalRegSlot, StopTheWorldWindow
from wse_model.noc.flit import (
    DEFAULT_EPOCH_TAG_BITS,
    DEFAULT_LINK_WIDTH_BYTES,
    Flit,
    FlitHeader,
    flit_count,
    split_payload,
)
from wse_model.noc.forward import (
    Hop,
    Landing,
    MulticastTrace,
    delivery_order,
    link_load,
    simulate_multicast,
)
from wse_model.noc.network import Noc, SendOutcome, SendPlan
from wse_model.noc.timing import (
    LinkTiming,
    NocTiming,
    ParameterStatus,
    PhaseTiming,
    SourceTiming,
    TimedSend,
    TimingParameter,
    TwoPhaseTiming,
    time_allgather,
    time_two_phase_allgather,
)

__all__ = [
    "CalRegBank",
    "CalRegImage",
    "CalRegSlot",
    "DEFAULT_EPOCH_TAG_BITS",
    "DEFAULT_LINK_WIDTH_BYTES",
    "Flit",
    "FlitHeader",
    "Hop",
    "Landing",
    "LinkTiming",
    "MulticastTrace",
    "Noc",
    "NocTiming",
    "ParameterStatus",
    "PhaseTiming",
    "SendOutcome",
    "SendPlan",
    "SourceTiming",
    "StopTheWorldWindow",
    "TimedSend",
    "TimingParameter",
    "TwoPhaseTiming",
    "delivery_order",
    "flit_count",
    "link_load",
    "simulate_multicast",
    "split_payload",
    "time_allgather",
    "time_two_phase_allgather",
]
