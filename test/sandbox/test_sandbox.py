from __future__ import annotations
import os

# === 配置:必须在 import dev_sidecar / Sandbox 之前设好 ===
os.environ["E2B_API_URL"] = "http://192.168.60.106:3000"        # 控制面:CubeAPI
os.environ["CUBE_REMOTE_PROXY_BASE"] = "https://192.168.60.106:443"  # 数据面:CubeProxy
os.environ["CUBE_REMOTE_SANDBOX_DOMAIN"] = "cube.app"
os.environ["E2B_API_KEY"] = "e2b_000000"
os.environ["CUBE_REMOTE_PROXY_VERIFY_SSL"] = "false"             # 自签证书,跳过校验
# 注意:绝对不要设 E2B_DOMAIN,sidecar 接管后它会干扰
os.environ.pop("E2B_DOMAIN", None)

TEMPLATE_ID = "tpl-8fa882f5d775429cad1530c9"

# === 启动本地 sidecar 并给 SDK 打补丁(必须在导入 Sandbox 之前)===
from dev_sidecar import setup_dev_sidecar
setup_dev_sidecar()

# === 打补丁之后再导入 Sandbox ===
from e2b_code_interpreter import Sandbox


def main() -> None:
    with Sandbox.create(template=TEMPLATE_ID) as sandbox:
        # print() 的输出走 stdout,不是 .text
        r1 = sandbox.run_code("print('Hello from remote CubeSandbox!')")
        print("第一次输出:", "".join(r1.logs.stdout))

        r2 = sandbox.run_code("import platform; print(platform.node()); print(2**10)")
        print("第二次输出:", "".join(r2.logs.stdout))

        # 裸表达式的值才会出现在 .text 里
        r3 = sandbox.run_code("2**10")
        print("第三次输出 (.text):", r3.text)


if __name__ == "__main__":
    main()
