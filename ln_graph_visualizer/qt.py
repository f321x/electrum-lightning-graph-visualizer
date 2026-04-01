from typing import TYPE_CHECKING, Optional, Dict, Tuple
from functools import partial

from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QDialog,
    QTextEdit, QLineEdit, QComboBox, QSpinBox, QSplitter,
    QWidget, QMenu, QApplication, QTabWidget,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont

from electrum.i18n import _
from electrum.plugin import BasePlugin, hook
from electrum.logging import get_logger
from electrum.util import format_time
from electrum.lnutil import LnFeatures

from .graph_data import (
    GraphNode, GraphEdge, extract_neighborhood, extract_path_subgraph,
    get_node_display_name,
)
from .graph_layout import LayoutWorker
from .graph_scene import GraphView, PATH_COLORS
from .pathfinding import (
    find_k_paths, find_paths_and_extract, compute_path_summary,
    parse_invoice_for_routing, InvoiceRoutingContext,
)
from .ab_ui import ABTestPanel

PATH_COLOR_NAMES = ['green', 'gold', 'orange', 'red']
assert len(PATH_COLOR_NAMES) == len(PATH_COLORS)

MODE_NEIGHBORHOOD = 0
MODE_PATH_VIEW = 1
MODE_AB_TEST = 2

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow
    from electrum.channel_db import ChannelDB
    from electrum.lnworker import LNWallet
    from electrum.simple_config import SimpleConfig

_logger = get_logger(__name__)


class DataWorker(QThread):
    """Extract graph data from channel_db in background."""
    finished = pyqtSignal(dict, dict)  # nodes, edges

    def __init__(self, channel_db, seed_node_id: bytes, depth: int = 1, max_nodes: int = 500):
        super().__init__()
        self.channel_db = channel_db
        self.seed_node_id = seed_node_id
        self.depth = depth
        self.max_nodes = max_nodes

    def run(self):
        try:
            nodes, edges = extract_neighborhood(
                self.channel_db,
                self.seed_node_id,
                depth=self.depth,
                max_nodes=self.max_nodes,
            )
            self.finished.emit(nodes, edges)
        except Exception as e:
            _logger.error(f"DataWorker error: {e}", exc_info=True)
            self.finished.emit({}, {})


class PathWorker(QThread):
    """Run pathfinding and extract path subgraphs in background."""
    finished = pyqtSignal(list, object, object)  # results, path_subgraph, context_subgraph

    def __init__(self, channel_db, source, dest, amount_msat, k):
        super().__init__()
        self.channel_db = channel_db
        self.source = source
        self.dest = dest
        self.amount_msat = amount_msat
        self.k = k

    def run(self):
        try:
            results = find_k_paths(
                self.channel_db, self.source, self.dest,
                self.amount_msat, self.k,
            )
            if results:
                paths = [r[0] for r in results]
                ctx_nodes, ctx_edges, path_nodes, path_edges = extract_path_subgraph(
                    self.channel_db, paths, context_hops=1)
                path_sub = (path_nodes, path_edges)
                ctx_sub = (ctx_nodes, ctx_edges)
            else:
                path_sub = ({}, {})
                ctx_sub = ({}, {})
            self.finished.emit(results, path_sub, ctx_sub)
        except Exception as e:
            _logger.error(f"PathWorker error: {e}", exc_info=True)
            self.finished.emit([], ({}, {}), ({}, {}))


class SearchWorker(QThread):
    """Search channel_db for a node by alias or pubkey prefix."""
    finished = pyqtSignal(object, object)  # node_id (bytes or None), alias (str or None)

    def __init__(self, channel_db, query: str):
        super().__init__()
        self.channel_db = channel_db
        self.query = query
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            query = self.query.lower()
            all_nodes = self.channel_db.get_node_infos()
            for node_id, node_info in all_nodes.items():
                if self._stop:
                    return
                if (node_info.alias and query in node_info.alias.lower()) or query in node_id.hex():
                    self.finished.emit(node_id, node_info.alias or node_id.hex()[:16] + '...')
                    return
            self.finished.emit(None, None)
        except Exception as e:
            _logger.error(f"SearchWorker error: {e}", exc_info=True)
            self.finished.emit(None, None)


