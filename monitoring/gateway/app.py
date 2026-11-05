"""Public telemetry ingestion and transparent Ollama chat metering.

Prompts, answers, cookies, room codes and user identities are never persisted.
Metrics endpoints are reachable only on the Docker network.
"""
import asyncio
import json
import math
import os
import re
import time
from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, web
from prometheus_client import Counter, Gauge, Histogram, CONTENT_TYPE_LATEST, generate_latest

AI_REQUESTS = Counter('mindul_ai_requests_total', 'Completed chat requests', ['model', 'status'])
AI_TOKENS = Counter('mindul_ai_tokens_total', 'Tokens from completed Ollama responses', ['model', 'direction'])
AI_LATENCY = Histogram('mindul_ai_request_seconds', 'End-to-end chat time including queue and loading', ['model'], buckets=(.5, 1, 2, 5, 10, 30, 60, 120, 300, 600))
AI_GENERATION = Counter('mindul_ai_generation_seconds_total', 'Ollama generation seconds', ['model'])
AI_LOAD = Counter('mindul_ai_load_seconds_total', 'Ollama model loading seconds', ['model'])
AI_ACTIVE = Gauge('mindul_ai_requests_active', 'Active chat requests')
AI_LAST_SPEED = Gauge('mindul_ai_last_generation_tokens_per_second', 'Last completed response output tokens divided by generation seconds', ['model'])
AI_LAST_TOTAL_SPEED = Gauge('mindul_ai_last_total_tokens_per_second', 'Last completed response output tokens divided by total Ollama time including loading and prompt processing', ['model'])
AI_LAST_COMPLETED = Gauge('mindul_ai_last_response_timestamp_seconds', 'Timestamp of the last completed Ollama response', ['model'])
MODEL_MEMORY = Gauge('mindul_ai_model_memory_bytes', 'Loaded model memory', ['model'])
MODEL_LOADED = Gauge('mindul_ai_model_loaded', 'Whether a model is loaded', ['model'])
RUM_BATCHES = Counter('mindul_rum_batches_total', 'Accepted telemetry batches', ['environment'])
RUM_EVENTS = Counter('mindul_rum_events_total', 'Browser events', ['environment', 'event'])
RUM_ERRORS = Counter('mindul_rum_errors_total', 'Browser exceptions', ['environment', 'type'])
RUM_VITALS = Histogram('mindul_rum_web_vital', 'Browser web vitals: milliseconds except CLS', ['environment', 'metric'], buckets=(.01, .1, .25, .5, 1, 10, 50, 100, 200, 500, 1000, 1800, 2500, 4000, 10000))
RUM_API = Histogram('mindul_rum_api_seconds', 'Browser-observed API latency', ['environment', 'route', 'status'], buckets=(.01,.025,.05,.1,.25,.5,1,2,5,10,30))
COLLECTOR_ERRORS = Counter('mindul_rum_forward_errors_total', 'Failed Alloy forwards', ['environment'])
ALERT_NOTIFICATIONS = Counter('mindul_alert_notifications_total', 'Internal Grafana alert notification batches', ['status'])

ORIGINS = {
    'production': 'https://mindul-murder-mystery.duckdns.org',
    'qa': 'https://qa-mindul-murder-mystery.duckdns.org',
}
EVENTS = {'session_start', 'view_changed', 'page_view', 'ws_open', 'ws_close', 'ws_error', 'ws_reconnect', 'ws_command_error', 'ws_command_timeout', 'api_request', 'room_join_failed', 'room_create_failed', 'game_start_failed'}
ROUTES = {'/api/guest','/api/state','/api/rooms','/api/rooms/join','/api/health'}
EVENT_FIELDS = {'route','status','duration_ms','code','command','attempt'}
TRACE_FIELDS = {'http.method','http.status_code','http.response.status_code','url.full','http.url','url.path','server.address','net.peer.name','service.name','service.version','deployment.environment','deployment.environment.name','session.id','faro.session.id','telemetry.sdk.name','telemetry.sdk.version','telemetry.sdk.language','exception.type'}
ERROR_TYPES = {'Error','TypeError','ReferenceError','SyntaxError','RangeError','URIError','EvalError','AggregateError','UnhandledRejection','DOMException'}
limits = defaultdict(lambda: [20.0, time.monotonic()])
for environment in ORIGINS:
    for event in EVENTS: RUM_EVENTS.labels(environment,event).inc(0)
    for kind in ERROR_TYPES: RUM_ERRORS.labels(environment,kind).inc(0)
    for metric in ('lcp','inp','cls','fcp','ttfb'): RUM_VITALS.labels(environment,metric)
    RUM_BATCHES.labels(environment).inc(0)
    COLLECTOR_ERRORS.labels(environment).inc(0)
