#!/usr/bin/env python3
from swarm.config.settings import PROJECT_ROOT
print(f"PROJECT_ROOT = {PROJECT_ROOT}")
print(f".env path = {PROJECT_ROOT / '.env'}")
import os
print(f"CWD = {os.getcwd()}")
