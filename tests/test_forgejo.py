import unittest
import tempfile
from multiverse_context.core import Store
from multiverse_context.forgejo import Forgejo


class PaginationTests(unittest.TestCase):
    def test_latest_comment_after_multiple_pages_is_retrievable(self):
        f = Forgejo("http://example.invalid")
        comments = [{"id": n, "body": "condition corrected" if n == 120 else "discussion"} for n in range(121)]
        calls = []
        def request(method, path, body=None):
            calls.append(path)
            page = int(path.split("page=")[1])
            return comments[(page-1)*50:page*50]
        f.request = request
        result = f.pages("/repos/team/demo/issues/1/comments")
        self.assertEqual(121, len(result))
        self.assertEqual("condition corrected", result[-1]["body"])
        self.assertEqual(3, len(calls))

    def test_notification_is_idempotent(self):
        f = Forgejo("http://example.invalid")
        rows = []
        def request(method, path, body=None):
            if method == "GET": return rows
            rows.append(dict(body, id=1)); return rows[-1]
        f.request = request
        self.assertEqual("published", f.notify("team/demo", 1, "entry", "Summary")["status"])
        self.assertEqual("unchanged", f.notify("team/demo", 1, "entry", "Summary")["status"])
        self.assertEqual(1, len(rows))

    def test_comment_timestamp_does_not_invalidate_unchanged_requirements(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp); f = Forgejo("http://example.invalid")
            issue = {"title": "Task", "body": "Keep the split fixed", "state": "open", "updated_at": "first"}
            f.request = lambda *a: issue
            f.pages = lambda *a: []
            before = f.sync_issue(store, "team/demo", 1)["sources"][0]
            issue["updated_at"] = "notification-only"
            after = f.sync_issue(store, "team/demo", 1)["sources"][0]
            self.assertEqual(before, after)
            issue["body"] = "Require two repeats"
            changed = f.sync_issue(store, "team/demo", 1)["sources"][0]
            self.assertNotEqual(before["digest"], changed["digest"])
            self.assertNotEqual(before["version"], changed["version"])