for model in ('qwen3.5:4b','qwen3:4b','qwen3:4b-instruct'):
    AI_REQUESTS.labels(model,'200').inc(0)
    for direction in ('input','output'): AI_TOKENS.labels(model,direction).inc(0)
    AI_GENERATION.labels(model).inc(0)
    AI_LOAD.labels(model).inc(0)


def safe_url(value):
    try:
        u = urlsplit(str(value))
        return urlunsplit((u.scheme, u.netloc, u.path, '', ''))[:512]
    except ValueError:
        return ''


def sanitize(payload, env):
    """Build a bounded allowlisted Faro envelope, independent of browser claims."""
    meta = payload.get('meta') or {}
    result = {'meta': {
        'app': {'name': 'murder-parker-web', 'environment': env, 'version': str(meta.get('app', {}).get('version', 'unknown'))[:64]},
        'page': {'url': ORIGINS[env] + '/'},
        'browser': {k: v for k, v in meta.get('browser', {}).items() if k in {'name','version','os','mobile','language','viewportWidth','viewportHeight'}},
    }}
    session_id = str(meta.get('session', {}).get('id', ''))
    if re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', session_id):
        result['meta']['session'] = {'id': session_id}
    events = []
    for event in payload.get('events', [])[:100]:
        name = event.get('name')
        if name not in EVENTS:
            continue
        attrs = {k: v for k,v in (event.get('attributes') or {}).items() if k in EVENT_FIELDS and isinstance(v,(str,int,float,bool))}
        if attrs.get('route') not in ROUTES: attrs.pop('route', None)
        if attrs.get('command') not in {'start','advance','vote','return','leave'}: attrs.pop('command', None)
        attrs = {k: str(v)[:64] for k,v in attrs.items()}
        events.append({'name':name, 'timestamp':event.get('timestamp'), 'attributes':attrs})
    result['events'] = events
    measurements = []
    for measurement in payload.get('measurements', [])[:100]:
        values = {str(k).lower():v for k,v in measurement.get('values',{}).items() if str(k).lower() in {'lcp','inp','cls','fcp','ttfb','fid'} and isinstance(v,(int,float)) and math.isfinite(v) and 0 <= v < 3600000}
        if values:
            measurements.append({'type': 'web-vitals', 'values':values, 'timestamp':measurement.get('timestamp')})
    result['measurements'] = measurements
    exceptions = []
    for exc in payload.get('exceptions',[])[:50]:
        kind = str(exc.get('type', 'Error'))
        kind = kind if kind in ERROR_TYPES else 'Error'
        frames = []
        for frame in (exc.get('stacktrace') or {}).get('frames',[])[:30]:
            frames.append({k:(safe_url(v) if k=='filename' else v) for k,v in frame.items() if k in {'filename','lineno','colno','function'} and isinstance(v,(str,int))})
        exceptions.append({'type':kind, 'value':kind, 'timestamp':exc.get('timestamp'), 'stacktrace':{'frames':frames}})
    result['exceptions'] = exceptions
    if isinstance(payload.get('traces'),dict):
        traces = json.loads(json.dumps(payload['traces']))
        traces = {'resourceSpans':traces.get('resourceSpans',[])[:100]}
        for resource in traces['resourceSpans']:
            resource['resource'] = {'attributes': [a for a in resource.get('resource',{}).get('attributes',[]) if a.get('key') in TRACE_FIELDS]}
            for key,value in [('service.name','murder-parker-web'),('deployment.environment',env)]:
                resource['resource']['attributes']=[a for a in resource['resource']['attributes'] if a.get('key')!=key]
                resource['resource']['attributes'].append({'key':key,'value':{'stringValue':value}})
            for scope in resource.get('scopeSpans',resource.get('instrumentationLibrarySpans',[])):
                for span in scope.get('spans',[]):
                    span['name'] = str(span.get('name','HTTP')).split('?')[0][:128]
                    attrs=[]
                    for attr in span.get('attributes',[]):
                        if attr.get('key') not in TRACE_FIELDS: continue
                        if attr.get('key') in {'http.url','url.full'}:
                            attr['value'] = {'stringValue':safe_url(attr.get('value',{}).get('stringValue',''))}
                        attrs.append(attr)
                    span['attributes']=attrs
                    span.pop('events',None)
                    span.pop('links',None)
                    if 'status' in span: span['status'].pop('message',None)
        result['traces']=traces
    return result


