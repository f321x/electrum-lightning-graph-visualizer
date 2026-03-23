## Electrum Lightning Graph Visualizer

This is a plugin for the Electrum Bitcoin Wallet that visualizes the graph of Lightning Network nodes and channels contained in the wallets gossip database. It also allows to use the pathfinding functionality of Electrum to find a route and visualize it in the graph.

For this to work you need to disable Trampoline Routing to make Electrum fetch the Lightning Network gossip from other nodes.

This is 100% vibecoded, but it works reasonably well and i verified that the LLM didn't put any backdoor in. PRs welcome.
