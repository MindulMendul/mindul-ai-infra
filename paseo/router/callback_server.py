#!/usr/bin/env python3
"""Accept one scoped completion capability per cloud job; no global management API."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from core import Queue

STATE=Path(os.environ.get('PASEO_ROUTER_STATE_DIR','/data'))


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self,*args):
        pass

    def reply(self,status,body):
        payload=json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers();self.wfile.write(payload)

    def do_GET(self):
        self.reply(200 if self.path=='/health' else 404,{'ok':self.path=='/health'})

    def do_POST(self):
        if self.path!='/automation/completion':self.reply(404,{'ok':False});return
        if not self.headers.get('Authorization','').startswith('Bearer '):self.reply(404,{'ok':False});return
        queue=None
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<1 or length>8192:raise ValueError('Invalid body length')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict) or set(data)-{'deliveryKey','status','pr_url'}:raise ValueError('Invalid fields')
            delivery=data['deliveryKey'];token=self.headers['Authorization'][7:]
            if not isinstance(delivery,str) or len(delivery)>200 or len(token)>256:raise ValueError('Invalid capability')
            queue=Queue(STATE/'queue.sqlite')
            accepted=queue.complete(delivery,token,data['status'],data.get('pr_url'))
            self.reply(200 if accepted else 404,{'ok':accepted})
        except (ValueError,KeyError,TypeError,json.JSONDecodeError):
            self.reply(400,{'ok':False})
        except Exception:
            self.reply(503,{'ok':False})
        finally:
            if queue:queue.db.close()


if __name__=='__main__':
    STATE.mkdir(parents=True,exist_ok=True)
    Queue(STATE/'queue.sqlite').db.close()
    server=ThreadingHTTPServer(('0.0.0.0',3190),Handler)
    server.timeout=15
    server.serve_forever()