async def rum(request):
    env=request.match_info['env']
    if env not in ORIGINS: raise web.HTTPNotFound()
    if request.headers.get('Origin') != ORIGINS[env]: raise web.HTTPForbidden()
    if request.content_length and request.content_length > 262144: raise web.HTTPRequestEntityTooLarge(max_size=262144, actual_size=request.content_length)
    bucket=limits[env]
    now=time.monotonic()
    bucket[0]=min(20,bucket[0]+(now-bucket[1])*10)
    bucket[1]=now
    if bucket[0]<1: raise web.HTTPTooManyRequests(headers={'Retry-After':'1'})
    bucket[0]-=1
    try:
        payload=await request.json()
        if not isinstance(payload,dict): raise ValueError()
        data=sanitize(payload,env)
    except (ValueError,TypeError,AttributeError,KeyError):
        raise web.HTTPBadRequest(text='Invalid telemetry')
    port=12347 if env=='production' else 12348
    try:
        async with request.app['http'].post(f'http://alloy:{port}/collect',json=data,timeout=ClientTimeout(total=5)) as response:
            if response.status>=300: raise RuntimeError('Collector rejected batch')
    except Exception:
        COLLECTOR_ERRORS.labels(env).inc()
        raise web.HTTPServiceUnavailable(text='Collector unavailable')
    RUM_BATCHES.labels(env).inc()
    for event in data['events']:
        RUM_EVENTS.labels(env,event['name']).inc()
        if event['name']=='api_request':
            attrs=event['attributes']
            try:
                duration=float(attrs.get('duration_ms',0))/1000
                status=str(int(attrs.get('status',0)))
                route=attrs.get('route','other')
                if 0<=duration<3600 and 0<=int(status)<=599:
                    RUM_API.labels(env,route,status).observe(duration)
            except (ValueError,TypeError): pass
    for measurement in data['measurements']:
        for name,value in measurement['values'].items(): RUM_VITALS.labels(env,name).observe(value)
    for exc in data['exceptions']: RUM_ERRORS.labels(env,exc['type']).inc()
    return web.Response(status=202)


def record_final(model, final):
    for key,direction in [('prompt_eval_count','input'),('eval_count','output')]:
        AI_TOKENS.labels(model,direction).inc(max(0,int(final.get(key,0))))
    AI_GENERATION.labels(model).inc(max(0,float(final.get('eval_duration',0)))/1e9)
    AI_LOAD.labels(model).inc(max(0,float(final.get('load_duration',0)))/1e9)
    output=max(0,int(final.get('eval_count',0)))
    generation=max(0,float(final.get('eval_duration',0)))/1e9
    total=max(0,float(final.get('total_duration',0)))/1e9
    if generation>0: AI_LAST_SPEED.labels(model).set(output/generation)
    if total>0: AI_LAST_TOTAL_SPEED.labels(model).set(output/total)
    AI_LAST_COMPLETED.labels(model).set(time.time())


