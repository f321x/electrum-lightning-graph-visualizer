from dataclasses import dataclass, field
from typing import Optional, Dict, Set, Tuple, List, Sequence, TYPE_CHECKING
from collections import deque

if TYPE_CHECKING:
    from electrum.channel_db import ChannelDB
    from electrum.lnrouter import PathEdge
    from electrum.util import ShortChannelID


@dataclass
class PolicyData:
    fee_base_msat: int
    fee_proportional_millionths: int
    cltv_delta: int
    htlc_minimum_msat: int
    htlc_maximum_msat: Optional[int]
    is_disabled: bool
    timestamp: int


@dataclass
class GraphNode:
    node_id: bytes
    alias: str
    features: int
    addresses: list = field(default_factory=list)
    channel_count: int = 0
    color: Optional[str] = None  # '#rrggbb' from node_announcement


@dataclass
class GraphEdge:
    short_channel_id: 'ShortChannelID'
    node1_id: bytes
    node2_id: bytes
    capacity_sat: Optional[int]
    policy_1to2: Optional[PolicyData] = None
    policy_2to1: Optional[PolicyData] = None


def _policy_from_db(policy) -> Optional[PolicyData]:
    if policy is None:
        return None
    return PolicyData(
        fee_base_msat=policy.fee_base_msat,
        fee_proportional_millionths=policy.fee_proportional_millionths,
        cltv_delta=policy.cltv_delta,
        htlc_minimum_msat=policy.htlc_minimum_msat,
        htlc_maximum_msat=policy.htlc_maximum_msat,
        is_disabled=bool(policy.is_disabled()),
        timestamp=policy.timestamp,
    )


def _extract_color_from_raw(raw: Optional[bytes]) -> Optional[str]:
    """Extract rgb_color from raw node_announcement bytes.

    Wire format: 2B type + 64B sig + 2B flen + flen features
                 + 4B timestamp + 33B node_id + 3B rgb_color + ...
    """
    if raw is None or len(raw) < 105:
        return None
    try:
        flen = int.from_bytes(raw[66:68], 'big')
        offset = 105 + flen
        if len(raw) < offset + 3:
            return None
        r, g, b = raw[offset], raw[offset + 1], raw[offset + 2]
        if r == 0 and g == 0 and b == 0:
            return None  # default/unset
        return f'#{r:02x}{g:02x}{b:02x}'
    except Exception:
        return None


def _make_graph_node(channel_db: 'ChannelDB', node_id: bytes,
                     channel_count: Optional[int] = None) -> GraphNode:
    node_info = channel_db.get_node_info_for_node_id(node_id)
    alias = node_info.alias if node_info else ''
    features = node_info.features if node_info else 0
    color = _extract_color_from_raw(node_info.raw if node_info else None)
    addresses = []
    for host, port, ts in channel_db.get_node_addresses(node_id):
        addresses.append(f"{host}:{port}")
    if channel_count is None:
        channel_count = len(channel_db.get_channels_for_node(node_id))
    return GraphNode(
        node_id=node_id,
        alias=alias,
        features=features,
        addresses=addresses,
        channel_count=channel_count,
        color=color,
    )


def _make_graph_edge(channel_db: 'ChannelDB', scid: 'ShortChannelID') -> Optional[GraphEdge]:
    ci = channel_db.get_channel_info(scid)
    if ci is None:
        return None
    p_1to2 = _policy_from_db(channel_db.get_policy_for_node(scid, ci.node1_id))
    p_2to1 = _policy_from_db(channel_db.get_policy_for_node(scid, ci.node2_id))
    capacity_sat = ci.capacity_sat
    # Gossip channels are trusted by default and not SPV-verified, so
    # capacity_sat is typically None.  Fall back to the max htlc_maximum_msat
    # advertised in channel policies, which nodes usually set to (or near)
    # the channel capacity.
    if capacity_sat is None:
        htlc_max = max(
            (p.htlc_maximum_msat for p in (p_1to2, p_2to1)
             if p is not None and p.htlc_maximum_msat is not None),
            default=None,
        )
        if htlc_max is not None:
            capacity_sat = htlc_max // 1000
    return GraphEdge(
        short_channel_id=ci.short_channel_id,
        node1_id=ci.node1_id,
        node2_id=ci.node2_id,
        capacity_sat=capacity_sat,
        policy_1to2=p_1to2,
        policy_2to1=p_2to1,
    )


