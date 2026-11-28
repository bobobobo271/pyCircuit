# CoarseWseMachine v1 contract

This model accepts valid address-free transfers over a fixed 5×8 mesh. A route
is a directed tree rooted at the source; targets can also forward. Transfer IDs
are unique. The host provides metadata, release times and explicit dependency
and completion groups. It never provides calculated arrival times.

The generated model arbitrates one flit per directed link per modeled tick and
one ejection flit per destination across all ingress directions. A source has
one active packet, eight waiting descriptors, and one injection flit held until
all root branches accept it. Link service occupancy and propagation latency are
separate. Multicast shares the trunk and independently arbitrates each branch.
The current profile uses 52 useful bytes per 64-byte wire flit. Tail padding
consumes a complete link service slot.

Endpoints read an entire packet before injection and write after full receipt.
Read and write ports are independent. Their first-beat and subsequent-beat
budgets come from the runtime profile. A completion publishes logical identity
and bytes after destination write service. A local self-copy consumes write
service and receive capacity but no network link traffic.

Calendar permission masks are indexed by directed link and slot. Closed slots
wait for a permitted slot. WaitGroup observes committed receive completions;
Barrier drains all pending work, applies the configured alignment/arm budget,
and establishes a new Calendar origin. RetireGroup releases reserved group
capacity. Queue occupancy applies backpressure; receive-capacity excess is an
explicit fault. Faulted transfers must not publish false completion.

The event heap is bounded, and equal-time ties follow stable insertion order.
Internal heap and metadata maintenance does not advance model time. Trace kinds
3 and 9 denote accepted link/ejection service; retry kinds 11/12 must not be
counted as transfers. Kind5 is receiver completion, kind10 is a barrier, and kind13 is a fault event.

The model verifies communication identity, bytes and order. It does not produce
tensor values. Timing parameters are model assumptions, not silicon measurements.
GFSIM and Verilator must use the same final intermediate artifact and the same
loader bytes. Verification conditions and independent expectations are in
verification/coarse_cases.py and tests/test_coarse.py. Every claim of a pass must
reference actual execution receipts; removed legacy tests are not passes.
