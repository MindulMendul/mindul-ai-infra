import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'paseo/router'))
import cloud
import triage
import callback_server
from core import Queue


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.worker={'provider':'codex-cloud','name':'codex','resource':'codex','environment_id':'env_123','binary':'/bin/codex'}
        self.job={'delivery':'job1','url':'https://github.com/MindulMendul/eniveil/issues/1'}

    @patch('cloud.subprocess.run')
    def test_native_cloud_launch_not_local_execution(self,run):
        run.return_value=SimpleNamespace(returncode=0,stdout='',stderr='https://chatgpt.com/codex/tasks/task_123\n')
        self.assertEqual(cloud.submit(self.worker,self.job,'scoped')['id'],'task_123')
        args=run.call_args.args[0]
        self.assertEqual(args[:9],['/bin/codex','cloud','exec','--env','env_123','--attempts','1','--branch','dev'])
        self.assertIn(self.job['url'],args[-1])

    @patch('cloud.subprocess.run')
    def test_api_key_login_is_not_subscription_cloud_auth(self,run):
        run.return_value=SimpleNamespace(returncode=0,stdout='Logged in using an API key',stderr='')
        self.assertFalse(cloud.available(self.worker))

    def test_reject_flag_environment_and_foreign_issue(self):
        with self.assertRaises(ValueError):cloud.validate_worker(dict(self.worker,environment_id='--other'))
        with self.assertRaises(ValueError):cloud.prompt(dict(self.job,url='https://github.com/other/repo/issues/1'),'token')

    def test_claude_rejects_foreign_endpoint(self):
        with self.assertRaises(ValueError):cloud.validate_worker({'provider':'claude-cloud','routine_url':'https://evil.example/fire'})

    @patch('triage._chat')
    def test_triage_rejects_untrusted_or_invalid_model_output(self, chat):
        chat.return_value='{"decision":"backlog"}'
        self.assertEqual(triage.classify('http://ollama','x','y'),'backlog')
        chat.return_value='{"decision":"run_shell"}'
        with self.assertRaises(RuntimeError):triage.classify('http://ollama','x','y')

    @patch('triage._chat', return_value='{"title":"정리된 제목","body":"완료 조건"}')
    def test_triage_writer_has_fixed_models_and_schema(self, chat):
        self.assertEqual(triage.rewrite('http://ollama','x','y')['title'],'정리된 제목')
        self.assertEqual(chat.call_args.args[1], 'qwen3.5:9b')

    @patch.dict('os.environ',{'PASEO_CLAUDE_ROUTINE_TOKEN':'routine-secret'})
    @patch('cloud.urllib.request.build_opener')
    def test_claude_scoped_trigger(self,opener):
        import io
        opener.return_value.open.return_value.__enter__.return_value=io.StringIO(json.dumps({'claude_code_session_id':'session1','claude_code_session_url':'https://claude.ai/code/session1'}))
        result=cloud.submit({'provider':'claude-cloud','routine_url':'https://api.anthropic.com/v1/claude_code/routines/trig_123/fire'},self.job,'completion-secret')
        self.assertEqual(result['id'],'session1')
        request=opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'),'Bearer routine-secret')
        self.assertIn('completion-secret',json.loads(request.data)['text'])
        self.assertEqual(request.get_method(),'POST')


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'queue.sqlite';self.q=Queue(self.path)
        item={'id':'item1','number':1,'url':'https://github.com/MindulMendul/eniveil/issues/1','status':'Backlog','open':True}
        self.q.observe('board',[item]);item['status']='Ready';self.q.observe('board',[item])
        job,_=self.q.reserve_available([{'name':'codex','resource':'codex'}]);self.delivery=job['delivery']
        self.token=self.q.completion_token(self.delivery)

    def tearDown(self):
        self.q.db.close();self.tmp.cleanup()

    def test_hash_only_invalid_capability_expiry(self):
        row=self.q.db.execute('SELECT * FROM completions').fetchone()
        self.assertEqual(row['token_hash'],hashlib.sha256(self.token.encode()).hexdigest())
        self.assertFalse(self.q.complete(self.delivery,'wrong','failed'))
        self.q.db.execute('UPDATE completions SET expires=0')
        self.assertFalse(self.q.complete(self.delivery,self.token,'failed'))
        self.assertEqual(self.q.counts(),{'dispatching':1})

    def test_callback_before_launch_response_stays_terminal_and_replays(self):
        url='https://github.com/MindulMendul/eniveil/pull/2'
        self.assertTrue(self.q.complete(self.delivery,self.token,'succeeded',url))
        self.q.cloud_ticket(self.delivery,'codex-cloud',{'id':'task1','url':'https://chatgpt.com/codex/tasks/task1'})
        self.assertEqual(self.q.counts(),{'succeeded':1})
        self.assertTrue(self.q.complete(self.delivery,self.token,'succeeded',url))
        self.assertFalse(self.q.complete(self.delivery,self.token,'failed'))

    def test_success_requires_scoped_pr(self):
        for url in (None,'https://github.com/other/repo/pull/1'):
            with self.assertRaises(ValueError):self.q.complete(self.delivery,self.token,'succeeded',url)

    def test_http_callback_authentication_and_completion(self):
        with patch.object(callback_server,'STATE',Path(self.tmp.name)):
            server=callback_server.ThreadingHTTPServer(('127.0.0.1',0),callback_server.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                url=f'http://127.0.0.1:{server.server_port}/automation/completion'
                data=json.dumps({'deliveryKey':self.delivery,'status':'failed'}).encode()
                with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(urllib.request.Request(url,data=data))
                self.assertEqual(error.exception.code,404)
                request=urllib.request.Request(url,data=data,headers={'Authorization':'Bearer '+self.token})
                with urllib.request.urlopen(request) as response:self.assertEqual(response.status,200)
                self.assertEqual(self.q.counts(),{'failed':1})
            finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
