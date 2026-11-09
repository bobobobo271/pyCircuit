# ruff: noqa: N802, F821, F823, F841 -- captured state and forward module connections.
"""Serialized rank-major AllGather with native storage and committed accounting.

Each participant owns a 64 KiB byte memory and its own receive account. Runtime
commands contain 512 transport fragments, not logical Calendar tensor rows.
Only this fixed fully-posted, ordered, single-fragment profile is admitted.
"""

import pycircuit as ac
from wse_calendar.forward import ForwardHop, HopInput, HopResult


@ac.struct
class HostRequest:
    kind: ac.u3
    node: ac.u5
    address: ac.u16
    data: ac.u64
    landing_permit: ac.u32


@ac.struct
class Transfer:
    stage: ac.u4
    phase: ac.u1
    identity: ac.u9
    source: ac.u5
    fragment: ac.u3
    nbytes: ac.u16
    src_address: ac.u16
    dst_address: ac.u16
    route: ac.bits[80]
    offset: ac.u16
    write: bool


@ac.struct
class NodeResult:
    word: ac.u64
    counted_b: ac.u32
    counted_c: ac.u32
    last_identity: ac.u10
    posted: bool
    reached: bool


@ac.struct
class Result:
    data: ac.u64
    counted_b: ac.u32
    counted_c: ac.u32
    last_identity: ac.u10
    stage: ac.u4
    descriptor: ac.u9
    offset: ac.u16
    done: bool


def hop_input(transfer: Transfer, position: ac.u3) -> HopInput:
    slot: ac.u6 = position
    column = transfer.phase == 1
    if column:
        slot = slot % 4
    source: ac.u6 = transfer.source
    base: ac.u6 = source & 7 if column else source & 24
    stride: ac.u6 = 8 if column else 1
    rank: ac.u6 = source[3:6] if column else source[0:3]
    node = base + stride * slot
    ingress = node + stride if slot < rank else node - stride
    if node == source:
        ingress = 0
    return HopInput(route=transfer.route, node=node, has_ingress=node != source, ingress=ingress)


def reached(transfer: Transfer, hops: ac.table[8, HopResult], node: ac.u5) -> bool:
    column = transfer.phase == 1
    source: ac.u6 = transfer.source
    base: ac.u6 = source & 7 if column else source & 24
    stride: ac.u6 = 8 if column else 1
    source_rank: ac.u3 = source[3:6] if column else source[0:3]
    node_wide: ac.u6 = node
    target: ac.u3 = node_wide[3:6] if column else node_wide[0:3]
    same_group = node[0:3] == source[0:3] if column else node[3:5] == source[3:5]
    path = same_group
    for index in range(8):
        rank: ac.u3 = index
        rank_wide: ac.u6 = index
        if rank >= source_rank and rank < target:
            peer = base + stride * (rank_wide + 1)
            path = path and not hops[index].fault and (hops[index].egress >> peer) & 1 != 0
        elif rank <= source_rank and rank > target:
            peer = base + stride * (rank_wide - 1)
            path = path and not hops[index].fault and (hops[index].egress >> peer) & 1 != 0
    return path and not hops[target].fault and hops[target].lands


@ac.module
def PayloadNode(
    host: HostRequest, transfer: Transfer, payload: ac.u64, node: ac.u5, landing: bool
) -> NodeResult:
    memory = ac.byte_mem[ac.u64](depth=65536)
    counted_b: ac.u32 = 0
    counted_c: ac.u32 = 0
    last_identity: ac.u10 = 1023
    posted: bool = False
    read_address = host.address if host.kind == 4 else transfer.src_address + transfer.offset

    @ac.rule
    def read() -> ac.u64:
        return memory.read(read_address)

    @ac.rule
    def store() -> None:
        initialize = host.kind == 1 and host.node == node
        copy = transfer.stage == 3 and transfer.write and (landing or node == transfer.source)
        if initialize or copy:
            destination: ac.u32 = transfer.dst_address
            byte_offset: ac.u32 = transfer.offset
            destination = destination + byte_offset
            address = host.address if initialize else destination[0:16]
            data = host.data if initialize else payload
            if initialize:
                assert transfer.stage == 0, "initial payload loading requires idle"
                assert host.address <= 1528 and host.address[0:3] == 0, "initial payload bounds"
            else:
                assert posted, "write requires posted receive"
                assert destination <= 65528, "destination memory bounds"
            memory.write(address, data)

    @ac.rule
    def account() -> None:
        identity: ac.u10 = transfer.identity
        nbytes: ac.u32 = transfer.nbytes
        if transfer.stage == 1:
            counted_b = 0
            counted_c = 0
            last_identity = 1023
            posted = True
        elif transfer.stage == 6:
            assert counted_b == 10752, "row complete before column post"
            last_identity = 1023
            posted = True
        elif transfer.stage == 4 and (landing or node == transfer.source):
            if node != transfer.source and last_identity != identity:
                if transfer.phase == 0:
                    counted_b = counted_b + nbytes
                    assert counted_b <= 10752, "row receive overflow"
                else:
                    counted_c = counted_c + nbytes
                    assert counted_c <= 36864, "column receive overflow"
            last_identity = identity
        elif transfer.stage == 7:
            assert counted_b == 10752 and counted_c == 36864, "final remote receive bytes"
            posted = False

    return NodeResult(
        word=read,
        counted_b=counted_b,
        counted_c=counted_c,
        last_identity=last_identity,
        posted=posted,
        reached=landing,
    )


