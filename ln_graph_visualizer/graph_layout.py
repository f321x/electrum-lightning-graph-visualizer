import math
import random
from typing import Dict, List, Tuple, Optional, Callable, Set, TYPE_CHECKING

from PyQt6.QtCore import QThread, pyqtSignal

if TYPE_CHECKING:
    from electrum.util import ShortChannelID
    from .graph_data import GraphNode, GraphEdge


class ForceDirectedLayout:
    """Fruchterman-Reingold force-directed layout, pure Python."""

    def __init__(
        self,
        nodes: Dict[bytes, 'GraphNode'],
        edges: Dict['ShortChannelID', 'GraphEdge'],
        width: float = 1000.0,
        height: float = 1000.0,
    ):
        self.width = width
        self.height = height
        self.node_ids = list(nodes.keys())
        self.n = len(self.node_ids)
        self.id_to_idx = {nid: i for i, nid in enumerate(self.node_ids)}

        # deduplicate so parallel channels don't multiply attractive force
        self.edge_pairs: List[Tuple[int, int]] = []
        self._adj: Dict[int, Set[int]] = {}
        seen_pairs: set = set()
        for edge in edges.values():
            i = self.id_to_idx.get(edge.node1_id)
            j = self.id_to_idx.get(edge.node2_id)
            if i is not None and j is not None and i != j:
                pair = (min(i, j), max(i, j))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    self.edge_pairs.append((i, j))
                self._adj.setdefault(i, set()).add(j)
                self._adj.setdefault(j, set()).add(i)

        # initialize positions randomly
        self.pos_x = [random.uniform(0, width) for _ in range(self.n)]
        self.pos_y = [random.uniform(0, height) for _ in range(self.n)]

        # optimal distance
        area = width * height
        self.k = math.sqrt(area / max(self.n, 1))

        # pinned nodes won't move
        self.pinned: Set[int] = set()
        self._cutoff_sq = (3.0 * self.k) ** 2

    def init_near_neighbors(self, known_idxs: Set[int]):
        """Place nodes not in known_idxs around their connected known neighbors.

        Single-anchor groups are evenly distributed in a circle;
        multi-anchor nodes are placed near the centroid of their anchors.
        """
        # group new nodes by anchor pattern
        anchor_groups: Dict[int, List[int]] = {}  # anchor_idx -> [new_node_idxs]
        multi_connected: List[Tuple[int, List[int]]] = []

        for i in range(self.n):
            if i in known_idxs:
                continue
            connected = [j for j in self._adj.get(i, []) if j in known_idxs]
            if not connected:
                continue
            if len(connected) == 1:
                anchor_groups.setdefault(connected[0], []).append(i)
            else:
                multi_connected.append((i, connected))

        # place single-anchor groups evenly in a circle
        for anchor, group in anchor_groups.items():
            count = len(group)
            for idx, i in enumerate(group):
                angle = (2 * math.pi * idx) / count
                r = self.k * (0.5 + random.random() * 0.3)
                self.pos_x[i] = self.pos_x[anchor] + r * math.cos(angle)
                self.pos_y[i] = self.pos_y[anchor] + r * math.sin(angle)

        # place multi-connected nodes near centroid of their anchors
        for i, connected in multi_connected:
            cx = sum(self.pos_x[j] for j in connected) / len(connected)
            cy = sum(self.pos_y[j] for j in connected) / len(connected)
            angle = random.uniform(0, 2 * math.pi)
            r = self.k * 0.3
            self.pos_x[i] = cx + r * math.cos(angle)
            self.pos_y[i] = cy + r * math.sin(angle)

    def set_existing_positions(
        self,
        positions: Dict[bytes, Tuple[float, float]],
        pin: bool = False,
    ) -> Set[int]:
        """Set positions for known nodes, optionally pin them.

        Returns the set of internal indices for nodes that were found,
        suitable for passing to init_near_neighbors().
        """
        known = set()
        for node_id, (x, y) in positions.items():
            idx = self.id_to_idx.get(node_id)
            if idx is not None:
                self.pos_x[idx] = x
                self.pos_y[idx] = y
                if pin:
                    self.pinned.add(idx)
                known.add(idx)
        return known

    def step(self, temperature: float):
        n = self.n
        k = self.k
        k_sq = k * k
        disp_x = [0.0] * n
        disp_y = [0.0] * n
        pos_x = self.pos_x
        pos_y = self.pos_y

        # repulsive forces — skip pairs beyond cutoff (force is negligible)
        cutoff_sq = self._cutoff_sq
        for i in range(n):
            xi = pos_x[i]
            yi = pos_y[i]
            dxi = 0.0
            dyi = 0.0
            for j in range(i + 1, n):
                dx = xi - pos_x[j]
                dy = yi - pos_y[j]
                dist_sq = dx * dx + dy * dy
                if dist_sq > cutoff_sq:
                    continue
                if dist_sq < 0.0001:
                    dist_sq = 0.0001
                dist = dist_sq ** 0.5
                force = k_sq / dist
                fx = dx / dist * force
                fy = dy / dist * force
                dxi += fx
                dyi += fy
                disp_x[j] -= fx
                disp_y[j] -= fy
            disp_x[i] += dxi
            disp_y[i] += dyi

        # attractive forces along edges
        edge_pairs = self.edge_pairs
        for i, j in edge_pairs:
            dx = pos_x[i] - pos_x[j]
            dy = pos_y[i] - pos_y[j]
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < 0.01:
                dist = 0.01
            force = (dist * dist) / k
            fx = dx / dist * force
            fy = dy / dist * force
            disp_x[i] -= fx
            disp_y[i] -= fy
            disp_x[j] += fx
            disp_y[j] += fy

        # apply displacements, clamped by temperature
        pinned = self.pinned
        for i in range(n):
            if i in pinned:
                continue
            dx = disp_x[i]
            dy = disp_y[i]
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < 0.01:
                continue
            scale = min(dist, temperature) / dist
            pos_x[i] += dx * scale
            pos_y[i] += dy * scale

    def run(self, iterations: int = 80, callback: Optional[Callable] = None,
            should_stop: Optional[Callable[[], bool]] = None) -> Dict[bytes, Tuple[float, float]]:
        """Run layout for given iterations. callback(positions) called periodically.
        Update interval adapts to graph size to avoid overwhelming the GUI.
        should_stop() is checked each iteration; if it returns True, layout stops early.
        """
        temp = self.width / 10.0
        cool = temp / (iterations + 1)
        update_interval = max(10, min(40, self.n // 10))

        for it in range(iterations):
            if should_stop and should_stop():
                break
            self.step(temp)
            temp -= cool
            if temp < 0.1:
                temp = 0.1
            if callback and (it + 1) % update_interval == 0:
                callback(self.get_positions())

        return self.get_positions()

    def get_positions(self) -> Dict[bytes, Tuple[float, float]]:
        return {self.node_ids[i]: (self.pos_x[i], self.pos_y[i])
                for i in range(self.n)}


class LayoutWorker(QThread):
    """Runs force-directed layout in a background thread."""
    positions_updated = pyqtSignal(dict)
    layout_finished = pyqtSignal(dict)

    def __init__(self, nodes, edges, iterations=80, width=1000, height=1000,
                 existing_positions=None, pin_existing=False):
        super().__init__()
        self.nodes = nodes
        self.edges = edges
        self.iterations = iterations
        self.width = width
        self.height = height
        self.existing_positions = existing_positions or {}
        self.pin_existing = pin_existing
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        layout = ForceDirectedLayout(
            self.nodes, self.edges,
            width=self.width, height=self.height,
        )

        known_idxs = layout.set_existing_positions(
            self.existing_positions, pin=self.pin_existing)
        if known_idxs:
            layout.init_near_neighbors(known_idxs)

        positions = layout.run(
            iterations=self.iterations,
            callback=lambda pos: self.positions_updated.emit(pos),
            should_stop=lambda: self._stop,
        )
        self.layout_finished.emit(positions)
