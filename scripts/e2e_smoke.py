"""E2E 冒烟：创建任务并轮询 7 步流程状态。"""
import json
import subprocess
import sys
import time

API = "http://localhost:8420"
PID = "e6ca3f4a-bca0-4bab-86f1-4195fceb8cb3"


def curl(args, timeout=60):
    r = subprocess.run(["curl", "-s", *args], capture_output=True, text=True, timeout=timeout)
    return r.stdout


def main():
    login = json.loads(curl(["-X", "POST", f"{API}/api/auth/login",
                             "-H", "Content-Type: application/json",
                             "-d", '{"username":"admin","password":"swarm"}']))
    tok = login["token"]
    bearer = "Bearer" + " " + tok
    auth = ["-H", "Authorization: " + bearer]

    body = json.dumps({
        "description": "在 src/dotenv/parser.py 顶部添加一行模块级注释 # parsed by swarm e2e test",
        "auto_accept": True,
    })
    resp = json.loads(curl(["-X", "POST", f"{API}/api/projects/{PID}/tasks",
                            "-H", "Content-Type: application/json", *auth, "-d", body]))
    task = resp.get("task") or resp
    tid = task.get("id")
    print(f"[create] task={tid} status={task.get('status')}", flush=True)
    if not tid:
        print("创建失败:", resp)
        return 1

    seen = []
    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(4)
        d = json.loads(curl([f"{API}/api/tasks/{tid}", *auth]))
        t = d.get("task") or d
        st = t.get("status")
        comp = t.get("completed_subtasks")
        total = t.get("subtask_count")
        if st != (seen[-1] if seen else None):
            seen.append(st)
            print(f"[poll] status={st} subtasks={comp}/{total}", flush=True)
        if st in ("DONE", "FAILED", "CANCELLED"):
            print(f"[final] status={st}")
            print(f"[final] has_diff={bool(t.get('merged_diff'))}")
            if t.get("merged_diff"):
                print("---- diff head ----")
                print("\n".join((t["merged_diff"] or "").splitlines()[:25]))
            print(f"[final] status_path={seen}")
            return 0
    print(f"[timeout] last status={seen[-1] if seen else '?'} path={seen}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