@ac.module
def PathLanding(transfer: Transfer, hops: ac.table[8, HopResult], node: ac.u5) -> bool:
    @ac.rule
    def route() -> bool:
        return reached(transfer, hops, node)

    return route


def command_address(cursor: ac.u9) -> ac.u15:
    widened: ac.u15 = cursor
    return widened * 48


@ac.module
def DescriptorMemory(host: HostRequest, stage: ac.u4, cursor: ac.u9) -> ac.bits[384]:
    commands = ac.byte_mem[ac.bits[384]](depth=24576)
    address = command_address(cursor)

    @ac.rule
    def access() -> ac.bits[384]:
        raw = commands.read(address)
        if host.kind == 2:
            assert stage == 0, "descriptor loading requires idle"
            assert host.address < 24576 and host.address[0:3] == 0, "descriptor word bounds"
            value: ac.bits[384] = host.data
            commands.write(host.address[0:15], value, mask=255)
        return raw

    return access


def decode(
    command: ac.bits[384],
    stage: ac.u4,
    cursor: ac.u9,
    offset: ac.u16,
    host: HostRequest,
) -> Transfer:
    phase: ac.u1 = command[0:1]
    source: ac.u5 = command[32:37]
    fragment: ac.u3 = command[64:67]
    nbytes: ac.u16 = command[96:112]
    src_address: ac.u16 = command[128:144]
    dst_address: ac.u16 = command[160:176]
    route: ac.bits[80] = command[192:272]
    group_mask: ac.u32 = 0x01010101 if phase == 1 else 0xFF
    group_shift: ac.u5 = source & 7 if phase == 1 else source & 24
    one_source: ac.u32 = 1
    required_mask = (group_mask << group_shift) & ~(one_source << source)
    all_permitted = (host.landing_permit & required_mask) == required_mask

    return Transfer(
        stage=stage,
        phase=phase,
        identity=cursor,
        source=source,
        fragment=fragment,
        nbytes=nbytes,
        src_address=src_address,
        dst_address=dst_address,
        route=route,
        offset=offset,
        write=all_permitted,
    )


def hop_rows(
    h0: HopResult,
    h1: HopResult,
    h2: HopResult,
    h3: HopResult,
    h4: HopResult,
    h5: HopResult,
    h6: HopResult,
    h7: HopResult,
) -> ac.table[8, HopResult]:
    values: ac.table[8, HopResult] = (h0, h1, h2, h3, h4, h5, h6, h7)
    return values


def node_rows8(
    n0: NodeResult,
    n1: NodeResult,
    n2: NodeResult,
    n3: NodeResult,
    n4: NodeResult,
    n5: NodeResult,
    n6: NodeResult,
    n7: NodeResult,
) -> ac.table[8, NodeResult]:
    values: ac.table[8, NodeResult] = (n0, n1, n2, n3, n4, n5, n6, n7)
    return values


@ac.module
def SelectEight(
    n0: NodeResult,
    n1: NodeResult,
    n2: NodeResult,
    n3: NodeResult,
    n4: NodeResult,
    n5: NodeResult,
    n6: NodeResult,
    n7: NodeResult,
    index: ac.u3,
) -> NodeResult:
    values = node_rows8(n0, n1, n2, n3, n4, n5, n6, n7)
    return values[index]


