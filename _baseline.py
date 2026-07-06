#!/usr/bin/env python3
from swarm.api.app import app



def _main():  # R2：import 副作用治理——临时脚本必须 __main__ 守卫
    print(f"Route count: {len(app.routes)}")
    for r in app.routes:
        if hasattr(r, 'methods'):
            print(f"  {r.methods} {r.path}")
        elif hasattr(r, 'path'):
            print(f"  WS/Mount {r.path}")
        else:
            print(f"  {r}")


if __name__ == "__main__":
    _main()
