import json
from pathlib import Path
import tempfile
import unittest

from multiverse_context.core import ContextError, Store, dump


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.src = self.store.ingest("forgejo:team/demo/issues/1", "Keep the test split fixed.\nDo not report success without evaluation.\n", {"kind": "issue"}, "v1")
        self.proposal = {"ref": "issue:demo-1", "kind": "issue", "title": "Research", "description": "Compare two methods",
                         "sections": {"requirements": "Keep split fixed", "unknowns": "No results yet"},
                         "sources": [self.src], "evidence": [{"source": self.src["ref"], "start": 1, "end": 2}]}
        self.store.submit(self.proposal, "user-source-extract")

    def test_source_update_marks_summary_and_changes_but_preserves_original(self):
        before = self.store.snapshot()
        self.store.ingest(self.src["ref"], "Require two seeds.\n", {"kind": "issue"}, "v2")
        result = self.store.open("issue:demo-1", "raw")
        self.assertIn("test split", result["content"])
        self.assertEqual("stale", result["freshness"]["status"])
        self.assertEqual("sources", self.store.changes(before)["changes"][0]["type"])
        self.assertIn("two seeds", self.store.startup("issue:demo-1")["requirements"][0]["original"])

    def test_pagination_recovers_every_character_within_byte_budget(self):
        p = dict(self.proposal, sections={"results": "实验条件不可遗漏。"*2500}, expected_revision=self.store.get("issue:demo-1")["revision"])
        self.store.submit(p, "agent-a")
        cursor = None; recovered = ""; pages = 0
        while True:
            result = self.store.open("issue:demo-1", limit=2200, cursor=cursor)
            self.assertLessEqual(len(dump(result).encode()), 2200)
            recovered += result["content"]; pages += 1
            cursor = result["next_cursor"]
            if cursor is None: break
            self.assertLess(pages, 500)
        self.assertEqual("## results\n" + p["sections"]["results"], recovered)
        self.assertGreater(pages, 1)

    def test_conflicting_writer_cannot_overwrite_new_summary(self):
        rev = self.store.get("issue:demo-1")["revision"]
        self.store.submit(dict(self.proposal, expected_revision=rev, sections={"result": "A"}), "a")
        with self.assertRaises(ContextError) as caught:
            self.store.submit(dict(self.proposal, expected_revision=rev, sections={"result": "B"}), "b")
        self.assertEqual("conflict", caught.exception.result["status"])
        self.assertEqual({"result": "A"}, self.store.get("issue:demo-1")["sections"])

    def test_reject_stale_fabricated_and_out_of_range_evidence(self):
        self.store.ingest(self.src["ref"], "New condition", {"kind": "issue"}, "v2")
        with self.assertRaises(ContextError) as e:
            self.store.validate(self.proposal)
        self.assertEqual("stale", e.exception.result["status"])
        p = dict(self.proposal, sources=[dict(self.src, digest="0"*64)])
        with self.assertRaises(ContextError): self.store.validate(p)
        p = dict(self.proposal, sources=[self.store.source(self.src["ref"])], evidence=[{"source": self.src["ref"], "start": 1, "end": 999}])
        with self.assertRaises(ContextError): self.store.validate(p)

    def test_missing_raw_source_is_visible(self):
        (self.store.root/"blobs"/(self.src["digest"]+".txt")).unlink()
        with self.assertRaises(ContextError) as e: self.store.open("issue:demo-1", "raw")
        self.assertEqual("missing", e.exception.result["status"])

    def test_cursor_cannot_silently_continue_after_summary_change(self):
        p = dict(self.proposal, expected_revision=self.store.get("issue:demo-1")["revision"], sections={"result": "x"*5000})
        self.store.submit(p, "a")
        page = self.store.open("issue:demo-1", limit=2200)
        p["expected_revision"] = self.store.get("issue:demo-1")["revision"]
        p["sections"] = {"result": "y"*5000}
        self.store.submit(p, "b")
        with self.assertRaises(ContextError) as e: self.store.open("issue:demo-1", cursor=page["next_cursor"])
        self.assertEqual("stale_cursor", e.exception.result["status"])

    def test_required_constraints_cannot_be_silently_truncated(self):
        self.store.ingest(self.src["ref"], "Hard requirement\n"*3000, {"kind": "issue"}, "v2")
        with self.assertRaises(ContextError) as e: self.store.startup("issue:demo-1", limit=16000)
        self.assertEqual("needs_budget", e.exception.result["status"])

    def test_review_claim_requires_evidence(self):
        with self.assertRaises(ContextError): self.store.validate(dict(self.proposal, review_status="reviewed"))

    def test_retry_after_lost_ack_is_idempotent(self):
        self.assertEqual("unchanged", self.store.submit(self.proposal, "user-source-extract")["status"])

    def test_raw_crlf_bytes_are_preserved(self):
        path = self.store.root / "windows.txt"
        path.write_bytes(b"first\r\nsecond\r\n")
        source = self.store.ingest_file("artifact:windows.txt", path)
        self.assertEqual("first\r\nsecond\r\n", self.store.blob(source))

    def test_new_experiment_marks_older_task_summary_for_refresh(self):
        self.store.submit(dict(self.proposal, ref="experiment:new", kind="experiment", task="issue:demo-1"), "agent-a")
        result = self.store.card(self.store.get("issue:demo-1"))
        self.assertEqual("stale", result["freshness"]["status"])
        self.assertEqual("new_related_progress", result["freshness"]["changes"][0]["status"])

    def test_new_comment_is_discoverable_before_summary_has_been_updated(self):
        # This test uses a matching repository/number to represent a real Issue.
        src = self.store.ingest(self.src["ref"], "Original", {"kind":"issue","repository":"team/demo","number":1}, "v3")
        self.store.submit(dict(self.proposal, sources=[src], evidence=[], expected_revision=self.store.get("issue:demo-1")["revision"]), "a")
        before = self.store.snapshot("issue:demo-1")
        comment = self.store.ingest("forgejo:team/demo/issues/1/comments/99", "New requirement: two repeats", {"kind":"comment","repository":"team/demo","number":1}, "c1")
        pack = self.store.startup("issue:demo-1", before)
        self.assertTrue(any(x.get("ref")==comment["ref"] for x in pack["task"]["freshness"]["changes"]))
        self.assertTrue(any(x["ref"]==comment["ref"] for x in pack["changes"]))
        self.assertIn("two repeats", self.store.open(comment["ref"], "raw")["content"])


if __name__ == "__main__": unittest.main()
