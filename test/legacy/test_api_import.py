#!/usr/bin/env python
"""Quick import test for swarm.api.app"""

from swarm.api.app import app

print("Import OK")
print("Routes:")
for r in app.routes:
    if hasattr(r, "methods"):
        print(f"  {r.methods} {r.path}")
