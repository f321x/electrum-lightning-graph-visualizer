import os
import time
import random
from dataclasses import dataclass, asdict
from typing import Optional, TYPE_CHECKING

from electrum.lnonion import OnionFailureCode
from electrum.lnutil import PaymentFailure, PaymentFeeBudget, NBLOCK_CLTV_DELTA_TOO_FAR_INTO_FUTURE, LnFeatures
from electrum.lnworker import LNWALLET_FEATURES
from electrum.logging import get_logger
from electrum import util

if TYPE_CHECKING:
    from electrum.lnworker import LNWallet
    from electrum.simple_config import SimpleConfig
    from electrum.channel_db import ChannelDB

_logger = get_logger(__name__)


@dataclass
class ProbeResult:
    target_pubkey_hex: str
    target_alias: str
    attempt_number: int
    success: bool
    error_code: Optional[int]
    error_code_name: str
    erring_node_hex: Optional[str]
    route_hops: int
    route_scids: list
    fee_msat: int
    latency_ms: float
    timestamp: float


@dataclass
class ExperimentConfig:
    target_pubkeys_hex: list
    random_count: int
    amount_msat: int
    attempts_per_node: int
    timeout_between_ms: int
    source_pubkey_hex: str
    enable_mpp: bool = False


@dataclass
class ExperimentRun:
    run_id: str
    label: str
    timestamp: float
    duration_s: float
    config: ExperimentConfig
    results: list
    electrum_version: str
    notes: str = ''

    def success_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.success) / len(self.results)

    def avg_fee_msat(self) -> float:
        successful = [r for r in self.results if r.success and r.fee_msat > 0]
        if not successful:
            return 0.0
        return sum(r.fee_msat for r in successful) / len(successful)

    def avg_hops(self) -> float:
        successful = [r for r in self.results if r.success and r.route_hops > 0]
        if not successful:
            return 0.0
        return sum(r.route_hops for r in successful) / len(successful)

    def avg_latency_ms(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.latency_ms for r in self.results) / len(self.results)

    def per_target_summary(self) -> dict:
        """Returns {pubkey_hex: {total, success, fail, avg_fee, avg_hops, avg_latency}}."""
        targets = {}
        for r in self.results:
            key = r.target_pubkey_hex
            if key not in targets:
                targets[key] = dict(
                    alias=r.target_alias, total=0, success=0, fail=0,
                    fee_sum=0, fee_count=0,
                    hops_sum=0, hops_count=0,
                    latency_sum=0.0,
                )
            t = targets[key]
            t['total'] += 1
            if r.success:
                t['success'] += 1
                if r.fee_msat > 0:
                    t['fee_sum'] += r.fee_msat
                    t['fee_count'] += 1
                if r.route_hops > 0:
                    t['hops_sum'] += r.route_hops
                    t['hops_count'] += 1
            else:
                t['fail'] += 1
            t['latency_sum'] += r.latency_ms
        for t in targets.values():
            fc = t.pop('fee_count')
            t['avg_fee'] = t.pop('fee_sum') / fc if fc else 0
            hc = t.pop('hops_count')
            t['avg_hops'] = t.pop('hops_sum') / hc if hc else 0
            t['avg_latency'] = t.pop('latency_sum') / t['total'] if t['total'] else 0
        return targets


# --- persistence ---

def get_experiments_dir(config: 'SimpleConfig') -> str:
    path = os.path.join(config.electrum_path(), 'ln_ab_tests')
    util.make_dir(path)
    return path


def save_experiment(config: 'SimpleConfig', run: ExperimentRun) -> str:
    dirpath = get_experiments_dir(config)
    filepath = os.path.join(dirpath, f'{run.run_id}.json')
    data = _run_to_dict(run)
    util.write_json_file(filepath, data)
    return filepath


def load_experiment(config: 'SimpleConfig', run_id: str) -> ExperimentRun:
    dirpath = get_experiments_dir(config)
    filepath = os.path.join(dirpath, f'{run_id}.json')
    data = util.read_json_file(filepath)
    return _run_from_dict(data)


def list_experiments(config: 'SimpleConfig') -> list:
    """Returns [(run_id, label, timestamp), ...] sorted by timestamp desc."""
    dirpath = get_experiments_dir(config)
    experiments = []
    for fname in os.listdir(dirpath):
        if not fname.endswith('.json'):
            continue
        run_id = fname[:-5]
        try:
            data = util.read_json_file(os.path.join(dirpath, fname))
            label = data.get('label', '')
            timestamp = data.get('timestamp', 0)
            experiments.append((run_id, label, timestamp))
        except Exception as e:
            _logger.debug(f"Failed to read experiment {fname}: {e}")
            continue
    experiments.sort(key=lambda x: x[2], reverse=True)
    return experiments


def delete_experiment(config: 'SimpleConfig', run_id: str) -> None:
    dirpath = get_experiments_dir(config)
    filepath = os.path.join(dirpath, f'{run_id}.json')
    try:
        os.remove(filepath)
    except FileNotFoundError:
        pass


