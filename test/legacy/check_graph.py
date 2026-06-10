from swarm.brain.graph import compile_brain_graph
compiled = compile_brain_graph()
nodes = compiled.nodes
print('Registered nodes:', list(nodes.keys()))
if 'dispatch' in nodes:
    print('dispatch node type:', type(nodes['dispatch']))
