import math
from typing import Dict, Optional, Tuple, TYPE_CHECKING

from PyQt6.QtWidgets import (
    QGraphicsScene, QGraphicsView, QGraphicsEllipseItem,
    QGraphicsPathItem, QGraphicsItem,
)
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QPainterPath,
    QPainterPathStroker, QWheelEvent, QMouseEvent,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPointF

if TYPE_CHECKING:
    from .graph_data import GraphNode, GraphEdge
    from electrum.util import ShortChannelID

# path highlight colors
PATH_COLORS = [
    QColor('#2ecc71'),  # green
    QColor('#f1c40f'),  # gold
    QColor('#e67e22'),  # orange
    QColor('#e74c3c'),  # red
]

NODE_DEFAULT_COLOR = QColor('#5dade2')
NODE_BORDER_COLOR = QColor('#2c3e50')
NODE_SOURCE_COLOR = QColor('#27ae60')
NODE_DEST_COLOR = QColor('#8e44ad')
EDGE_DEFAULT_COLOR = QColor('#95a5a6')
EDGE_DISABLED_COLOR = QColor('#d5dbdb')


def _node_radius(channel_count: int) -> float:
    return max(5.0, min(25.0, math.log2(channel_count + 2) * 4))


def _edge_width(capacity_sat: Optional[int]) -> float:
    if capacity_sat is None or capacity_sat <= 0:
        return 1.5
    return max(1.0, min(5.0, math.log2(capacity_sat / 100_000 + 1)))


class NodeItem(QGraphicsEllipseItem):

    def __init__(self, node: 'GraphNode', parent=None):
        self.node = node
        r = _node_radius(node.channel_count)
        super().__init__(-r, -r, 2 * r, 2 * r, parent)

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setZValue(10)

        self._base_color = QColor(node.color) if node.color else NODE_DEFAULT_COLOR
        self.setBrush(QBrush(self._base_color))
        self.setPen(QPen(NODE_BORDER_COLOR, 1.5))

        alias = node.alias or node.node_id.hex()[:16]
        self.setToolTip(f"{alias}\n{node.node_id.hex()[:20]}...")

        self._edges: list = []  # EdgeItems connected to this node
        self._highlight_color: Optional[QColor] = None
        self._is_source = False
        self._is_dest = False

    def add_edge(self, edge_item: 'EdgeItem'):
        self._edges.append(edge_item)

    def set_highlight(self, color: Optional[QColor] = None):
        self._highlight_color = color
        if color:
            self.setPen(QPen(color, 3.0))
        else:
            self.setPen(QPen(NODE_BORDER_COLOR, 1.5))

    def _set_role(self, active: bool, color: QColor, other_active: bool):
        if active:
            self.setBrush(QBrush(color))
            self.setPen(QPen(color.darker(150), 3.0))
        elif not other_active:
            self.setBrush(QBrush(self._base_color))
            self.setPen(QPen(NODE_BORDER_COLOR, 1.5))

    def set_source(self, is_source: bool):
        self._is_source = is_source
        self._set_role(is_source, NODE_SOURCE_COLOR, self._is_dest)

    def set_dest(self, is_dest: bool):
        self._is_dest = is_dest
        self._set_role(is_dest, NODE_DEST_COLOR, self._is_source)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for edge_item in self._edges:
                edge_item.update_position()
        return super().itemChange(change, value)


