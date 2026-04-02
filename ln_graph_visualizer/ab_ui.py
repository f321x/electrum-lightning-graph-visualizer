from typing import Optional, TYPE_CHECKING

from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QWidget,
    QTextEdit, QLineEdit, QComboBox, QSpinBox, QGroupBox,
    QProgressBar, QFormLayout, QDialog, QDialogButtonBox,
    QApplication, QCheckBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from electrum.i18n import _
from electrum.logging import get_logger
from electrum.util import format_time

from .ab_testing import (
    ExperimentRun, list_experiments, load_experiment, delete_experiment,
)
from .ab_worker import ProbeWorker

if TYPE_CHECKING:
    from electrum.lnworker import LNWallet
    from electrum.channel_db import ChannelDB
    from electrum.simple_config import SimpleConfig

_logger = get_logger(__name__)

TARGET_SPECIFIC = 0
TARGET_RANDOM = 1


class ABTestPanel(QWidget):

    def __init__(
        self,
        channel_db: 'ChannelDB',
        own_pubkey: Optional[bytes],
        lnworker: Optional['LNWallet'],
        config: Optional['SimpleConfig'],
        parent=None,
    ):
        super().__init__(parent)
        self.channel_db = channel_db
        self.own_pubkey = own_pubkey
        self.lnworker = lnworker
        self._config = config
        self._probe_worker: Optional[ProbeWorker] = None
        self._loaded_targets: Optional[list] = None  # multi-target pubkey hex list for replay
        self._last_experiment: Optional[ExperimentRun] = None
        self._captured_logs: Optional[str] = None
        self._setup_ui()
        self._connect_signals()
        self._refresh_experiment_list()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # --- experiment setup ---
        setup_box = QGroupBox(_('Experiment Setup'))
        form = QFormLayout(setup_box)
        form.setContentsMargins(6, 18, 6, 4)

        self.label_input = QLineEdit()
        self.label_input.setPlaceholderText(_('e.g. "baseline" or "new-fee-logic-v2"'))
        label_label = QLabel(_('Label:'))
        label_label.setToolTip(_('Short name to identify this experiment run (shown in saved experiments list and comparisons)'))
        form.addRow(label_label, self.label_input)

        self.target_mode_combo = QComboBox()
        self.target_mode_combo.addItems([_('Specific Node'), _('Random Nodes')])
        target_mode_label = QLabel(_('Target Mode:'))
        target_mode_label.setToolTip(_("'Specific Node' probes a single pubkey. 'Random Nodes' picks N random nodes from the gossip graph."))
        form.addRow(target_mode_label, self.target_mode_combo)

        self.target_input = QLineEdit()
        self.target_input.setPlaceholderText(_('Target node pubkey (hex)'))
        self._target_row_label = QLabel(_('Target Node:'))
        self._target_row_label.setToolTip(_('33-byte compressed public key of the destination node (66 hex characters)'))
        form.addRow(self._target_row_label, self.target_input)

        self.random_count_spin = QSpinBox()
        self.random_count_spin.setMinimum(1)
        self.random_count_spin.setMaximum(200)
        self.random_count_spin.setValue(10)
        self._random_row_label = QLabel(_('Random Count:'))
        self._random_row_label.setToolTip(_('Number of random nodes to pick from the gossip graph as probe destinations'))
        form.addRow(self._random_row_label, self.random_count_spin)
        self._random_row_label.setVisible(False)
        self.random_count_spin.setVisible(False)

        self.amount_input = QLineEdit()
        self.amount_input.setPlaceholderText('10000')
        self.amount_input.setText('10000')
        self.amount_input.setMaximumWidth(120)
        amount_label = QLabel(_('Amount (sat):'))
        amount_label.setToolTip(_('Payment amount in satoshis for each probe. Uses an invalid payment_hash so funds are never actually sent.'))
        form.addRow(amount_label, self.amount_input)

        self.attempts_spin = QSpinBox()
        self.attempts_spin.setMinimum(1)
        self.attempts_spin.setMaximum(50)
        self.attempts_spin.setValue(3)
        attempts_label = QLabel(_('Attempts/Node:'))
        attempts_label.setToolTip(_('Number of independent probe attempts per target node. Multiple attempts reveal route reliability variance.'))
        form.addRow(attempts_label, self.attempts_spin)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setMinimum(0)
        self.timeout_spin.setMaximum(30000)
        self.timeout_spin.setValue(500)
        self.timeout_spin.setSuffix(' ms')
        timeout_label = QLabel(_('Timeout:'))
        timeout_label.setToolTip(_('Delay in milliseconds between consecutive probes. Prevents overwhelming the local node and the network.'))
        form.addRow(timeout_label, self.timeout_spin)

        self.mpp_checkbox = QCheckBox(_('Enable MPP (multi-part payments)'))
        self.mpp_checkbox.setToolTip(_('Allow the payment to be split into multiple parts. When disabled, probes try to send the full amount as a single htlc.'))
        form.addRow('', self.mpp_checkbox)

        btn_layout = QHBoxLayout()
        self.run_btn = QPushButton(_('Run Experiment'))
        self.stop_btn = QPushButton(_('Stop'))
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.run_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addStretch()
        form.addRow(btn_layout)

        layout.addWidget(setup_box)

        # --- progress ---
        progress_layout = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        progress_layout.addWidget(self.progress_bar, 1)
        self.progress_label = QLabel()
        progress_layout.addWidget(self.progress_label)
        layout.addLayout(progress_layout)

        # --- live results ---
        results_box = QGroupBox(_('Live Results'))
        results_layout = QVBoxLayout(results_box)
        results_layout.setContentsMargins(6, 18, 6, 4)
        self.results_text = QTextEdit()
        self.results_text.setReadOnly(True)
        self.results_text.setFont(QFont('Monospace', 9))
        self.results_text.setMaximumHeight(200)
        results_layout.addWidget(self.results_text)
        copy_btn_layout = QHBoxLayout()
        self.copy_md_btn = QPushButton(_('Copy as Markdown'))
        self.copy_md_btn.setEnabled(False)
        copy_btn_layout.addWidget(self.copy_md_btn)
        self.copy_logs_btn = QPushButton(_('Copy Electrum Logs'))
        self.copy_logs_btn.setEnabled(False)
        self.copy_logs_btn.setToolTip(_('Copy all (Electrum) log output captured during the experiment to the clipboard'))
        copy_btn_layout.addWidget(self.copy_logs_btn)
        copy_btn_layout.addStretch()
        results_layout.addLayout(copy_btn_layout)
        layout.addWidget(results_box)

        # --- saved experiments ---
        history_box = QGroupBox(_('Saved Experiments'))
        history_layout = QVBoxLayout(history_box)
        history_layout.setContentsMargins(6, 18, 6, 4)

        combo_layout = QHBoxLayout()
        combo_layout.addWidget(QLabel(_('Run A:')))
        self.run_a_combo = QComboBox()
        self.run_a_combo.setMinimumWidth(200)
        combo_layout.addWidget(self.run_a_combo, 1)
        combo_layout.addWidget(QLabel(_('Run B:')))
        self.run_b_combo = QComboBox()
        self.run_b_combo.setMinimumWidth(200)
        combo_layout.addWidget(self.run_b_combo, 1)
        history_layout.addLayout(combo_layout)

        history_btn_layout = QHBoxLayout()
        self.compare_btn = QPushButton(_('Compare'))
        self.load_targets_btn = QPushButton(_('Load Targets from Selected'))
        self.delete_btn = QPushButton(_('Delete Selected'))
        history_btn_layout.addWidget(self.compare_btn)
        history_btn_layout.addWidget(self.load_targets_btn)
        history_btn_layout.addWidget(self.delete_btn)
        history_btn_layout.addStretch()
        history_layout.addLayout(history_btn_layout)

        layout.addWidget(history_box)
        layout.addStretch()

    def _connect_signals(self):
        self.target_mode_combo.currentIndexChanged.connect(self._on_target_mode_changed)
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn.clicked.connect(self._on_stop)
        self.copy_md_btn.clicked.connect(self._on_copy_markdown)
        self.copy_logs_btn.clicked.connect(self._on_copy_logs)
        self.compare_btn.clicked.connect(self._on_compare)
        self.load_targets_btn.clicked.connect(self._on_load_targets)
        self.delete_btn.clicked.connect(self._on_delete)

    def _on_target_mode_changed(self, index):
        is_specific = index == TARGET_SPECIFIC
        self._target_row_label.setVisible(is_specific)
        self.target_input.setVisible(is_specific)
        self._random_row_label.setVisible(not is_specific)
        self.random_count_spin.setVisible(not is_specific)

    # --- run experiment ---

    def _on_run(self):
        if not self.lnworker:
            self.progress_label.setText(_('Lightning wallet not available'))
            return
        if not self._config:
            self.progress_label.setText(_('Config not available'))
            return

        label = self.label_input.text().strip()
        if not label:
            self.progress_label.setText(_('Please enter an experiment label'))
            return

        amount_text = self.amount_input.text().strip() or '10000'
        try:
            amount_sat = int(amount_text)
        except ValueError:
            self.progress_label.setText(_('Invalid amount'))
            return
        if amount_sat <= 0:
            self.progress_label.setText(_('Amount must be positive'))
            return
        amount_msat = amount_sat * 1000

        targets = None
        random_count = 0

        if self.target_mode_combo.currentIndex() == TARGET_SPECIFIC:
            # check for multi-target replay from a previous experiment
            if self._loaded_targets:
                targets = []
                for pk_hex in self._loaded_targets:
                    try:
                        pk_bytes = bytes.fromhex(pk_hex)
                        alias = self._get_node_alias(pk_bytes)
                        targets.append((pk_bytes, alias))
                    except ValueError:
                        continue
                self._loaded_targets = None
                if not targets:
                    self.progress_label.setText(_('No valid targets from loaded experiment'))
                    return
            else:
                # single specific node
                pubkey_hex = self.target_input.text().strip()
                if not pubkey_hex:
                    self.progress_label.setText(_('Please enter a target node pubkey'))
                    return
                try:
                    pubkey_bytes = bytes.fromhex(pubkey_hex)
                    if len(pubkey_bytes) != 33:
                        raise ValueError
                except ValueError:
                    self.progress_label.setText(_('Invalid pubkey (must be 66 hex chars)'))
                    return
                alias = self._get_node_alias(pubkey_bytes)
                targets = [(pubkey_bytes, alias)]
        else:
            random_count = self.random_count_spin.value()

        self._stop_worker()

        self.results_text.clear()
        self._captured_logs = None
        self.copy_logs_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_label.setText(_('Starting experiment...'))

        self._probe_worker = ProbeWorker(
            self.lnworker,
            self.channel_db,
            self._config,
            label=label,
            targets=targets,
            random_count=random_count,
            own_pubkey=self.own_pubkey,
            amount_msat=amount_msat,
            attempts_per_node=self.attempts_spin.value(),
            timeout_between_ms=self.timeout_spin.value(),
            enable_mpp=self.mpp_checkbox.isChecked(),
        )
        self._probe_worker.probe_completed.connect(self._on_probe_completed)
        self._probe_worker.progress_updated.connect(self._on_progress_updated)
        self._probe_worker.experiment_finished.connect(self._on_experiment_finished)
        self._probe_worker.logs_captured.connect(self._on_logs_captured)
        self._probe_worker.error_occurred.connect(self._on_error)
        self._probe_worker.start()

    def _on_stop(self):
        if self._probe_worker:
            self._probe_worker.stop()
        self.stop_btn.setEnabled(False)
        self.progress_label.setText(_('Stopping...'))

    def _on_probe_completed(self, result):
        if result.success:
            line = (
                f"OK  {result.target_alias or result.target_pubkey_hex[:16]} "
                f"(attempt {result.attempt_number}): "
                f"reached dest, {result.route_hops} hops, "
                f"{result.fee_msat} msat fee, {result.latency_ms:.0f}ms"
            )
        else:
            line = (
                f"FAIL {result.target_alias or result.target_pubkey_hex[:16]} "
                f"(attempt {result.attempt_number}): "
                f"{result.error_code_name}"
            )
            if result.route_hops > 0:
                line += f", {result.route_hops} hops"
            line += f", {result.latency_ms:.0f}ms"
        self.results_text.append(line)

    def _on_progress_updated(self, completed, total):
        if self.progress_bar.maximum() != total:
            self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(completed)
        self.progress_label.setText(f'{completed}/{total} probes')

    def _reset_run_ui(self):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setVisible(False)

    def _on_experiment_finished(self, experiment):
        self._reset_run_ui()
        self._last_experiment = experiment
        self.copy_md_btn.setEnabled(True)

        n_success = sum(1 for r in experiment.results if r.success)
        n_total = len(experiment.results)
        rate = experiment.success_rate() * 100
        avg_fee = experiment.avg_fee_msat()
        avg_hops = experiment.avg_hops()
        avg_latency = experiment.avg_latency_ms()

        self.progress_label.setText(
            f'Done: {n_success}/{n_total} probes successful ({rate:.1f}%), '
            f'{experiment.duration_s:.1f}s total'
        )

        self.results_text.append('')
        self.results_text.append(f'=== Summary: "{experiment.label}" ===')
        self.results_text.append(f'Success rate: {rate:.1f}%')
        if avg_fee > 0:
            self.results_text.append(f'Avg fee: {avg_fee:.0f} msat')
        if avg_hops > 0:
            self.results_text.append(f'Avg hops: {avg_hops:.1f}')
        self.results_text.append(f'Avg latency: {avg_latency:.0f} ms')
        self.results_text.append(f'Saved as: {experiment.run_id}')

        self._refresh_experiment_list()

    def _on_error(self, message):
        self._reset_run_ui()
        self.progress_label.setText(f'Error: {message}')

    # --- saved experiments ---

    def _refresh_experiment_list(self):
        if not self._config:
            return
        experiments = list_experiments(self._config)
        for combo in (self.run_a_combo, self.run_b_combo):
            combo.clear()
            for run_id, label, timestamp in experiments:
                combo.addItem(f'{label} ({_fmt_ts(timestamp)})', run_id)

    def _get_selected_run_id(self, combo: QComboBox) -> Optional[str]:
        return combo.currentData()

    def _on_compare(self):
        if not self._config:
            return
        run_id_a = self._get_selected_run_id(self.run_a_combo)
        run_id_b = self._get_selected_run_id(self.run_b_combo)
        if not run_id_a or not run_id_b:
            self.progress_label.setText(_('Select two experiments to compare'))
            return
        try:
            run_a = load_experiment(self._config, run_id_a)
            run_b = load_experiment(self._config, run_id_b)
        except Exception as e:
            self.progress_label.setText(f'Error loading: {e}')
            return
        dialog = ComparisonDialog(run_a, run_b, parent=self)
        dialog.exec()

    def _on_load_targets(self):
        if not self._config:
            return
        run_id = self._get_selected_run_id(self.run_a_combo)
        if not run_id:
            self.progress_label.setText(_('Select an experiment to load targets from'))
            return
        try:
            run = load_experiment(self._config, run_id)
        except Exception as e:
            self.progress_label.setText(f'Error loading: {e}')
            return
        pubkeys = run.config.target_pubkeys_hex
        if len(pubkeys) == 1:
            self.target_mode_combo.setCurrentIndex(TARGET_SPECIFIC)
            self.target_input.setText(pubkeys[0])
        else:
            # store multi-target for replay
            self._loaded_targets = pubkeys
            self.target_mode_combo.setCurrentIndex(TARGET_SPECIFIC)
            self.target_input.setText(', '.join(pubkeys))
        self.amount_input.setText(str(run.config.amount_msat // 1000))
        self.attempts_spin.setValue(run.config.attempts_per_node)
        self.timeout_spin.setValue(run.config.timeout_between_ms)
        self.mpp_checkbox.setChecked(run.config.enable_mpp)
        self.progress_label.setText(
            _('Loaded {} target(s) and parameters from "{}"').format(len(pubkeys), run.label))

    def _on_delete(self):
        if not self._config:
            return
        run_id = self._get_selected_run_id(self.run_a_combo)
        if not run_id:
            return
        try:
            delete_experiment(self._config, run_id)
        except Exception as e:
            self.progress_label.setText(f'Error deleting: {e}')
            return
        self._refresh_experiment_list()
        self.progress_label.setText(_('Experiment deleted'))

    # --- copy markdown ---

    def _on_copy_markdown(self):
        if not self._last_experiment:
            return
        md = _experiment_to_markdown(self._last_experiment)
        QApplication.clipboard().setText(md)
        self.progress_label.setText(_('Copied to clipboard'))

    def _on_logs_captured(self, logs: str):
        self._captured_logs = logs
        self.copy_logs_btn.setEnabled(bool(logs))

    def _on_copy_logs(self):
        if not self._captured_logs:
            return
        QApplication.clipboard().setText(self._captured_logs)
        self.progress_label.setText(_('Logs copied to clipboard'))

    # --- helpers ---

    def _get_node_alias(self, pubkey: bytes) -> str:
        info = self.channel_db.get_node_info_for_node_id(pubkey)
        if info and info.alias:
            return info.alias
        return ''

    def _stop_worker(self):
        from .qt import PluginDialog
        PluginDialog._stop_worker(self._probe_worker)
        self._probe_worker = None

    def stop_worker(self):
        """Public interface for cleanup from parent dialog."""
        self._stop_worker()


class ComparisonDialog(QDialog):

    def __init__(self, run_a: ExperimentRun, run_b: ExperimentRun, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_('A/B Test Comparison'))
        self.setMinimumSize(700, 500)
        self.resize(800, 600)

        layout = QVBoxLayout(self)

        stats_a = _compute_stats(run_a)
        stats_b = _compute_stats(run_b)

        columns = QHBoxLayout()

        for label_text, run, s in [(_('Run A'), run_a, stats_a), (_('Run B'), run_b, stats_b)]:
            box = QGroupBox(f'{label_text}: "{run.label}"')
            form = QFormLayout(box)
            form.setContentsMargins(6, 18, 6, 4)

            form.addRow(_('Timestamp:'), QLabel(_fmt_ts(run.timestamp)))
            form.addRow(_('Electrum:'), QLabel(run.electrum_version))
            form.addRow(_('Targets:'), QLabel(str(len(run.config.target_pubkeys_hex))))
            form.addRow(_('Total probes:'), QLabel(str(len(run.results))))
            form.addRow(_('Amount:'), QLabel(f'{run.config.amount_msat // 1000} sat'))
            form.addRow(_('MPP:'), QLabel(_('Yes') if run.config.enable_mpp else _('No')))
            form.addRow(_('Success rate:'), QLabel(f'{s["sr"] * 100:.1f}%'))
            form.addRow(_('Avg hops:'), QLabel(f'{s["hops"]:.2f}'))
            form.addRow(_('Avg fee:'), QLabel(f'{s["fee"]:.0f} msat'))
            form.addRow(_('Avg latency:'), QLabel(f'{s["lat"]:.0f} ms'))
            form.addRow(_('Duration:'), QLabel(f'{run.duration_s:.1f}s'))

            columns.addWidget(box)

        layout.addLayout(columns)

        sa, sb = stats_a, stats_b
        delta_box = QGroupBox(_('Delta (B - A)'))
        delta_form = QFormLayout(delta_box)
        delta_form.setContentsMargins(6, 18, 6, 4)

        sr_delta = (sb['sr'] - sa['sr']) * 100
        fee_delta = sb['fee'] - sa['fee']
        hops_delta = sb['hops'] - sa['hops']
        lat_delta = sb['lat'] - sa['lat']

        delta_form.addRow(_('Success rate:'), QLabel(_format_delta(sr_delta, '%', higher_is_better=True)))
        delta_form.addRow(_('Avg fee:'), QLabel(_format_delta(fee_delta, ' msat', higher_is_better=False)))
        delta_form.addRow(_('Avg hops:'), QLabel(_format_delta(hops_delta, '', higher_is_better=False)))
        delta_form.addRow(_('Avg latency:'), QLabel(_format_delta(lat_delta, ' ms', higher_is_better=False)))

        layout.addWidget(delta_box)

        # --- per-target breakdown ---
        targets_a = set(run_a.config.target_pubkeys_hex)
        targets_b = set(run_b.config.target_pubkeys_hex)
        overlap = targets_a & targets_b

        if overlap:
            breakdown_box = QGroupBox(_('Per-Target Breakdown ({} overlapping targets)').format(len(overlap)))
            breakdown_layout = QVBoxLayout(breakdown_box)
            breakdown_layout.setContentsMargins(6, 18, 6, 4)

            breakdown_text = QTextEdit()
            breakdown_text.setReadOnly(True)
            breakdown_text.setFont(QFont('Monospace', 9))

            lines = []
            header = f"{'Target':<24} {'A success':>10} {'B success':>10} {'A fee':>10} {'B fee':>10} {'A hops':>7} {'B hops':>7}"
            lines.append(header)
            lines.append('-' * len(header))

            for alias, a_sr, b_sr, a_fee, b_fee, a_hops, b_hops in _per_target_data(run_a, run_b, overlap):
                lines.append(f"{alias:<24} {a_sr:>10} {b_sr:>10} {a_fee:>10} {b_fee:>10} {a_hops:>7} {b_hops:>7}")

            breakdown_text.setPlainText('\n'.join(lines))
            breakdown_layout.addWidget(breakdown_text)
            layout.addWidget(breakdown_box)
        elif targets_a != targets_b:
            layout.addWidget(QLabel(
                _('Note: experiments used different target sets. '
                  'Per-target comparison not available.')))

        # --- buttons ---
        button_layout = QHBoxLayout()
        copy_md_btn = QPushButton(_('Copy as Markdown'))
        copy_md_btn.clicked.connect(lambda: self._copy_markdown(run_a, run_b, stats_a, stats_b))
        button_layout.addWidget(copy_md_btn)
        button_layout.addStretch()
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        button_layout.addWidget(button_box)
        layout.addLayout(button_layout)


    def _copy_markdown(self, run_a, run_b, stats_a, stats_b):
        md = _comparison_to_markdown(run_a, run_b, stats_a, stats_b)
        QApplication.clipboard().setText(md)


def _compute_stats(run: ExperimentRun) -> dict:
    return {
        'sr': run.success_rate(),
        'fee': run.avg_fee_msat(),
        'hops': run.avg_hops(),
        'lat': run.avg_latency_ms(),
    }


def _fmt_ts(timestamp: float) -> str:
    return format_time(int(timestamp)) if timestamp else '?'


def _per_target_data(run_a: ExperimentRun, run_b: ExperimentRun, overlap: set) -> list:
    """Returns [(alias, a_sr, b_sr, a_fee, b_fee, a_hops, b_hops), ...]."""
    summary_a = run_a.per_target_summary()
    summary_b = run_b.per_target_summary()
    rows = []
    for pk in sorted(overlap):
        sa = summary_a.get(pk, {})
        sb = summary_b.get(pk, {})
        alias = sa.get('alias', '') or sb.get('alias', '') or pk[:16] + '...'
        rows.append((
            alias,
            f"{sa.get('success', 0)}/{sa.get('total', 0)}",
            f"{sb.get('success', 0)}/{sb.get('total', 0)}",
            f"{sa.get('avg_fee', 0):.0f}",
            f"{sb.get('avg_fee', 0):.0f}",
            f"{sa.get('avg_hops', 0):.1f}",
            f"{sb.get('avg_hops', 0):.1f}",
        ))
    return rows


def _experiment_to_markdown(exp: ExperimentRun) -> str:
    stats = _compute_stats(exp)
    n_success = sum(1 for r in exp.results if r.success)

    lines = [
        f'## A/B Test: "{exp.label}"',
        '',
        '| Metric | Value |',
        '|--------|-------|',
        f'| Timestamp | {_fmt_ts(exp.timestamp)} |',
        f'| Electrum | {exp.electrum_version} |',
        f'| Amount | {exp.config.amount_msat // 1000} sat |',
        f'| MPP | {"Yes" if exp.config.enable_mpp else "No"} |',
        f'| Targets | {len(exp.config.target_pubkeys_hex)} |',
        f'| Attempts/node | {exp.config.attempts_per_node} |',
        f'| Total probes | {len(exp.results)} |',
        f'| Success rate | {stats["sr"] * 100:.1f}% ({n_success}/{len(exp.results)}) |',
        f'| Avg fee | {stats["fee"]:.0f} msat |',
        f'| Avg hops | {stats["hops"]:.2f} |',
        f'| Avg latency | {stats["lat"]:.0f} ms |',
        f'| Duration | {exp.duration_s:.1f}s |',
    ]

    if exp.results:
        lines += [
            '',
            '### Probe Results',
            '',
            '| Target | Attempt | Result | Hops | Fee (msat) | Latency (ms) | Error |',
            '|--------|---------|--------|------|------------|--------------|-------|',
        ]
        for r in exp.results:
            name = r.target_alias or r.target_pubkey_hex[:16] + '...'
            status = 'OK' if r.success else 'FAIL'
            error = '' if r.success else r.error_code_name
            lines.append(
                f'| {name} | {r.attempt_number} | {status} '
                f'| {r.route_hops} | {r.fee_msat} | {r.latency_ms:.0f} | {error} |'
            )

    return '\n'.join(lines) + '\n'


def _comparison_to_markdown(
    run_a: ExperimentRun, run_b: ExperimentRun,
    stats_a: dict, stats_b: dict,
) -> str:
    def _run_table(label, run, s):
        return [
            f'### {label}: "{run.label}"',
            '',
            '| Metric | Value |',
            '|--------|-------|',
            f'| Timestamp | {_fmt_ts(run.timestamp)} |',
            f'| Electrum | {run.electrum_version} |',
            f'| Amount | {run.config.amount_msat // 1000} sat |',
            f'| MPP | {"Yes" if run.config.enable_mpp else "No"} |',
            f'| Targets | {len(run.config.target_pubkeys_hex)} |',
            f'| Total probes | {len(run.results)} |',
            f'| Success rate | {s["sr"] * 100:.1f}% |',
            f'| Avg hops | {s["hops"]:.2f} |',
            f'| Avg fee | {s["fee"]:.0f} msat |',
            f'| Avg latency | {s["lat"]:.0f} ms |',
            f'| Duration | {run.duration_s:.1f}s |',
        ]

    lines = ['## A/B Test Comparison', '']
    lines += _run_table('Run A', run_a, stats_a)
    lines += ['']
    lines += _run_table('Run B', run_b, stats_b)

    sr_delta = (stats_b['sr'] - stats_a['sr']) * 100
    fee_delta = stats_b['fee'] - stats_a['fee']
    hops_delta = stats_b['hops'] - stats_a['hops']
    lat_delta = stats_b['lat'] - stats_a['lat']

    lines += [
        '',
        '### Delta (B - A)',
        '',
        '| Metric | Delta |',
        '|--------|-------|',
        f'| Success rate | {_format_delta(sr_delta, "%", higher_is_better=True)} |',
        f'| Avg fee | {_format_delta(fee_delta, " msat", higher_is_better=False)} |',
        f'| Avg hops | {_format_delta(hops_delta, "", higher_is_better=False)} |',
        f'| Avg latency | {_format_delta(lat_delta, " ms", higher_is_better=False)} |',
    ]

    targets_a = set(run_a.config.target_pubkeys_hex)
    targets_b = set(run_b.config.target_pubkeys_hex)
    overlap = targets_a & targets_b

    if overlap:
        lines += [
            '',
            f'### Per-Target Breakdown ({len(overlap)} overlapping targets)',
            '',
            '| Target | A success | B success | A fee | B fee | A hops | B hops |',
            '|--------|-----------|-----------|-------|-------|--------|--------|',
        ]
        for alias, a_sr, b_sr, a_fee, b_fee, a_hops, b_hops in _per_target_data(run_a, run_b, overlap):
            lines.append(f'| {alias} | {a_sr} | {b_sr} | {a_fee} | {b_fee} | {a_hops} | {b_hops} |')

    return '\n'.join(lines) + '\n'


def _format_delta(value: float, suffix: str, higher_is_better: bool) -> str:
    if abs(value) < 0.01:
        return f'0{suffix} (no change)'
    sign = '+' if value > 0 else ''
    is_improvement = (value > 0) == higher_is_better
    indicator = '(better)' if is_improvement else '(worse)'
    return f'{sign}{value:.1f}{suffix} {indicator}'
