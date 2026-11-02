"""Canonical Markdown summaries, immutable evidence and bounded disclosure.

Frontmatter is emitted as JSON (a YAML 1.2 subset) to keep the runtime dependency
free. Markdown prose is derived from sections in that same document, never a
separately editable second source of truth.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime, timezone

KINDS = {"project", "repository", "issue", "module", "file", "commit", "pr", "experiment", "handoff"}


class ContextError(Exception):
    def __init__(self, status, message, **details):
        super().__init__(message)
        self.result = {"status": status, "message": message, **details}


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path):
    return json.loads(Path(path).read_text())


def git(repo, *args, env=None):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    if result.returncode:
        # Callers never put credentials in command arguments or remotes.
        raise ContextError("git_error", result.stderr.strip()[:2000])
    return result.stdout.strip()


def key(ref):
    if not isinstance(ref, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*:[^\s]{1,512}", ref):
        raise ContextError("invalid", "Reference must be kind:stable-id", ref=str(ref))
    return re.sub(r"[^A-Za-z0-9._-]", "_", ref)[:96] + "-" + digest(ref)[:12]


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def init(self):
        for folder in ("objects", "sources", "blobs", "tasks", ".runtime"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        if not (self.root / "README.md").exists():
            atomic(self.root / "README.md", "# Research context\n\nCanonical summaries are in objects/. Immutable evidence is in blobs/.\nUse mvctx discover/open/startup; generated summaries are not verified conclusions.\n")
            atomic(self.root / ".gitignore", ".runtime/\n.write-*\n")
        return {"status": "ok", "root": str(self.root)}

    @contextlib.contextmanager
    def locked(self):
        self.init()
        with (self.root / ".runtime/lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def audit(self, event, **fields):
        self.init()
        with (self.root / ".runtime/events.jsonl").open("a") as out:
            fcntl.flock(out, fcntl.LOCK_EX)
            out.write(dump({"time": now(), "event": event, **fields}) + "\n")

    def bind_task_issue(self, task, source_ref):
        """Supervisor-only authoritative identity, separate from agent summaries."""
        source = self.source(source_ref)
        if source["origin"].get("kind") != "issue":
            raise ContextError("invalid", "Task authority must reference an Issue")
        binding = {"task": task, "issue_source": source_ref}
        with self.locked():
            path = self.root / "tasks" / (key(task) + ".json")
            if path.exists() and read_json(path) != binding:
                raise ContextError("conflict", "Task already bound to a different Issue")
            if not path.exists(): atomic(path, dump(binding) + "\n")

    def issue_sources(self, task):
        path = self.root / "tasks" / (key(task) + ".json")
        if path.exists():
            return [self.source(read_json(path)["issue_source"])]
        # Compatibility for old CLI stores without a trusted task configuration.
        return [self.source(s["ref"]) for s in self.get(task)["sources"]
                if self.source(s["ref"])["origin"].get("kind") == "issue"]

    def save_snapshot(self, snapshot):
        identifier = digest(dump(snapshot))
        atomic(self.root / ".runtime/snapshots" / (identifier + ".json"), dump(snapshot) + "\n")
        return {"id": identifier, "task": snapshot["task"], "observed_at": snapshot["observed_at"],
                "object_count": len(snapshot["objects"]), "source_count": len(snapshot["sources"])}

    def resolve_snapshot(self, snapshot):
        if "objects" in snapshot: return snapshot  # legacy full snapshots
        identifier = snapshot.get("id", "")
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise ContextError("invalid", "Invalid snapshot handle")
        path = self.root / ".runtime/snapshots" / (identifier + ".json")
        if not path.exists(): raise ContextError("missing", "Snapshot handle expired or belongs to another store")
        value = read_json(path)
        if digest(dump(value)) != identifier or value["task"] != snapshot.get("task"):
            raise ContextError("invalid", "Snapshot identity mismatch")
        return value

    def source(self, ref):
        p = self.root / "sources" / (key(ref) + ".json")
        if not p.exists():
            raise ContextError("missing", "Source is not registered", ref=ref)
        return read_json(p)

    def ingest(self, ref, text, origin, version, media_type="text/plain"):
        if not isinstance(text, str) or not isinstance(origin, dict) or not version:
            raise ContextError("invalid", "Source needs text, origin and version")
        sha = digest(text)
        with self.locked():
            atomic(self.root / "blobs" / (sha + ".txt"), text)
            p = self.root / "sources" / (key(ref) + ".json")
            old = read_json(p) if p.exists() else None
            record = {"ref": ref, "digest": sha, "version": str(version), "origin": origin,
                      "media_type": media_type, "size_chars": len(text), "observed_at": now()}
            if old and all(old.get(k) == record[k] for k in ("digest", "version", "origin", "media_type")):
                return old
            atomic(p, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        self.audit("source_ingested", ref=ref, digest=sha, version=str(version))
        return record

    def ingest_file(self, ref, path, origin=None, version=None):
        p = Path(path)
        if not p.is_file() or p.is_symlink():
            raise ContextError("missing", "Expected a regular artifact file", ref=ref)
        try:
            content = p.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise ContextError("unsupported", "Register a text manifest for binary artifacts, with location, sha256 and size") from None
        return self.ingest(ref, content, origin or {"kind": "artifact", "path": p.name}, version or digest(p.read_bytes()))

    def ingest_git(self, repo, repo_id, path, revision="HEAD"):
        if path.startswith("/") or ".." in Path(path).parts:
            raise ContextError("invalid", "Expected repository-relative path")
        commit = git(repo, "rev-parse", "--verify", revision + "^{commit}")
        result = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"], capture_output=True)
        if result.returncode:
            raise ContextError("missing", "File not found in selected commit", path=path, version=commit)
        try:
            content = result.stdout.decode()
        except UnicodeDecodeError:
            content = dump({"binary": True, "sha256": digest(result.stdout), "bytes": len(result.stdout)})
        return self.ingest(f"git:{repo_id}/{path}", content,
                           {"kind": "git", "repository": repo_id, "path": path}, commit)

    def _path(self, ref):
        return self.root / "objects" / (key(ref) + ".md")

    def get(self, ref):
        p = self._path(ref)
        if not p.exists():
            raise ContextError("missing", "Summary not found", ref=ref)
        text = p.read_text()
        try:
            return json.loads(text.split("\n---\n", 1)[0].removeprefix("---\n"))
        except (ValueError, IndexError):
            raise ContextError("invalid", "Invalid canonical frontmatter", ref=ref)

    def all(self):
        if not (self.root / "objects").exists():
            return []
        result = []
        for p in sorted((self.root / "objects").glob("*.md")):
            try:
                result.append(json.loads(p.read_text().split("\n---\n", 1)[0].removeprefix("---\n")))
            except ValueError:
                raise ContextError("invalid", "Invalid canonical document", file=p.name)
        return result

    def validate(self, proposal):
        p = dict(proposal)
        key(p.get("ref"))
        if p.get("kind") not in KINDS:
            raise ContextError("invalid", "Unknown summary kind", supported_kinds=sorted(KINDS))
        if not p.get("title") or not p.get("description"):
            raise ContextError("invalid", "Need title and description")
        if len(p["description"]) > 600:
            raise ContextError("invalid", "Description exceeds 600 characters")
        sections = p.get("sections")
        if not isinstance(sections, dict) or not sections or any(not isinstance(v, str) for v in sections.values()):
            raise ContextError("invalid", "sections must map names to prose")
        if p.get("review_status", "unreviewed") not in {"unreviewed", "reviewed", "disputed"}:
            raise ContextError("invalid", "Unknown review status")
        if p.get("review_status") == "reviewed" and not p.get("review_evidence"):
            raise ContextError("invalid", "Reviewed summaries require review_evidence")
        if not isinstance(p.get("sources"), list) or not p["sources"]:
            raise ContextError("invalid", "At least one version-bound source is required")
        canonical_sources = []
        for s in p["sources"]:
            latest = self.source(s["ref"])
            if not re.fullmatch(r"[0-9a-f]{64}", s.get("digest", "")) or not s.get("version"):
                raise ContextError("invalid", "Source needs digest and version")
            blob = self.root / "blobs" / (s["digest"] + ".txt")
            if not blob.exists() or digest(blob.read_bytes()) != s["digest"]:
                raise ContextError("missing", "Source snapshot missing or corrupted", ref=s["ref"])
            if latest["digest"] != s["digest"] or latest["version"] != s["version"]:
                raise ContextError("stale", "Re-read changed source before publishing", ref=s["ref"], latest=latest)
            canonical_sources.append(latest)
        p["sources"] = canonical_sources
        for ev in p.get("evidence", []):
            bound = next((s for s in p["sources"] if s["ref"] == ev.get("source")), None)
            if bound is None:
                raise ContextError("invalid", "Evidence source is not bound")
            total = len(self.blob(bound).splitlines())
            if not (isinstance(ev.get("start"), int) and isinstance(ev.get("end"), int)
                    and 1 <= ev["start"] <= ev["end"] <= total):
                raise ContextError("invalid", "Evidence needs a valid inclusive line range")
        for rel in p.get("relations", []):
            key(rel["ref"])
            if not rel.get("type"):
                raise ContextError("invalid", "Relations need type and ref")
        return p

    def submit(self, proposal, actor):
        with self.locked():
            p = self.validate(proposal)
            old = self.get(p["ref"]) if self._path(p["ref"]).exists() else None
            expected = p.pop("expected_revision", None)
            if not old and expected:
                raise ContextError("conflict", "Cannot update absent summary")
            p.pop("revision", None)
            p.pop("updated_at", None)
            p.pop("generated_by", None)
            p.setdefault("review_status", "unreviewed")
            p.setdefault("state", "active")
            p.setdefault("relations", [])
            p.setdefault("evidence", [])
            revision = digest(dump(p))
            if old and old["revision"] == revision:
                return {"status": "unchanged", "ref": p["ref"], "revision": revision}
            if old and expected != old["revision"]:
                raise ContextError("conflict", "Summary changed; read and merge first", current_revision=old["revision"])
            p.update(revision=revision, updated_at=now(), generated_by=actor)
            body = f"# {p['title']}\n\n{p['description']}\n"
            for name, content in p["sections"].items():
                body += f"\n## {name}\n\n{content}\n"
            body += "\n## Sources\n\n" + "\n".join(
                f"- `{s['ref']}` @ `{s['version']}`: [snapshot](../blobs/{s['digest']}.txt)" for s in p["sources"]) + "\n"
            atomic(self._path(p["ref"]), "---\n" + json.dumps(p, ensure_ascii=False, indent=2) + "\n---\n\n" + body)
        self.audit("summary_submitted", ref=p["ref"], revision=revision, actor=actor)
        return {"status": "ok", "ref": p["ref"], "revision": revision}

    def blob(self, bound):
        sha = bound.get("digest", "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ContextError("invalid", "Invalid snapshot digest")
        path = self.root / "blobs" / (sha + ".txt")
        if not path.is_file():
            raise ContextError("missing", "Snapshot unavailable", digest=sha)
        text = path.read_bytes().decode("utf-8")
        if digest(text) != sha:
            raise ContextError("invalid", "Snapshot integrity check failed", digest=sha)
        return text

    def freshness(self, obj):
        changes = []
        for s in obj["sources"]:
            try:
                latest = self.source(s["ref"])
                if (latest["version"], latest["digest"]) != (s["version"], s["digest"]):
                    changes.append({"source": s["ref"], "covered": s, "latest": latest})
            except ContextError as e:
                changes.append({"source": s["ref"], "status": e.result["status"]})
        if obj["kind"] == "issue" or (self.root / "tasks" / (key(obj["ref"]) + ".json")).exists():
            covered = {s["ref"] for s in obj["sources"]}
            for pending in self.task_sources(obj["ref"]):
                if pending["ref"] not in covered:
                    changes.append({"status": "uncovered_source", "ref": pending["ref"], "version": pending["version"],
                                    "read": "open this source ref with --view raw"})
            for related in self.all():
                if (related["ref"] != obj["ref"] and related.get("task") == obj["ref"]
                        and related["updated_at"] > obj["updated_at"]):
                    changes.append({"status": "new_related_progress", "ref": related["ref"], "revision": related["revision"]})
        return {"status": "stale" if changes else "no_known_changes", "changes": changes,
                "scope": "registered sources only; external changes require sync"}

    def bounded_freshness(self, obj):
        value = self.freshness(obj)
        total = len(value["changes"])
        return dict(value, changes=value["changes"][:8], total_changes=total,
                    omitted_changes=max(0,total-8),
                    continuation="Use context_changes pages for this task; open source refs for exact evidence")

    def card(self, obj):
        return {k: obj.get(k) for k in ("ref", "kind", "title", "description", "state", "review_status", "revision", "task")} | {
            "freshness": self.bounded_freshness(obj), "expand": ["summary", "evidence", "raw"]}

    def discover(self, task=None, query="", kind=None, limit=10, cursor=0):
        if not 1 <= limit <= 100 or cursor < 0:
            raise ContextError("invalid", "limit must be 1..100 and cursor nonnegative")
        words = query.lower().split()
        result = []
        for obj in self.all():
            linked = obj.get("task") == task or obj["ref"] == task or any(x["ref"] == task for x in obj["relations"])
            if task and not linked:
                continue
            if kind and obj["kind"] != kind:
                continue
            hay = dump(obj).lower()
            if words and not all(w in hay for w in words):
                continue
            result.append(self.card(obj))
        page = result[cursor:cursor+limit]
        self.audit("discover", task=task, query=query, returned=[x["ref"] for x in page])
        return {"status": "ok", "items": page, "total": len(result), "next_cursor": cursor+limit if cursor+limit < len(result) else None,
                "coverage": "registered summaries; no match is not proof of no relevant material"}

    def open(self, ref, view="summary", sections=None, limit=8000, cursor=None, source=None, start=None, end=None):
        if not 1000 <= limit <= 100000:
            raise ContextError("needs_budget", "Response limit must be 1000..100000 UTF-8 bytes")
        try:
            obj = self.get(ref)
        except ContextError as e:
            if e.result["status"] != "missing" or view not in ("raw", "L3"):
                raise
            raw_source = self.source(ref)
            obj = {"ref": ref, "kind": "source", "revision": digest(dump([raw_source["ref"],raw_source["version"],raw_source["digest"]])),
                   "sources": [raw_source], "sections": {"raw": ""}, "review_status": "unreviewed"}
        selected = sections or list(obj["sections"])
        if any(s not in obj["sections"] for s in selected):
            raise ContextError("missing", "Requested section not found", available=list(obj["sections"]))
        if view in ("card", "L0"):
            content = dump(self.card(obj))
        elif view in ("summary", "L1"):
            content = "\n\n".join(f"## {s}\n{obj['sections'][s]}" for s in selected)
        elif view in ("evidence", "L2"):
            pieces = []
            for e in obj["evidence"]:
                if sections and e.get("sections") and not set(sections).intersection(e["sections"]):
                    continue
                bound = next(s for s in obj["sources"] if s["ref"] == e["source"])
                lines = self.blob(bound).splitlines()
                excerpt = "\n".join(f"{i+1}: {lines[i]}" for i in range(e["start"]-1, e["end"]))
                pieces.append(f"SOURCE {bound['ref']} VERSION {bound['version']} SHA256 {bound['digest']}\n{excerpt}")
            content = "\n\n".join(pieces)
        elif view in ("raw", "L3"):
            matches = [s for s in obj["sources"] if source is None or s["ref"] == source]
            if len(matches) != 1:
                raise ContextError("select_source", "Select exactly one bound source", sources=obj["sources"])
            bound = matches[0]
            content = self.blob(bound)
            if start is not None or end is not None:
                lines = content.splitlines(keepends=True)
                a, b = start or 1, end or len(lines)
                if not (1 <= a <= b <= len(lines)):
                    raise ContextError("invalid", "Invalid raw line range")
                content = "".join(lines[a-1:b])
        else:
            raise ContextError("invalid", "Unknown view")
        signature = digest(dump([ref, obj["revision"], view, selected, source, start, end]))
        offset = 0
        if cursor:
            try:
                cur = json.loads(base64.urlsafe_b64decode(cursor))
                if cur["signature"] != signature:
                    raise ValueError()
                offset = cur["offset"]
                if not isinstance(offset, int) or not 0 <= offset <= len(content):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise ContextError("stale_cursor", "Cursor belongs to a different view or version")
        base = {"status": "ok", "ref": ref, "view": view, "revision": obj["revision"],
                "sources": obj["sources"], "freshness": self.bounded_freshness(obj), "review_status": obj["review_status"],
                "coverage": {"sections": selected, "offset": offset, "total_chars": len(content), "line_start": start, "line_end": end},
                "content": "", "next_cursor": None, "budget_unit": "UTF-8 bytes including response metadata"}
        if len(dump(base).encode()) > limit:
            raise ContextError("needs_budget", "Metadata alone exceeds budget", minimum_bytes=len(dump(base).encode()))
        low, high, best = 0, len(content)-offset, dict(base)
        while low <= high:
            n = (low+high)//2
            truncated = offset+n < len(content)
            candidate = dict(base, status="truncated" if truncated else "ok", content=content[offset:offset+n],
                             next_cursor=base64.urlsafe_b64encode(dump({"signature": signature, "offset": offset+n}).encode()).decode() if truncated else None)
            if len(dump(candidate).encode()) <= limit:
                best = candidate
                low = n+1
            else:
                high = n-1
        if content[offset:] and not best["content"]:
            raise ContextError("needs_budget", "Budget does not allow progress; increase limit")
        self.audit("read", ref=ref, view=view, revision=obj["revision"], sources=obj["sources"],
                   offset=offset, returned_chars=len(best["content"]), status=best["status"])
        return best

    def snapshot(self, task=None):
        objects = {o["ref"]: o["revision"] for o in self.all() if not task or o.get("task") == task or o["ref"] == task}
        sources = {s["ref"]: {"digest": s["digest"], "version": s["version"]}
                   for ref in objects for s in self.get(ref)["sources"]}
        # Capture latest observed source versions, including uncovered additions.
        for ref in sources:
            latest = self.source(ref)
            sources[ref] = {"digest": latest["digest"], "version": latest["version"]}
        for obj_ref in objects:
            if self.get(obj_ref)["kind"] == "issue" or (self.root / "tasks" / (key(obj_ref) + ".json")).exists():
                for s in self.task_sources(obj_ref):
                    sources[s["ref"]] = {"digest": s["digest"], "version": s["version"]}
        return {"task": task, "objects": objects, "sources": sources, "observed_at": now()}

    def task_sources(self, task):
        """Include synchronized discussion that has not yet reached the summary."""
        issue_origins = [s["origin"] for s in self.issue_sources(task)]
        result = []
        for p in sorted((self.root/"sources").glob("*.json")):
            s = read_json(p); origin = s["origin"]
            if any(origin.get("kind") in ("issue", "comment") and origin.get("repository") == issue.get("repository")
                   and origin.get("number") == issue.get("number") for issue in issue_origins):
                result.append(s)
        return result

    def changes(self, previous, task=None):
        previous = self.resolve_snapshot(previous)
        if task is not None and previous.get("task") not in (None, task):
            raise ContextError("denied", "Snapshot belongs to another task")
        current = self.snapshot(task if task is not None else previous.get("task"))
        changed = []
        for category in ("objects", "sources"):
            for ref in sorted(set(previous.get(category, {})) | set(current[category])):
                before, after = previous.get(category, {}).get(ref), current[category].get(ref)
                if before != after:
                    changed.append({"type": category, "ref": ref, "before": before, "after": after})
        return {"status": "ok", "changes": changed, "snapshot": current}

    def changes_page(self, previous, task, limit=8000, cursor=None, current=None):
        """Bounded immutable delta pages; target snapshot advances only after drain."""
        if not isinstance(limit, int) or not 2000 <= limit <= 100000:
            raise ContextError("needs_budget", "Change page budget must be 2000..100000 bytes")
        offset = 0
        if cursor:
            try:
                token = json.loads(base64.urlsafe_b64decode(cursor))
                identifier, offset = token["batch"], token["offset"]
                if not re.fullmatch(r"[0-9a-f]{64}", identifier) or not isinstance(offset, int): raise ValueError()
                batch = read_json(self.root / ".runtime/change-pages" / (identifier + ".json"))
                if digest(dump(batch)) != identifier or batch["task"] != task or not 0 <= offset <= len(batch["changes"]): raise ValueError()
            except (ValueError, KeyError, TypeError, OSError):
                raise ContextError("stale_cursor", "Change cursor is unavailable or belongs to another task") from None
        else:
            baseline = previous if previous is not None else {"task":task,"objects":{},"sources":{}}
            delta = self.changes(baseline, task)
            batch = {"task": task, "changes": delta["changes"], "snapshot": delta["snapshot"]}
            identifier = digest(dump(batch))
            atomic(self.root / ".runtime/change-pages" / (identifier + ".json"), dump(batch) + "\n")
        changes = batch["changes"]
        target = self.save_snapshot(batch["snapshot"])
        def page(count):
            end = offset + count
            more = end < len(changes)
            return {"status": "truncated" if more else "ok", "changes": changes[offset:end],
                    "snapshot": target, "total_changes": len(changes), "offset": offset,
                    "next_cursor": base64.urlsafe_b64encode(dump({"batch": identifier, "offset": end}).encode()).decode() if more else None,
                    "budget_unit": "UTF-8 bytes including response metadata"}
        low, high, best = 0, len(changes)-offset, None
        while low <= high:
            n = (low+high)//2; candidate = page(n)
            if len(dump(candidate).encode()) <= limit: best=candidate; low=n+1
            else: high=n-1
        if best is None or (offset < len(changes) and not best["changes"]):
            raise ContextError("needs_budget", "Change page cannot progress; increase limit")
        return best

    def startup(self, task, previous=None, limit=16000):
        obj = self.get(task)
        # Authority never depends on an agent's editable summary source list.
        requirements = [{"source": source, "original": self.blob(source)} for source in self.issue_sources(task)]
        full_snapshot = self.snapshot(task)
        candidates = [o for o in self.all() if o["ref"] != task and o.get("task") == task]
        freshness = self.freshness(obj)
        # Detailed freshness is available through open; push only bounded hints.
        hints = freshness["changes"][:8]
        task_card = {k: obj.get(k) for k in ("ref", "kind", "title", "description", "state", "review_status", "revision", "task")}
        task_card["title"] = str(task_card["title"])[:256]
        task_card["freshness"] = {"status": freshness["status"], "changes": hints,
                                  "total_changes": len(freshness["changes"]), "omitted_changes": len(freshness["changes"])-len(hints)}
        pack = {"status": "ok", "task": task_card, "requirements": requirements,
                "summary": {}, "summary_omitted": True, "related": [], "omitted_cards": len(candidates),
                "changes": [], "changes_next_cursor": None, "first_read": previous is None,
                "access": "Discover related cards; open task summary/evidence/raw on demand. Follow changes_next_cursor with context_changes(cursor=...). Full snapshots stay in the supervisor store.",
                "snapshot": self.save_snapshot(full_snapshot)}
        # Even the hints may be large (e.g. source metadata); omit them explicitly.
        if len(dump(pack).encode()) > limit:
            task_card["freshness"]["changes"] = []
            task_card["freshness"]["omitted_changes"] = len(freshness["changes"])
        if len(dump(pack).encode()) > limit:
            raise ContextError("needs_budget", "Original requirements exceed startup budget; increase it", minimum_bytes=len(dump(pack).encode()))
        # A first read lists existing objects/sources too, so uncovered comments
        # remain reachable even when their freshness hints do not fit.
        baseline = previous if previous is not None else {"task":task,"objects":{},"sources":{}}
        room = limit - len(dump(pack).encode())
        if room < 2500:
            # Produce a pinned continuation without dropping any delta.
            delta = self.changes(baseline, task)
            batch = {"task": task, "changes": delta["changes"], "snapshot": delta["snapshot"]}
            identifier = digest(dump(batch))
            atomic(self.root / ".runtime/change-pages" / (identifier + ".json"), dump(batch) + "\n")
            pack["changes_next_cursor"] = base64.urlsafe_b64encode(dump({"batch": identifier, "offset": 0}).encode()).decode() if delta["changes"] else None
        else:
            page = self.changes_page(baseline, task, limit=min(8000, room-256))
            pack["changes"] = page["changes"]
            pack["changes_next_cursor"] = page["next_cursor"]
            pack["snapshot"] = page["snapshot"]
        if len(dump(pack).encode()) > limit:
            raise ContextError("needs_budget", "Original requirements and continuation exceed budget")
        pack["summary"] = obj["sections"]; pack["summary_omitted"] = False
        if len(dump(pack).encode()) > limit:
            pack["summary"] = {}; pack["summary_omitted"] = True
        for candidate in candidates[:100]:
            item = self.card(candidate)
            # Large source/freshness lists remain behind open, never mandatory.
            item["freshness"] = {"status": item["freshness"]["status"]}
            pack["related"].append(item); pack["omitted_cards"] -= 1
            if len(dump(pack).encode()) > limit:
                pack["related"].pop(); pack["omitted_cards"] += 1
        self.audit("startup", task=task, refs=[task]+[x["ref"] for x in pack["related"]])
        return pack

    def publish(self, message="Update research context", env=None):
        with self.locked():
            if not (self.root / ".git").exists():
                git(self.root, "init", "-b", "main")
            git(self.root, "add", "--", "objects", "sources", "blobs", "tasks", "README.md", ".gitignore")
            if git(self.root, "diff", "--cached", "--name-only"):
                git(self.root, "-c", "user.name=Multiverse Context", "-c", "user.email=context@localhost", "commit", "-m", message)
            head = git(self.root, "rev-parse", "HEAD")
            remotes = git(self.root, "remote").splitlines()
            if "origin" in remotes:
                git(self.root, "push", "-u", "origin", "HEAD", env=env)
                state = "published"
            else:
                state = "committed_local"
        return {"status": state, "commit": head}
