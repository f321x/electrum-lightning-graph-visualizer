# LN Graph Visualizer Plugin

Qt plugin for visualizing the Lightning Network gossip graph from `channel_db`. Targets Electrum developers debugging routing decisions.

## Access

Tools > LN Graph Visualizer (requires `LIGHTNING_USE_GOSSIP` enabled and synced gossip data).

## Architecture

```
qt.py           Plugin class + PluginDialog + QThread workers
graph_data.py   Data extraction from channel_db into plain dataclasses
graph_layout.py Fruchterman-Reingold force-directed layout (pure Python)
graph_scene.py  QGraphicsView/Scene, NodeItem, EdgeItem rendering
pathfinding.py  K-shortest paths via blacklist iteration
ab_testing.py   A/B test data model, async probe function, JSON persistence
ab_worker.py    ProbeWorker QThread bridging Qt→asyncio for probing
ab_ui.py        ABTestPanel widget + ComparisonDialog
```

### Threading Model

All heavy work runs in QThread workers to keep the GUI responsive:
- **DataWorker** — BFS neighborhood extraction from channel_db
- **LayoutWorker** — force-directed layout iterations (emits progressive updates every 10 iterations)
- **PathWorker** — pathfinding + path subgraph extraction
- **SearchWorker** — alias/pubkey search across full channel_db

- **ProbeWorker** — A/B test probing via `asyncio.run_coroutine_threadsafe` to bridge Qt→asyncio

Workers are stopped via `_stop_worker()` before starting a new one of the same type. Scene updates happen on the GUI thread via Qt signals.

### State Flags

- `_scene_stale` — set `True` when `_nodes`/`_edges` change; causes `_apply_positions()` to call `build_graph()` (full scene rebuild) instead of `update_positions()` (position-only update). Reset to `False` after rebuild.
- `_pending_highlight` — deferred `(source, dest, amount_msat)` tuple applied after layout finishes, since highlights require scene items to exist.

### Data Flow

1. User clicks Load → DataWorker extracts neighborhood → `_on_data_loaded` stores `_nodes`/`_edges`, sets `_scene_stale=True`
2. `_run_layout()` starts LayoutWorker → emits `positions_updated` every 10 iterations → `_apply_positions()` builds/updates scene
3. `layout_finished` → fit view, apply pending highlights, update status

### Key Electrum APIs Used

- `channel_db.get_channels_for_node(node_id)` → `Set[ShortChannelID]`
- `channel_db.get_channel_info(scid)` → `Optional[ChannelInfo]`
- `channel_db.get_policy_for_node(scid, node_id)` → `Optional[Policy]`
- `channel_db.get_node_info_for_node_id(node_id)` → `Optional[NodeInfo]`
- `channel_db.get_node_addresses(node_id)` → `Sequence[Tuple[str, int, int]]`
- `channel_db.get_node_infos()` → `Dict[bytes, NodeInfo]`
- `LNPathFinder(channel_db)` — private instance per search (avoids polluting real blacklist)

## Conventions

- Node IDs are `bytes` (33-byte compressed pubkeys) throughout. Hex conversion only at UI boundaries.
- Parallel channels between the same node pair are rendered as distinct curves with offset spacing.
- `extract_path_subgraph()` returns 4 values: `(context_nodes, context_edges, path_only_nodes, path_only_edges)`. The `path_sub`/`ctx_sub` tuples in PathWorker unpack different pairs depending on the view mode.
- Capacity filter uses `QGraphicsItem.setVisible()` on existing scene items (no scene rebuild needed). After a scene rebuild, the filter is reapply via `_get_min_capacity_filter()`.
- The `_append_policy_lines()` static method is shared between node and edge detail display.

## Gotchas

- `build_graph()` clears all scene items, so capacity filter visibility and path highlights must be reapplied after any rebuild.
- `update_positions()` temporarily disables `ItemSendsGeometryChanges` during batch moves to avoid O(n*m) edge updates, then updates all edges once at the end.
- Layout positions are stored per-node as `Dict[bytes, Tuple[float, float]]`. When expanding a node's neighborhood, existing positions are passed to the new layout as pinned anchors.
- `_stop_worker()` handles the full lifecycle: disconnect signals, stop, quit, wait, deleteLater. Always call before reassigning a worker reference.

## A/B Testing

The Mode dropdown has an "A/B Test" option that shows the `ABTestPanel`. Probing sends payments with random invalid `payment_hash` via `lnworker.pay_to_node(attempts=1)`. `INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS` from the destination = probe success; other onion errors = probe failure. Results are persisted as JSON in `{electrum_path}/ln_ab_tests/`. The `ComparisonDialog` loads two experiments and shows side-by-side stats with deltas. Target sets from previous experiments can be replayed via "Load Targets from Selected".