async def chat(request):
    try: body=await request.json()
    except (ValueError,TypeError): raise web.HTTPBadRequest()
    if not isinstance(body,dict): raise web.HTTPBadRequest()
    # Bound metric cardinality: only installed model names become labels.
    model=body.get('model','')
    label=model if model in {'qwen3.5:4b','qwen3:4b','qwen3:4b-instruct'} else 'other'
    started=time.monotonic()
    status='upstream_error'
    AI_ACTIVE.inc()
    try:
        async with request.app['http'].post(os.getenv('OLLAMA_URL','http://ollama:11434')+'/api/chat',json=body) as upstream:
            status=str(upstream.status)
            if body.get('stream',True) and upstream.status==200:
                response=web.StreamResponse(status=upstream.status,headers={'Content-Type':upstream.headers.get('Content-Type','application/x-ndjson')})
                await response.prepare(request)
                pending=b''
                complete=False
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                    pending+=chunk
                    while b'\n' in pending:
                        line,pending=pending.split(b'\n',1)
                        try:
                            final=json.loads(line)
                            if final.get('done'):
                                record_final(label,final)
                                complete=True
                        except (ValueError,TypeError): pass
                    if len(pending)>1048576: pending=b''
                if pending:
                    try:
                        final=json.loads(pending)
                        if final.get('done'):
                            record_final(label,final)
                            complete=True
                    except (ValueError,TypeError): pass
                if not complete: status='incomplete'
                await response.write_eof()
                return response
            data=await upstream.read()
            if upstream.status==200:
                try: record_final(label,json.loads(data))
                except (ValueError,TypeError): status='invalid_response'
            return web.Response(status=upstream.status,body=data,headers={'Content-Type':upstream.headers.get('Content-Type','application/json')})
    except (ConnectionResetError,asyncio.CancelledError):
        status='cancelled'
        raise
    except Exception:
        status='upstream_error'
        raise web.HTTPBadGateway(text='AI upstream unavailable')
    finally:
        AI_ACTIVE.dec()
        AI_REQUESTS.labels(label,status).inc()
        AI_LATENCY.labels(label).observe(time.monotonic()-started)


async def poll_models(app):
    while True:
        try:
            async with app['http'].get(os.getenv('OLLAMA_URL','http://ollama:11434')+'/api/ps',timeout=ClientTimeout(total=5)) as response:
                data=await response.json()
                MODEL_MEMORY.clear()
                MODEL_LOADED.clear()
                MODEL_MEMORY.labels('qwen3.5:4b').set(0)
                MODEL_LOADED.labels('qwen3.5:4b').set(0)
                for model in data.get('models',[]):
                    name=model.get('name','unknown')[:64]
                    MODEL_MEMORY.labels(name).set(model.get('size',0))
                    MODEL_LOADED.labels(name).set(1)
        except Exception: pass
        await asyncio.sleep(15)


async def lifespan(app):
    app['http']=ClientSession(timeout=ClientTimeout(total=None,sock_connect=10,sock_read=600))
    poll=asyncio.create_task(poll_models(app))
    yield
    poll.cancel()
    await asyncio.gather(poll,return_exceptions=True)
    await app['http'].close()


app=web.Application(client_max_size=1048576)
app.cleanup_ctx.append(lifespan)
async def cors_headers(request,response):
    if request.path == '/api/chat':
        response.headers['Access-Control-Allow-Origin']='*'
        response.headers['Access-Control-Allow-Methods']='POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers']='Content-Type, Authorization, Traceparent'
async def preflight(_):
    return web.Response(status=204)
async def receive_alert(request):
    payload=await request.json()
    status=payload.get('status','other')
    if status not in {'firing','resolved'}:status='other'
    ALERT_NOTIFICATIONS.labels(status).inc()
    alerts=[]
    for alert in payload.get('alerts',[])[:100]:
        labels=alert.get('labels',{})
        alerts.append({key:str(labels.get(key,''))[:128] for key in ('alertname','severity','service','container')})
    print(json.dumps({'event':'grafana_alert','status':status,'alerts':alerts}),flush=True)
    return web.json_response({'accepted':True})
app.on_response_prepare.append(cors_headers)
app.router.add_options('/api/chat',preflight)
app.router.add_post('/internal/alerts',receive_alert)
app.router.add_get('/metrics',lambda _:web.Response(body=generate_latest(),headers={'Content-Type':CONTENT_TYPE_LATEST}))
app.router.add_get('/health',lambda _:web.json_response({'ok':True}))
app.router.add_post('/rum/{env}',rum)
app.router.add_post('/api/chat',chat)
if __name__=='__main__': web.run_app(app,host='0.0.0.0',port=8080,access_log=None)
