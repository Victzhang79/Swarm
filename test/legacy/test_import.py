#!/usr/bin/env python3
import importlib
try:
    mod = importlib.import_module("langgraph.serde.msgpack")
    print("Available:", dir(mod))
except Exception as e:
    print(f"Import failed: {e}")
    # Try alternative
    try:
        from langgraph.checkpoint.serde.msgpack import MsgpackSerializer
        print("Alternative import OK")
    except Exception as e2:
        print(f"Alternative also failed: {e2}")