def _collect_edges_for_node(
    channel_db: 'ChannelDB',
    node_id: bytes,
    edges: Dict['ShortChannelID', 'GraphEdge'],
    node_channels: Optional[List] = None,
) -> Set[bytes]:
    """Add edges for node_id to edges dict, return set of new neighbor IDs."""
    if node_channels is None:
        node_channels = channel_db.get_channels_for_node(node_id)
    neighbors: Set[bytes] = set()
    for scid in node_channels:
        if scid in edges:
            continue
        edge = _make_graph_edge(channel_db, scid)
        if edge is None:
            continue
        edges[scid] = edge
        neighbor = edge.node2_id if edge.node1_id == node_id else edge.node1_id
        neighbors.add(neighbor)
    return neighbors


def extract_neighborhood(
    channel_db: 'ChannelDB',
    seed_node_id: bytes,
    depth: int = 1,
    max_nodes: int = 500,
) -> Tuple[Dict[bytes, GraphNode], Dict['ShortChannelID', GraphEdge]]:
    """BFS from seed_node_id up to `depth` hops. Returns (nodes, edges)."""
    nodes: Dict[bytes, GraphNode] = {}
    edges: Dict['ShortChannelID', GraphEdge] = {}

    visited: Set[bytes] = set()
    queue: deque = deque()
    queue.append((seed_node_id, 0))
    visited.add(seed_node_id)

    while queue:
        node_id, d = queue.popleft()
        if len(nodes) >= max_nodes:
            break

        node_channels = channel_db.get_channels_for_node(node_id)
        nodes[node_id] = _make_graph_node(channel_db, node_id,
                                          channel_count=len(node_channels))

        if d >= depth:
            continue

        neighbors = _collect_edges_for_node(channel_db, node_id, edges, node_channels)
        for neighbor in neighbors:
            if neighbor not in visited and len(nodes) + len(queue) < max_nodes:
                visited.add(neighbor)
                queue.append((neighbor, d + 1))

    # ensure all edge endpoints are in nodes dict
    for edge in list(edges.values()):
        for nid in (edge.node1_id, edge.node2_id):
            if nid not in nodes:
                nodes[nid] = _make_graph_node(channel_db, nid)

    return nodes, edges


def extract_path_subgraph(
    channel_db: 'ChannelDB',
    paths: Sequence[Sequence['PathEdge']],
    context_hops: int = 1,
) -> Tuple[Dict[bytes, GraphNode], Dict['ShortChannelID', GraphEdge],
           Dict[bytes, GraphNode], Dict['ShortChannelID', GraphEdge]]:
    """Extract nodes/edges from found paths plus N-hop context around path nodes.

    Returns (context_nodes, context_edges, path_only_nodes, path_only_edges).
    The path-only dicts are strict subsets containing only nodes/edges on the paths themselves.
    """
    path_node_ids: Set[bytes] = set()
    path_scids: Set['ShortChannelID'] = set()

    for path in paths:
        for edge in path:
            path_node_ids.add(edge.start_node)
            path_node_ids.add(edge.end_node)
            path_scids.add(edge.short_channel_id)

    nodes: Dict[bytes, GraphNode] = {}
    edges: Dict['ShortChannelID', GraphEdge] = {}

    # add path edges
    for scid in path_scids:
        edge = _make_graph_edge(channel_db, scid)
        if edge is not None:
            edges[scid] = edge

    # add path nodes and context
    context_node_ids: Set[bytes] = set()
    for nid in path_node_ids:
        node_channels = channel_db.get_channels_for_node(nid)
        nodes[nid] = _make_graph_node(channel_db, nid,
                                      channel_count=len(node_channels))
        if context_hops >= 1:
            context_node_ids.update(
                _collect_edges_for_node(channel_db, nid, edges, node_channels))

    for nid in context_node_ids:
        if nid not in nodes:
            nodes[nid] = _make_graph_node(channel_db, nid)

    # derive path-only subsets
    path_only_nodes = {nid: nodes[nid] for nid in path_node_ids if nid in nodes}
    path_only_edges = {scid: edges[scid] for scid in path_scids if scid in edges}

    return nodes, edges, path_only_nodes, path_only_edges


def get_node_display_name(node: GraphNode) -> str:
    if node.alias:
        return node.alias
    return node.node_id.hex()[:16] + '...'