class EdgeItem(QGraphicsPathItem):

    def __init__(self, edge: 'GraphEdge',
                 node1_item: NodeItem, node2_item: NodeItem,
                 parallel_index: int = 0, parallel_count: int = 1,
                 parent=None):
        super().__init__(parent)
        self.edge = edge
        self.node1_item = node1_item
        self.node2_item = node2_item
        self._parallel_index = parallel_index
        self._parallel_count = parallel_count

        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setZValue(1)

        cap_str = f"{edge.capacity_sat:,} sat" if edge.capacity_sat else "unknown"
        self.setToolTip(f"Channel: {edge.short_channel_id}\nCapacity: {cap_str}")

        # channel is disabled if all existing policies are disabled (or no policies exist)
        p1_disabled = edge.policy_1to2 is None or edge.policy_1to2.is_disabled
        p2_disabled = edge.policy_2to1 is None or edge.policy_2to1.is_disabled
        self._is_disabled = p1_disabled and p2_disabled

        self._default_width = _edge_width(edge.capacity_sat)
        self._highlight_color: Optional[QColor] = None
        self._highlight_width: float = self._default_width

        self._apply_style()

        node1_item.add_edge(self)
        node2_item.add_edge(self)
        self.update_position()

    def _apply_style(self):
        if self._highlight_color:
            pen = QPen(self._highlight_color, self._highlight_width)
        elif self._is_disabled:
            pen = QPen(EDGE_DISABLED_COLOR, self._default_width, Qt.PenStyle.DashLine)
        else:
            pen = QPen(EDGE_DEFAULT_COLOR, self._default_width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        self.setPen(pen)

    def set_highlight(self, color: Optional[QColor] = None, width: float = 0):
        self._highlight_color = color
        self._highlight_width = width if width > 0 else self._default_width
        if color:
            self.setZValue(5)
        else:
            self.setZValue(1)
        self._apply_style()

    def update_position(self):
        p1 = self.node1_item.scenePos()
        p2 = self.node2_item.scenePos()
        path = QPainterPath()
        path.moveTo(p1)
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        dist = math.sqrt(dx * dx + dy * dy)
        if dist < 1:
            path.lineTo(p2)
        else:
            if self._parallel_count <= 1:
                offset = min(15.0, dist * 0.1)
            else:
                spacing = 15.0
                raw = spacing * (self._parallel_index - (self._parallel_count - 1) / 2)
                # cap so curves don't loop past endpoints on short edges
                offset = max(-dist * 0.4, min(dist * 0.4, raw))
            nx = -dy / dist * offset
            ny = dx / dist * offset
            mid = QPointF((p1.x() + p2.x()) / 2 + nx,
                          (p1.y() + p2.y()) / 2 + ny)
            path.quadTo(mid, p2)
        self.setPath(path)

    def shape(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(max(10.0, self._default_width + 6))
        return stroker.createStroke(self.path())


class GraphView(QGraphicsView):
    """QGraphicsView with zoom and pan support."""
    node_clicked = pyqtSignal(bytes)       # node_id
    edge_clicked = pyqtSignal(object)      # ShortChannelID
    node_double_clicked = pyqtSignal(bytes)
    node_context_menu = pyqtSignal(bytes, object)  # node_id, QPoint(global)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)

        self._node_items: Dict[bytes, NodeItem] = {}
        self._edge_items: Dict['ShortChannelID', EdgeItem] = {}

    def wheelEvent(self, event: QWheelEvent):
        factor = 1.15
        if event.angleDelta().y() > 0:
            self.scale(factor, factor)
        else:
            self.scale(1 / factor, 1 / factor)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.pos())
            if isinstance(item, NodeItem):
                self.node_clicked.emit(item.node.node_id)
            elif isinstance(item, EdgeItem):
                self.edge_clicked.emit(item.edge.short_channel_id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        item = self.itemAt(event.pos())
        if isinstance(item, NodeItem):
            self.node_double_clicked.emit(item.node.node_id)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        item = self.itemAt(event.pos())
        if isinstance(item, NodeItem):
            self.node_context_menu.emit(item.node.node_id, event.globalPos())
        else:
            super().contextMenuEvent(event)

    def clear_graph(self):
        self._scene.clear()
        self._node_items.clear()
        self._edge_items.clear()

    def build_graph(
        self,
        nodes: Dict[bytes, 'GraphNode'],
        edges: Dict['ShortChannelID', 'GraphEdge'],
        positions: Dict[bytes, Tuple[float, float]],
    ):
        """Build the scene from data + positions."""
        self.clear_graph()

        # disable BSP indexing during batch construction — avoids
        # re-indexing the spatial tree after every addItem() call
        self._scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)

        # create node items
        for node_id, node in nodes.items():
            item = NodeItem(node)
            pos = positions.get(node_id, (0, 0))
            item.setPos(pos[0], pos[1])
            self._scene.addItem(item)
            self._node_items[node_id] = item

        # ChannelInfo guarantees node1_id < node2_id, so the tuple is canonical
        pair_edges: Dict[tuple, list] = {}
        for scid, edge in edges.items():
            pair_edges.setdefault((edge.node1_id, edge.node2_id), []).append((scid, edge))

        for edge_list in pair_edges.values():
            count = len(edge_list)
            for idx, (scid, edge) in enumerate(edge_list):
                n1 = self._node_items.get(edge.node1_id)
                n2 = self._node_items.get(edge.node2_id)
                if n1 is None or n2 is None:
                    continue
                item = EdgeItem(edge, n1, n2, parallel_index=idx, parallel_count=count)
                self._scene.addItem(item)
                self._edge_items[scid] = item

        self._scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.BspTreeIndex)

    def update_positions(self, positions: Dict[bytes, Tuple[float, float]]):
        """Update node positions (e.g. during layout iterations).
        Temporarily disables geometry-change notifications so each edge
        is updated once at the end, not once per connected node move.
        """
        # disable itemChange edge-updates during batch move
        flag = QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        moved_nodes = []
        for node_id, (x, y) in positions.items():
            item = self._node_items.get(node_id)
            if item:
                item.setFlag(flag, False)
                item.setPos(x, y)
                moved_nodes.append(item)
        # re-enable and update all edges once
        for item in moved_nodes:
            item.setFlag(flag, True)
        edges_updated: set = set()
        for item in moved_nodes:
            for edge_item in item._edges:
                if edge_item not in edges_updated:
                    edges_updated.add(edge_item)
                    edge_item.update_position()

    def highlight_paths(
        self,
        paths,  # list of list of PathEdge
        source_id: Optional[bytes] = None,
        dest_id: Optional[bytes] = None,
    ):
        """Highlight found paths with distinct colors."""
        # reset all highlights
        for item in self._node_items.values():
            item.set_highlight(None)
            item.set_source(False)
            item.set_dest(False)
        for item in self._edge_items.values():
            item.set_highlight(None)

        # highlight each path
        path_nodes_by_rank: Dict[bytes, int] = {}  # node_id -> best path index
        for path_idx, path in enumerate(paths):
            color = PATH_COLORS[min(path_idx, len(PATH_COLORS) - 1)]
            width = 4.0 if path_idx == 0 else 3.0
            for edge in path:
                edge_item = self._edge_items.get(edge.short_channel_id)
                if edge_item:
                    edge_item.set_highlight(color, width)
                for nid in (edge.start_node, edge.end_node):
                    if nid not in path_nodes_by_rank:
                        path_nodes_by_rank[nid] = path_idx
                        node_item = self._node_items.get(nid)
                        if node_item:
                            node_item.set_highlight(color)

        # mark source and dest
        if source_id:
            item = self._node_items.get(source_id)
            if item:
                item.set_source(True)
        if dest_id:
            item = self._node_items.get(dest_id)
            if item:
                item.set_dest(True)

    def clear_highlights(self):
        for item in self._node_items.values():
            item.set_highlight(None)
            item.set_source(False)
            item.set_dest(False)
        for item in self._edge_items.values():
            item.set_highlight(None)

    def has_nodes(self) -> bool:
        return bool(self._node_items)

    def get_node_item(self, node_id: bytes) -> 'Optional[NodeItem]':
        return self._node_items.get(node_id)

    def fit_view(self):
        rect = self._scene.itemsBoundingRect()
        if not rect.isEmpty():
            rect.adjust(-50, -50, 50, 50)
            self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    def filter_by_capacity(self, min_capacity_sat: int):
        """Hide edges below min_capacity_sat and orphaned nodes."""
        for edge_item in self._edge_items.values():
            cap = edge_item.edge.capacity_sat or 0
            edge_item.setVisible(cap >= min_capacity_sat)

        # hide nodes whose visible edges all got filtered out
        for node_item in self._node_items.values():
            if min_capacity_sat <= 0:
                node_item.setVisible(True)
            else:
                has_visible = any(e.isVisible() for e in node_item._edges)
                node_item.setVisible(has_visible or not node_item._edges)

    def visible_edge_count(self) -> int:
        return sum(1 for e in self._edge_items.values() if e.isVisible())

    def visible_node_count(self) -> int:
        return sum(1 for n in self._node_items.values() if n.isVisible())

    def get_current_positions(self) -> Dict[bytes, Tuple[float, float]]:
        """Get current node positions (may have been moved by user)."""
        return {nid: (item.scenePos().x(), item.scenePos().y())
                for nid, item in self._node_items.items()}
