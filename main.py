"""agent-hive CLI, API key manager, and FastAPI HTTP server entrypoint."""
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
  /keys                     list issued API keys
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


def _load_runtime(cfg_arg=None):
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env", Path.cwd() / ".env")
    cfg_raw = cfg_arg or os.environ.get("HIVE_CONFIG")
    cfg_path = Path(cfg_raw) if cfg_raw else root / "config.toml"
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

    ct_raw = (
        os.environ.get("HIVE_CUSTOM_TOOLS_DIR")
        or cfg.get("tool_creator", {}).get("tools_dir")
        or str(db_path.parent / "custom_tools")
    )
    ct_path = Path(ct_raw)
    if not ct_path.is_absolute():
        ct_path = root / ct_path

    allow_python = _env_bool("HIVE_ALLOW_PYTHON", rt.get("allow_python", False))
    reg = Registry(db_path)
    return cfg, reg, ToolLibrary(ws_path, allow_python, registry=reg, custom_tools_dir=ct_path)


def _handle_key_cli(argv):
    p = argparse.ArgumentParser(prog="main.py key", description="Generate and manage agent-hive API keys")
    p.add_argument("--config", default=None, help="path to config.toml")
    sub = p.add_subparsers(dest="action", required=True)

    c = sub.add_parser("create", help="create a new API key and print distribution snippet")
    c.add_argument("name", help="human label (e.g. 'partner-app')")
    c.add_argument("--scopes", default="read,write", help="comma-separated: read,write,admin")
    c.add_argument("--days", type=float, default=None, help="expiration in days (optional)")
    c.add_argument("--base-url", default=os.environ.get("HIVE_PUBLIC_URL", "http://localhost:8000"),
                   help="base URL to embed in copy-ready snippets")
    c.add_argument("--json", dest="as_json", action="store_true", help="output JSON bundle")

    sub.add_parser("list", help="list all API keys")

    r = sub.add_parser("revoke", help="revoke an API key by ID")
    r.add_argument("key_id")

    rot = sub.add_parser("rotate", help="rotate an API key by ID")
    rot.add_argument("key_id")
    rot.add_argument("--base-url", default=os.environ.get("HIVE_PUBLIC_URL", "http://localhost:8000"))

    args = p.parse_args(argv)
    _, reg, _ = _load_runtime(args.config)

    if args.action == "list":
        keys = reg.list_api_keys()
        if not keys:
            print("No API keys issued yet.")
            return
        for k in keys:
            print(f"{k['id']}  {k['name']:<20}  {k['key_prefix']:<18}  "
                  f"scopes={','.join(k['scopes']):<16}  status={k['status']:<7}  reqs={k['request_count']}")
        return

    if args.action == "revoke":
        print(f"Revoked {args.key_id}." if reg.revoke_api_key(args.key_id) else f"Key '{args.key_id}' not found or already revoked.")
        return

    if args.action == "create":
        scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
        raw_key, meta = reg.create_api_key(args.name, scopes=scopes, expires_in_days=args.days)
    else:  # rotate
        res = reg.rotate_api_key(args.key_id)
        if not res:
            print(f"No API key '{args.key_id}'.", file=sys.stderr)
            sys.exit(1)
        raw_key, meta = res

    base = args.base_url.rstrip("/")
    if getattr(args, "as_json", False):
        print(json.dumps({
            "api_key": raw_key,
            "key": meta,
            "base_url": base,
            "docs_url": f"{base}/docs",
            "auth_header": f"Authorization: Bearer {raw_key}",
        }, indent=2))
        return

    print(f"Created API key '{meta['name']}' ({meta['id']}) scopes={meta['scopes']}")
    print(f"\nAPI Key (shown once):\n  {raw_key}\n")
    print("Quick distribute (.env):")
    print(f"  AGENT_HIVE_URL={base}")
    print(f"  AGENT_HIVE_API_KEY={raw_key}\n")
    print("Quick test (curl):")
    print(f'  curl -X POST "{base}/api/v1/chat" \\')
    print(f'    -H "Authorization: Bearer {raw_key}" \\')
    print('    -H "Content-Type: application/json" \\')
    print('    -d \'{"message": "List your agents"}\'')


def _parse_args(argv):
    p = argparse.ArgumentParser(description="agent-hive: multi-model agent orchestrator")
    p.add_argument("config", nargs="?", default=None, help="path to config.toml")
    p.add_argument("--config", dest="config_opt", default=None, help="path to config.toml")
    p.add_argument("--serve", action="store_true",
                   default=_env_bool("HIVE_SERVE", False),
                   help="run FastAPI HTTP server & dashboard instead of interactive CLI")
    p.add_argument("--host", default=os.environ.get("HIVE_HOST", "0.0.0.0"),
                   help="HTTP server bind host (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=int(os.environ.get("HIVE_PORT", "8000")),
                   help="HTTP server bind port (default: 8000)")
    return p.parse_args(argv)


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("key", "keys"):
        _handle_key_cli(sys.argv[2:])
        return

    args = _parse_args(sys.argv[1:])
    cfg, reg, lib = _load_runtime(args.config_opt or args.config)
    orch = Orchestrator(cfg, reg, lib, log)

    if args.serve:
        from hive.server import serve
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
            elif cmd == "/keys":
                keys = reg.list_api_keys()
                print("\n".join(f"- {k['id']} {k['name']} [{k['status']}] scopes={k['scopes']} "
                                f"reqs={k['request_count']}" for k in keys) or "No API keys issued yet.")
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
