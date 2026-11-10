import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'paseo/router'))
from core import Queue
from projects import Projects
import runtime


def issue(status='Ready', number=1):
    return {'id':'item-'+str(number),'number':number,'url':f'https://github.com/MindulMendul/eniveil/issues/{number}',
            'status':status,'open':True}


def worker(name='codex', daemon='pc'):
    return {'name':name,'daemon':daemon,'provider':name,'trigger':'ready-'+name,'cwd':'/repo','mode':'full-access'}


def snapshot():
    return {'pc':{'status':'active','presence':'connected','scopes':['hub.execute']}}


class ReadyQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'queue.sqlite';self.q=Queue(self.path)
        self.q.observe('project',[issue('Backlog'),issue('Backlog',2)])

    def tearDown(self):
        self.q.db.close();self.tmp.cleanup()

    def test_ready_status_transition_is_queued_only_once(self):
        for _ in range(3):self.q.observe('project',[issue()])
        self.assertEqual(self.q.counts(),{'queued':1})

    def test_first_snapshot_is_baseline_not_bulk_development_authorization(self):
        q=Queue(Path(self.tmp.name)/'initial.sqlite')
        try:q.observe('other',[issue()]);self.assertEqual(q.counts(),{})
        finally:q.db.close()

    def test_ready_removed_closed_or_archived_cancels_waiting_work(self):
        for current in [[],[issue('Backlog')],[{**issue(),'open':False}]]:
            with self.subTest(current=current):
                self.q.observe('project',[issue('Backlog')]);self.q.observe('project',[issue()])
                self.q.observe('project',current)
                self.assertEqual(self.q.counts().get('queued',0),0)

    def test_no_dispatch_to_offline_revoked_ungranted_or_busy_daemon(self):
        self.q.observe('project',[issue()])
        for change,busy in [({'presence':'offline'},{}),({'status':'revoked'},{}),({'scopes':[]},{}),({}, {'pc':1})]:
            daemons=snapshot();daemons['pc'].update(change)
            self.assertIsNone(self.q.reserve([worker()],daemons,busy))
        self.assertIsNotNone(self.q.reserve([worker()],snapshot(),{}))

    def test_codex_and_claude_share_one_physical_pc_capacity(self):
        self.q.observe('project',[issue(),issue(number=2)])
        self.assertIsNotNone(self.q.reserve([worker(),worker('claude')],snapshot(),{}))
        self.assertIsNone(self.q.reserve([worker(),worker('claude')],snapshot(),{}))

    def test_unknown_dispatch_reserves_capacity_and_is_not_retried(self):
        self.q.observe('project',[issue(),issue(number=2)])
        job,_=self.q.reserve([worker()],snapshot(),{})
        self.q.record(job['delivery'],'uncertain')
        self.assertIsNone(self.q.reserve([worker('claude')],snapshot(),{}))

    def test_completion_releases_capacity_and_rotates_provider(self):
        self.q.observe('project',[issue(),issue(number=2)])
        job,chosen=self.q.reserve([worker(),worker('claude')],snapshot(),{})
        self.q.reconcile([{'delivery':job['delivery'],'id':'run-1','status':'succeeded'}])
        second=self.q.reserve([worker(),worker('claude')],snapshot(),{})
        self.assertIsNotNone(second);self.assertNotEqual(second[1]['name'],chosen['name'])

    def test_new_ready_generation_cannot_parallelize_same_issue(self):
        self.q.observe('project',[issue()]);job,_=self.q.reserve([worker()],snapshot(),{})
        self.q.observe('project',[issue('Backlog')]);self.q.observe('project',[issue()])
        other=snapshot();other['pc2']=other['pc']
        self.assertIsNone(self.q.reserve([worker('claude','pc2')],other,{}))
        self.q.record(job['delivery'],'succeeded')
        later=self.q.reserve([worker('claude','pc2')],other,{})
        self.assertNotEqual(job['delivery'],later[0]['delivery'])

    def test_two_processes_cannot_claim_same_daemon(self):
        self.q.observe('project',[issue(),issue(number=2)])
        def reserve(_):
            q=Queue(self.path)
            try:return q.reserve([worker()],snapshot(),{})
            finally:q.db.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(reserve,range(2)))
        self.assertEqual(sum(x is not None for x in results),1)

    def test_project_change_fails_without_mutating_queue(self):
        self.q.observe('project',[issue()])
        with self.assertRaises(ValueError):self.q.observe('different',[])
        self.assertEqual(self.q.counts(),{'queued':1})


