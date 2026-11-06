"""agent-hive CLI: talk to the orchestrator, or manage agents directly with /commands."""
import json
import os
import sys
import tomllib
from pathlib import Path

from hive.llm import LLMError
from hive.orchestrator import Orchestrator
from hive.registry import Registry
from hive.tools import ToolLibrary

HELP = """Commands:
  /agents            list agents
  /show NAME         show an agent's full spec
  /run NAME TASK     give a task straight to an agent (skips the orchestrator)
  /delete NAME       delete an agent
  /tasks             recent tasks
  /reset             clear the orchestrator's conversation memory
  /help              this help
  /quit              exit
Anything else goes to the orchestrator."""

DIM, RESET = "\033[2m", "\033[0m"


def load_dotenv(*paths):
    """Load key=value pairs from .env files into os.environ (stdlib only)."""
    for p in paths:
        path = Path(p)
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if k and k not in os.environ:
                os.environ[k] = v


def log(msg):
    print(f"{DIM}{msg}{RESET}", flush=True)


def main():
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env", Path.cwd() / ".env")
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "config.toml"
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    rt = cfg["runtime"]
    reg = Registry(rt["db_path"])
    lib = ToolLibrary(rt["workspace"], rt.get("allow_python", False))
    orch = Orchestrator(cfg, reg, lib, log)

    print("agent-hive ready. Type /help for commands.")
    while True:
        try:
            line = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        try:
            if cmd in ("/quit", "/exit"):
                break
            elif cmd == "/help":
                print(HELP)
            elif cmd == "/agents":
                agents = reg.list_agents()
                print("\n".join(f"- {a.name} [{a.model} @ {a.endpoint}] {a.description} "
                                f"tools={a.tools}" for a in agents) or "No agents yet.")
            elif cmd == "/show":
                a = reg.get_agent(rest.strip())
                print(json.dumps(a.__dict__, indent=2) if a else "No such agent.")
            elif cmd == "/run":
                name, _, task = rest.strip().partition(" ")
                if not task:
                    print("Usage: /run NAME TASK")
                else:
                    print(orch.run_worker(name, task))
            elif cmd == "/delete":
                print("Deleted." if reg.delete_agent(rest.strip()) else "No such agent.")
            elif cmd == "/tasks":
                for t in reversed(reg.recent_tasks(10)):
                    res = " ".join((t["result"] or "").split())[:100]
                    print(f"#{t['id']} {t['agent']} [{t['status']}] {t['task'][:60]} → {res}")
            elif cmd == "/reset":
                orch.history.clear()
                print("Orchestrator memory cleared.")
            elif cmd.startswith("/"):
                print("Unknown command. /help lists them.")
            else:
                print(f"\nhive> {orch.handle(line)}")
        except LLMError as e:
            print(f"Model server error: {e}")


if __name__ == "__main__":
    main()
