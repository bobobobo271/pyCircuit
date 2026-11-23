# ruff: noqa: F821, F823, F841, N802 -- captured state and generated width constants.
"""Directed Calendar link for the FFN row and column timing scenarios.

The benchmark supplies exact wire widths and instantiates a chain in each
direction. Flits carry the wire header and metadata for byte accounting.
"""

import pycircuit as ac


@ac.struct
class Flit:
    route: ac.bits[ROUTE_WIDTH]
    opcode: ac.u3
    red_op: ac.u3
    epoch: ac.u8
    source: ac.u6
    sequence: ac.u16
    nbytes: ac.u7


def pair(route: ac.bits[ROUTE_WIDTH], node: ac.u6) -> ac.u2:
    shift: ac.u7 = node
    return (route >> (shift * 2))[:2]


@ac.module
def Hop(
    incoming: Flit,
    node: ac.u6,
    downstream: ac.u6,
    route: ac.bits[ROUTE_WIDTH],
    opcode: ac.u3,
    epoch: ac.u8,
    calreg: ac.u8,
    flits_per_row: ac.u16,
    flits_per_source: ac.u16,
    full_bytes: ac.u7,
    tail_bytes: ac.u7,
) -> Flit:
    outgoing: Flit = Flit()
    sequence: ac.u16 = 0
    row_flit: ac.u16 = 0

    @ac.rule
    def advance() -> None:
        # An empty registered link carries zero payload bytes.
        following = Flit()
        if incoming.nbytes != 0:
            own_pair = pair(incoming.route, node)
            assert own_pair >= 2, "incoming flit outside the passing route"
            assert incoming.opcode == opcode and incoming.epoch == epoch, "receive context"
            assert incoming.source != node, "self-sourced flit on an incoming link"
            if own_pair == 3:
                ac.log(
                    "info",
                    "wse_land",
                    epoch,
                    node,
                    incoming.source,
                    incoming.sequence,
                    incoming.nbytes,
                )
            if downstream != node:
                adjacent_pair = pair(incoming.route, downstream)
                assert adjacent_pair != 1, "illegal downstream route pair"
                if adjacent_pair >= 2:
                    following = incoming

        # An incoming flit that needs the egress has priority. The local
        # stream retains its position until the link accepts its next copy.
        if following.nbytes == 0 and downstream != node and sequence < flits_per_source:
            own_pair = pair(route, node)
            adjacent_pair = pair(route, downstream)
            assert own_pair == 2 and adjacent_pair != 1, "invalid injection route"
            assert opcode != 0 and opcode != 7 and ((calreg >> opcode) & 1) != 0, "CalReg"
            if adjacent_pair >= 2:
                nbytes = tail_bytes if row_flit == flits_per_row - 1 else full_bytes
                following = Flit(
                    route=route,
                    opcode=opcode,
                    red_op=0,
                    epoch=epoch,
                    source=node,
                    sequence=sequence,
                    nbytes=nbytes,
                )
                sequence = sequence + 1
                row_flit = 0 if row_flit == flits_per_row - 1 else row_flit + 1

        if following.nbytes != 0:
            ac.log("info", "wse_link", epoch, node, downstream)
        outgoing = following

    return outgoing
