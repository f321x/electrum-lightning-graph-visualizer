from typing import List, Tuple, Optional, Sequence, TYPE_CHECKING

from electrum.lnrouter import LNPathFinder

if TYPE_CHECKING:
    from electrum.channel_db import ChannelDB
    from electrum.lnrouter import LNPaymentPath, LNPaymentRoute


def find_k_paths(
    channel_db: 'ChannelDB',
    source: bytes,
    dest: bytes,
    amount_msat: int,
    k: int = 3,
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
        )
        if path is None:
            break

        path_scids = tuple(e.short_channel_id for e in path)
        if path_scids in seen_scid_tuples:
            path_finder.add_edge_to_blacklist(path[0].short_channel_id)
            continue

        seen_scid_tuples.add(path_scids)

        try:
            route = path_finder.create_route_from_path(path)
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