@ac.module
def SelectNode(
    n0: NodeResult,
    n1: NodeResult,
    n2: NodeResult,
    n3: NodeResult,
    n4: NodeResult,
    n5: NodeResult,
    n6: NodeResult,
    n7: NodeResult,
    n8: NodeResult,
    n9: NodeResult,
    n10: NodeResult,
    n11: NodeResult,
    n12: NodeResult,
    n13: NodeResult,
    n14: NodeResult,
    n15: NodeResult,
    n16: NodeResult,
    n17: NodeResult,
    n18: NodeResult,
    n19: NodeResult,
    n20: NodeResult,
    n21: NodeResult,
    n22: NodeResult,
    n23: NodeResult,
    n24: NodeResult,
    n25: NodeResult,
    n26: NodeResult,
    n27: NodeResult,
    n28: NodeResult,
    n29: NodeResult,
    n30: NodeResult,
    n31: NodeResult,
    index: ac.u5,
) -> NodeResult:
    group0 = SelectEight(n0, n1, n2, n3, n4, n5, n6, n7, index[0:3])
    group1 = SelectEight(n8, n9, n10, n11, n12, n13, n14, n15, index[0:3])
    group2 = SelectEight(n16, n17, n18, n19, n20, n21, n22, n23, index[0:3])
    group3 = SelectEight(n24, n25, n26, n27, n28, n29, n30, n31, index[0:3])
    return (
        group0
        if index[3:5] == 0
        else group1 if index[3:5] == 1 else group2 if index[3:5] == 2 else group3
    )


@ac.struct
class Feedback:
    posted: bool
    complete_b: bool
    complete_c: bool


@ac.struct
class ControllerState:
    stage: ac.u4
    cursor: ac.u9
    offset: ac.u16
    payload: ac.u64


@ac.module
def Controller(
    host: HostRequest,
    command: ac.bits[384],
    source_word: ac.u64,
    feedback: Feedback,
    landings: ac.u32,
    current: ControllerState,
) -> ControllerState:
    transfer = decode(command, current.stage, current.cursor, current.offset, host)

    @ac.rule
    def advance() -> ControllerState:
        stage: ac.u4 = current.stage
        cursor: ac.u9 = current.cursor
        offset: ac.u16 = current.offset
        payload_word: ac.u64 = current.payload
        phase = transfer.phase
        source = transfer.source
        nbytes = transfer.nbytes
        route = transfer.route
        all_posted = feedback.posted
        complete_b = feedback.complete_b
        complete_c = feedback.complete_c
        if host.kind == 4:
            assert stage == 7, "readback requires done"
            assert (
                host.address >= 16384 and host.address <= 65528 and host.address[0:3] == 0
            ), "readback bounds"
        if stage == 0:
            if host.kind == 3:
                cursor = 0
                offset = 0
                stage = 1
        elif stage == 1 or stage == 6:
            stage = 2
        elif stage == 2:
            expected_phase: ac.u1 = cursor[8:9]
            expected_source: ac.u5 = cursor[3:8]
            expected_fragment: ac.u3 = cursor[0:3]
            expected_bytes: ac.u32 = 1536 if expected_phase == 1 else 192
            expected_base: ac.u32 = 16384 if expected_phase == 1 else 4096
            expected_source_wide: ac.u6 = expected_source
            rank: ac.u32 = (
                expected_source_wide[3:6] if expected_phase == 1 else expected_source_wide[0:3]
            )
            fragment_wide: ac.u32 = expected_fragment
            source_base: ac.u32 = 4096 if expected_phase == 1 else 0
            expected_src = source_base + fragment_wide * expected_bytes
            expected_dst = (
                expected_base + rank * (8 * expected_bytes) + fragment_wide * expected_bytes
            )
            expected_route: ac.bits[80] = 0x00000003000300030003 if expected_phase == 1 else 0xFFFF
            shift: ac.u7 = (
                expected_source_wide[0:3] if expected_phase == 1 else expected_source_wide[3:6]
            )
            shift_factor: ac.u7 = 2 if expected_phase == 1 else 16
            shift = shift * shift_factor
            expected_route = expected_route << shift
            one: ac.bits[80] = 1
            source_offset: ac.u7 = expected_source
            expected_route = expected_route & ~(one << (source_offset * 2))
            expected_phase_word: ac.u32 = expected_phase
            expected_source_word: ac.u32 = expected_source
            expected_fragment_word: ac.u32 = expected_fragment
            expected_remote: ac.u32 = 36864 if phase == 1 else 10752
            expected_capacity: ac.u32 = 49152 if phase == 1 else 12288
            assert command[0:32] == expected_phase_word, "descriptor phase order"
            assert command[32:64] == expected_source_word, "descriptor source order"
            assert command[64:96] == expected_fragment_word, "descriptor fragment order"
            assert command[96:128] == expected_bytes, "descriptor fragment byte count"
            assert command[128:160] == expected_src, "descriptor source address"
            assert command[160:192] == expected_dst, "descriptor destination address"
            assert (
                route == expected_route and command[272:288] == 0
            ), "descriptor exact 40-node route"
            assert command[288:320] == expected_remote, "descriptor expected receive bytes"
            assert command[320:352] == expected_base, "descriptor arena base"
            assert command[352:384] == expected_capacity, "descriptor arena capacity"
            assert all_posted, "all participants posted before send"
            group: ac.u32 = 0x01010101 if phase == 1 else 0xFF
            group_shift: ac.u5 = source & 7 if phase == 1 else source & 24
            one_member: ac.u32 = 1
            required = (group << group_shift) & ~(one_member << source)
            assert landings == required, "actual forwarding paths and landings"
            source_address: ac.u32 = transfer.src_address
            byte_offset: ac.u32 = offset
            source_address = source_address + byte_offset
            assert source_address <= 65528, "source memory bounds"
            payload_word = source_word
            stage = 3
        elif stage == 3:
            if transfer.write:
                if offset + 8 == nbytes:
                    stage = 4
                else:
                    offset = offset + 8
                    stage = 2
        elif stage == 4:
            stage = 5
        elif stage == 5:
            if cursor == 255:
                if complete_b:
                    cursor = cursor + 1
                    offset = 0
                    stage = 6
            elif cursor == 511:
                if complete_b and complete_c:
                    stage = 7
            else:
                cursor = cursor + 1
                offset = 0
                stage = 2

        return ControllerState(stage=stage, cursor=cursor, offset=offset, payload=payload_word)

    return advance


