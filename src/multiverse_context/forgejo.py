"""Small Forgejo adapter with explicit pagination and idempotent notifications."""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote

from .core import ContextError, digest


class Forgejo:
    def __init__(self, url, token=None):
        self.url = url.rstrip("/")
        self.token = token if token is not None else os.environ.get("FORGEJO_TOKEN", "")

    def request(self, method, path, body=None):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "token " + self.token
        request = urllib.request.Request(self.url + "/api/v1" + path, method=method, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read()
                return json.loads(text) if text else None
        except urllib.error.HTTPError as error:
            status = {401: "denied", 403: "denied", 404: "missing", 409: "conflict"}.get(error.code, "remote_error")
            raise ContextError(status, "Forgejo request failed", http_status=error.code, path=path) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise ContextError("unavailable", "Forgejo connection failed", error_type=type(error).__name__) from None

    def pages(self, path, limit=50):
        rows = []
        for page in range(1, 1001):
            separator = "&" if "?" in path else "?"
            batch = self.request("GET", f"{path}{separator}limit={limit}&page={page}")
            if not isinstance(batch, list):
                raise ContextError("remote_error", "Expected paginated list")
            rows.extend(batch)
            if len(batch) < limit:
                return rows
        raise ContextError("truncated", "Pagination safety bound reached; source is incomplete", received=len(rows))

    def sync_issue(self, store, repository, number):
        route = f"/repos/{repository}/issues/{number}"
        issue = self.request("GET", route)
        def ingest_stable(ref, text, origin):
            # Issue updated_at also changes for comments/derived notifications.
            # Compare the actual source material to avoid false invalidation.
            try:
                old = store.source(ref)
                if old["digest"] == digest(text) and old["origin"] == origin:
                    return old
            except ContextError as e:
                if e.result["status"] != "missing": raise
            return store.ingest(ref, text, origin, digest(json.dumps([text, origin], sort_keys=True)))
        sources = [ingest_stable(f"forgejo:{repository}/issues/{number}", issue.get("body") or "",
                               {"kind": "issue", "repository": repository, "number": number,
                                "title": issue["title"], "url": issue.get("html_url", "")})]
        comments = self.pages(route + "/comments")
        for comment in comments:
            # Exclude our derived previews to prevent recursively ingesting them as evidence.
            if "<!-- mvctx:" in (comment.get("body") or ""):
                continue
            sources.append(ingest_stable(f"forgejo:{repository}/issues/{number}/comments/{comment['id']}", comment.get("body") or "",
                                        {"kind": "comment", "repository": repository, "number": number, "id": comment["id"],
                                         "author": comment.get("user", {}).get("login"), "url": comment.get("html_url", "")}))
        store.audit("issue_synchronized", repository=repository, number=number, source_count=len(sources), comment_count=len(comments))
        return {"status": "ok", "issue": {"title": issue["title"], "state": issue["state"], "number": number},
                "sources": sources, "comment_count": len(comments), "coverage": "all pages fetched"}

    def notify(self, repository, number, marker, body):
        tag = "<!-- mvctx:" + digest(marker) + " -->"
        route = f"/repos/{repository}/issues/{number}/comments"
        if any(tag in (c.get("body") or "") for c in self.pages(route)):
            return {"status": "unchanged"}
        result = self.request("POST", route, {"body": body + "\n\n" + tag})
        return {"status": "published", "id": result["id"], "url": result.get("html_url")}