def _run_to_dict(run: ExperimentRun) -> dict:
    return asdict(run)


def _run_from_dict(d: dict) -> ExperimentRun:
    cfg = d['config']
    cfg.setdefault('enable_mpp', False)
    config = ExperimentConfig(**cfg)
    results = [ProbeResult(**r) for r in d['results']]
    return ExperimentRun(
        run_id=d['run_id'],
        label=d['label'],
        timestamp=d['timestamp'],
        duration_s=d['duration_s'],
        config=config,
        results=results,
        electrum_version=d.get('electrum_version', ''),
        notes=d.get('notes', ''),
    )


# --- probing ---

_TRAMPOLINE_BITS = (
    LnFeatures.OPTION_TRAMPOLINE_ROUTING_OPT_ECLAIR
    | LnFeatures.OPTION_TRAMPOLINE_ROUTING_REQ_ECLAIR
    | LnFeatures.OPTION_TRAMPOLINE_ROUTING_OPT_ELECTRUM
    | LnFeatures.OPTION_TRAMPOLINE_ROUTING_REQ_ELECTRUM
)
_MPP_BITS = LnFeatures.BASIC_MPP_OPT | LnFeatures.BASIC_MPP_REQ


def _probe_invoice_features(enable_mpp: bool) -> int:
    features = LNWALLET_FEATURES.for_invoice()
    features &= ~_TRAMPOLINE_BITS
    if not enable_mpp:
        features &= ~_MPP_BITS
    return int(features)


async def probe_node(
    lnworker: 'LNWallet',
    target_pubkey: bytes,
    amount_msat: int,
    target_alias: str = '',
    attempt_number: int = 1,
    enable_mpp: bool = False,
) -> ProbeResult:
    payment_hash = os.urandom(32)
    payment_secret = os.urandom(32)
    t0 = time.monotonic()

    budget = PaymentFeeBudget(
        fee_msat=max(amount_msat, 50_000_000),  # generous budget for probing
        cltv=NBLOCK_CLTV_DELTA_TOO_FAR_INTO_FUTURE,
    )

    success = False
    error_code = None
    error_code_name = ''
    erring_node_hex = None
    route_hops = 0
    route_scids = []
    fee_msat = 0

    invoice_features = _probe_invoice_features(enable_mpp)

    try:
        await lnworker.pay_to_node(
            node_pubkey=target_pubkey,
            payment_hash=payment_hash,
            payment_secret=payment_secret,
            amount_to_pay=amount_msat,
            min_final_cltv_delta=144,
            r_tags=[],
            invoice_features=invoice_features,
            attempts=1,
            budget=budget,
        )
    except PaymentFailure:
        pass
    except Exception as e:
        _logger.info(f"probe_node unexpected error: {e!r}")
        error_code_name = str(e)

    latency_ms = (time.monotonic() - t0) * 1000

    # read the htlc log
    log_entries = lnworker.logs.pop(payment_hash.hex(), [])
    if log_entries:
        last = log_entries[-1]
        if last.route:
            route_hops = len(last.route)
            route_scids = [str(edge.short_channel_id) for edge in last.route]
            # compute total fee from route (backward accumulation like lnrouter)
            if route_hops > 1:
                amt = amount_msat
                for edge in reversed(last.route[1:]):
                    amt += edge.fee_for_edge(amt)
                fee_msat = amt - amount_msat
        if last.failure_msg:
            error_code = int(last.failure_msg.code)
            error_code_name = last.failure_msg.code_name()
            if last.failure_msg.code == OnionFailureCode.INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS:
                success = True
            if last.sender_idx is not None and last.route:
                try:
                    erring_node_hex = last.route[last.sender_idx].node_id.hex()
                except (IndexError, AttributeError):
                    pass

    if not log_entries and not error_code_name:
        error_code_name = 'no route found'

    return ProbeResult(
        target_pubkey_hex=target_pubkey.hex(),
        target_alias=target_alias,
        attempt_number=attempt_number,
        success=success,
        error_code=error_code,
        error_code_name=error_code_name,
        erring_node_hex=erring_node_hex,
        route_hops=route_hops,
        route_scids=route_scids,
        fee_msat=fee_msat,
        latency_ms=round(latency_ms, 1),
        timestamp=time.time(),
    )


def resolve_random_targets(
    channel_db: 'ChannelDB',
    count: int,
    exclude_pubkey: Optional[bytes] = None,
) -> list:
    """Pick random node pubkeys from the gossip DB.

    Returns list of (pubkey_bytes, alias_str) tuples.
    """
    all_nodes = channel_db.get_node_infos()
    candidates = list(all_nodes.keys())
    if exclude_pubkey:
        candidates = [n for n in candidates if n != exclude_pubkey]
    chosen = random.sample(candidates, min(count, len(candidates)))
    result = []
    for nid in chosen:
        info = all_nodes.get(nid)
        alias = info.alias if info else ''
        result.append((nid, alias))
    return result
