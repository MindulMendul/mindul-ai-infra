"""Opt-in live Docker/Caddy smoke test. Creates and removes only its own test app."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
CLI=ROOT/'scripts/mindul.py'


def execute(args, **kwargs):
    return subprocess.run([str(a) for a in args],check=True,**kwargs)


def main():
    if os.environ.get('MINDUL_RUN_INTEGRATION')!='1':
        raise SystemExit('Set MINDUL_RUN_INTEGRATION=1 to test against this live Docker/Caddy server.')
    name='deploy-smoke-'+str(int(time.time()))
    project='mindul-'+name+'-qa'
    app_dir=Path.home()/'.local/share/mindul-deploy/apps'/(name+'-qa')
    assert not app_dir.exists()
    domain=name+'-qa.'+os.environ.get('MINDUL_BASE_DOMAIN','mindul-server.duckdns.org')
    url='https://'+domain
    with tempfile.TemporaryDirectory(prefix='mindul-deploy-test-') as temporary:
        repo=Path(temporary)/'repository'
        repo.mkdir()
        execute(['git','init','-b','qa',repo])
        (repo/'Dockerfile').write_text('FROM python:3.12-slim\nWORKDIR /app\nCOPY server.py .\nRUN mkdir /data && chown 65534:65534 /data\nUSER 65534\nEXPOSE 8080\nCMD ["python","server.py"]\n')
        (repo/'mindul.json').write_text(json.dumps({'version':1,'health_path':'/health','volumes':{'data':'/data'}}))
        env_file=Path(temporary)/'app.env'
        env_file.write_text("PASSWORD='literal$PASSWORD'\n")
        server='''from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
import os
assert os.environ['PASSWORD']=='literal$PASSWORD'
VERSION = "VERSION_VALUE"
p=Path('/data/retained')
if not p.exists(): p.write_text('retained-data')
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  failed=VERSION=='broken' and self.path=='/health'
  self.send_response(500 if failed else 200)
  self.end_headers()
  self.wfile.write((VERSION+' '+p.read_text()).encode())
HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
'''
        def commit(version):
            (repo/'server.py').write_text(server.replace('VERSION_VALUE',version))
            execute(['git','-C',repo,'add','.'])
            execute(['git','-C',repo,'-c','user.name=Mindul test','-c','user.email=mindul-test@example.invalid','commit','-m',version])
        def cli(*args,**kwargs):return subprocess.run(['python3',str(CLI),*map(str,args)],**kwargs)
        def content():
            with urllib.request.urlopen(url,timeout=10) as response:return response.read().decode()
        try:
            commit('v1')
            execute(['python3',CLI,'deploy',repo,'--name',name,'-e','qa','--env-file',env_file,'--timeout','15','--https-timeout','60'])
            assert content()=='v1 retained-data'
            config=json.loads((app_dir/'releases'/json.loads((app_dir/'state.json').read_text())['history'][0]/'compose.json').read_text())
            assert 'ports' not in config['services']['web']
            print('Dockerfile → HTTPS / no published port / persistent volume: OK',flush=True)
            (repo/'compose.yaml').write_text('services:\n  web:\n    build: .\n    ports: ["8080:8080"]\n    volumes: ["data:/data"]\n    environment:\n      PASSWORD: ${PASSWORD}\nvolumes:\n  data:\n')
            commit('v2')
            execute(['python3',CLI,'redeploy',name,'-e','qa','--timeout','15','--https-timeout','15'])
            assert content()=='v2 retained-data'
            print('Compose detection / saved settings / preserved data: OK',flush=True)
            execute(['python3',CLI,'rollback',name,'-e','qa','--timeout','15'])
            assert content()=='v1 retained-data'
            print('Manual rollback: OK',flush=True)
            commit('broken')
            result=cli('redeploy',name,'-e','qa','--timeout','5','--https-timeout','5')
            assert result.returncode!=0
            assert content()=='v1 retained-data'
            print('Failed health check automatically restores previous app and data: OK',flush=True)
        finally:
            if (app_dir/'config.json').exists():
                cli('remove',name,'-e','qa',check=True)
            else:
                # Only this unique test project's resources are eligible for cleanup.
                for release in (app_dir/'releases').glob('*'):
                    compose=release/'compose.json'
                    if compose.exists():
                        subprocess.run(['docker','compose','-p',project,'-f',str(compose),'down'],check=False)
            route=ROOT/'deployments/caddy'/(project+'.caddy')
            assert not route.exists(),'Test route was not removed'
            for release in (app_dir/'releases').glob('*'):
                meta=json.loads((release/'release.json').read_text()) if (release/'release.json').exists() else {}
                for image in meta.get('images',[]):subprocess.run(['docker','image','rm',image],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            # This volume contains only data created by this test, never real app data.
            subprocess.run(['docker','volume','rm',project+'-data'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            shutil.rmtree(app_dir,ignore_errors=True)
    print('Live deployment integration test passed; test resources cleaned up.',flush=True)


if __name__=='__main__':main()