@ac.module
def Aggregate(
    n0: NodeResult,
    n1: NodeResult,
    n2: NodeResult,
    n3: NodeResult,
    n4: NodeResult,
    n5: NodeResult,
    n6: NodeResult,
    n7: NodeResult,
    n8: NodeResult,
    n9: NodeResult,
    n10: NodeResult,
    n11: NodeResult,
    n12: NodeResult,
    n13: NodeResult,
    n14: NodeResult,
    n15: NodeResult,
    n16: NodeResult,
    n17: NodeResult,
    n18: NodeResult,
    n19: NodeResult,
    n20: NodeResult,
    n21: NodeResult,
    n22: NodeResult,
    n23: NodeResult,
    n24: NodeResult,
    n25: NodeResult,
    n26: NodeResult,
    n27: NodeResult,
    n28: NodeResult,
    n29: NodeResult,
    n30: NodeResult,
    n31: NodeResult,
) -> Feedback:
    posted_0 = (
        n0.posted
        and n1.posted
        and n2.posted
        and n3.posted
        and n4.posted
        and n5.posted
        and n6.posted
        and n7.posted
    )
    posted_1 = (
        n8.posted
        and n9.posted
        and n10.posted
        and n11.posted
        and n12.posted
        and n13.posted
        and n14.posted
        and n15.posted
    )
    posted_2 = (
        n16.posted
        and n17.posted
        and n18.posted
        and n19.posted
        and n20.posted
        and n21.posted
        and n22.posted
        and n23.posted
    )
    posted_3 = (
        n24.posted
        and n25.posted
        and n26.posted
        and n27.posted
        and n28.posted
        and n29.posted
        and n30.posted
        and n31.posted
    )
    complete_b_0 = (
        n0.counted_b == 10752
        and n1.counted_b == 10752
        and n2.counted_b == 10752
        and n3.counted_b == 10752
        and n4.counted_b == 10752
        and n5.counted_b == 10752
        and n6.counted_b == 10752
        and n7.counted_b == 10752
    )
    complete_b_1 = (
        n8.counted_b == 10752
        and n9.counted_b == 10752
        and n10.counted_b == 10752
        and n11.counted_b == 10752
        and n12.counted_b == 10752
        and n13.counted_b == 10752
        and n14.counted_b == 10752
        and n15.counted_b == 10752
    )
    complete_b_2 = (
        n16.counted_b == 10752
        and n17.counted_b == 10752
        and n18.counted_b == 10752
        and n19.counted_b == 10752
        and n20.counted_b == 10752
        and n21.counted_b == 10752
        and n22.counted_b == 10752
        and n23.counted_b == 10752
    )
    complete_b_3 = (
        n24.counted_b == 10752
        and n25.counted_b == 10752
        and n26.counted_b == 10752
        and n27.counted_b == 10752
        and n28.counted_b == 10752
        and n29.counted_b == 10752
        and n30.counted_b == 10752
        and n31.counted_b == 10752
    )
    complete_c_0 = (
        n0.counted_c == 36864
        and n1.counted_c == 36864
        and n2.counted_c == 36864
        and n3.counted_c == 36864
        and n4.counted_c == 36864
        and n5.counted_c == 36864
        and n6.counted_c == 36864
        and n7.counted_c == 36864
    )
    complete_c_1 = (
        n8.counted_c == 36864
        and n9.counted_c == 36864
        and n10.counted_c == 36864
        and n11.counted_c == 36864
        and n12.counted_c == 36864
        and n13.counted_c == 36864
        and n14.counted_c == 36864
        and n15.counted_c == 36864
    )
    complete_c_2 = (
        n16.counted_c == 36864
        and n17.counted_c == 36864
        and n18.counted_c == 36864
        and n19.counted_c == 36864
        and n20.counted_c == 36864
        and n21.counted_c == 36864
        and n22.counted_c == 36864
        and n23.counted_c == 36864
    )
    complete_c_3 = (
        n24.counted_c == 36864
        and n25.counted_c == 36864
        and n26.counted_c == 36864
        and n27.counted_c == 36864
        and n28.counted_c == 36864
        and n29.counted_c == 36864
        and n30.counted_c == 36864
        and n31.counted_c == 36864
    )
    return Feedback(
        posted=posted_0 and posted_1 and posted_2 and posted_3,
        complete_b=complete_b_0 and complete_b_1 and complete_b_2 and complete_b_3,
        complete_c=complete_c_0 and complete_c_1 and complete_c_2 and complete_c_3,
    )


