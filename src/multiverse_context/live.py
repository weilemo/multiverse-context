"""Supervisor-side RPC backend for Multiverse's native MCP bridge.

Each call uses the immutable trusted run configuration and that agent's Forgejo
identity. A shared lock serializes publishers; agents never access credentials.
"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
import stat
import uuid

from .core import ContextError, Store, atomic, digest, dump, git, key, now, read_json
from .forgejo import Forgejo

TOOLS = {"context_select", "context_task", "context_discover", "context_open", "context_changes", "context_import_file", "context_source", "context_publish"}


class LiveContext:
    def __init__(self, run_dir, api=None, publish_remote=True):
        self.run = Path(run_dir).resolve()
        self.config = read_json(self.run/"research-context.json")
        self.plan = read_json(self.run/"plan.json")
        self.forgejo = read_json(self.run/"forgejo.json")
        self.workspace = Path(self.plan["workspace"]).resolve()
        self.task = self.repository = self.issue = self.repo = None
        self.state_dir = None
        self.store = Store(self.config["store"])
        if self.store.root.is_relative_to(self.workspace):
            raise ContextError("denied", "Publisher store cannot be native-writable")
        self.store.init()
        self.actor = self.plan["agent"]
        self.remote = publish_remote
        if api is None:
            credentials = tomllib.loads(Path(self.plan["credentials"]).read_text())
            token = credentials["forgejo_agent_tokens"][self.actor]
            self.api = Forgejo(self.forgejo["url"], token)
        else:
            self.api = api

    def set_scope(self, repository, issue, task):
        if not isinstance(repository, str) or type(issue) is not int or issue < 1:
            raise ContextError("invalid", "Select a repository and a positive Issue number")
        allowed = [r for r in self.forgejo["repositories"] if r["full_name"] == repository]
        if len(allowed) != 1:
            raise ContextError("denied", "Task repository is not assigned to this run")
        repo = self.workspace / allowed[0]["directory"]
        if repo.is_symlink() or not repo.resolve().is_relative_to(self.workspace):
            raise ContextError("denied", "Repository must be inside this run workspace")
        self.repository, self.issue, self.task, self.repo = repository, issue, task, repo.resolve()
        self.state_dir = self.run / "context-tasks" / key(task)

    def resolve_task(self, repository, issue):
        source_ref = f"forgejo:{repository}/issues/{issue}"
        matches = {read_json(p)["task"] for p in (self.store.root/"tasks").glob("*.json")
                   if read_json(p)["issue_source"] == source_ref}
        # Reuse pre-binding summaries, including old deployments with a custom task ref.
        if not matches:
            matches = {o["ref"] for o in self.store.all() if o["kind"] == "issue"
                       and any(s["ref"] == source_ref for s in o["sources"])}
        if len(matches) > 1:
            raise ContextError("conflict", "Multiple task identities for this Issue; reconcile supervisor bindings")
        if matches: return next(iter(matches))
        if (self.config.get("repository"), self.config.get("issue")) == (repository, issue) and self.config.get("task"):
            return self.config["task"]  # migration alias only; never auto-select
        return f"issue:{repository}#{issue}"

    def select_task(self, args):
        repository, issue = args.get("repository"), args.get("issue")
        # Check trusted assignment before reading any selected remote material.
        self.set_scope(repository, issue, "issue:selection")
        task = self.resolve_task(repository, issue)
        self.set_scope(repository, issue, task)
        synced = self.sync_issue()
        self.publish("Synchronize selected task sources")
        atomic(self.run/"context-active-task.json", dump({"repository":repository,"issue":issue,"task":task})+"\n")
        return {"status":"ok","task":task,"repository":repository,"issue":issue,
                "title":str(synced["issue"]["title"])[:256],"issue_source":synced["sources"][0]["ref"],
                "workspace_repository":self.repo.name,
                "workspace_directory":str(self.repo),
                "access":"Selection does not claim an Issue or read its requirements. Read issue_source via context_open(view=raw), discover summaries, or optionally call context_task. Work assignment is managed separately in Forgejo."}

    def load_task(self):
        path = self.run/"context-active-task.json"
        if not path.exists():
            raise ContextError("task_required", "Choose an Issue with context_select(repository, issue) first")
        selected = read_json(path)
        self.set_scope(selected["repository"], selected["issue"], selected["task"])

    @contextlib.contextmanager
    def locked(self):
        with (self.store.root/".runtime/live.lock").open("a") as file:
            deadline = time.monotonic()+25
            while True:
                try:
                    fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>deadline: raise ContextError("busy", "Another stage is publishing; retry")
                    time.sleep(.05)
            try: yield
            finally: fcntl.flock(file,fcntl.LOCK_UN)

    def git_environment(self):
        helper = self.run/"context-askpass.py"
        atomic(helper,"#!/usr/bin/env python3\nimport json,os,sys,tomllib\nfrom pathlib import Path\np=json.loads(Path(os.environ['MVCTX_RUN'],'plan.json').read_text())\nf=json.loads(Path(os.environ['MVCTX_RUN'],'forgejo.json').read_text())\nc=tomllib.loads(Path(p['credentials']).read_text())\nprint(f['login'] if 'username' in sys.argv[1].lower() else c['forgejo_agent_tokens'][p['agent']])\n")
        os.chmod(helper,0o700)
        return dict(os.environ,GIT_ASKPASS=str(helper),GIT_TERMINAL_PROMPT="0",MVCTX_RUN=str(self.run))

    def synchronize(self):
        if not self.remote: return
        if not (self.store.root/".git").is_dir():
            raise ContextError("not_initialized", "Clone the shared context repository before enabling native context")
        expected = self.forgejo["url"].rstrip("/")+"/team/research-context.git"
        actual = git(self.store.root,"remote","get-url","origin")
        if actual.rstrip("/").removesuffix(".git") != expected.removesuffix(".git"):
            raise ContextError("denied", "Publisher origin must be the configured Forgejo research-context repository")
        env = self.git_environment()
        # Finish an interrupted local transaction before fetching; no update reset.
        if git(self.store.root,"status","--porcelain"):
            self.store.publish("Recover pending context publication",env=env)
        git(self.store.root,"fetch","origin",env=env)
        ahead = git(self.store.root,"rev-list","--count","origin/main..HEAD")
        if int(ahead): git(self.store.root,"push","origin","HEAD:main",env=env)
        git(self.store.root,"merge","--ff-only","origin/main")

    def publish(self, message):
        if not self.remote: return {"status":"published","commit":"test-local"}
        result = self.store.publish(message,env=self.git_environment())
        if result["status"] != "published": raise ContextError("unpublished","Shared store has no remote")
        return result

    def sync_issue(self):
        if self.store._path(self.task).exists():
            obj = self.store.get(self.task)
            if obj["kind"] != "issue" or obj.get("task") not in (None, self.task):
                raise ContextError("conflict", "Task identity collides with another summary; reconcile it before selecting")
            original = self.store.issue_sources(self.task)
            expected = f"forgejo:{self.repository}/issues/{self.issue}"
            if original and not any(s["ref"] == expected for s in original):
                raise ContextError("conflict", "Existing task identifies another original Issue")
        synced = self.api.sync_issue(self.store,self.repository,self.issue)
        source = synced["sources"][0]
        self.store.bind_task_issue(self.task, source["ref"])
        if not self.store._path(self.task).exists():
            self.store.submit({"ref":self.task,"kind":"issue","task":self.task,"title":synced["issue"]["title"],
                "description":"Original research task; stage summaries are available through context_discover",
                "sections":{"requirements":self.store.blob(source),"state":"No execution conclusion has been recorded"},
                "sources":[source],"evidence":[],"state":"active"},"source-extractor")
        return synced

    def check_ref(self, ref):
        key(ref)
        try:
            obj=self.store.get(ref)
            if obj["ref"] != self.task and obj.get("task") != self.task:
                raise ContextError("denied","Object belongs to another task")
            return obj
        except ContextError as error:
            if error.result["status"] != "missing": raise
        source=self.store.source(ref)
        origin=source["origin"]
        if origin.get("repository") != self.repository:
            raise ContextError("denied","Source belongs to another repository")
        if origin.get("kind") in ("issue","comment") and origin.get("number")!=self.issue:
            raise ContextError("denied","Source belongs to another Issue")
        return source

    def import_file(self, args):
        relative=Path(args["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] in (".git",".context"):
            raise ContextError("denied","Expected a task-repository relative artifact path")
        path=self.repo
        for part in relative.parts:
            path=path/part
            if path.is_symlink():raise ContextError("denied","Artifact paths cannot traverse symlinks")
        if not path.resolve().is_relative_to(self.repo) or not path.is_file():
            raise ContextError("missing","Artifact file not found in task repository")
        if path.stat().st_size>2_000_000:raise ContextError("needs_manifest","Artifacts over 2 MB require a text manifest")
        data=path.read_bytes()
        try: text=data.decode("utf-8")
        except UnicodeDecodeError:raise ContextError("needs_manifest","Binary artifact requires text manifest") from None
        ref="artifact:"+self.repository+"/"+relative.as_posix()
        try:
            old=self.store.source(ref)
            if old["digest"]!=digest(data) and args.get("expected_digest")!=old["digest"]:
                raise ContextError("conflict","Source changed; read its latest descriptor first",current=old)
        except ContextError as e:
            if e.result["status"]!="missing":raise
        record=self.store.ingest(ref,text,{"kind":"artifact","repository":self.repository,"path":relative.as_posix()},digest(data))
        published=self.publish("Register stage evidence: "+ref)
        return {"status":"published","source":record,"publication":published}

    def change_page(self, args, previous):
        state_path = self.state_dir / "context-change-continuation.json"
        state = read_json(state_path) if state_path.exists() else {}
        cursor = args.get("cursor")
        def replay():
            result = state["last_result"]
            if len(dump(result).encode()) > args.get("limit",8000):
                raise ContextError("needs_budget", "Retry with the original page budget", minimum_bytes=len(dump(result).encode()))
            if result["next_cursor"] is None:
                atomic(self.state_dir/"context-read-snapshot.json",dump(result["snapshot"])+"\n")
            return result
        if cursor and cursor == state.get("last_cursor"):
            return replay()  # lost reply: repeat exactly that page
        if state.get("next_cursor") and cursor is None:
            return replay()  # never silently skip an unread first page
        if cursor and cursor != state.get("next_cursor"):
            raise ContextError("stale_cursor", "Cursor is not the pending page for this run")
        result = self.store.changes_page(previous, self.task, limit=args.get("limit",8000), cursor=cursor)
        atomic(state_path, dump({"next_cursor":result["next_cursor"],"last_cursor":cursor,"last_result":result})+"\n")
        if result["next_cursor"] is None:
            atomic(self.state_dir/"context-read-snapshot.json",dump(result["snapshot"])+"\n")
        return result

    def materialize_source(self, args):
        """Copy a version-checked source into the selected workspace, not through LLM prose."""
        if args.get("view") != "raw" or set(args) - {"ref", "view", "save_to", "expected_digest"}:
            raise ContextError("invalid", "save_to requires a direct source ref, view=raw and expected_digest; no partial reads")
        source = self.store.source(args["ref"])
        if args.get("expected_digest") != source["digest"]:
            raise ContextError("conflict", "Read the current source descriptor before saving", current=source)
        data = self.store.blob(source).encode("utf-8")
        if len(data) > 2_000_000:
            raise ContextError("needs_manifest", "Sources over 2 MB require a manifest")
        if digest(data) != source["digest"]:
            raise ContextError("invalid", "Stored source content does not match its digest")
        path = Path(args["save_to"])
        if path.is_absolute() or not path.parts or any(p in ("..", ".git", ".context") for p in path.parts):
            raise ContextError("denied", "save_to must be a task-repository relative file outside protected directories")
        # Directory descriptors and O_NOFOLLOW prevent symlink traversal, including
        # parent replacement while a native agent is running. Never overwrite files.
        with contextlib.ExitStack() as stack:
            directory = os.open(self.repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            try:
                for part in path.parts[:-1]:
                    try: os.mkdir(part, dir_fd=directory)
                    except FileExistsError: pass
                    directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    stack.callback(os.close, directory)
                temporary = ".context-copy-" + uuid.uuid4().hex
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    try:
                        os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    except FileExistsError:
                        existing = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                        with os.fdopen(existing, "rb") as saved:
                            if not stat.S_ISREG(os.fstat(saved.fileno()).st_mode) or saved.read(len(data)+1) != data:
                                raise ContextError("conflict", "Destination already exists with different content; choose another path")
                finally:
                    os.unlink(temporary, dir_fd=directory)
            except OSError:
                raise ContextError("denied", "Destination is not a writable regular file within the selected repository") from None
        return {"status":"ok", "ref":source["ref"], "view":"raw", "source":source,
                "saved":{"path":path.as_posix(), "workspace_path":str(self.repo/path), "bytes":len(data), "digest":source["digest"]},
                "coverage":"Complete source copied exactly; inspect or execute locally. Copying does not verify its conclusions."}

    def execute(self, name, args):
        if name not in TOOLS or not isinstance(args,dict):raise ContextError("invalid","Unknown context operation")
        # No caller-controlled identity, backend path, credentials or store.
        allowed={"context_select":{"repository","issue"},"context_task":set(),"context_changes":{"cursor","limit"},
            "context_discover":{"query","kind","limit","cursor"},
            "context_open":{"ref","view","source","sections","limit","cursor","start","end","save_to","expected_digest"},
            "context_import_file":{"path","expected_digest"},"context_source":{"ref"},"context_publish":{"proposal"}}
        if set(args)-allowed[name]:raise ContextError("invalid","Unexpected arguments")
        with self.locked():
            if name != "context_select": self.load_task()
            self.synchronize()
            if name == "context_select":
                result = self.select_task(args)
                self.record(name,args,result)
                return result
            if name in ("context_task","context_changes","context_publish"):
                self.sync_issue()
                self.publish("Synchronize original task sources")
            cursor_path=self.state_dir/"context-read-snapshot.json"
            previous=read_json(cursor_path) if cursor_path.exists() else None
            if name=="context_task":
                overhead = len(dump({"workspace_repository":self.repo.name}).encode()) + 1
                result=self.store.startup(self.task,previous,limit=32000-overhead)
                result["workspace_repository"]=self.repo.name
                continuation = result["changes_next_cursor"]
                state_path = self.state_dir/"context-change-continuation.json"
                if continuation:
                    page = {"status":"truncated","changes":result["changes"],"next_cursor":continuation,"snapshot":result["snapshot"]}
                    atomic(state_path,dump({"next_cursor":continuation,"last_cursor":None,"last_result":page})+"\n")
                else:
                    atomic(cursor_path,dump(result["snapshot"])+"\n")
                    state_path.unlink(missing_ok=True)
            elif name=="context_changes":
                result=self.change_page(args,previous)
            elif name=="context_discover":result=self.store.discover(task=self.task,**args)
            elif name=="context_open":
                self.check_ref(args["ref"])
                if "save_to" in args:
                    result=self.materialize_source(args)
                else:
                    if "expected_digest" in args:raise ContextError("invalid", "expected_digest requires save_to")
                    result=self.store.open(**args)
            elif name=="context_source":
                self.check_ref(args["ref"])
                result={"status":"ok","source":self.store.source(args["ref"])}
            elif name=="context_import_file":result=self.import_file(args)
            else:
                proposal=dict(args["proposal"])
                if proposal.get("task",self.task)!=self.task:raise ContextError("denied","Cannot publish another task")
                proposal["task"]=self.task
                if proposal["ref"].startswith("issue:") and proposal["ref"] != self.task:
                    raise ContextError("denied", "Issue refs are reserved for the selected task root")
                if proposal["ref"] == self.task and proposal.get("kind") != "issue":
                    raise ContextError("invalid", "The configured task object must remain an Issue")
                if self.store._path(proposal["ref"]).exists():self.check_ref(proposal["ref"])
                for source in proposal.get("sources",[]):self.check_ref(source["ref"])
                pending=self.state_dir/"context-pending"/(digest(dump(proposal))+".json")
                atomic(pending,dump(proposal)+"\n")
                submitted=self.store.submit(proposal,self.actor)
                published=self.publish("Stage summary by "+self.actor+": "+proposal["ref"])
                result={"status":"published","summary":submitted,"publication":published}
                # The summary is committed before the notice. Failure of the notice
                # cannot make an already published stage disappear or be fabricated.
                try:
                    link=self.forgejo["url"].rstrip("/")+"/team/research-context/src/commit/"+published["commit"]+"/objects/"+key(proposal["ref"])+".md"
                    result["notice"]=self.api.notify(self.repository,self.issue,"live:"+self.task+":"+proposal["ref"]+":"+submitted["revision"],
                        "阶段成果已共享：["+proposal["ref"]+"]("+link+")，作者 `"+self.actor+"`。上下文版本 `"+published["commit"][:12]+"`。通过 context_changes / context_open 读取依据。")
                except ContextError as e:result["notice"]=e.result
                pending.unlink(missing_ok=True)
            self.record(name,args,result)
            return result

    def record(self, name, args, result):
        record={"time":now(),"agent":self.actor,"run":self.plan["run_id"],"tool":name,"status":result.get("status"),
                "task":self.task,"repository":self.repository,"issue":self.issue,
                "ref":args.get("ref") or args.get("proposal",{}).get("ref"),"view":args.get("view"),"result":result}
        with (self.run/"context-events.jsonl").open("a") as out:out.write(dump(record)+"\n")
        atomic(self.run/"context-status.json",dump({k:v for k,v in record.items() if k!="result"})+"\n")


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--run-dir",required=True)
    args=parser.parse_args()
    request={}
    failed=False
    try:
        request=json.loads(sys.stdin.buffer.read(2_000_001))
        result=LiveContext(args.run_dir).execute(request["name"],request.get("arguments") or {})
    except ContextError as e:
        result=e.result;failed=True
    except (OSError,ValueError,KeyError,TypeError):
        result={"status":"invalid","message":"Context request or runtime configuration invalid; inspect trusted run configuration"};failed=True
    if failed:
        record={"time":now(),"tool":request.get("name") if isinstance(request,dict) else None,"status":result["status"],"result":result}
        with (Path(args.run_dir)/"context-events.jsonl").open("a") as out:out.write(dump(record)+"\n")
        atomic(Path(args.run_dir)/"context-status.json",dump({k:v for k,v in record.items() if k!="result"})+"\n")
    atomic(Path(args.run_dir)/"context-last-result.json",dump(result)+"\n")
    print(dump(result))
    return 0


if __name__=="__main__":raise SystemExit(main())
