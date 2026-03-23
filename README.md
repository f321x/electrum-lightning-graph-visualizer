## Electrum Lightning Graph Visualizer

<img width="1920" height="1200" alt="Screenshot_20260323_181843" src="https://github.com/user-attachments/assets/d6663aeb-e4af-42e8-bdac-5f2eb3656344" />


This is a plugin for the Electrum Bitcoin Wallet that visualizes the graph of Lightning Network nodes and channels contained in the wallets gossip database. It also allows to use the pathfinding functionality of Electrum to find a route and visualize it in the graph.

For this to work you need to disable Trampoline Routing to make Electrum fetch the Lightning Network gossip from other nodes.

This is 100% vibecoded, but it works reasonably well and i verified that the LLM didn't put any backdoor in. PRs welcome.
