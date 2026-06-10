import os

from e2b_code_interpreter import Sandbox

# --- CubeSandbox 配置 ---
os.environ["E2B_API_URL"] = "http://192.168.60.106:3000"
os.environ["E2B_API_KEY"] = "e2b_000000"          # 你的 key
os.environ["E2B_DOMAIN"] = "192.168.60.106"
TEMPLATE_ID = "tpl-8fa882f5d775429cad1530c9"

# 创建沙箱 → 跑代码 → 自动销毁
with Sandbox.create(template=TEMPLATE_ID) as sandbox:
    result = sandbox.run_code("print('Hello from remote CubeSandbox!')")
    print("沙箱输出:", result.text)

    # 再试个能算东西的,确认真在远程执行
    result2 = sandbox.run_code("import platform; print(platform.node()); print(2**10)")
    print("第二次输出:", result2.text)
