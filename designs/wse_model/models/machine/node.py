# ruff: noqa: N802, F823, F841 -- pyCircuit declarations and captured state.
"""Addressed node context for the serialized WSE machine.

The machine admits data only after POST and preflight. Its instruction set
has no independent early-delivery input. The standalone Calendar receiver
continues to implement that interface and its pending queue.
"""

import pycircuit as ac


@ac.struct
class NodeContext:
    installed: ac.u8
    epochs: ac.u64
    active: bool
    opcode: ac.u3
    epoch: ac.u8
    dst: ac.u32
    capacity: ac.u32
    expected: ac.u32
    counted: ac.u32
    identity_count: ac.u6


def pack(row: NodeContext) -> ac.bits[256]:
    active: ac.u1 = 1 if row.active else 0
    return ac.concat(
        row.identity_count, row.counted, row.expected, row.capacity, row.dst,
        row.epoch, row.opcode, active, row.epochs, row.installed,
    )


def unpack(word: ac.bits[256]) -> NodeContext:
    return NodeContext(
        installed=word[0:8], epochs=word[8:72], active=word[72:73] != 0,
        opcode=word[73:76], epoch=word[76:84], dst=word[84:116],
        capacity=word[116:148], expected=word[148:180], counted=word[180:212],
        identity_count=word[212:218],
    )


def observation(row: NodeContext) -> ac.u64:
    complete: ac.u1 = 1 if row.active and row.counted >= row.expected else 0
    active: ac.u1 = 1 if row.active else 0
    return ac.concat(row.epoch, complete, active, row.counted)


@ac.module
def NodeStore():
    # Separate read port for the host. Mirror just the externally visible fields.
    # Row zero absorbs the continuous read port when no transaction issues.
    # Logical nodes 0..39 occupy rows 1..40 in each memory.
    contexts = ac.byte_mem[ac.bits[256]](depth=1312)
    observations = ac.byte_mem[ac.bits[64]](depth=328)
    identities = ac.byte_mem[ac.bits[2048]](depth=10496)
    # RAM contents survive reset. First access after reset sees an empty context.
    initialized: ac.u40 = 0

    @ac.method
    def load(node: ac.u6) -> NodeContext:
        index: ac.u11 = node
        word: ac.bits[256] = 0
        if ((initialized >> node) & 1) != 0:
            word = contexts.read((index + 1) * 32)
        return unpack(word)

    @ac.method
    def save(node: ac.u6, row: NodeContext) -> None:
        index: ac.u11 = node
        contexts.write((index + 1) * 32, pack(row))
        view = observation(row)
        observe_index: ac.u9 = node
        observations.write((observe_index + 1) * 8, view)
        one: ac.u40 = 1
        initialized = initialized | (one << node)

    @ac.method
    def observe(node: ac.u6) -> ac.u64:
        index: ac.u9 = node
        view: ac.u64 = 0
        if ((initialized >> node) & 1) != 0:
            view = observations.read((index + 1) * 8)
        return view

    @ac.method
    def contains(node: ac.u6, count: ac.u6, identity: ac.bits[22]) -> bool:
        index: ac.u14 = node
        entries = identities.read((index + 1) * 256)
        found = False
        for slot in range(56):
            pos: ac.u16 = slot
            entry = entries >> (pos * 32)
            if slot < count and entry[0:22] == identity:
                found = True
        return found

    @ac.method
    def append(node: ac.u6, slot: ac.u6, identity: ac.bits[22]) -> None:
        index: ac.u14 = node
        pos: ac.u16 = slot
        data: ac.bits[2048] = identity
        mask: ac.bits[256] = 15
        identities.write((index + 1) * 256, data << (pos * 32), mask=mask << (pos * 4))

    @ac.method
    def publish(node: ac.u6, row: NodeContext, changed: bool, observer: ac.u6) -> ac.u64:
        view = observe(observer)
        if changed:
            save(node, row)
        return observation(row) if changed and node == observer else view
