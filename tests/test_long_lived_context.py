"""Regressions for the two independent acceptance blockers."""
import json
import unittest
from multiverse_context.core import ContextError, dump, read_json
import test_live


class IssueWithComments(test_live.FakeForgejo):
    def __init__(self): self.comments = []
    def sync_issue(self, store, repo, number):
        result = super().sync_issue(store,repo,number)
        for i,text in enumerate(self.comments):
            result['sources'].append(store.ingest(f'forgejo:{repo}/issues/{number}/comments/{i}',text,
                {'kind':'comment','repository':repo,'number':number},f'c{i}'))
        return result


class LongLivedTests(unittest.TestCase):
    def setUp(self):
        fixture=test_live.LiveTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.a,self.b=fixture.a,fixture.b
        self.api=IssueWithComments();self.a.api=self.b.api=self.api

    def add_results(self,start,stop):
        for n in range(start,stop):
            s=self.a.store.ingest(f'artifact:team/demo/results/{n}.txt',f'score {n}\n',
                {'kind':'artifact','repository':'team/demo','path':f'results/{n}.txt'},str(n))
            self.a.store.submit({'ref':f'experiment:{n}','kind':'experiment','task':self.a.task,
                'title':f'Experiment {n}','description':'Measured result','sections':{'result':f'score {n}'},'sources':[s]},'a')

    def test_issue_source_survives_summary_rewrite_and_new_comments(self):
        initial=self.a.execute('context_task',{})
        self.add_results(0,1)
        source=self.a.store.source('artifact:team/demo/results/0.txt')
        obj=self.a.store.get(self.a.task)
        self.a.execute('context_publish',{'proposal':{'ref':self.a.task,'kind':'issue','title':'Progress',
            'description':'Updated progress','sections':{'progress':'one result'},'sources':[source],
            'expected_revision':obj['revision']}})
        self.api.comments.append('Additional requirement: repeat twice')
        pack=self.b.execute('context_task',{})
        self.assertEqual(initial['requirements'],pack['requirements'])
        self.assertTrue(any(x['ref'].endswith('/comments/0') for x in pack['task']['freshness']['changes']))
        changed=self.a.execute('context_changes',{})
        self.assertTrue(any(x['ref'].endswith('/comments/0') for x in changed['changes']))
        self.assertIn('repeat twice',self.b.execute('context_open',{'ref':'forgejo:team/demo/issues/1/comments/0','view':'raw'})['content'])
        with self.assertRaises(ContextError):
            self.a.store.bind_task_issue(self.a.task,source['ref'])

    def test_old_damaged_issue_is_repaired_by_trusted_configuration(self):
        source=self.a.store.ingest('artifact:team/demo/result.txt','score',{'kind':'artifact','repository':'team/demo'},'v1')
        self.a.store.submit({'ref':self.a.task,'kind':'issue','title':'Old summary','description':'Missing original binding',
            'sections':{'progress':'score'},'sources':[source], 'expected_revision':self.a.store.get(self.a.task)['revision']},'old-agent')
        for binding in (self.a.store.root/'tasks').glob('*.json'):binding.unlink()
        pack=self.a.execute('context_task',{})
        self.assertEqual('Compare methods; keep split fixed',pack['requirements'][0]['original'])
        self.assertTrue(list((self.a.store.root/'tasks').glob('*.json')))
        other=self.a.store.ingest('forgejo:team/demo/issues/2','Other issue',{'kind':'issue','repository':'team/demo','number':2},'v1')
        with self.assertRaises(ContextError) as e:self.a.store.bind_task_issue(self.a.task,other['ref'])
        self.assertEqual('conflict',e.exception.result['status'])

    def test_200_and_1000_objects_start_with_bounded_context_and_expand_last(self):
        self.a.execute('context_task',{})
        obj=self.a.store.get(self.a.task)
        self.a.store.submit(dict(obj,expected_revision=obj['revision'],sections={'huge_history':'x'*40000}),'a')
        for lo,hi in ((0,200),(200,1000)):
            self.add_results(lo,hi)
            pack=self.b.execute('context_task',{})
            self.assertLessEqual(len(dump(pack).encode()),32000)
            self.assertNotIn('objects',pack['snapshot'])
            self.assertEqual(hi+1,pack['snapshot']['object_count'])
            self.assertTrue(pack['summary_omitted'])
            self.assertGreater(pack['omitted_cards'],0)
            self.assertEqual('Compare methods; keep split fixed',pack['requirements'][0]['original'])
            self.assertEqual(f'score {hi-1}\n',self.b.execute('context_open',{'ref':f'experiment:{hi-1}','view':'raw'})['content'])
            summary=self.b.execute('context_open',{'ref':self.a.task,'view':'summary','limit':8000})
            self.assertEqual('truncated',summary['status'])
            self.assertLessEqual(len(dump(summary).encode()),8000)
            self.assertTrue(summary['content'])
            following=self.b.execute('context_open',{'ref':self.a.task,'view':'summary','limit':8000,'cursor':summary['next_cursor']})
            self.assertTrue(following['content'])

    def test_change_pages_retry_and_new_arrivals_do_not_skip_or_advance_early(self):
        self.a.execute('context_task',{});self.b.execute('context_task',{})
        initial=read_json(self.a.state_dir/'context-read-snapshot.json')
        self.add_results(0,200)
        page=self.a.execute('context_changes',{'limit':4000})
        self.assertEqual('truncated',page['status'])
        self.assertEqual(initial,read_json(self.a.state_dir/'context-read-snapshot.json'))
        self.assertEqual(page,self.a.execute('context_changes',{'limit':4000}))
        self.add_results(200,201) # arrives after the delta snapshot was pinned
        with self.assertRaises(ContextError):
            self.b.execute('context_changes',{'cursor':page['next_cursor']})
        seen=list(page['changes']);last_cursor=None
        while page['next_cursor']:
            last_cursor=page['next_cursor']
            page=self.a.execute('context_changes',{'cursor':last_cursor,'limit':4000})
            self.assertLessEqual(len(dump(page).encode()),4000)
            seen.extend(page['changes'])
            self.assertEqual(page,self.a.execute('context_changes',{'cursor':last_cursor,'limit':4000}))
        self.assertEqual(400,len(seen))
        self.assertEqual(400,len({(r['type'],r['ref']) for r in seen}))
        self.assertNotEqual(initial,read_json(self.a.state_dir/'context-read-snapshot.json'))
        following=self.a.execute('context_changes',{})
        self.assertEqual({'experiment:200','artifact:team/demo/results/200.txt'},{r['ref'] for r in following['changes']})

    def test_first_startup_pages_all_comments_beyond_freshness_hints(self):
        self.api.comments = [f'Additional requirement {n}' for n in range(40)]
        pack = self.b.execute('context_task',{})
        self.assertGreater(pack['task']['freshness']['omitted_changes'],0)
        seen = list(pack['changes']); cursor = pack['changes_next_cursor']
        while cursor:
            page=self.b.execute('context_changes',{'cursor':cursor})
            seen.extend(page['changes']);cursor=page['next_cursor']
        self.assertEqual(40,len([r for r in seen if '/comments/' in r['ref']]))
        result=self.b.execute('context_open',{'ref':'forgejo:team/demo/issues/1/comments/39','view':'raw'})
        self.assertEqual('Additional requirement 39',result['content'])

    def test_original_requirements_still_fail_closed_when_they_alone_are_too_large(self):
        self.a.execute('context_task',{})
        # Direct store test avoids fake Forgejo restoring its short fixture.
        self.a.store.ingest('forgejo:team/demo/issues/1','mandatory\n'*10000,
            {'kind':'issue','repository':'team/demo','number':1},'large')
        with self.assertRaises(ContextError) as e:self.a.store.startup(self.a.task,limit=32000)
        self.assertEqual('needs_budget',e.exception.result['status'])


if __name__=='__main__':unittest.main()
