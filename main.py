"""agent-hive CLI & HTTP server entrypoint."""
import argparse
import json
import os
import signal
import sys
import tomllib
from pathlib import Path

from hive.llm import LLMError
from hive.orchestrator import Orchestrator
from hive.registry import Registry
from hive.server import serve
from hive.tools import ToolLibrary

HELP = """Commands:
  /agents                   list agents
  /show NAME                show an agent's full spec
  /run NAME TASK            give a task straight to an agent (skips the orchestrator)
  /model                    show current orchestrator, default worker, and agent models
  /model orch MODEL [EP]    switch the orchestrator model (e.g. /model orch v4, /model orch r1)
  /model workers MODEL [EP] switch the default model for new child agents (e.g. /model workers r1)
  /model AGENT MODEL [EP]   switch an existing child agent's model (e.g. /model coder r1)
  /delete NAME              delete an agent
  /tasks                    recent tasks
  /reset                    clear the orchestrator's conversation memory
  /help                     this help
  /quit                     exit
Anything else goes to the orchestrator."""

DIM, RESET = "\033[2m", "\033[0m"


PLACEHOLDERS = {"your_deepseek_api_key_here", "sk-your-deepseek-api-key", ""}


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
            if not k or v in PLACEHOLDERS:
                continue
            if k not in os.environ or os.environ[k] in PLACEHOLDERS:
                os.environ[k] = v


def log(msg):
    print(f"{DIM}{msg}{RESET}", flush=True)


def _env_bool(name, default):
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _parse_args(argv):
    p = argparse.ArgumentParser(description="agent-hive: multi-model agent orchestrator")
    p.add_argument("config", nargs="?", default=None, help="path to config.toml")
    p.add_argument("--config", dest="config_opt", default=None, help="path to config.toml")
    p.add_argument("--serve", action="store_true",
                   default=_env_bool("HIVE_SERVE", False),
                   help="run HTTP API server instead of interactive CLI")
    p.add_argument("--host", default=os.environ.get("HIVE_HOST", "0.0.0.0"),
                   help="HTTP server bind host (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=int(os.environ.get("HIVE_PORT", "8000")),
                   help="HTTP server bind port (default: 8000)")
    return p.parse_args(argv)


def main():
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env", Path.cwd() / ".env")
    args = _parse_args(sys.argv[1:])

    cfg_arg = args.config_opt or args.config or os.environ.get("HIVE_CONFIG")
    cfg_path = Path(cfg_arg) if cfg_arg else root / "config.toml"
    if not cfg_path.is_file() and (root / "config.toml.example").is_file():
        cfg_path = root / "config.toml.example"
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    rt = cfg["runtime"]

    db_raw = os.environ.get("HIVE_DB_PATH") or rt["db_path"]
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = root / db_path

    ws_raw = os.environ.get("HIVE_WORKSPACE") or rt["workspace"]
    ws_path = Path(ws_raw)
    if not ws_path.is_absolute():
        ws_path = root / ws_path

    allow_python = _env_bool("HIVE_ALLOW_PYTHON", rt.get("allow_python", False))

    reg = Registry(db_path)
    lib = ToolLibrary(ws_path, allow_python)
    orch = Orchestrator(cfg, reg, lib, log)

    if args.serve:
        serve(orch, host=args.host, port=args.port)
        return

    def _sigterm(*_a):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except ValueError:
        pass

    print("agent-hive ready. Type /help for commands.", flush=True)
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
            elif cmd == "/model":
                parts = rest.strip().split()
                if not parts:
                    oc = cfg["orchestrator"]
                    w = cfg["workers"]
                    om, oep = orch.resolve_model(oc["model"], oc["endpoint"])
                    wm, wep = orch.resolve_model(w["model"], w["endpoint"])
                    print(f"Orchestrator : {oc['model']} -> {om} @ {oep}")
                    print(f"Workers (def): {w['model']} -> {wm} @ {wep}")
                    aliases = cfg.get("models", {})
                    if aliases:
                        print("Aliases      : " + ", ".join(f"{k}={v}" for k, v in aliases.items()))
                    for a in reg.list_agents():
                        print(f"Agent {a.name:<12}: {a.model} @ {a.endpoint}")
                elif len(parts) >= 2:
                    target, new_m = parts[0], parts[1]
                    new_ep = parts[2] if len(parts) > 2 else None
                    rm, rep = orch.resolve_model(new_m, new_ep)
                    if target in ("orch", "orchestrator"):
                        cfg["orchestrator"]["model"] = rm
                        if rep:
                            cfg["orchestrator"]["endpoint"] = rep
                        print(f"Orchestrator set to {rm} @ {cfg['orchestrator']['endpoint']}")
                    elif target in ("workers", "worker", "child", "children"):
                        cfg["workers"]["model"] = rm
                        if rep:
                            cfg["workers"]["endpoint"] = rep
                        print(f"Default worker set to {rm} @ {cfg['workers']['endpoint']}")
                    else:
                        a = reg.get_agent(target)
                        if not a:
                            print(f"No agent named '{target}'.")
                        else:
                            a.model = rm
                            if rep:
                                a.endpoint = rep
                            reg.save_agent(a)
                            print(f"Agent '{a.name}' set to {a.model} @ {a.endpoint}")
                else:
                    print("Usage: /model [orch|workers|AGENT_NAME] MODEL [ENDPOINT]")
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
