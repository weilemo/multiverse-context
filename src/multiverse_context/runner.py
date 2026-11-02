"""Start/finish adapter around Multiverse's actual --run-agent supervisor.

No native model loop is implemented here. Agents receive private filesystem copies
of the context and CLI; only this maintenance process publishes shared summaries.
"""
from pathlib import Path
import json
import os
import shutil
import signal
import subprocess
import time

from .core import ContextError, Store, atomic, digest, dump, git, read_json


def prepare(store, workspace, task, instruction, tool_src, previous=None):
    workspace = Path(workspace).resolve()
    context = workspace / ".context"
    context.mkdir(parents=True, exist_ok=True)
    mirror = context / "store"
    # Mirror is disposable and owned by this adapter, never a source repository.
    if mirror.exists():
        shutil.rmtree(mirror)
    shutil.copytree(store.root, mirror, ignore=shutil.ignore_patterns(".git", ".runtime"))
    lib = context / "lib" / "multiverse_context"
    shutil.copytree(tool_src, lib, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    atomic(context / "mvctx.py", "import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).parent/'lib'))\nfrom multiverse_context.cli import main\nraise SystemExit(main())\n")
    (context / "outbox").mkdir(exist_ok=True)
    bundle = store.startup(task, previous)
    atomic(context / "startup.json", dump(bundle)+"\n")
    protocol = """
Context protocol (required):
- Startup context below contains original requirements and source references.
- Use `python3 .context/mvctx.py --store .context/store discover --task ISSUE_REF` to find related objects.
- Use `python3 .context/mvctx.py --store .context/store open REF --view summary|evidence|raw`.
- For raw with multiple sources pass --source SOURCE_REF. Continue truncated reads with --cursor NEXT_CURSOR.
- If task freshness lists uncovered_source, read that source ref with --view raw before acting; it may contain new requirements not yet summarized.
- Register your result files using `ingest-file artifact:REPOSITORY/PATH REPOSITORY/PATH`.
- `source REF` returns the full version-bound descriptor to put in proposal.sources.
- Prefer `draft REF --kind KIND --task ISSUE_REF --sources SOURCE_REF ... --output FILE` to create a proposal with exact descriptors and expected_revision; fill its description, sections and evidence ranges.
- Write one proposal JSON per summary: ref, kind, title, description, task, sections (name->text), sources (descriptors), evidence (source,start,end), relations (type,ref), state, review_status.
- Use source evidence for observations. Mark interpretation, limitations, next_steps separately. Do not invent results or requirements.
- When updating an object include expected_revision from its current card. New objects omit it.
- Queue each proposal with `submit FILE --actor ACTOR --outbox .context/outbox`.
- Do not publish the shared context yourself. The maintenance adapter validates and publishes your queued proposals after the run.
- Before finishing write .context/artifacts.json as an array of {"ref":"artifact:REPOSITORY/PATH","path":"REPOSITORY/PATH"} for every new result/report you registered.
- Each proposed summary must cite actual registered sources. The maintenance process rejects missing, changed or out-of-range evidence.
"""
    instruction_path = context / "instruction.md"
    atomic(instruction_path, instruction + "\n" + protocol.replace("ISSUE_REF", task) + "\n\nSTARTUP CONTEXT\n" + dump(bundle) + "\n")
    return instruction_path, store.resolve_snapshot(bundle["snapshot"])


def collect(store, workspace, actor, repository, allowed_root):
    workspace = Path(workspace).resolve()
    allowed_root = Path(allowed_root).resolve()
    local = Store(workspace / ".context/store")
    manifest_path = workspace / ".context/artifacts.json"
    registered = []
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if not isinstance(manifest, list):
            raise ContextError("invalid", "Artifact manifest must be an array")
        for item in manifest:
            p = (workspace/item["path"]).resolve()
            if not p.is_relative_to(allowed_root) or not p.is_file() or p.is_symlink():
                raise ContextError("denied", "Artifact must be a regular file inside the task repository")
            local_record = local.source(item["ref"])
            if digest(p.read_bytes()) != local_record["digest"]:
                raise ContextError("stale", "Artifact changed after registration", ref=item["ref"])
            record = store.ingest(item["ref"], p.read_bytes().decode("utf-8"), local_record["origin"], local_record["version"])
            registered.append({"ref": item["ref"], "path": p.relative_to(allowed_root).as_posix(), "digest": record["digest"]})
    results = []
    for path in sorted((workspace/".context/outbox").glob("*.json")):
        queued = read_json(path)
        # The wrapper assigns identity; an agent cannot set another author by JSON.
        try:
            result = store.submit(queued["proposal"], actor)
        except ContextError as e:
            result = e.result | {"proposal": path.name}
        results.append(result)
        if result["status"] in ("ok", "unchanged"):
            processed = workspace/".context/processed"
            processed.mkdir(exist_ok=True)
            shutil.move(str(path), str(processed/path.name))
    return {"status": "ok" if results and all(x["status"] in ("ok", "unchanged") for x in results) else "needs_attention",
            "summaries": results, "artifacts": registered}


def run_once(store, workspace, task, instruction, tool_src, run_dir, plan, native, forgejo,
             actor, repository, allowed_root, binary="gewu-multiverse", previous=None):
    instruction_path, snapshot = prepare(store, workspace, task, instruction, tool_src, previous)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=False)
    plan = dict(plan, instruction=str(instruction_path), workspace=str(Path(workspace).resolve()))
    for name, data in (("plan.json", plan), ("native.json", native), ("forgejo.json", forgejo)):
        atomic(run_dir/name, json.dumps(data, indent=2)+"\n")
        os.chmod(run_dir/name, 0o600)
    shutil.copy2(Path(workspace)/".context/startup.json", run_dir/"context-startup.json")
    shutil.copytree(Path(workspace)/".context/lib/multiverse_context",run_dir/"context-tool-source",ignore=shutil.ignore_patterns("__pycache__"))
    start = time.monotonic()
    with (run_dir/"supervisor.log").open("w") as log:
        proc = subprocess.Popen([binary, "--run-agent", str(run_dir)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=plan["timeout_secs"]+45)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL); proc.wait()
            code = 124
    try:
        collected = collect(store, workspace, actor, repository, allowed_root)
    except ContextError as e:
        collected = e.result
    events = Path(workspace)/".context/store/.runtime/events.jsonl"
    if events.exists():
        shutil.copy2(events,run_dir/"context-events.jsonl")
    result = {"actor": actor, "run_dir": str(run_dir), "exit_code": code, "seconds": time.monotonic()-start,
              "initial_snapshot": snapshot, "collection": collected, "ended_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}
    result["tool_source_sha256"] = {p.name: digest(p.read_bytes()) for p in (Path(workspace)/".context/lib/multiverse_context").glob("*.py")}
    atomic(run_dir/"context-result.json", json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    store.audit("run_finished", actor=actor, exit_code=code, seconds=result["seconds"], collection_status=collected["status"])
    return result
