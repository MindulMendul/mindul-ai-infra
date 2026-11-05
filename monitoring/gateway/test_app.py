import json
import os
import unittest
from aiohttp import ClientSession, web
import app as gateway


class PrivacyTests(unittest.TestCase):
    def test_rum_allowlist_removes_game_content_and_invitation_urls(self):
        data=gateway.sanitize({
            'meta':{'user':{'id':'private-player'},'page':{'url':'https://example/?room=SECRET'},'app':{'version':'test'}},
            'logs':[{'message':'secret role'}],
            'events':[{'name':'api_request','attributes':{'route':'/api/rooms/join','status':500,'duration_ms':10,'body':'secret role','room':'SECRET'}}],
            'exceptions':[{'type':'TypeError','value':'private-player SECRET','stacktrace':{'frames':[{'filename':'https://example/app.js?room=SECRET','lineno':5}]}}],
            'measurements':[{'values':{'lcp':123,'nickname':'private-player'}}],
        },'qa')
        encoded=json.dumps(data)
        for value in ['SECRET','private-player','secret role','nickname']:
            self.assertNotIn(value,encoded)
        self.assertEqual(data['events'][0]['attributes']['route'],'/api/rooms/join')
        self.assertEqual(data['measurements'][0]['values'],{'lcp':123})

    def test_traces_remove_query_strings_headers_and_exception_messages(self):
        data=gateway.sanitize({'traces':{'resourceSpans':[{'resource':{'attributes':[{'key':'cookie','value':{'stringValue':'SECRET'}}]},'scopeSpans':[{'spans':[{'name':'GET /?room=SECRET','attributes':[{'key':'http.url','value':{'stringValue':'https://example/?room=SECRET'}},{'key':'request.body','value':{'stringValue':'SECRET'}}],'events':[{'name':'SECRET'}],'status':{'message':'SECRET'}}]}]}]}},'production')
        self.assertNotIn('SECRET',json.dumps(data))


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_streaming_and_upstream_errors(self):
        async def upstream(request):
            body=await request.json()
            if body.get('model')=='missing': return web.json_response({'error':'not found'},status=404)
            final={'model':'qwen3.5:4b','done':True,'message':{'content':'hello'},'prompt_eval_count':4,'eval_count':2,'eval_duration':1000000000}
            if body.get('stream',True):
                response=web.StreamResponse(headers={'Content-Type':'application/x-ndjson'})
                await response.prepare(request)
                # Split the final record across chunks; the proxy must reassemble it.
                content=(json.dumps(final)+'\n').encode()
                await response.write(content[:17]); await response.write(content[17:])
                await response.write_eof()
                return response
            return web.json_response(final)
        fake=web.Application()
        fake.router.add_post('/api/chat',upstream)
        fake.router.add_get('/api/ps',lambda _:web.json_response({'models':[]}))
        runner=web.AppRunner(fake)
        await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0)
        await site.start()
        os.environ['OLLAMA_URL']=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        proxy=web.AppRunner(gateway.app)
        await proxy.setup()
        proxy_site=web.TCPSite(proxy,'127.0.0.1',0)
        await proxy_site.start()
        base=f'http://127.0.0.1:{proxy_site._server.sockets[0].getsockname()[1]}'
        try:
            async with ClientSession() as session:
                async with session.options(base+'/api/chat',headers={'Origin':'https://example.org'}) as r:
                    self.assertEqual(r.status,204)
                    self.assertEqual(r.headers.get('Access-Control-Allow-Origin'),'*')
                async with session.post(base+'/api/chat',json=[]) as r:
                    self.assertEqual(r.status,400)
                for stream in [False,True]:
                    async with session.post(base+'/api/chat',json={'model':'qwen3.5:4b','stream':stream,'messages':[]}) as r:
                        self.assertEqual(r.status,200)
                        self.assertEqual(json.loads(await r.text())['message']['content'],'hello')
                async with session.post(base+'/api/chat',json={'model':'missing'}) as r:
                    self.assertEqual(r.status,404)
                async with session.post(base+'/rum/qa',json={}) as r:
                    self.assertEqual(r.status,403)
                async with session.get(base+'/metrics') as r:
                    metrics=await r.text()
                    self.assertIn('mindul_ai_tokens_total{direction="output",model="qwen3.5:4b"} 4.0',metrics)
                    self.assertIn('mindul_ai_requests_active 0.0',metrics)
                    self.assertIn('mindul_ai_last_generation_tokens_per_second{model="qwen3.5:4b"} 2.0',metrics)
        finally:
            await proxy.cleanup()
            await runner.cleanup()


if __name__=='__main__': unittest.main()
