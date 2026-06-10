import os

# --- LangSmith 配置 ---
os.environ["LANGSMITH_TRACING"] = "true"
os.environ["LANGSMITH_API_KEY"] = "REDACTED-SECRET"   # 换成你的
os.environ["LANGSMITH_PROJECT"] = "swarm-dev"

from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="MiniMax-M2.7-Pro",              # 换成你 OpenWebUI 里的模型名
    base_url="http://ai.bit:3000/api",   # 确认你的 OpenWebUI 端口
    api_key="sk-REDACTED",                       # OpenWebUI 生成的 key
)

resp = llm.invoke("用一句话介绍你自己")
print(resp.content)
