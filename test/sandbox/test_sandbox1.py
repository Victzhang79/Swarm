from __future__ import annotations

import os

os.environ["E2B_API_URL"] = "http://192.168.60.106:3000"
os.environ["CUBE_REMOTE_PROXY_BASE"] = "https://192.168.60.106:443"
os.environ["CUBE_REMOTE_SANDBOX_DOMAIN"] = "cube.app"
os.environ["E2B_API_KEY"] = "e2b_000000"
os.environ["CUBE_REMOTE_PROXY_VERIFY_SSL"] = "false"
os.environ.pop("E2B_DOMAIN", None)

TEMPLATE_ID = "tpl-6f9b38b4584a46b0a9c99ae9"

from dev_sidecar import setup_dev_sidecar

setup_dev_sidecar()
from e2b_code_interpreter import Sandbox


def main() -> None:
    sbx = Sandbox.create(template=TEMPLATE_ID)
    print("=" * 50)
    print("sandbox_id   =", sbx.sandbox_id)
    print("envd_port    =", sbx.connection_config.envd_port)
    print("get_host(49999) =", sbx.get_host(49999))
    print("sidecar 实际访问的 Host 头应为: 49999-%s.cube.app" % sbx.sandbox_id)
    print("=" * 50)
    print("沙箱保持存活中。另开终端做 curl 测试,测完回这里按回车销毁。")
    input()
    sbx.kill()

if __name__ == "__main__":
    main()
