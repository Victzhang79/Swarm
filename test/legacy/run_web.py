#!/usr/bin/env python3
"""Swarm API 启动脚本

用法:
    python run_web.py          # 默认端口 8420
    python run_web.py 9000     # 自定义端口
"""

import sys
from pathlib import Path

# 加载 .env
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / '.env')

if __name__ == '__main__':
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8420
    uvicorn.run('swarm.api.app:app', host='0.0.0.0', port=port, reload=True)
