import argparse
import json
import os
from pathlib import Path
import sys

from .core import ContextError, Store, atomic, dump, read_json
from .forgejo import Forgejo


def parser():
    p = argparse.ArgumentParser(prog="mvctx")
    p.add_argument("--store", default=os.environ.get("MVCTX_STORE", ".context/store"))
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    f = commands.add_parser("ingest-file")
    f.add_argument("ref"); f.add_argument("path"); f.add_argument("--version")
    g = commands.add_parser("ingest-git")
    g.add_argument("repo"); g.add_argument("repo_id"); g.add_argument("path"); g.add_argument("--revision", default="HEAD")
    s = commands.add_parser("source")
    s.add_argument("ref")
    d = commands.add_parser("draft")
    d.add_argument("ref"); d.add_argument("--kind", required=True); d.add_argument("--task")
    d.add_argument("--sources", nargs="+", required=True); d.add_argument("--output", required=True)
    s = commands.add_parser("submit")
    s.add_argument("proposal"); s.add_argument("--actor", required=True)
    s.add_argument("--outbox", help="Validate and queue proposal for maintenance publisher")
    d = commands.add_parser("discover")
    d.add_argument("--task"); d.add_argument("--query", default=""); d.add_argument("--kind")
    d.add_argument("--limit", type=int, default=10); d.add_argument("--cursor", type=int, default=0)
    o = commands.add_parser("open")
    o.add_argument("ref"); o.add_argument("--view", default="summary")
    o.add_argument("--sections", nargs="+"); o.add_argument("--limit", type=int, default=8000)
    o.add_argument("--cursor"); o.add_argument("--source")
    o.add_argument("--start", type=int); o.add_argument("--end", type=int)
    b = commands.add_parser("batch")
    b.add_argument("requests", help="JSON array of open arguments")
    b.add_argument("--limit", type=int, default=32000, help="Total response byte budget")
    s = commands.add_parser("snapshot"); s.add_argument("--task"); s.add_argument("--output")
    c = commands.add_parser("changes"); c.add_argument("previous"); c.add_argument("--task")
    s = commands.add_parser("startup"); s.add_argument("task"); s.add_argument("--previous"); s.add_argument("--limit", type=int, default=16000)
    s = commands.add_parser("publish"); s.add_argument("--message", default="Update research context")
    f = commands.add_parser("sync-issue")
    f.add_argument("repository"); f.add_argument("number", type=int); f.add_argument("--url", default="http://127.0.0.1:3000")
    return p


def execute(args):
    store = Store(args.store)
    cmd = args.command
    if cmd == "init": return store.init()
    if cmd == "ingest-file": return store.ingest_file(args.ref, args.path, version=args.version)
    if cmd == "ingest-git": return store.ingest_git(args.repo, args.repo_id, args.path, args.revision)
    if cmd == "source": return store.source(args.ref)
    if cmd == "draft":
        from .core import KINDS
        if args.kind not in KINDS: raise ContextError("invalid", "Unknown kind", kinds=sorted(KINDS))
        proposal = {"ref":args.ref,"kind":args.kind,"title":args.ref,"description":"",
                    "task":args.task,"state":"active","review_status":"unreviewed",
                    "sections":{"observations":"","limitations":"","next_steps":""},
                    "sources":[store.source(ref) for ref in args.sources],"evidence":[],"relations":[]}
        if args.task: proposal["relations"]=[{"type":"task","ref":args.task}]
        try: proposal["expected_revision"]=store.get(args.ref)["revision"]
        except ContextError as e:
            if e.result["status"] != "missing": raise
        if Path(args.output).exists(): raise ContextError("conflict", "Draft output already exists; preserve and edit it")
        atomic(args.output,json.dumps(proposal,ensure_ascii=False,indent=2)+"\n")
        return {"status":"draft","path":args.output,"next":"Fill description, sections and evidence line ranges, then submit"}
    if cmd == "submit":
        proposal = read_json(args.proposal)
        if args.outbox:
            store.validate(proposal)
            from .core import digest
            path = Path(args.outbox) / (digest(dump(proposal)) + ".json")
            atomic(path, dump({"actor": args.actor, "proposal": proposal}) + "\n")
            return {"status": "queued", "path": str(path), "ref": proposal["ref"]}
        return store.submit(proposal, args.actor)
    if cmd == "discover": return store.discover(args.task, args.query, args.kind, args.limit, args.cursor)
    if cmd == "open": return store.open(args.ref, args.view, args.sections, args.limit, args.cursor, args.source, args.start, args.end)
    if cmd == "batch":
        requests = read_json(args.requests)
        if not isinstance(requests, list) or not 1 <= len(requests) <= 20:
            raise ContextError("invalid", "Batch must contain 1..20 reads")
        result = {"status": "ok", "results": [], "remaining_requests": []}
        for i, request in enumerate(requests):
            remaining = args.limit - len(dump(result).encode()) - 256
            if remaining < 1000:
                result["status"] = "truncated"; result["remaining_requests"] = requests[i:]; break
            try:
                item = store.open(**(request | {"limit": min(request.get("limit", 8000), remaining)}))
            except ContextError as e:
                item = e.result
            result["results"].append(item)
        if len(dump(result).encode()) > args.limit:
            raise ContextError("needs_budget", "Batch continuation metadata exceeds budget")
        return result
    if cmd == "snapshot":
        result = store.snapshot(args.task)
        if args.output: atomic(args.output, dump(result) + "\n")
        return result
    if cmd == "changes": return store.changes(read_json(args.previous), args.task)
    if cmd == "startup": return store.startup(args.task, read_json(args.previous) if args.previous else None, args.limit)
    if cmd == "publish": return store.publish(args.message)
    if cmd == "sync-issue": return Forgejo(args.url).sync_issue(store, args.repository, args.number)
    raise ContextError("invalid", "Unknown command")


def main():
    args = parser().parse_args()
    try:
        result = execute(args)
        code = 0
    except ContextError as e:
        result, code = e.result, 2
    except (OSError, ValueError, KeyError) as e:
        result, code = {"status": "invalid", "message": str(e)}, 2
    print(dump(result))
    return code


if __name__ == "__main__":
    sys.exit(main())