def as_bit(value: bool) -> ac.u1:
    result: ac.u1 = 1 if value else 0
    return result


@ac.module
def LandingMask(
    p0: bool,
    p1: bool,
    p2: bool,
    p3: bool,
    p4: bool,
    p5: bool,
    p6: bool,
    p7: bool,
    p8: bool,
    p9: bool,
    p10: bool,
    p11: bool,
    p12: bool,
    p13: bool,
    p14: bool,
    p15: bool,
    p16: bool,
    p17: bool,
    p18: bool,
    p19: bool,
    p20: bool,
    p21: bool,
    p22: bool,
    p23: bool,
    p24: bool,
    p25: bool,
    p26: bool,
    p27: bool,
    p28: bool,
    p29: bool,
    p30: bool,
    p31: bool,
) -> ac.u32:
    return ac.concat(
        as_bit(p31),
        as_bit(p30),
        as_bit(p29),
        as_bit(p28),
        as_bit(p27),
        as_bit(p26),
        as_bit(p25),
        as_bit(p24),
        as_bit(p23),
        as_bit(p22),
        as_bit(p21),
        as_bit(p20),
        as_bit(p19),
        as_bit(p18),
        as_bit(p17),
        as_bit(p16),
        as_bit(p15),
        as_bit(p14),
        as_bit(p13),
        as_bit(p12),
        as_bit(p11),
        as_bit(p10),
        as_bit(p9),
        as_bit(p8),
        as_bit(p7),
        as_bit(p6),
        as_bit(p5),
        as_bit(p4),
        as_bit(p3),
        as_bit(p2),
        as_bit(p1),
        as_bit(p0),
    )


@ac.module
def NodeGroup(
    host: HostRequest,
    transfer: Transfer,
    payload: ac.u64,
    hops: ac.table[8, HopResult],
    base: ac.u5,
) -> tuple[
    NodeResult,
    NodeResult,
    NodeResult,
    NodeResult,
    NodeResult,
    NodeResult,
    NodeResult,
    NodeResult,
]:
    path0 = PathLanding(transfer, hops, base + 0)
    path1 = PathLanding(transfer, hops, base + 1)
    path2 = PathLanding(transfer, hops, base + 2)
    path3 = PathLanding(transfer, hops, base + 3)
    path4 = PathLanding(transfer, hops, base + 4)
    path5 = PathLanding(transfer, hops, base + 5)
    path6 = PathLanding(transfer, hops, base + 6)
    path7 = PathLanding(transfer, hops, base + 7)
    node0 = PayloadNode(host, transfer, payload, base + 0, path0)
    node1 = PayloadNode(host, transfer, payload, base + 1, path1)
    node2 = PayloadNode(host, transfer, payload, base + 2, path2)
    node3 = PayloadNode(host, transfer, payload, base + 3, path3)
    node4 = PayloadNode(host, transfer, payload, base + 4, path4)
    node5 = PayloadNode(host, transfer, payload, base + 5, path5)
    node6 = PayloadNode(host, transfer, payload, base + 6, path6)
    node7 = PayloadNode(host, transfer, payload, base + 7, path7)
    return node0, node1, node2, node3, node4, node5, node6, node7


