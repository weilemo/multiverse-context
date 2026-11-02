import json
from pathlib import Path
import tempfile
import unittest
from multiverse_context.core import Store, ContextError, atomic, dump
from multiverse_context.runner import collect, prepare


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/"canonical")
        source=self.store.ingest("forgejo:team/demo/issues/1","Keep split fixed",{"kind":"issue"},"1")
        self.store.submit({"ref":"issue:1","kind":"issue","title":"Task","description":"Compare methods",
                           "sources":[source],"sections":{"requirements":"Keep split fixed"}},"extractor")
        self.workspace=self.root/"agent";self.repo=self.workspace/"demo";self.repo.mkdir(parents=True)
        prepare(self.store,self.workspace,"issue:1","Do research",Path(__file__).parents[1]/"src/multiverse_context")

    def test_agent_authored_artifact_and_summary_survive_independent_copy(self):
        output=self.repo/"result.json";output.write_text('{"metric":0.5}\n')
        local=Store(self.workspace/".context/store")
        source=local.ingest_file("artifact:demo/result.json",output)
        proposal={"ref":"experiment:1","kind":"experiment","title":"Result","description":"Observed score",
                  "task":"issue:1","sources":[source],"sections":{"result":"metric 0.5; unreviewed"},
                  "evidence":[{"source":source["ref"],"start":1,"end":1}]}
        atomic(self.workspace/".context/artifacts.json",dump([{"ref":source["ref"],"path":"demo/result.json"}]))
        atomic(self.workspace/".context/outbox/one.json",dump({"actor":"fake-other-agent","proposal":proposal}))
        result=collect(self.store,self.workspace,"real-agent","demo",self.repo)
        self.assertEqual("ok",result["status"])
        self.assertEqual("real-agent",self.store.get("experiment:1")["generated_by"])
        self.assertFalse((self.workspace/".context/outbox/one.json").exists())
        self.assertTrue((self.workspace/".context/processed/one.json").exists())
        second=self.root/"other-agent"
        prepare(self.store,second,"issue:1","Review",Path(__file__).parents[1]/"src/multiverse_context")
        evidence=Store(second/".context/store").open("experiment:1","raw")["content"]
        self.assertIn('"metric":0.5',evidence)

    def test_mutated_artifact_cannot_publish_old_digest(self):
        output=self.repo/"result.txt";output.write_text('before')
        local=Store(self.workspace/".context/store")
        source=local.ingest_file("artifact:demo/result.txt",output)
        atomic(self.workspace/".context/artifacts.json",dump([{"ref":source["ref"],"path":"demo/result.txt"}]))
        output.write_text('after')
        with self.assertRaises(ContextError) as e:collect(self.store,self.workspace,"a","demo",self.repo)
        self.assertEqual("stale",e.exception.result["status"])

    def test_manifest_cannot_read_outside_task_repository(self):
        outside=self.workspace/"outside.txt";outside.write_text("private")
        atomic(self.workspace/".context/artifacts.json",dump([{"ref":"artifact:outside","path":"outside.txt"}]))
        with self.assertRaises(ContextError) as e:collect(self.store,self.workspace,"a","demo",self.repo)
        self.assertEqual("denied",e.exception.result["status"])
