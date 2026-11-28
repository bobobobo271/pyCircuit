"""Runtime loader and observations; indices identify metadata, never device bytes."""

import pycircuit as ac


@ac.struct
class HostRequest:
    kind: ac.u4
    index: ac.u16
    data: ac.bits[512]
    count: ac.u16
    landing_permit: ac.u40


@ac.struct
class Result:
    model_time: ac.u64
    events: ac.u32
    event_kind: ac.u8
    event_source: ac.u6
    event_node: ac.u6
    event_id: ac.u16
    event_flit: ac.u16
    event_direction: ac.u3
    event_bytes: ac.u32
    read_bytes: ac.u64
    write_bytes: ac.u64
    link_bytes: ac.u64
    pc: ac.u16
    stage: ac.u5
    fault: ac.u8
    done: bool


@ac.struct
class Transfer:
    kind: ac.u4
    source: ac.u6
    identity: ac.u16
    nbytes: ac.u32
    targets: ac.u40
    route: ac.u8
    dependency: ac.u8
    group: ac.u8
    release: ac.u64
    block: ac.u32


def decode(word: ac.bits[512]) -> Transfer:
    return Transfer(
        kind=word[0:4],
        source=word[4:10],
        identity=word[10:26],
        nbytes=word[26:58],
        targets=word[58:98],
        route=word[98:106],
        dependency=word[106:114],
        group=word[114:122],
        release=word[122:186],
        block=word[186:218],
    )


def row(index: ac.u16) -> ac.u9:
    narrowed: ac.u9 = index[0:9]
    return narrowed * 8


def link_row(index: ac.u16) -> ac.u11:
    narrowed: ac.u11 = index[0:11]
    return narrowed * 8


def receive_row(index: ac.u16) -> ac.u14:
    narrowed: ac.u14 = index[0:14]
    return narrowed * 8


def packet_row(index: ac.u16) -> ac.u12:
    narrowed: ac.u12 = index[0:12]
    return narrowed * 64


def next_row(index: ac.u16) -> ac.u11:
    narrowed: ac.u11 = index[0:11]
    return narrowed * 2


def route_row(index: ac.u16, node: ac.u16) -> ac.u14:
    word = index * 40 + node
    return word[0:14]


def calendar_row(index: ac.u16) -> ac.u13:
    word = index * 40
    return word[0:13]


def service(
    nbytes: ac.u32, first: ac.u64, numerator: ac.u64, denominator: ac.u64, width: ac.u32
) -> ac.u64:
    beats: ac.u64 = (nbytes - 1) // width
    return first + (beats * numerator + denominator - 1) // denominator


def payload(kind: ac.u4, source: ac.u6, node: ac.u6, flit: ac.u16, direction: ac.u3) -> ac.u64:
    k: ac.u64 = kind
    s: ac.u64 = source
    n: ac.u64 = node
    f: ac.u64 = flit
    d: ac.u64 = direction
    return k | (s << 4) | (n << 10) | (f << 16) | (d << 32)


def put(storage: ac.bits[512], index: ac.u4, value: ac.u64) -> ac.bits[512]:
    shift: ac.u9 = index
    shift = shift * 64
    mask: ac.bits[512] = 18446744073709551615
    wide: ac.bits[512] = value
    return (storage & ~(mask << shift)) | (wide << shift)


def get(storage: ac.bits[512], index: ac.u4) -> ac.u64:
    shift: ac.u9 = index
    shift = shift * 64
    word = storage >> shift
    return word[0:64]
