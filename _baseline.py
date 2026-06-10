#!/usr/bin/env python3
from swarm.api.app import app
print(f"Route count: {len(app.routes)}")
for r in app.routes:
    if hasattr(r, 'methods'):
        print(f"  {r.methods} {r.path}")
    elif hasattr(r, 'path'):
        print(f"  WS/Mount {r.path}")
    else:
        print(f"  {r}")
