# ruff: noqa: N802, F823, F841
"""Metadata-only, runtime-arbitrated 5x8 WSE network.

Serial metadata microsteps leave model_time unchanged. Only extracted model
 events, endpoint service and barriers advance it. No packet data is stored.
"""
import pycircuit as ac
from wse_coarse.event_queue import EventQueue
from wse_coarse.types import (
    HostRequest,
    Result,
    Transfer,
    calendar_row,
    decode,
    get,
    link_row,
    next_row,
    packet_row,
    payload,
    put,
    receive_row,
    route_row,
    row,
    service,
)


@ac.module
def CoarseWseMachine(host: HostRequest) -> Result:
    program = ac.byte_mem[ac.bits[512]](depth=65536)
    routes = ac.byte_mem[ac.u8](depth=10240)
    calendar = ac.byte_mem[ac.bits[320]](depth=6400)
    queue_next = ac.byte_mem[ac.u16](depth=2048)
    heads = ac.byte_mem[ac.u64](depth=320)
    tails = ac.byte_mem[ac.u64](depth=320)
    counts = ac.byte_mem[ac.u64](depth=320)
    busy = ac.byte_mem[ac.u64](depth=320)
    packets = ac.byte_mem[ac.bits[512]](depth=2560)
    left = ac.byte_mem[ac.u64](depth=320)
    roots = ac.byte_mem[ac.u64](depth=320)
    outstanding = ac.byte_mem[ac.u64](depth=320)
    received = ac.byte_mem[ac.u64](depth=12800)
    used = ac.byte_mem[ac.u64](depth=320)
    group_bytes = ac.byte_mem[ac.u64](depth=81920)
    expected = ac.byte_mem[ac.u64](depth=2048)
    completed = ac.byte_mem[ac.u64](depth=2048)
    identities = ac.byte_mem[ac.u8](depth=65536)
    read_free = ac.byte_mem[ac.u64](depth=320)
    write_free = ac.byte_mem[ac.u64](depth=320)
    ejection_free = ac.byte_mem[ac.u64](depth=320)
    link_free = ac.byte_mem[ac.u64](depth=1280)
    queue = EventQueue()
    current: Transfer = Transfer(
        kind=0,
        source=0,
        identity=0,
        nbytes=0,
        targets=0,
        route=0,
        dependency=255,
        group=0,
        release=0,
        block=0,
    )
    current_bits: ac.bits[512] = 0
    pc: ac.u16 = 0
    count: ac.u16 = 0
    stage: ac.u5 = 0
    scan: ac.u6 = 0
    selected: ac.u6 = 0
    dispatch: ac.u16 = 0
    dispatch_count: ac.u64 = 0
    inflight: ac.u8 = 0
    queued: ac.u16 = 0
    fault: ac.u8 = 0
    model_time: ac.u64 = 0
    origin: ac.u64 = 0
    events: ac.u32 = 0
    event_kind: ac.u8 = 0
    event_source: ac.u6 = 0
    event_node: ac.u6 = 0
    event_id: ac.u16 = 0
    event_flit: ac.u16 = 0
    event_direction: ac.u3 = 0
    event_bytes: ac.u32 = 0
    read_bytes: ac.u64 = 0
    write_bytes: ac.u64 = 0
    link_bytes: ac.u64 = 0
    pending_when: ac.bits[512] = 0
    pending_payload: ac.bits[512] = 0
    pending_count: ac.u4 = 0
    pending_read: ac.u4 = 0
    read_first: ac.u64 = 200
    write_first: ac.u64 = 200
    numerator: ac.u64 = 10
    denominator: ac.u64 = 7
    width: ac.u32 = 128
    flit_bytes: ac.u32 = 52
    hop_ticks: ac.u64 = 1
    slot_ticks: ac.u64 = 1024
    tx_ticks: ac.u64 = 2
    rx_ticks: ac.u64 = 2
    barrier_ticks: ac.u64 = 24
    capacity: ac.u64 = 65536
    guard_ticks: ac.u64 = 1

    @ac.method
    def schedule(when: ac.u64, value: ac.u64) -> None:
        assert pending_count < 8, "pending event capacity"
        pending_when = put(pending_when, pending_count, when)
        pending_payload = put(pending_payload, pending_count, value)
        pending_count = pending_count + 1

    @ac.method
    def ingest() -> None:
        queue_empty = queue.empty()
        if pc >= count:
            fault = 2
            stage = 7
        else:
            word = program.read(pc * 64)
            command = decode(word)
            if command.kind == 1:
                source: ac.u16 = command.source
                queued_source = counts.read(row(source))
                if queued_source == 8:
                    scan = 0
                    stage = 8
                else:
                    if queued_source == 0:
                        head: ac.u64 = pc
                        heads.write(row(source), head)
                    else:
                        tail = tails.read(row(source))
                        previous: ac.u16 = tail[0:16]
                        queue_next.write(next_row(previous), pc)
                    tail_value: ac.u64 = pc
                    tails.write(row(source), tail_value)
                    counts.write(row(source), queued_source + 1)
                    queued = queued + 1
                    if command.release > model_time:
                        schedule(command.release, payload(7, command.source, 0, 0, 0))
                    pc = pc + 1
            elif command.kind == 2:
                group: ac.u16 = command.group
                want = expected.read(link_row(group))
                have = completed.read(link_row(group))
                if want == have:
                    pc = pc + 1
                elif inflight == 0 and queued == 0 and queue_empty:
                    fault = 11
                    stage = 7
                else:
                    scan = 0
                    stage = 8
            elif command.kind == 3 or command.kind == 4 or command.kind == 5:
                if inflight != 0 or queued != 0 or not queue_empty:
                    scan = 0
                    stage = 8
                elif command.kind == 3:
                    model_time = model_time + barrier_ticks
                    origin = model_time
                    event_kind = 10
                    event_bytes = 0
                    events = events + 1
                    pc = pc + 1
                elif command.kind == 4:
                    current = command
                    selected = 0
                    stage = 12
                else:
                    stage = 6
            else:
                fault = 2
                stage = 7

    @ac.method
    def scan_sources() -> None:
        queue_empty = queue.empty()
        if scan < 40:
            source: ac.u16 = scan
            active = busy.read(row(source))
            waiting = counts.read(row(source))
            if active == 0 and waiting != 0:
                head = heads.read(row(source))
                dispatch = head[0:16]
                dispatch_count = waiting
                stage = 11
            else:
                scan = scan + 1
        elif not queue_empty:
            queue.start_pop()
            stage = 10
        elif inflight != 0 or queued != 0:
            fault = 11
            stage = 7
        else:
            stage = 1

    @ac.method
    def dispatch_source() -> None:
        word = program.read(dispatch * 64)
        command = decode(word)
        group: ac.u16 = command.dependency
        want = expected.read(link_row(group))
        have = completed.read(link_row(group))
        if command.release > model_time or (command.dependency != 255 and want != have):
            scan = scan + 1
            stage = 8
        else:
            source: ac.u16 = scan
            next_head = queue_next.read(next_row(dispatch))
            next_word: ac.u64 = next_head
            heads.write(row(source), next_word)
            counts.write(row(source), dispatch_count - 1)
            queued = queued - 1
            busy.write(row(source), 1)
            inflight = inflight + 1
            current = command
            current_bits = word
            selected = 0
            seen = identities.read(command.identity)
            if seen != 0:
                fault = 14
                stage = 7
            else:
                identities.write(command.identity, 1)
                stage = 3

    @ac.method
    def reserve() -> None:
        one: ac.u40 = 1
        node: ac.u16 = selected
        source: ac.u16 = current.source
        if current.targets & (one << selected) != 0:
            occupied = used.read(row(node))
            nbytes: ac.u64 = current.nbytes
            if occupied + nbytes > capacity:
                fault = 5
                stage = 7
            else:
                used.write(row(node), occupied + nbytes)
                received.write(receive_row(source * 40 + node), 0)
        if stage == 3:
            if selected == 39:
                packets.write(packet_row(source), current_bits)
                targets: ac.u64 = current.targets
                left.write(row(source), targets)
                outstanding.write(row(source), 0)
                free = read_free.read(row(source))
                begin = free if free > model_time else model_time
                finish = begin + service(current.nbytes, read_first, numerator, denominator, width)
                read_free.write(row(source), finish)
                schedule(finish, payload(1, current.source, current.source, 0, 0))
                stage = 1
            else:
                selected = selected + 1

    @ac.method
    def extract() -> None:
        incoming = queue.result()
        model_time = incoming.when
        event_kind = incoming.payload[0:4]
        event_source = incoming.payload[4:10]
        event_node = incoming.payload[10:16]
        event_flit = incoming.payload[16:32]
        event_direction = incoming.payload[32:35]
        event_bytes = 0
        if event_kind == 7:
            event_id = 0
            events = events + 1
            stage = 1
        else:
            source: ac.u16 = event_source
            bits = packets.read(packet_row(source))
            current = decode(bits)
            event_id = current.identity
            stage = 9

    @ac.method
    def process_event() -> None:
        one: ac.u40 = 1
        source: ac.u16 = event_source
        node: ac.u16 = event_node
        count_pending = outstanding.read(row(source))
        if event_kind == 2 or event_kind == 3 or event_kind == 4 or event_kind == 9:
            count_pending = count_pending - 1
        queue_size = queue.size()
        if queue_size > 1016:
            fault = 10
            stage = 7
        else:
            stage = 1
            events = events + 1
            if event_kind == 1:
                nbytes: ac.u64 = current.nbytes
                read_bytes = read_bytes + nbytes
                event_bytes = current.nbytes
                source_route: ac.u16 = current.route
                source_edges = routes.read(route_row(source_route, source))
                if source_edges != 0:
                    schedule(model_time + tx_ticks, payload(2, event_source, event_source, 0, 0))
                    count_pending = count_pending + 1
                if current.targets & (one << event_source) != 0:
                    free = write_free.read(row(source))
                    begin = free if free > model_time else model_time
                    finish = begin + service(
                        current.nbytes, write_first, numerator, denominator, width
                    )
                    write_free.write(row(source), finish)
                    schedule(finish, payload(5, event_source, event_source, 0, 0))
            elif event_kind == 2 or event_kind == 4:
                route: ac.u16 = current.route
                edges = routes.read(route_row(route, node))
                initial_root_mask: ac.u64 = edges[0:4]
                for direction in range(4):
                    d: ac.u3 = direction
                    bit: ac.u8 = 1
                    if edges & (bit << d) != 0:
                        schedule(model_time, payload(3, event_source, event_node, event_flit, d))
                        count_pending = count_pending + 1
                if event_kind == 2:
                    roots.write(row(source), initial_root_mask)
                elif current.targets & (one << event_node) != 0:
                    schedule(model_time, payload(9, event_source, event_node, event_flit, 0))
                    count_pending = count_pending + 1
            elif event_kind == 3:
                port_index: ac.u16 = event_direction
                link = node * 4 + port_index
                free = link_free.read(link_row(link))
                candidate = free if free > model_time else model_time
                slot_wide = ((candidate - origin) // slot_ticks) % 8
                slot: ac.u16 = slot_wide[0:16]
                permissions = calendar.read(calendar_row(link))
                source_bits = permissions >> event_source
                windows: ac.u8 = 0
                for window in range(8):
                    offset: ac.u9 = window
                    w: ac.u3 = window
                    entry = source_bits >> (offset * 40)
                    permitted: ac.u8 = entry[0:1]
                    windows = windows | (permitted << w)
                repeat: ac.u16 = windows
                upcoming = (repeat | (repeat << 8)) >> slot
                permitted_windows = upcoming[0:8]
                distance: ac.u64 = ac.count_trailing_zeros(permitted_windows)
                scheduled = (
                    candidate
                    if distance == 0
                    else origin + ((candidate - origin) // slot_ticks + distance) * slot_ticks
                )
                if permitted_windows == 0:
                    event_kind = 13
                    fault = 12
                    stage = 7
                elif scheduled != model_time:
                    schedule(
                        scheduled, payload(3, event_source, event_node, event_flit, event_direction)
                    )
                    count_pending = count_pending + 1
                    event_kind = 11
                else:
                    link_free.write(link_row(link), model_time + 1)
                    link_bytes = link_bytes + 64
                    event_bytes = 64
                    flits_wide = (current.nbytes + flit_bytes - 1) // flit_bytes
                    flits: ac.u16 = flits_wide[0:16]
                    if event_node == event_source:
                        root_mask = roots.read(row(source))
                        direction_bit: ac.u64 = 1
                        root_mask = root_mask & ~(direction_bit << event_direction)
                        roots.write(row(source), root_mask)
                        if root_mask == 0 and event_flit + 1 < flits:
                            schedule(
                                model_time + 1,
                                payload(2, event_source, event_source, event_flit + 1, 0),
                            )
                            count_pending = count_pending + 1
                    peer: ac.u6 = (
                        event_node - 8
                        if event_direction == 0
                        else (
                            event_node - 1
                            if event_direction == 1
                            else event_node + 1 if event_direction == 2 else event_node + 8
                        )
                    )
                    schedule(model_time + hop_ticks, payload(4, event_source, peer, event_flit, 0))
                    count_pending = count_pending + 1
            elif event_kind == 9:
                free = ejection_free.read(row(node))
                if free > model_time or host.landing_permit & (one << event_node) == 0:
                    ready = free if free > model_time else model_time + 1
                    schedule(ready, payload(9, event_source, event_node, event_flit, 0))
                    count_pending = count_pending + 1
                    event_kind = 12
                else:
                    ejection_free.write(row(node), model_time + 1)
                    address = receive_row(source * 40 + node)
                    received_count = received.read(address)
                    received_count = received_count + 1
                    received.write(address, received_count)
                    flits_wide = (current.nbytes + flit_bytes - 1) // flit_bytes
                    landing_flits: ac.u64 = flits_wide
                    flit_index: ac.u32 = event_flit
                    event_bytes = (
                        flit_bytes
                        if flit_index + 1 < flits_wide
                        else current.nbytes - flit_index * flit_bytes
                    )
                    if received_count == landing_flits:
                        free_write = write_free.read(row(node))
                        ready = model_time + rx_ticks
                        begin = free_write if free_write > ready else ready
                        finish = begin + service(
                            current.nbytes, write_first, numerator, denominator, width
                        )
                        write_free.write(row(node), finish)
                        schedule(finish, payload(5, event_source, event_node, 0, 0))
            elif event_kind == 5:
                group: ac.u16 = current.group
                have = completed.read(link_row(group))
                completed.write(link_row(group), have + 1)
                group_index: ac.u17 = group
                node_index: ac.u17 = node
                group_address: ac.u17 = (group_index * 40 + node_index) * 8
                bytes_done = group_bytes.read(group_address)
                nbytes: ac.u64 = current.nbytes
                group_bytes.write(group_address, bytes_done + nbytes)
                write_bytes = write_bytes + nbytes
                event_bytes = current.nbytes
                remaining = left.read(row(source))
                target_bit: ac.u64 = 1
                remaining = remaining & ~(target_bit << event_node)
                left.write(row(source), remaining)
                if remaining == 0:
                    schedule(model_time + guard_ticks, payload(6, event_source, event_source, 0, 0))
            elif event_kind == 6:
                if count_pending != 0:
                    schedule(model_time + 1, payload(6, event_source, event_source, 0, 0))
                else:
                    busy.write(row(source), 0)
                    inflight = inflight - 1
            else:
                fault = 13
                stage = 7
            outstanding.write(row(source), count_pending)

    @ac.method
    def retire() -> None:
        group: ac.u17 = current.group
        node: ac.u17 = selected
        address: ac.u17 = (group * 40 + node) * 8
        bytes_done = group_bytes.read(address)
        node_word: ac.u16 = selected
        occupied = used.read(row(node_word))
        used.write(row(node_word), occupied - bytes_done)
        group_bytes.write(address, 0)
        if selected == 39:
            pc = pc + 1
            stage = 1
        else:
            selected = selected + 1

    @ac.rule
    def execute() -> Result:
        queue_busy = queue.busy()
        if queue_busy:
            queue.advance()
        elif pending_read < pending_count:
            queue.push(get(pending_when, pending_read), get(pending_payload, pending_read))
            pending_read = pending_read + 1
            if pending_read == pending_count:
                pending_read = 0
                pending_count = 0
        elif host.kind != 0:
            if stage != 0:
                fault = 1
                stage = 7
            elif host.kind == 1:
                program.write(host.index * 64, host.data)
            elif host.kind == 2:
                routes.write(host.index[0:14], host.data[0:8])
            elif host.kind == 3:
                wide_address: ac.u16 = host.index * 5
                address: ac.u13 = wide_address[0:13]
                mask: ac.u40 = 31
                value: ac.bits[320] = host.data[0:40]
                calendar.write(address, value, mask=mask)
            elif host.kind == 4:
                expected.write(link_row(host.index), host.data[0:64])
            elif host.kind == 5:
                count = host.count
                stage = 1
            elif host.kind == 6:
                if host.index == 0:
                    read_first = host.data[0:64]
                if host.index == 1:
                    write_first = host.data[0:64]
                if host.index == 2:
                    numerator = host.data[0:64]
                if host.index == 3:
                    denominator = host.data[0:64]
                if host.index == 4:
                    width = host.data[0:32]
                if host.index == 5:
                    flit_bytes = host.data[0:32]
                if host.index == 6:
                    hop_ticks = host.data[0:64]
                if host.index == 7:
                    slot_ticks = host.data[0:64]
                if host.index == 8:
                    tx_ticks = host.data[0:64]
                if host.index == 9:
                    rx_ticks = host.data[0:64]
                if host.index == 10:
                    barrier_ticks = host.data[0:64]
                if host.index == 11:
                    capacity = host.data[0:64]
                if host.index == 12:
                    guard_ticks = host.data[0:64]
        elif stage == 1:
            ingest()
        elif stage == 3:
            reserve()
        elif stage == 8:
            scan_sources()
        elif stage == 11:
            dispatch_source()
        elif stage == 10:
            extract()
        elif stage == 9:
            process_event()
        elif stage == 12:
            retire()
        return Result(
            model_time=model_time,
            events=events,
            event_kind=event_kind,
            event_source=event_source,
            event_node=event_node,
            event_id=event_id,
            event_flit=event_flit,
            event_direction=event_direction,
            event_bytes=event_bytes,
            read_bytes=read_bytes,
            write_bytes=write_bytes,
            link_bytes=link_bytes,
            pc=pc,
            stage=stage,
            fault=fault,
            done=stage == 6,
        )

    return execute
