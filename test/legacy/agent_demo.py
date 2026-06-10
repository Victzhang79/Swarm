import os

os.environ["LANGSMITH_TRACING"] = "true"
os.environ["LANGSMITH_API_KEY"] = "REDACTED-SECRET"      # 你的
os.environ["LANGSMITH_PROJECT"] = "swarm-dev"

from pathlib import Path
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

WORKSPACE = Path("workspace")
def _resolve(filename: str) -> Path:
    """把模型传来的文件名规范化到 workspace 目录下,容忍多余的 workspace 前缀。"""
    p = Path(filename)
    parts = [x for x in p.parts if x not in (".", "workspace")]
    return WORKSPACE.joinpath(*parts)

# --- 给 agent 的工具 ---

@tool
def read_file(filename: str) -> str:
    """读取 workspace 目录下指定文件的内容。只需传文件名,如 math_util.py。"""
    return _resolve(filename).read_text(encoding="utf-8")

@tool
def write_file(filename: str, content: str) -> str:
    """把内容写入 workspace 目录下的指定文件,覆盖原内容。只需传文件名,如 math_util.py。"""
    _resolve(filename).write_text(content, encoding="utf-8")
    return f"已写入 {filename}"


# --- 模型 ---
llm = ChatOpenAI(
    model="MiniMax-M2.7-Pro",              # 你的模型名
    base_url="http://ai.bit:3000/api",
    api_key="sk-REDACTED",
)

# --- 创建 agent ---
agent = create_react_agent(llm, tools=[read_file, write_file])

# --- 跑一个任务 ---
task = (
    "workspace 目录下有个 math_util.py 文件。"
    "请先读取它,然后给 double 函数加上类型注解和一句 docstring,"
    "最后把修改后的完整代码写回原文件。"
)

result = agent.invoke({"messages": [("user", task)]})

# 打印最后的回复
print(result["messages"][-1].content)