class InvoicePathWorker(QThread):
    """Parse a bolt11 invoice and find routes in background."""
    finished = pyqtSignal(list, object, object, object)  # results, path_sub, ctx_sub, invoice_ctx
    error = pyqtSignal(str)

    def __init__(self, channel_db, bolt11_str, source, k, my_sending_channels=None):
        super().__init__()
        self.channel_db = channel_db
        self.bolt11_str = bolt11_str
        self.source = source
        self.k = k
        self.my_sending_channels = my_sending_channels

    def run(self):
        try:
            ctx = parse_invoice_for_routing(self.bolt11_str, self.channel_db)

            results, path_sub, ctx_sub = find_paths_and_extract(
                self.channel_db, self.source, ctx.destination,
                ctx.amount_msat, self.k,
                my_sending_channels=self.my_sending_channels,
                private_route_edges=ctx.private_route_edges)

            self.finished.emit(results, path_sub, ctx_sub, ctx)
        except Exception as e:
            _logger.error(f"InvoicePathWorker error: {e}", exc_info=True)
            self.error.emit(str(e))


class PluginDialog(QDialog):

    def __init__(
        self,
        channel_db: 'ChannelDB',
        own_pubkey: Optional[bytes],
        parent=None,
        *,
        lnworker: Optional['LNWallet'] = None,
        config: Optional['SimpleConfig'] = None,
    ):
        super().__init__(parent)
        self.channel_db = channel_db
        self.own_pubkey = own_pubkey
        self.lnworker = lnworker
        self._config = config

        self.setWindowTitle(_('LN Graph Visualizer'))
        self.setMinimumSize(1000, 700)
        self.resize(1200, 800)

        self._nodes: Dict[bytes, GraphNode] = {}
        self._edges: Dict = {}
        self._positions: Dict[bytes, Tuple[float, float]] = {}
        self._current_paths = []
        self._current_routes = []
        self._layout_worker: Optional[LayoutWorker] = None
        self._data_worker: Optional[DataWorker] = None
        self._path_worker: Optional[PathWorker] = None
        self._search_worker: Optional[SearchWorker] = None
        self._invoice_worker: Optional[InvoicePathWorker] = None
        self._pending_highlight = None  # (source, dest, amount_msat) deferred until layout finishes
        self._node_labels: Dict[bytes, str] = {}  # role labels for nodes without aliases (e.g. Sender/Recipient)
        self._scene_stale = True  # True when self._nodes/_edges changed and scene needs rebuild

        self._setup_ui()
        self._connect_signals()

        if self.own_pubkey:
            self.seed_input.setText(self.own_pubkey.hex())

        self._update_status()

    @staticmethod
    def _make_k_spinbox() -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(1, 10)
        spin.setValue(3)
        spin.setMaximumWidth(60)
        return spin

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(4)

        # --- top toolbar: mode + neighborhood controls ---
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel(_('Mode:')))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([_('Neighborhood'), _('Path View'), _('A/B Test')])
        toolbar.addWidget(self.mode_combo)
        toolbar.addSpacing(16)

        toolbar.addWidget(QLabel(_('Seed Node:')))
        self.seed_input = QLineEdit()
        self.seed_input.setPlaceholderText(_('Node pubkey (hex)'))
        self.seed_input.setMinimumWidth(200)
        toolbar.addWidget(self.seed_input, 1)

        toolbar.addWidget(QLabel(_('Depth:')))
        self.depth_spin = QSpinBox()
        self.depth_spin.setMinimum(1)
        self.depth_spin.setMaximum(3)
        self.depth_spin.setValue(1)
        toolbar.addWidget(self.depth_spin)

        self.load_btn = QPushButton(_('Load'))
        toolbar.addWidget(self.load_btn)

        toolbar.addSpacing(16)
        toolbar.addWidget(QLabel(_('Search:')))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(_('alias or pubkey prefix'))
        self.search_input.setMaximumWidth(180)
        toolbar.addWidget(self.search_input)
        self.search_btn = QPushButton(_('Find'))
        toolbar.addWidget(self.search_btn)

        main_layout.addLayout(toolbar)

        # --- pathfinding / invoice route tabs ---
        self.path_tabs = QTabWidget()

        path_widget = QWidget()
        path_layout = QHBoxLayout(path_widget)
        path_layout.setContentsMargins(6, 6, 6, 4)

        path_layout.addWidget(QLabel(_('Source:')))
        self.source_input = QLineEdit()
        self.source_input.setPlaceholderText(_('Source pubkey (hex)'))
        path_layout.addWidget(self.source_input, 1)

        path_layout.addWidget(QLabel(_('Dest:')))
        self.dest_input = QLineEdit()
        self.dest_input.setPlaceholderText(_('Destination pubkey (hex)'))
        path_layout.addWidget(self.dest_input, 1)

        path_layout.addWidget(QLabel(_('Amount (sat):')))
        self.amount_input = QLineEdit()
        self.amount_input.setPlaceholderText('100000')
        self.amount_input.setMaximumWidth(120)
        path_layout.addWidget(self.amount_input)

        path_layout.addWidget(QLabel(_('Paths:')))
        self.k_spin = self._make_k_spinbox()
        path_layout.addWidget(self.k_spin)

        self.find_paths_btn = QPushButton(_('Find Paths'))
        path_layout.addWidget(self.find_paths_btn)
        self.clear_paths_btn = QPushButton(_('Clear'))
        path_layout.addWidget(self.clear_paths_btn)

        self.path_tabs.addTab(path_widget, _('Pathfinding'))

        inv_widget = QWidget()
        inv_layout = QHBoxLayout(inv_widget)
        inv_layout.setContentsMargins(6, 6, 6, 4)

        inv_layout.addWidget(QLabel(_('Invoice:')))
        self.invoice_input = QLineEdit()
        self.invoice_input.setPlaceholderText(_('lnbc1...'))
        inv_layout.addWidget(self.invoice_input, 1)

        inv_layout.addWidget(QLabel(_('Paths:')))
        self.invoice_k_spin = self._make_k_spinbox()
        inv_layout.addWidget(self.invoice_k_spin)

        self.find_invoice_btn = QPushButton(_('Find Invoice Route'))
        inv_layout.addWidget(self.find_invoice_btn)
        self.clear_invoice_btn = QPushButton(_('Clear'))
        inv_layout.addWidget(self.clear_invoice_btn)

        self.path_tabs.addTab(inv_widget, _('Invoice Route'))

        main_layout.addWidget(self.path_tabs)

        # --- A/B test panel (hidden by default) ---
        self.ab_panel = ABTestPanel(
            self.channel_db, self.own_pubkey, self.lnworker, self._config, parent=self,
        )
        self.ab_panel.setVisible(False)
        main_layout.addWidget(self.ab_panel)

        # --- main area: graph + info panel ---
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.graph_view = GraphView()
        splitter.addWidget(self.graph_view)

        # info panel
        info_widget = QWidget()
        info_widget.setMinimumWidth(250)
        info_widget.setMaximumWidth(360)
        info_layout = QVBoxLayout(info_widget)
        info_layout.setContentsMargins(4, 4, 4, 4)

        info_layout.addWidget(QLabel(_('Details')))
        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setFont(QFont('Monospace', 9))
        info_layout.addWidget(self.detail_text, 1)

        info_layout.addWidget(QLabel(_('Path Results')))
        self.path_text = QTextEdit()
        self.path_text.setReadOnly(True)
        self.path_text.setFont(QFont('Monospace', 9))
        info_layout.addWidget(self.path_text, 1)

        splitter.addWidget(info_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        main_layout.addWidget(splitter, 1)

        # --- status bar ---
        status_layout = QHBoxLayout()
        self.status_label = QLabel()
        status_layout.addWidget(self.status_label, 1)
        status_layout.addWidget(QLabel(_('Min capacity:')))
        self.capacity_filter = QComboBox()
        self._capacity_thresholds = [
            (0, _('All')),
            (10_000, '> 10K sat'),
            (100_000, '> 100K sat'),
            (1_000_000, '> 1M sat'),
            (10_000_000, '> 10M sat'),
            (100_000_000, '> 1 BTC'),
        ]
        for _val, label in self._capacity_thresholds:
            self.capacity_filter.addItem(label)
        status_layout.addWidget(self.capacity_filter)

        self.fit_btn = QPushButton(_('Fit View'))
        status_layout.addWidget(self.fit_btn)
        self.relayout_btn = QPushButton(_('Re-layout'))
        status_layout.addWidget(self.relayout_btn)
        main_layout.addLayout(status_layout)

    def _connect_signals(self):
        self.load_btn.clicked.connect(self._on_load)
        self.find_paths_btn.clicked.connect(self._on_find_paths)
        self.clear_paths_btn.clicked.connect(self._on_clear_paths)
        self.fit_btn.clicked.connect(self.graph_view.fit_view)
        self.relayout_btn.clicked.connect(self._on_relayout)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.search_btn.clicked.connect(self._on_search)
        self.search_input.returnPressed.connect(self._on_search)
        self.capacity_filter.currentIndexChanged.connect(self._on_capacity_filter_changed)

        self.find_invoice_btn.clicked.connect(self._on_find_invoice_paths)
        self.clear_invoice_btn.clicked.connect(self._on_clear_paths)
        self.invoice_input.returnPressed.connect(self._on_find_invoice_paths)

        self.graph_view.node_clicked.connect(self._on_node_clicked)
        self.graph_view.edge_clicked.connect(self._on_edge_clicked)
        self.graph_view.node_double_clicked.connect(self._on_node_double_clicked)
        self.graph_view.node_context_menu.connect(self._on_node_context_menu)

    def _on_mode_changed(self, index):
        self.path_tabs.setVisible(index != MODE_AB_TEST)
        self.ab_panel.setVisible(index == MODE_AB_TEST)

    # --- actions ---

    def _parse_pubkey(self, text: str) -> Optional[bytes]:
        text = text.strip()
        if not text:
            return None
        try:
            b = bytes.fromhex(text)
            if len(b) == 33:
                return b
        except ValueError:
            pass
        # try prefix search in channel_db
        try:
            prefix = bytes.fromhex(text)
            return self.channel_db.get_node_by_prefix(prefix)
        except Exception:
            return None

    @staticmethod
    def _stop_worker(worker):
        if worker is None:
            return
        try:
            worker.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            if worker.isRunning():
                if hasattr(worker, 'stop'):
                    worker.stop()
                worker.quit()
                worker.wait(2000)
            worker.deleteLater()
        except RuntimeError:
            pass

    @staticmethod
    def _append_policy_lines(lines, policy):
        if policy:
            lines.append(f"  -> Fee: {policy.fee_base_msat} msat + {policy.fee_proportional_millionths} ppm")
            lines.append(f"  -> CLTV delta: {policy.cltv_delta}")
            lines.append(f"  -> HTLC min: {policy.htlc_minimum_msat} msat")
            max_str = f"{policy.htlc_maximum_msat}" if policy.htlc_maximum_msat else "none"
            lines.append(f"  -> HTLC max: {max_str} msat")
            lines.append(f"  -> Disabled: {policy.is_disabled}")
            lines.append(f"  -> Timestamp: {format_time(policy.timestamp)}")
        else:
            lines.append(f"  -> (no policy)")

    def _on_load(self):
        seed = self._parse_pubkey(self.seed_input.text())
        if seed is None:
            self.status_label.setText(_('Invalid seed node pubkey'))
            return
        depth = self.depth_spin.value()
        self.status_label.setText(_('Loading neighborhood...'))
        self.load_btn.setEnabled(False)

        self._stop_worker(self._data_worker)
        self._data_worker = DataWorker(
            self.channel_db, seed, depth=depth, max_nodes=500,
        )
        self._data_worker.finished.connect(self._on_data_loaded)
        self._data_worker.start()

    def _on_data_loaded(self, nodes, edges):
        self.load_btn.setEnabled(True)
        self._data_worker = None
        if not nodes:
            self.status_label.setText(_('No nodes found for this pubkey'))
            return
        self._nodes = nodes
        self._edges = edges
        self._scene_stale = True
        self._run_layout(nodes, edges)

    def _run_layout(self, nodes, edges, existing_positions=None, pin_existing=False):
        self.status_label.setText(_('Computing layout...'))
        self.relayout_btn.setEnabled(False)

        self._stop_worker(self._layout_worker)

        self._layout_worker = LayoutWorker(
            nodes, edges, iterations=80,
            existing_positions=existing_positions or {},
            pin_existing=pin_existing,
        )
        self._layout_worker.positions_updated.connect(self._apply_positions)
        self._layout_worker.layout_finished.connect(self._on_layout_finished)
        self._layout_worker.start()

    def _get_min_capacity_filter(self) -> int:
        idx = self.capacity_filter.currentIndex()
        if 0 < idx < len(self._capacity_thresholds):
            return self._capacity_thresholds[idx][0]
        return 0

    def _apply_positions(self, positions):
        if self._scene_stale:
            self.graph_view.build_graph(self._nodes, self._edges, positions)
            self._scene_stale = False
            # build_graph recreates all scene items, so visibility from the
            # capacity filter must be restored
            min_cap = self._get_min_capacity_filter()
            if min_cap > 0:
                self.graph_view.filter_by_capacity(min_cap)
        else:
            self.graph_view.update_positions(positions)
        self._positions = positions

    def _on_layout_finished(self, positions):
        self._apply_positions(positions)
        self.graph_view.fit_view()
        self.relayout_btn.setEnabled(True)
        # apply any pending path highlights
        if self._pending_highlight and self._current_paths:
            source, dest, amount_msat = self._pending_highlight
            self._pending_highlight = None
            self._apply_path_highlights(source, dest, amount_msat)
        self._update_status()

    def _on_capacity_filter_changed(self, index: int):
        min_cap = self._get_min_capacity_filter()
        self.graph_view.filter_by_capacity(min_cap)
        self._update_status()

    def _on_relayout(self):
        if self._nodes:
            self._run_layout(self._nodes, self._edges)

    # --- node expansion ---

    def _on_node_double_clicked(self, node_id: bytes):
        self.status_label.setText(_('Expanding neighborhood...'))
        self._stop_worker(self._data_worker)
        self._data_worker = DataWorker(
            self.channel_db, node_id, depth=1, max_nodes=200,
        )
        self._data_worker.finished.connect(
            partial(self._on_expand_loaded, node_id))
        self._data_worker.start()

    def _on_expand_loaded(self, center_node_id, nodes, edges):
        self._data_worker = None
        if not nodes:
            self._update_status()
            return

        MAX_TOTAL_NODES = 800
        if len(self._nodes) >= MAX_TOTAL_NODES:
            self.status_label.setText(
                _('Node limit reached ({})').format(MAX_TOTAL_NODES))
            return

        new_nodes = {nid: n for nid, n in nodes.items() if nid not in self._nodes}
        new_edges = {scid: e for scid, e in edges.items() if scid not in self._edges}

        if not new_nodes and not new_edges:
            self.status_label.setText(_('No new nodes to expand'))
            return

        self._nodes.update(new_nodes)
        self._edges.update(new_edges)
        self._scene_stale = True

        # run layout with existing positions pinned
        existing_pos = self.graph_view.get_current_positions()
        self._run_layout(self._nodes, self._edges,
                         existing_positions=existing_pos, pin_existing=True)

    # --- pathfinding ---

    def _on_find_paths(self):
        source = self._parse_pubkey(self.source_input.text())
        dest = self._parse_pubkey(self.dest_input.text())
        if source is None or dest is None:
            self.status_label.setText(_('Invalid source or destination pubkey'))
            return
        amount_text = self.amount_input.text().strip() or '100000'
        try:
            amount_sat = int(amount_text)
        except ValueError:
            self.status_label.setText(_('Invalid amount'))
            return
        amount_msat = amount_sat * 1000
        k = self.k_spin.value()

        self._node_labels.clear()
        self.status_label.setText(_('Finding paths...'))
        self.find_paths_btn.setEnabled(False)

        self._stop_worker(self._path_worker)
        self._path_worker = PathWorker(
            self.channel_db, source, dest, amount_msat, k)
        self._path_worker.finished.connect(
            partial(self._on_manual_paths_found, source, dest, amount_msat))
        self._path_worker.start()

    def _on_manual_paths_found(self, source, dest, amount_msat, results, path_sub, ctx_sub):
        self.find_paths_btn.setEnabled(True)
        self._path_worker = None
        self._on_paths_found(source, dest, amount_msat, results, path_sub, ctx_sub)

    def _on_paths_found(self, source, dest, amount_msat, results, path_sub, ctx_sub):
        if not results:
            self.status_label.setText(_('No paths found'))
            self.path_text.setPlainText(_('No paths found between these nodes.'))
            return

        self._current_paths = [r[0] for r in results]
        self._current_routes = [r[1] for r in results]

        # switch to path view mode if selected
        if self.mode_combo.currentIndex() == MODE_PATH_VIEW:
            # path view mode — rebuild graph with path subgraph
            path_nodes, path_edges = ctx_sub
            self._nodes = path_nodes
            self._edges = path_edges
            self._scene_stale = True
            self._pending_highlight = (source, dest, amount_msat)
            self._run_layout(path_nodes, path_edges)
        else:
            # neighborhood mode — add path nodes to existing graph if missing
            path_nodes, path_edges = path_sub

            new_nodes = {nid: n for nid, n in path_nodes.items() if nid not in self._nodes}
            new_edges = {scid: e for scid, e in path_edges.items() if scid not in self._edges}

            if new_nodes or new_edges:
                self._nodes.update(new_nodes)
                self._edges.update(new_edges)
                self._scene_stale = True
                existing_pos = self.graph_view.get_current_positions()
                self._pending_highlight = (source, dest, amount_msat)
                self._run_layout(self._nodes, self._edges,
                                 existing_positions=existing_pos, pin_existing=True)
            else:
                self._apply_path_highlights(source, dest, amount_msat)

    def _node_display(self, node: Optional[GraphNode], node_id: bytes) -> str:
        if node_id in self._node_labels:
            return self._node_labels[node_id]
        return get_node_display_name(node, node_id)

    def _apply_path_highlights(self, source, dest, amount_msat):
        self.graph_view.highlight_paths(self._current_paths, source, dest)

        # show path summaries
        lines = []
        for i, route in enumerate(self._current_routes):
            summary = compute_path_summary(route, amount_msat)
            cname = PATH_COLOR_NAMES[min(i, len(PATH_COLOR_NAMES) - 1)]
            lines.append(f"--- Path {i + 1} ({cname}) ---")
            lines.append(f"  Hops: {summary['hop_count']}")
            lines.append(f"  Fee:  {summary['total_fee_msat']} msat")
            lines.append(f"  CLTV: {summary['total_cltv']} blocks")
            # show each hop
            for j, edge in enumerate(self._current_paths[i]):
                src_item = self._nodes.get(edge.start_node)
                dst_item = self._nodes.get(edge.end_node)
                src_name = self._node_display(src_item, edge.start_node)
                dst_name = self._node_display(dst_item, edge.end_node)
                lines.append(f"  {j + 1}. {src_name} -> {dst_name}")
                lines.append(f"     chan: {edge.short_channel_id}")
                if j < len(route):
                    hop = route[j]
                    lines.append(f"     fee: {hop.fee_base_msat} + {hop.fee_proportional_millionths} ppm")
                    lines.append(f"     cltv: {hop.cltv_delta}")
            lines.append('')

        self.path_text.setPlainText('\n'.join(lines))
        self._update_status()

    def _on_clear_paths(self):
        self._current_paths = []
        self._current_routes = []
        self._node_labels.clear()
        self.graph_view.clear_highlights()
        self.path_text.clear()
        self._update_status()

    # --- invoice pathfinding ---

    def _on_find_invoice_paths(self):
        bolt11 = self.invoice_input.text().strip()
        if not bolt11:
            self.status_label.setText(_('Please paste a bolt11 invoice'))
            return
        if self.own_pubkey is None:
            self.status_label.setText(_('No Lightning wallet available — cannot determine source node'))
            return

        k = self.invoice_k_spin.value()
        self.status_label.setText(_('Parsing invoice and finding routes...'))
        self.find_invoice_btn.setEnabled(False)

        my_sending_channels = None
        if self.lnworker:
            my_sending_channels = {
                chan.short_channel_id: chan
                for chan in self.lnworker.get_channels_for_sending()
                if chan.short_channel_id is not None
            }

        self._stop_worker(self._invoice_worker)
        self._invoice_worker = InvoicePathWorker(
            self.channel_db, bolt11, self.own_pubkey, k,
            my_sending_channels=my_sending_channels)
        self._invoice_worker.finished.connect(self._on_invoice_paths_found)
        self._invoice_worker.error.connect(self._on_invoice_error)
        self._invoice_worker.start()

    def _on_invoice_paths_found(self, results, path_sub, ctx_sub, invoice_ctx: InvoiceRoutingContext):
        self.find_invoice_btn.setEnabled(True)

        dest_hex = invoice_ctx.destination.hex()

        self.source_input.setText(self.own_pubkey.hex())
        self.dest_input.setText(dest_hex)
        self.amount_input.setText(str(invoice_ctx.amount_msat // 1000))

        self._node_labels.clear()
        self._node_labels.update({
            self.own_pubkey: _('Sender'),
            invoice_ctx.destination: _('Recipient'),
        })

        # node may not be in self._nodes yet if it only appeared in the path subgraph
        dest_node = ctx_sub[0].get(invoice_ctx.destination) or self._nodes.get(invoice_ctx.destination)
        dest_name = self._node_display(dest_node, invoice_ctx.destination)
        lines = [
            f"=== Invoice ===",
            f"Destination: {dest_name} ({dest_hex[:16]}...)",
            f"Amount:      {invoice_ctx.amount_msat // 1000} sat ({invoice_ctx.amount_msat} msat)",
            f"Description: {invoice_ctx.description or '(none)'}",
            f"Payment Hash: {invoice_ctx.payment_hash.hex()}",
            f"Route Hints: {invoice_ctx.route_hint_count} private path(s)",
            f"CLTV Delta:  {invoice_ctx.min_final_cltv_delta}",
        ]
        self.detail_text.setPlainText('\n'.join(lines))

        self._invoice_worker = None
        self._on_paths_found(self.own_pubkey, invoice_ctx.destination,
                             invoice_ctx.amount_msat, results, path_sub, ctx_sub)

    def _on_invoice_error(self, error_msg: str):
        self.find_invoice_btn.setEnabled(True)
        self._invoice_worker = None
        self.status_label.setText(_('Invoice error: {}').format(error_msg))

    # --- search ---

    def _on_search(self):
        query = self.search_input.text().strip().lower()
        if not query:
            return
        # search in currently displayed nodes first
        for node_id, node in self._nodes.items():
            if (node.alias and query in node.alias.lower()) or query in node_id.hex():
                item = self.graph_view.get_node_item(node_id)
                if item:
                    self.graph_view.centerOn(item)
                    self._on_node_clicked(node_id)
                    self.status_label.setText(
                        _('Found: {}').format(get_node_display_name(node)))
                    return
        # search in full channel_db in background
        self.status_label.setText(_('Searching...'))
        self.search_btn.setEnabled(False)
        self._stop_worker(self._search_worker)
        self._search_worker = SearchWorker(self.channel_db, query)
        self._search_worker.finished.connect(self._on_db_search_result)
        self._search_worker.start()

    def _on_db_search_result(self, node_id, alias):
        self.search_btn.setEnabled(True)
        self._search_worker = None
        if node_id is not None:
            self.seed_input.setText(node_id.hex())
            self.status_label.setText(
                _('Found in DB: {} — click Load to view').format(alias))
        else:
            query = self.search_input.text().strip()
            self.status_label.setText(_('No node found matching: {}').format(query))

    # --- detail panel ---

    def _on_node_clicked(self, node_id: bytes):
        node = self._nodes.get(node_id)
        if node is None:
            return
        lines = [
            f"=== Node ===",
            f"Alias:    {node.alias or '(none)'}",
            f"Pubkey:   {node.node_id.hex()}",
            f"Features: {', '.join(LnFeatures(node.features).get_names()) or '(none)'}",
            f"Channels: {node.channel_count}",
        ]
        if node.addresses:
            lines.append(f"Addresses:")
            for addr in node.addresses[:5]:
                lines.append(f"  {addr}")
            if len(node.addresses) > 5:
                lines.append(f"  ... and {len(node.addresses) - 5} more")
        self.detail_text.setPlainText('\n'.join(lines))

    def _on_edge_clicked(self, scid):
        edge = self._edges.get(scid)
        if edge is None:
            return

        n1 = self._nodes.get(edge.node1_id)
        n2 = self._nodes.get(edge.node2_id)
        n1_name = self._node_display(n1, edge.node1_id)
        n2_name = self._node_display(n2, edge.node2_id)

        cap_str = f"{edge.capacity_sat:,}" if edge.capacity_sat else "unknown"

        lines = [
            f"=== Channel ===",
            f"SCID:     {edge.short_channel_id}",
            f"Capacity: {cap_str} sat",
            f"",
            f"Node 1: {n1_name}",
            f"  {edge.node1_id.hex()}",
        ]
        self._append_policy_lines(lines, edge.policy_1to2)
        lines.append(f"")
        lines.append(f"Node 2: {n2_name}")
        lines.append(f"  {edge.node2_id.hex()}")
        self._append_policy_lines(lines, edge.policy_2to1)

        self.detail_text.setPlainText('\n'.join(lines))

    # --- context menu ---

    def _on_node_context_menu(self, node_id: bytes, global_pos):
        menu = QMenu(self)
        menu.addAction(_('Set as Source'), lambda: self._set_as_source(node_id))
        menu.addAction(_('Set as Destination'), lambda: self._set_as_dest(node_id))
        menu.addSeparator()
        menu.addAction(_('Expand Neighborhood'), lambda: self._on_node_double_clicked(node_id))
        menu.addAction(_('Load as Seed'), lambda: self._load_as_seed(node_id))
        menu.addSeparator()
        menu.addAction(_('Copy Pubkey'), lambda: QApplication.clipboard().setText(node_id.hex()))
        menu.exec(global_pos)

    def _set_as_source(self, node_id: bytes):
        self.source_input.setText(node_id.hex())

    def _set_as_dest(self, node_id: bytes):
        self.dest_input.setText(node_id.hex())

    def _load_as_seed(self, node_id: bytes):
        self.seed_input.setText(node_id.hex())
        self._on_load()

    # --- status ---

    def _update_status(self):
        n_edges = len(self._edges)
        n_nodes = len(self._nodes)
        n_paths = len(self._current_paths)
        db_nodes = self.channel_db.num_nodes
        db_chans = self.channel_db.num_channels

        if self._get_min_capacity_filter() > 0:
            v_edges = self.graph_view.visible_edge_count()
            v_nodes = self.graph_view.visible_node_count()
            parts = [f"Showing: {v_nodes}/{n_nodes} nodes, {v_edges}/{n_edges} channels"]
        else:
            parts = [f"Showing: {n_nodes} nodes, {n_edges} channels"]

        parts.append(f"DB: {db_nodes} nodes, {db_chans} channels")
        if n_paths:
            parts.append(f"Paths: {n_paths}")
        self.status_label.setText(' | '.join(parts))

    def closeEvent(self, event):
        self.ab_panel.stop_worker()
        for worker in (self._layout_worker, self._data_worker, self._path_worker,
                       self._search_worker, self._invoice_worker):
            self._stop_worker(worker)
        super().closeEvent(event)


class Plugin(BasePlugin):

    def __init__(self, parent, config, name):
        BasePlugin.__init__(self, parent, config, name)
        self._dialogs: Dict[int, PluginDialog] = {}

    @hook
    def init_menubar(self, window: 'ElectrumWindow'):
        from electrum.gui.qt.util import read_QIcon_from_bytes
        action = window.tools_menu.addAction(
            _('LN Graph Visualizer'),
            partial(self.show_dialog, window),
        )
        action.setIcon(read_QIcon_from_bytes(self.read_file('ln_graph.png')))

    def show_dialog(self, window: 'ElectrumWindow'):
        network = window.network
        if not network or not network.channel_db:
            window.show_message(
                _('Lightning gossip data not available.\n'
                  'Make sure LIGHTNING_USE_GOSSIP is enabled and gossip data has been synced.'))
            return

        win_id = id(window)
        if win_id in self._dialogs and self._dialogs[win_id].isVisible():
            self._dialogs[win_id].raise_()
            self._dialogs[win_id].activateWindow()
            return

        own_pubkey = None
        lnworker = None
        wallet = window.wallet
        if wallet and hasattr(wallet, 'lnworker') and wallet.lnworker:
            lnworker = wallet.lnworker
            try:
                own_pubkey = lnworker.node_keypair.pubkey
            except Exception:
                pass

        dialog = PluginDialog(
            network.channel_db, own_pubkey, parent=window,
            lnworker=lnworker, config=self.config,
        )
        self._dialogs[win_id] = dialog
        dialog.finished.connect(lambda _=None, wid=win_id: self._dialogs.pop(wid, None))
        dialog.showMaximized()

    @hook
    def on_close_window(self, window: 'ElectrumWindow'):
        win_id = id(window)
        dialog = self._dialogs.pop(win_id, None)
        if dialog:
            dialog.close()

    def on_close(self):
        for dialog in self._dialogs.values():
            dialog.close()
        self._dialogs.clear()