@ac.module
def PayloadAllGather(host: HostRequest) -> Result:
    control: ControllerState = ControllerState()
    command = DescriptorMemory(host, control.stage, control.cursor)
    next_control = Controller(host, command, sending.word, feedback, landings, control)
    transfer = decode(command, control.stage, control.cursor, control.offset, host)
    hop0 = ForwardHop(hop_input(transfer, 0))
    hop1 = ForwardHop(hop_input(transfer, 1))
    hop2 = ForwardHop(hop_input(transfer, 2))
    hop3 = ForwardHop(hop_input(transfer, 3))
    hop4 = ForwardHop(hop_input(transfer, 4))
    hop5 = ForwardHop(hop_input(transfer, 5))
    hop6 = ForwardHop(hop_input(transfer, 6))
    hop7 = ForwardHop(hop_input(transfer, 7))
    hops = hop_rows(hop0, hop1, hop2, hop3, hop4, hop5, hop6, hop7)
    node0, node1, node2, node3, node4, node5, node6, node7 = NodeGroup(
        host, transfer, control.payload, hops, 0
    )
    node8, node9, node10, node11, node12, node13, node14, node15 = NodeGroup(
        host, transfer, control.payload, hops, 8
    )
    node16, node17, node18, node19, node20, node21, node22, node23 = NodeGroup(
        host, transfer, control.payload, hops, 16
    )
    node24, node25, node26, node27, node28, node29, node30, node31 = NodeGroup(
        host, transfer, control.payload, hops, 24
    )
    observed = SelectNode(
        node0,
        node1,
        node2,
        node3,
        node4,
        node5,
        node6,
        node7,
        node8,
        node9,
        node10,
        node11,
        node12,
        node13,
        node14,
        node15,
        node16,
        node17,
        node18,
        node19,
        node20,
        node21,
        node22,
        node23,
        node24,
        node25,
        node26,
        node27,
        node28,
        node29,
        node30,
        node31,
        host.node,
    )
    sending = SelectNode(
        node0,
        node1,
        node2,
        node3,
        node4,
        node5,
        node6,
        node7,
        node8,
        node9,
        node10,
        node11,
        node12,
        node13,
        node14,
        node15,
        node16,
        node17,
        node18,
        node19,
        node20,
        node21,
        node22,
        node23,
        node24,
        node25,
        node26,
        node27,
        node28,
        node29,
        node30,
        node31,
        transfer.source,
    )

    feedback = Aggregate(
        node0,
        node1,
        node2,
        node3,
        node4,
        node5,
        node6,
        node7,
        node8,
        node9,
        node10,
        node11,
        node12,
        node13,
        node14,
        node15,
        node16,
        node17,
        node18,
        node19,
        node20,
        node21,
        node22,
        node23,
        node24,
        node25,
        node26,
        node27,
        node28,
        node29,
        node30,
        node31,
    )
    landings = LandingMask(
        node0.reached,
        node1.reached,
        node2.reached,
        node3.reached,
        node4.reached,
        node5.reached,
        node6.reached,
        node7.reached,
        node8.reached,
        node9.reached,
        node10.reached,
        node11.reached,
        node12.reached,
        node13.reached,
        node14.reached,
        node15.reached,
        node16.reached,
        node17.reached,
        node18.reached,
        node19.reached,
        node20.reached,
        node21.reached,
        node22.reached,
        node23.reached,
        node24.reached,
        node25.reached,
        node26.reached,
        node27.reached,
        node28.reached,
        node29.reached,
        node30.reached,
        node31.reached,
    )

    @ac.rule
    def tick() -> None:
        control = next_control

    return Result(
        data=observed.word,
        counted_b=observed.counted_b,
        counted_c=observed.counted_c,
        last_identity=observed.last_identity,
        stage=control.stage,
        descriptor=control.cursor,
        offset=control.offset,
        done=control.stage == 7,
    )
