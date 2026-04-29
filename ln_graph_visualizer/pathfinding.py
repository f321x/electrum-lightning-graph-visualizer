from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional, Sequence, TYPE_CHECKING

from electrum.bolt11 import decode_bolt11_invoice
from electrum.lnrouter import LNPathFinder, RouteEdge
from electrum.lnutil import ShortChannelID

from .graph_data import extract_path_subgraph

if TYPE_CHECKING:
    from electrum.channel_db import ChannelDB
    from electrum.lnrouter import LNPaymentPath, LNPaymentRoute


@dataclass
class InvoiceRoutingContext:
    destination: bytes
    amount_msat: int
    private_route_edges: Dict[ShortChannelID, RouteEdge]
    description: str
    payment_hash: bytes
    min_final_cltv_delta: int
    route_hint_count: int


def build_private_route_edges(
    channel_db: 'ChannelDB',
    r_tags,
    invoice_pubkey: bytes,
) -> Dict[ShortChannelID, RouteEdge]:
    """Convert invoice routing hints (r_tags) into private RouteEdge objects.

    Replicates the logic from lnworker.create_route_for_single_htlc
    for building private_route_edges from invoice r_tags.
    """
    private_route_edges: Dict[ShortChannelID, RouteEdge] = {}
    for private_path in r_tags:
        # shift node pubkeys by one towards the destination
        private_path_nodes = [edge[0] for edge in private_path][1:] + [invoice_pubkey]
        private_path_rest = [edge[1:] for edge in private_path]
        start_node = private_path[0][0]
        for end_node, edge_rest in zip(private_path_nodes, private_path_rest):
            short_channel_id, fee_base_msat, fee_proportional_millionths, cltv_delta = edge_rest
            short_channel_id = ShortChannelID(short_channel_id)
            # if we have a routing policy in the db, that takes precedence
            channel_policy = channel_db.get_policy_for_node(
                short_channel_id=short_channel_id,
                node_id=start_node)
            if channel_policy:
                fee_base_msat = channel_policy.fee_base_msat
                fee_proportional_millionths = channel_policy.fee_proportional_millionths
                cltv_delta = channel_policy.cltv_delta
            node_info = channel_db.get_node_info_for_node_id(node_id=end_node)
            route_edge = RouteEdge(
                start_node=start_node,
                end_node=end_node,
                short_channel_id=short_channel_id,
                fee_base_msat=fee_base_msat,
                fee_proportional_millionths=fee_proportional_millionths,
                cltv_delta=cltv_delta,
                node_features=node_info.features if node_info else 0)
            private_route_edges[route_edge.short_channel_id] = route_edge
            start_node = end_node
    return private_route_edges


def parse_invoice_for_routing(
    bolt11_str: str,
    channel_db: 'ChannelDB',
) -> InvoiceRoutingContext:
    """Parse a BOLT11 invoice and extract routing-relevant data."""
    lnaddr = decode_bolt11_invoice(bolt11_str)
    destination = lnaddr.pubkey.serialize()
    amount_msat = lnaddr.get_amount_msat()
    if amount_msat is None:
        raise ValueError("Invoice has no amount — cannot compute route")
    r_tags = lnaddr.get_routing_info('r')
    private_route_edges = build_private_route_edges(
        channel_db, r_tags, destination)

    return InvoiceRoutingContext(
        destination=destination,
        amount_msat=amount_msat,
        private_route_edges=private_route_edges,
        description=lnaddr.get_description(),
        payment_hash=lnaddr.paymenthash,
        min_final_cltv_delta=lnaddr.get_min_final_cltv_delta(),
        route_hint_count=len(r_tags),
    )


def find_k_paths(
    channel_db: 'ChannelDB',
    source: bytes,
    dest: bytes,
    amount_msat: int,
    k: int = 3,
    my_sending_channels: Optional[dict] = None,
    private_route_edges: Optional[Dict[ShortChannelID, RouteEdge]] = None,
) -> List[Tuple['LNPaymentPath', 'LNPaymentRoute']]:
    """Find up to k diverse shortest paths using blacklist-based iteration.

    Uses a private LNPathFinder instance to avoid interfering with
    the real pathfinder's blacklist and liquidity hints.
    """
    path_finder = LNPathFinder(channel_db)
    results: List[Tuple['LNPaymentPath', 'LNPaymentRoute']] = []
    seen_scid_tuples = set()

    for attempt in range(k * 3):
        path = path_finder.find_path_for_payment(
            nodeA=source,
            nodeB=dest,
            invoice_amount_msat=amount_msat,
            my_sending_channels=my_sending_channels,
            private_route_edges=private_route_edges,
        )
        if path is None:
            break

        path_scids = tuple(e.short_channel_id for e in path)
        if path_scids in seen_scid_tuples:
            path_finder.add_edge_to_blacklist(path[0].short_channel_id)
            continue

        seen_scid_tuples.add(path_scids)

        try:
            route = path_finder.create_route_from_path(
                path,
                my_channels=my_sending_channels,
                private_route_edges=private_route_edges,
            )
        except Exception:
            path_finder.add_edge_to_blacklist(path[0].short_channel_id)
            continue

        results.append((list(path), list(route)))

        if len(results) >= k:
            break

        # blacklist middle edge to force diversity in next iteration
        mid = len(path) // 2
        path_finder.add_edge_to_blacklist(path[mid].short_channel_id)

    return results


def find_paths_and_extract(
    channel_db: 'ChannelDB',
    source: bytes,
    dest: bytes,
    amount_msat: int,
    k: int,
    my_sending_channels: Optional[dict] = None,
    private_route_edges: Optional[Dict[ShortChannelID, RouteEdge]] = None,
) -> Tuple[List[Tuple['LNPaymentPath', 'LNPaymentRoute']], tuple, tuple]:
    """Run pathfinding and extract subgraphs."""
    results = find_k_paths(
        channel_db, source, dest, amount_msat, k,
        my_sending_channels=my_sending_channels,
        private_route_edges=private_route_edges,
    )
    if results:
        paths = [r[0] for r in results]
        ctx_nodes, ctx_edges, path_nodes, path_edges = extract_path_subgraph(
            channel_db, paths, context_hops=1,
            private_route_edges=private_route_edges)
        path_sub = (path_nodes, path_edges)
        ctx_sub = (ctx_nodes, ctx_edges)
    else:
        path_sub = ({}, {})
        ctx_sub = ({}, {})
    return results, path_sub, ctx_sub


def compute_path_summary(route: 'LNPaymentRoute', amount_msat: int) -> dict:
    """Compute summary stats for a route."""
    hop_count = len(route)
    total_fee_msat = 0
    total_cltv = 0
    amt = amount_msat
    for edge in reversed(route[1:]):
        fee = edge.fee_for_edge(amt)
        total_fee_msat += fee
        total_cltv += edge.cltv_delta
        amt += fee
    return {
        'hop_count': hop_count,
        'total_fee_msat': total_fee_msat,
        'total_cltv': total_cltv,
    }
