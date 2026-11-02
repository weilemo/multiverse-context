import json
from pathlib import Path
import tempfile
import unittest
from multiverse_context.core import ContextError, atomic, dump
from multiverse_context.live import LiveContext


class FakeForgejo:
    def sync_issue(self,store,repo,number):
        s=store.ingest('forgejo:'+repo+'/issues/'+str(number),'Compare methods; keep split fixed',{'kind':'issue','repository':repo,'number':number},'v1')
        return {'issue':{'title':'Research'},'sources':[s]}
    def notify(self,*a):return {'status':'published'}


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.api=FakeForgejo()
        self.a=self.agent('a');self.b=self.agent('b')
        for agent in (self.a,self.b):agent.execute('context_select',{'repository':'team/demo','issue':1})
    def agent(self,name,task='issue:1'):
        run=self.root/name/'run';run.mkdir(parents=True)
        workspace=self.root/name/'workspace';(workspace/'demo').mkdir(parents=True)
        atomic(run/'research-context.json',dump({'store':str(self.root/'shared'),'repository':'team/demo','issue':1,'task':task}))
        atomic(run/'plan.json',dump({'workspace':str(workspace),'agent':name,'run_id':name+'-run'}))
        atomic(run/'forgejo.json',dump({'url':'http://localhost','login':name,'repositories':[{'full_name':'team/demo','directory':'demo'}]}))
        return LiveContext(run,api=self.api,publish_remote=False)
    def proposal(self,source):
        return {'ref':'experiment:stage1','kind':'experiment','title':'Stage one','description':'Partial result for immediate review',
                'sections':{'observations':'score 0.5','limitations':'one run'},'sources':[source],
                'evidence':[{'source':source['ref'],'start':1,'end':1}]}
    def test_publish_visible_to_other_running_agent_without_finishing_either(self):
        self.a.execute('context_task',{});self.b.execute('context_task',{})
        (self.a.repo/'result.txt').write_text('score 0.5\n')
        source=self.a.execute('context_import_file',{'path':'result.txt'})['source']
        result=self.a.execute('context_publish',{'proposal':self.proposal(source)})
        self.assertEqual('published',result['status'])
        changes=self.b.execute('context_changes',{})
        self.assertTrue(any(x['ref']=='experiment:stage1' for x in changes['changes']))
        raw=self.b.execute('context_open',{'ref':'experiment:stage1','view':'raw'})
        self.assertEqual('score 0.5\n',raw['content'])
        self.assertFalse((self.a.state_dir/'context-result.json').exists())
        self.assertFalse((self.b.state_dir/'context-result.json').exists())
    def test_identity_and_task_are_bound_by_supervisor(self):
        self.a.execute('context_task',{})
        with self.assertRaises(ContextError):self.a.execute('context_discover',{'actor':'b'})
        with self.assertRaises(ContextError):self.a.execute('context_import_file',{'path':'../run/plan.json'})
        (self.a.repo/'link').symlink_to(self.b.run/'plan.json')
        with self.assertRaises(ContextError):self.a.execute('context_import_file',{'path':'link'})
    def test_stage_update_requires_current_object_revision(self):
        self.a.execute('context_task',{})
        (self.a.repo/'result.txt').write_text('score 0.5\n')
        source=self.a.execute('context_import_file',{'path':'result.txt'})['source']
        p=self.proposal(source);self.a.execute('context_publish',{'proposal':p})
        p['sections']={'observations':'score 0.6'}
        with self.assertRaises(ContextError) as e:self.b.execute('context_publish',{'proposal':p})
        self.assertEqual('conflict',e.exception.result['status'])
        self.assertTrue(list((self.b.state_dir/'context-pending').glob('*.json')))
    def test_source_replacement_requires_current_digest(self):
        self.a.execute('context_task',{})
        (self.a.repo/'result.txt').write_text('one')
        self.a.execute('context_import_file',{'path':'result.txt'})
        (self.b.repo/'result.txt').write_text('two')
        with self.assertRaises(ContextError) as e:self.b.execute('context_import_file',{'path':'result.txt'})
        self.assertEqual('conflict',e.exception.result['status'])

    def evidence(self, text='原始结果\n' * 12000):
        (self.a.repo/'result.txt').write_text(text)
        return self.a.execute('context_import_file',{'path':'result.txt'})['source']

    def save(self, source, path='evidence/peer.txt', **overrides):
        return self.b.execute('context_open',dict(ref=source['ref'],view='raw',save_to=path,
                                                   expected_digest=source['digest'],**overrides))

    def test_peer_source_saved_completely_without_model_copying_and_retry_is_safe(self):
        source=self.evidence()
        result=self.save(source)
        self.assertEqual((self.a.repo/'result.txt').read_bytes(),(self.b.repo/'evidence/peer.txt').read_bytes())
        self.assertEqual(source['digest'],result['saved']['digest'])
        self.assertEqual(str(self.b.repo/'evidence/peer.txt'),result['saved']['workspace_path'])
        self.assertNotIn('content',result)
        self.assertLess(len(dump(result).encode()),2000)
        self.assertEqual(result,self.save(source))

    def test_save_rejects_stale_source_and_preserves_existing_work(self):
        source=self.evidence('one')
        self.save(source)
        (self.a.repo/'result.txt').write_text('two')
        updated=self.a.execute('context_import_file',{'path':'result.txt','expected_digest':source['digest']})['source']
        for stale in [source,updated]:
            with self.assertRaises(ContextError) as error:self.save(stale)
            self.assertEqual('conflict',error.exception.result['status'])
        self.assertEqual('one',(self.b.repo/'evidence/peer.txt').read_text())

    def test_save_rejects_protected_paths_and_symlink_parents_or_leaf(self):
        source=self.evidence('one')
        (self.b.repo/'escape').symlink_to(self.a.repo,target_is_directory=True)
        (self.b.repo/'leaf').symlink_to(self.a.repo/'result.txt')
        for path in ['../escape.txt','/tmp/escape.txt','.git/config','x/.git/config','.context/config','escape/result.txt','leaf']:
            with self.subTest(path=path),self.assertRaises(ContextError):self.save(source,path)
        self.assertEqual('one',(self.a.repo/'result.txt').read_text())

    def test_save_requires_raw_complete_read_and_explicit_digest(self):
        source=self.evidence('one')
        for args in [dict(ref=source['ref'],save_to='peer.txt'),
                     dict(ref=source['ref'],view='raw',save_to='peer.txt'),
                     dict(ref=source['ref'],view='raw',save_to='peer.txt',expected_digest=source['digest'],limit=1000)]:
            with self.assertRaises(ContextError):self.b.execute('context_open',args)
        self.assertFalse((self.b.repo/'peer.txt').exists())

    def test_save_checks_scope_and_blob_integrity(self):
        source=self.evidence('one')
        foreign=self.a.store.ingest('artifact:foreign','secret',{'repository':'team/foreign','kind':'artifact'},'v1')
        with self.assertRaises(ContextError):self.save(foreign)
        (self.a.store.root/'blobs'/(source['digest']+'.txt')).write_text('modified')
        with self.assertRaises(ContextError):self.save(source)
        self.assertFalse((self.b.repo/'evidence/peer.txt').exists())
