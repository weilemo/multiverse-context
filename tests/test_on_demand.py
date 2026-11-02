"""Behavioral acceptance for explicit task selection and isolated read progress."""
import unittest
from multiverse_context.core import ContextError, atomic, dump, read_json
from multiverse_context.live import LiveContext
import test_live


class API(test_live.FakeForgejo):
    def __init__(self): self.calls=[];self.notices=[];self.large=False
    def sync_issue(self,store,repo,number):
        self.calls.append((repo,number))
        if number == 404:raise ContextError('missing','Issue not found')
        text=('must preserve this requirement\n'*4000) if self.large else f'{repo} #{number}: original requirements'
        source=store.ingest(f'forgejo:{repo}/issues/{number}',text,{'kind':'issue','repository':repo,'number':number},'v1')
        return {'issue':{'title':f'Task {number}'},'sources':[source]}
    def notify(self,*args):self.notices.append(args);return {'status':'published'}


class OnDemandTests(unittest.TestCase):
    def setUp(self):
        fixture=test_live.LiveTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.agent=fixture.agent('fresh')
        atomic(self.agent.run/'research-context.json',dump({'store':str(fixture.root/'shared')}))
        self.api=API()
        self.agent=LiveContext(self.agent.run,api=self.api,publish_remote=False)
    def select(self,issue=47,repository='team/demo'):
        return self.agent.execute('context_select',{'repository':repository,'issue':issue})
    def publish_result(self,n):
        source=self.agent.store.ingest(f'artifact:team/demo/result-{n}',str(n),{'kind':'artifact','repository':'team/demo'},str(n))
        return self.agent.execute('context_publish',{'proposal':{'ref':f'experiment:{self.agent.issue}-{n}',
            'kind':'experiment','title':'Measured result','description':'One observation','sections':{'result':str(n)},'sources':[source]}})
    def test_task_free_config_requires_explicit_selection_and_no_startup_alias(self):
        self.assertIsNone(self.agent.task)
        self.assertEqual([],self.api.calls)
        for name,args in [('context_task',{}),('context_discover',{}),('context_open',{'ref':'issue:1'}),
                          ('context_changes',{}),('context_import_file',{'path':'result.txt'}),('context_publish',{'proposal':{}})]:
            with self.assertRaises(ContextError) as e:self.agent.execute(name,args)
            self.assertEqual('task_required',e.exception.result['status'])
        self.assertEqual([],self.api.calls)
        with self.assertRaises(ContextError):self.agent.execute('context_startup',{})
        chosen=self.select()
        self.assertEqual('issue:team/demo#47',chosen['task'])
        self.assertNotIn('requirements',chosen)
        self.assertNotIn('summary',chosen)
        self.assertFalse((self.agent.state_dir/'context-read-snapshot.json').exists())
        raw=self.agent.execute('context_open',{'ref':chosen['issue_source'],'view':'raw'})
        self.assertEqual('team/demo #47: original requirements',raw['content'])
        self.assertEqual([],self.api.notices) # selection does not claim/comment
    def test_large_original_can_be_selected_and_read_without_pack(self):
        self.api.large=True
        chosen=self.select()
        result=self.agent.execute('context_open',{'ref':chosen['issue_source'],'view':'raw','limit':4000})
        self.assertEqual('truncated',result['status'])
        self.assertLessEqual(len(dump(result).encode()),4000)
        self.assertTrue(result['content'])
        with self.assertRaises(ContextError) as e:self.agent.execute('context_task',{})
        self.assertEqual('needs_budget',e.exception.result['status'])
        self.assertEqual('ok',self.agent.execute('context_discover',{})['status'])
    def test_selection_persists_across_rpc_and_is_per_run(self):
        chosen=self.select()
        again=LiveContext(self.agent.run,api=self.api,publish_remote=False)
        self.assertEqual(chosen['task'],again.execute('context_discover',{})['items'][0]['ref'])
        self.assertEqual(chosen['task'],read_json(self.agent.run/'context-active-task.json')['task'])
    def test_failed_or_unassigned_selection_preserves_current_task(self):
        self.select();original=read_json(self.agent.run/'context-active-task.json')
        count=len(self.api.calls)
        for args in ({'repository':'team/forbidden','issue':47},{'repository':'team/demo','issue':True},{'repository':'team/demo','issue':0}):
            with self.assertRaises(ContextError):self.agent.execute('context_select',args)
        self.assertEqual(count,len(self.api.calls))
        with self.assertRaises(ContextError):self.select(404)
        self.assertEqual(original,read_json(self.agent.run/'context-active-task.json'))
        self.agent.execute('context_discover',{})
        self.assertEqual(47,self.agent.issue)
    def test_switching_preserves_pending_pages_and_does_not_mix_baselines(self):
        self.select(47);self.agent.execute('context_changes',{})
        for n in range(20):self.publish_result(n)
        first=self.agent.execute('context_changes',{'limit':2000})
        self.assertTrue(first['next_cursor'])
        state47=self.agent.state_dir
        self.select(52)
        self.assertNotEqual(state47,self.agent.state_dir)
        with self.assertRaises(ContextError):self.agent.execute('context_changes',{'cursor':first['next_cursor']})
        with self.assertRaises(ContextError):self.agent.execute('context_open',{'ref':'experiment:47-0'})
        second=self.agent.execute('context_changes',{})
        self.assertFalse(any(r['ref'].startswith('experiment:47-') for r in second['changes']))
        self.publish_result(100)
        self.select(47)
        self.assertEqual(state47,self.agent.state_dir)
        self.assertEqual(first,self.agent.execute('context_changes',{'limit':2000}))
        seen=list(first['changes']);page=first
        while page['next_cursor']:
            page=self.agent.execute('context_changes',{'cursor':page['next_cursor'],'limit':2000})
            seen.extend(page['changes'])
        self.assertEqual(40,len(seen))
        self.assertEqual(40,len({(r['type'],r['ref']) for r in seen}))
        self.assertEqual([],self.agent.execute('context_changes',{})['changes'])
        self.select(52)
        self.assertIn('experiment:52-100',{r['ref'] for r in self.agent.execute('context_changes',{})['changes']})
    def test_existing_custom_task_binding_is_reused_and_legacy_config_never_selects(self):
        config={'store':str(self.agent.store.root),'repository':'team/demo','issue':1,'task':'issue:1'}
        atomic(self.agent.run/'research-context.json',dump(config))
        old=LiveContext(self.agent.run,api=self.api,publish_remote=False)
        with self.assertRaises(ContextError) as e:old.execute('context_discover',{})
        self.assertEqual('task_required',e.exception.result['status'])
        chosen=old.execute('context_select',{'repository':'team/demo','issue':1})
        self.assertEqual('issue:1',chosen['task'])
        self.assertEqual(1,len([p for p in (old.store.root/'tasks').glob('*.json') if read_json(p)['issue_source']=='forgejo:team/demo/issues/1']))
    def test_two_assigned_repositories_with_same_issue_are_distinct(self):
        forgejo=read_json(self.agent.run/'forgejo.json')
        forgejo['repositories'].append({'full_name':'team/other','directory':'other'})
        (self.agent.workspace/'other').mkdir()
        atomic(self.agent.run/'forgejo.json',dump(forgejo))
        self.agent=LiveContext(self.agent.run,api=self.api,publish_remote=False)
        one=self.select(47);state1=self.agent.state_dir
        two=self.select(47,'team/other')
        self.assertNotEqual(one['task'],two['task'])
        self.assertNotEqual(state1,self.agent.state_dir)
        (self.agent.repo/'result.txt').write_text('other result')
        source=self.agent.execute('context_import_file',{'path':'result.txt'})['source']
        self.assertEqual('team/other',source['origin']['repository'])
        with self.assertRaises(ContextError):self.agent.execute('context_open',{'ref':one['issue_source'],'view':'raw'})

    def test_task_identity_collision_fails_without_rebinding_or_switching(self):
        chosen=self.select(47)
        source=self.agent.store.source(chosen['issue_source'])
        self.agent.store.submit({'ref':'issue:team/demo#52','kind':'experiment','task':chosen['task'],
            'title':'Existing collision','description':'Legacy invalid root','sections':{'result':'From task 47'},'sources':[source]},'legacy')
        with self.assertRaises(ContextError) as e:self.select(52)
        self.assertEqual('conflict',e.exception.result['status'])
        self.assertEqual(47,read_json(self.agent.run/'context-active-task.json')['issue'])
        self.assertFalse(any(read_json(p)['task']=='issue:team/demo#52' for p in (self.agent.store.root/'tasks').glob('*.json')))
        obj=self.agent.store.get('issue:team/demo#52')
        self.agent.store.submit(dict(obj,kind='issue',task='issue:team/demo#52',expected_revision=obj['revision']),'legacy')
        with self.assertRaises(ContextError):self.select(52)
        self.assertFalse(any(read_json(p)['task']=='issue:team/demo#52' for p in (self.agent.store.root/'tasks').glob('*.json')))

    def test_publish_cannot_reserve_another_issue_root(self):
        chosen=self.select(47)
        source=self.agent.store.source(chosen['issue_source'])
        with self.assertRaises(ContextError) as e:self.agent.execute('context_publish',{'proposal':{
            'ref':'issue:team/demo#52','kind':'experiment','title':'Invalid root','description':'Wrong identity',
            'sections':{'result':'47 result'},'sources':[source]}})
        self.assertEqual('denied',e.exception.result['status'])

if __name__=='__main__':unittest.main()