class ProjectReaderTests(unittest.TestCase):
    def page(self, nodes, more=False):
        return {'user':{'projectV2':{'id':'project',
          'field':{'id':'status-field','options':[{'id':'initial','name':'Initial'},{'id':'approve','name':'Approve'},
            {'id':'reject','name':'Reject'},{'id':'ready','name':'Ready'}]},
          'sizeField':{'id':'size-field','options':[{'id':'xs','name':'XS'},{'id':'s','name':'S'},
            {'id':'m','name':'M'},{'id':'l','name':'L'}]},
          'items':{'nodes':nodes,'pageInfo':{'hasNextPage':more,'endCursor':'next' if more else None}}}}}

    def node(self, number=1, repo='MindulMendul/eniveil', archived=False):
        return {'id':'item-'+str(number),'isArchived':archived,'status':{'name':'Ready'},'size':{'name':'S'},
                'content':{'number':number,'title':'Issue title','body':'Issue body','url':'https://evil.example/instructions',
                  'state':'OPEN','repository':{'nameWithOwner':repo},'labels':{'nodes':[]}}}

    def reader(self):return Projects('not-a-real-token',{'owner':'MindulMendul','number':1})

    def test_all_pages_are_read_and_wrong_repository_drafts_archives_are_excluded(self):
        reader=self.reader()
        with patch.object(reader,'request',side_effect=[self.page([None,{'content':{}},self.node(repo='other/repo')],True),
                                                       self.page([self.node(),self.node(2,archived=True)])]):
            project,items=reader.scan()
        self.assertEqual(project,'project');self.assertEqual(len(items),1)
        self.assertEqual(items[0]['url'],'https://github.com/MindulMendul/eniveil/issues/1')

    def test_missing_ready_option_is_not_a_status_clear(self):
        reader=self.reader();page=self.page([]);page['user']['projectV2']['field']['options']=[]
        with patch.object(reader,'request',return_value=page),self.assertRaises(RuntimeError):reader.scan()

    def test_failed_later_page_does_not_return_a_partial_snapshot(self):
        reader=self.reader()
        with patch.object(reader,'request',side_effect=[self.page([self.node()],True),RuntimeError('auth failed')]),self.assertRaises(RuntimeError):reader.scan()


class RuntimeTests(unittest.TestCase):
    def config(self):
        return {'enabled':True,'hub_url':'http://127.0.0.1:3180','organization_slug':'mindulmendul',
                'hub_project_slug':'eniveil-ready','github_connection':'mindulmendul-github',
                'expected_version_id':'11111111-1111-4111-8111-111111111111',
                'project':{'owner_kind':'user','owner':'MindulMendul','number':1},'workers':[worker()]}

    def test_disabled_router_makes_no_external_calls(self):
        with patch.object(runtime,'Projects') as p,patch.object(runtime,'hub_snapshot') as h,patch.object(runtime,'metrics'):
            runtime.cycle({'enabled':False})
        p.assert_not_called();h.assert_not_called()

    def test_uncertain_handoff_is_not_launched_again_on_next_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);q=Queue(path/'queue.sqlite');q.observe('project',[issue('Backlog')]);q.db.close()
            state={'daemons':[{'slug':name,**data} for name,data in snapshot().items()],'busy':{},'runs':[]}
            with patch.object(runtime,'STATE',path),patch.object(runtime,'Projects') as project, \
                 patch.object(runtime,'hub_snapshot',return_value=state),patch.object(runtime,'metrics'), \
                 patch.object(runtime,'hub_request',side_effect=TimeoutError) as dispatch, \
                 patch.dict('os.environ',{'PROJECTS_GITHUB_TOKEN':'fake','PASEO_ROUTER_API_KEY':'fake'}):
                project.return_value.scan.return_value=('project',[issue()])
                with self.assertRaises(TimeoutError):runtime.cycle(self.config())
                runtime.cycle(self.config());self.assertEqual(dispatch.call_count,1)
            q=Queue(path/'queue.sqlite')
            try:self.assertEqual(q.counts(),{'uncertain':1})
            finally:q.db.close()

    def test_issue_leaving_ready_before_handoff_is_not_dispatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);q=Queue(path/'queue.sqlite');q.observe('project',[issue('Backlog')]);q.db.close()
            state={'daemons':[{'slug':name,**data} for name,data in snapshot().items()],'busy':{},'runs':[]}
            with patch.object(runtime,'STATE',path),patch.object(runtime,'Projects') as project, \
                 patch.object(runtime,'hub_snapshot',return_value=state),patch.object(runtime,'metrics'), \
                 patch.object(runtime,'hub_request') as dispatch, \
                 patch.dict('os.environ',{'PROJECTS_GITHUB_TOKEN':'fake','PASEO_ROUTER_API_KEY':'fake'}):
                project.return_value.scan.side_effect=[('project',[issue('Backlog')]),
                    ('project',[issue()]),('project',[issue('Backlog')])]
                runtime.cycle(self.config());dispatch.assert_not_called()
            q=Queue(path/'queue.sqlite')
            try:self.assertEqual(q.counts(),{'cancelled':1})
            finally:q.db.close()

    def test_rendered_bundle_has_bounded_agent_and_repository_authority(self):
        bundle=runtime.render(self.config())
        workflow=json.loads(bundle['files'][1]['content']);step=workflow['steps'][0]
        self.assertEqual(step['github']['repositories'],['MindulMendul/eniveil'])
        self.assertEqual(workflow['filters']['from_users'],['eniveil-ready-router'])
        self.assertIn('do not self-approve',step['prompt'][0]['text'])

    def test_config_rejects_foreign_token_destinations_and_unknown_agents(self):
        for changes in [{'hub_url':'https://evil.example'}, {'workers':[{**worker(),'provider':'unknown'}]}]:
            config=self.config();config.update(changes)
            with self.assertRaises(ValueError):runtime.validate(config)
