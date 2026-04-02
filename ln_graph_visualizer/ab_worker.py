import logging
import time
import uuid
import asyncio
from typing import Optional, TYPE_CHECKING

from PyQt6.QtCore import QThread, pyqtSignal

from electrum.logging import get_logger
from electrum import util
from electrum.version import ELECTRUM_VERSION

from .ab_testing import (
    ProbeResult, ExperimentConfig, ExperimentRun,
    probe_node, resolve_random_targets, save_experiment,
)

if TYPE_CHECKING:
    from electrum.lnworker import LNWallet
    from electrum.channel_db import ChannelDB
    from electrum.simple_config import SimpleConfig

_logger = get_logger(__name__)

PROBE_TIMEOUT_S = 120

_LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s - %(message)s'


class _LogCaptureHandler(logging.Handler):
    """Captures formatted log records into a list during an experiment."""

    def __init__(self):
        super().__init__()
        self.records: list[str] = []
        self.setFormatter(logging.Formatter(_LOG_FORMAT))

    def emit(self, record):
        try:
            self.records.append(self.format(record))
        except Exception:
            pass


class ProbeWorker(QThread):
    """Run a series of probes in background, emitting progress signals."""
    probe_completed = pyqtSignal(object)       # ProbeResult
    progress_updated = pyqtSignal(int, int)    # (completed, total)
    experiment_finished = pyqtSignal(object)   # ExperimentRun
    logs_captured = pyqtSignal(str)            # full log text from experiment
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        lnworker: 'LNWallet',
        channel_db: 'ChannelDB',
        config: 'SimpleConfig',
        *,
        label: str,
        targets: Optional[list] = None,
        random_count: int = 0,
        own_pubkey: Optional[bytes] = None,
        amount_msat: int = 10_000_000,
        attempts_per_node: int = 3,
        timeout_between_ms: int = 500,
        enable_mpp: bool = False,
    ):
        super().__init__()
        self.lnworker = lnworker
        self.channel_db = channel_db
        self._config = config
        self.label = label
        self._explicit_targets = targets       # list of (pubkey_bytes, alias_str)
        self._random_count = random_count
        self.own_pubkey = own_pubkey
        self.amount_msat = amount_msat
        self.attempts_per_node = attempts_per_node
        self.timeout_between_ms = timeout_between_ms
        self.enable_mpp = enable_mpp
        self._stop = False

    def stop(self):
        self._stop = True

    def _clear_pathfinder_blacklist(self):
        self.lnworker.network.path_finder.clear_blacklist()

    def run(self):
        log_handler = _LogCaptureHandler()
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)
        try:
            self._run_experiment()
        except Exception as e:
            _logger.error(f"ProbeWorker error: {e}", exc_info=True)
            self.error_occurred.emit(str(e))
        finally:
            root_logger.removeHandler(log_handler)
            self.logs_captured.emit('\n'.join(log_handler.records))

    def _run_experiment(self):
        # resolve targets
        if self._explicit_targets:
            targets = self._explicit_targets
        elif self._random_count > 0:
            targets = resolve_random_targets(
                self.channel_db, self._random_count, exclude_pubkey=self.own_pubkey,
            )
            if not targets:
                self.error_occurred.emit('No nodes found in gossip database')
                return
        else:
            self.error_occurred.emit('No targets specified')
            return

        target_pubkeys_hex = [t[0].hex() for t in targets]
        total = len(targets) * self.attempts_per_node
        completed = 0
        results = []
        start_time = time.time()
        loop = util.get_asyncio_loop()

        exp_config = ExperimentConfig(
            target_pubkeys_hex=target_pubkeys_hex,
            random_count=self._random_count,
            amount_msat=self.amount_msat,
            attempts_per_node=self.attempts_per_node,
            timeout_between_ms=self.timeout_between_ms,
            source_pubkey_hex=self.own_pubkey.hex() if self.own_pubkey else '',
            enable_mpp=self.enable_mpp,
        )

        self._clear_pathfinder_blacklist()

        for target_pubkey, target_alias in targets:
            if self._stop:
                break
            self._clear_pathfinder_blacklist()
            for attempt in range(1, self.attempts_per_node + 1):
                if self._stop:
                    break
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        probe_node(
                            self.lnworker, target_pubkey, self.amount_msat,
                            target_alias=target_alias, attempt_number=attempt,
                            enable_mpp=self.enable_mpp,
                        ),
                        loop,
                    )
                    result = future.result(timeout=PROBE_TIMEOUT_S)
                except Exception as e:
                    _logger.info(f"Probe failed for {target_pubkey.hex()[:16]}: {e!r}")
                    result = ProbeResult(
                        target_pubkey_hex=target_pubkey.hex(),
                        target_alias=target_alias,
                        attempt_number=attempt,
                        success=False,
                        error_code=None,
                        error_code_name=str(e),
                        erring_node_hex=None,
                        route_hops=0,
                        route_scids=[],
                        fee_msat=0,
                        latency_ms=0,
                        timestamp=time.time(),
                    )
                results.append(result)
                completed += 1
                self.probe_completed.emit(result)
                self.progress_updated.emit(completed, total)

                # inter-probe delay
                if self.timeout_between_ms > 0 and not self._stop:
                    self.msleep(self.timeout_between_ms)

        self._clear_pathfinder_blacklist()

        duration_s = time.time() - start_time
        experiment = ExperimentRun(
            run_id=uuid.uuid4().hex,
            label=self.label,
            timestamp=start_time,
            duration_s=round(duration_s, 1),
            config=exp_config,
            results=results,
            electrum_version=ELECTRUM_VERSION,
        )

        # persist
        try:
            save_experiment(self._config, experiment)
        except Exception as e:
            _logger.error(f"Failed to save experiment: {e}", exc_info=True)

        self.experiment_finished.emit(experiment)
