#!/usr/bin/env python3
"""Infrastructure lifecycle, encrypted local backups and bounded maintenance."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import ssl
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

ROOT=Path(os.environ.get('MINDUL_INFRA_DIR',Path(__file__).resolve().parents[1])).resolve()
STATE=Path(os.environ.get('MINDUL_OPS_STATE',Path.home()/'.local/share/mindul-ops'))
BACKUPS=Path(os.environ.get('MINDUL_BACKUP_DIR',Path.home()/'.local/share/mindul-backups'))
KEY=Path(os.environ.get('MINDUL_BACKUP_KEY',Path.home()/'.config/mindul/backup.key'))
APPS=Path(os.environ.get('MINDUL_STATE_DIR',Path.home()/'.local/share/mindul-deploy'))
HELPER=ROOT/'ops/volume_snapshot.py'


def run(args,*,capture=False,check=True,**kwargs):
    result=subprocess.run([str(a) for a in args],text=True,stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None,**kwargs)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode})'+(': '+result.stderr.strip()[:1200] if capture else ''))
    return result.stdout.strip() if capture and result.returncode==0 else result


def read(path,default=None):return json.loads(path.read_text()) if path.exists() else default


def atomic(path,text,mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp');tmp.write_text(text);tmp.chmod(mode);tmp.replace(path)


def write(path,value):atomic(path,json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def config():return read(ROOT/'ops/config.json')


def initialize():
    for path in (STATE,BACKUPS,KEY.parent,APPS):path.mkdir(parents=True,exist_ok=True);path.chmod(0o700)
    metrics=STATE/'metrics';metrics.mkdir(exist_ok=True);metrics.chmod(0o755)
    if not KEY.exists():atomic(KEY,base64.urlsafe_b64encode(os.urandom(48)).decode()+'\n')
    for path,content in [(ROOT/'.env','CODE_SERVER_PASSWORD='+uuid.uuid4().hex+'\nOLLAMA_MODEL=qwen3.5:4b\n'),
                         (ROOT/'monitoring/.env','GRAFANA_ADMIN_PASSWORD='+uuid.uuid4().hex+'\n')]:
        if not path.exists():atomic(path,content)
        path.chmod(0o600)
    secrets=ROOT/'monitoring/secrets';secrets.mkdir(exist_ok=True);secrets.chmod(0o700)
    token=secrets/'web-metrics.token'
    if not token.exists():atomic(token,uuid.uuid4().hex+uuid.uuid4().hex+'\n',mode=0o644)
    if not (STATE/'status.json').exists():update_metrics()


@contextlib.contextmanager
def locked(apps=False):
    initialize()
    with contextlib.ExitStack() as stack:
        paths=[STATE/'.lock']+([APPS/'.lock'] if apps else [])
        for path in paths:
            handle=stack.enter_context(path.open('a'))
            fcntl.flock(handle,fcntl.LOCK_EX)
        yield


def update_metrics(**values):
    status=read(STATE/'status.json',{})
    status.update(values);write(STATE/'status.json',status)
    defaults={'backup_last_success':0,'backup_last_attempt_success':1,'backup_last_restore_check':0,
              'backup_bytes':0,'maintenance_last_success':0,'deployment_last_success':0}
    names={'backup_last_success':'mindul_backup_last_success_timestamp_seconds',
           'backup_last_attempt_success':'mindul_backup_last_attempt_success',
           'backup_last_restore_check':'mindul_backup_last_restore_check_timestamp_seconds',
           'backup_bytes':'mindul_backup_size_bytes',
           'maintenance_last_success':'mindul_maintenance_last_success_timestamp_seconds',
           'deployment_last_success':'mindul_infra_deploy_last_success_timestamp_seconds'}
    lines=[]
    for key,default in defaults.items():
        metric=names[key]
        lines.extend([f'# TYPE {metric} gauge',f'{metric} {status.get(key,default)}'])
    atomic(STATE/'metrics/operations.prom','\n'.join(lines)+'\n',0o644)


def base(*args,capture=False):
    return run(['docker','compose','--project-directory',ROOT,'-f',ROOT/'docker-compose.yml',*args],capture=capture)


def monitoring(*args,capture=False):
    return run(['docker','compose','--env-file',ROOT/'monitoring/.env','-f',ROOT/'monitoring/compose.yaml',*args],capture=capture)


def helper(command,volume,output,*,write_volume=False,exclude=(),budget=None,capture=False):
    target='/target' if write_volume else '/source'
    args=['docker','run','--rm','--network','none','--user','0',
          '-v',f'{volume}:{target}'+('' if write_volume else ':ro'),
          '-v',f'{output}:/snapshot'+(':ro' if write_volume else ''),
          '-v',f'{HELPER}:/tools/snapshot.py:ro',
          '-e',f'OUT_UID={os.getuid()}','-e',f'OUT_GID={os.getgid()}',
          '-e','EXCLUDE='+json.dumps(list(exclude)),
          '-e','MAX_BYTES='+str(config()['backup_max_plaintext_bytes'] if budget is None else budget),
          config()['helper_image'],'python','/tools/snapshot.py',command]
    return run(args,capture=capture)


def inspect_container(name):return json.loads(run(['docker','inspect',name],capture=True))[0]


def migrate_storage():
    tasks=[('mindul-kafka','kafka','mindul-ai-infra_kafka_data','/var/lib/kafka/data',None),
           ('mindul-vscode','code-server','mindul-ai-infra_vscode_config','/config',
            ['.bashrc','.profile','.config','.local','.ssh','data','extensions'])]
    for name,service,volume,target,items in tasks:
        raw=run(['docker','inspect',name],capture=True,check=False)
        if not isinstance(raw,str):continue
        current=json.loads(raw)[0]
        if any(m.get('Name')==volume and m['Destination']==target for m in current['Mounts']):continue
        existing=run(['docker','volume','inspect',volume],capture=True,check=False)
        if isinstance(existing,str):raise RuntimeError(f'Migration destination already exists; inspect before proceeding: {volume}')
        print('Migrating persistent storage:',name,flush=True)
        run(['docker','stop','--time','20',name])
        try:
            logical='kafka_data' if service=='kafka' else 'vscode_config'
            run(['docker','volume','create','--label','com.docker.compose.project=mindul-ai-infra',
                 '--label','com.docker.compose.volume='+logical,volume])
            env=dict(entry.split('=',1) for entry in current['Config']['Env'])
            paths=[env.get('KAFKA_LOG_DIRS','/tmp/kraft-combined-logs')+'/.'] if items is None else ['/config/'+item for item in items]
            for source in paths:
                producer=subprocess.Popen(['docker','cp',name+':'+source,'-'],stdout=subprocess.PIPE)
                consumer=subprocess.Popen(['docker','run','--rm','-i','--network','none','--user','0',
                    '-v',volume+':/target','-v',str(HELPER)+':/tools/snapshot.py:ro',
                    config()['helper_image'],'python','/tools/snapshot.py','import-tar'],stdin=producer.stdout)
                producer.stdout.close()
                output_code=consumer.wait();input_code=producer.wait()
                if output_code or input_code:raise RuntimeError('Storage migration stream failed: '+source)
            base('up','-d','--no-deps',service)
        except BaseException:
            run(['docker','start',name],check=False)
            raise
    write(STATE/'migrations.json',{'persistent_storage_verified_at':time.time()})


def archive_signature(path):
    sha=hashlib.sha256()
    signer=hmac.new(hashlib.sha256(b'mindul-backup-hmac:'+KEY.read_bytes().strip()).digest(),digestmod=hashlib.sha256)
    with path.open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):sha.update(chunk);signer.update(chunk)
    return sha.hexdigest(),signer.hexdigest()


def encrypt(stage,destination):
    process=subprocess.Popen(['openssl','enc','-aes-256-cbc','-salt','-pbkdf2','-iter','200000',
                              '-pass','file:'+str(KEY),'-out',str(destination)],stdin=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=process.stdin,mode='w|gz') as archive:archive.add(stage,arcname='snapshot')
        process.stdin.close()
        if process.wait():raise RuntimeError('Backup encryption failed')
    finally:
        if process.poll() is None:process.kill();process.wait()
    destination.chmod(0o600)


@contextlib.contextmanager
def decrypted(backup):
    backup=Path(backup).resolve()
    manifest=read(backup.with_suffix('.json'))
    if not manifest:raise RuntimeError('Backup authentication manifest is missing')
    sha,signature=archive_signature(backup)
    if not hmac.compare_digest(signature,manifest['hmac_sha256']) or sha!=manifest['sha256']:
        raise RuntimeError('Backup signature check failed; refusing to decrypt')
    with tempfile.TemporaryDirectory(prefix='restore-',dir=STATE) as temporary:
        target=Path(temporary)
        process=subprocess.Popen(['openssl','enc','-d','-aes-256-cbc','-pbkdf2','-iter','200000',
                                  '-pass','file:'+str(KEY),'-in',str(backup)],stdout=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=process.stdout,mode='r|gz') as archive:
                archive.extractall(target,filter='data')
            process.stdout.close()
            if process.wait():raise RuntimeError('Backup decryption failed')
            snapshot=target/'snapshot'
            inventory=read(snapshot/'inventory.json',{})
            if inventory.get('volumes')!=manifest['volumes']:raise RuntimeError('Backup manifest does not match authenticated archive')
            yield snapshot,manifest
        finally:
            if process.poll() is None:process.kill();process.wait()


def backup_volumes():
    volumes=list(config()['volumes'])
    known={v['name'] for v in volumes}
    for path in (APPS/'apps').glob('*/config.json'):
        app=read(path)
        names=run(['docker','volume','ls','--filter','label=com.docker.compose.project='+app['project'],'--format','{{.Name}}'],capture=True).splitlines()
        for name in names:
            if name not in known:
                containers=run(['docker','ps','-q','--filter','volume='+name],capture=True).splitlines()
                volumes.append({'name':name,'mode':'cold','containers':containers});known.add(name)
    return volumes


def copy_configs(stage):
    destination=stage/'config';destination.mkdir()
    paths=run(['git','-C',ROOT,'ls-files','--cached','--others','--exclude-standard'],capture=True).splitlines()
    for relative in [*paths,'.env','monitoring/.env','monitoring/secrets/web-metrics.token','paseo/.env','paseo/.operator-credentials','paseo/.router.json','paseo/.router.env']:
        source=ROOT/relative
        if source.is_file():
            target=destination/'infra'/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    shutil.copytree(ROOT/'deployments/caddy',destination/'caddy-routes',dirs_exist_ok=True)
    if APPS.exists():shutil.copytree(APPS,destination/'app-state',ignore=shutil.ignore_patterns('.git','node_modules','.lock'),dirs_exist_ok=True,symlinks=True)
    repos={}
    for name in ('murder-parker','murder-parker-qa'):
        repo=Path.home()/name
        if not repo.exists():continue
        repos[name]={'commit':run(['git','-C',repo,'rev-parse','HEAD'],capture=True),
                     'branch':run(['git','-C',repo,'branch','--show-current'],capture=True)}
        for relative in ('.env','deploy/.env'):
            path=repo/relative
            if path.exists():
                target=destination/name/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
    router=Path.home()/'.local/share/mindul-paseo-router/queue.sqlite'
    if router.exists():
        target=destination/'paseo-router/queue.sqlite';target.parent.mkdir(exist_ok=True)
        with sqlite3.connect(router) as source_db, sqlite3.connect(target) as backup_db:
            source_db.backup(backup_db)
    write(stage/'repositories.json',repos)


def create_backup():
    with tempfile.TemporaryDirectory(prefix='backup-stage-',dir=STATE) as temporary:
        stage=Path(temporary);copy_configs(stage)
        snapshots=stage/'volumes';snapshots.mkdir()
        inventory=[];maximum=config()['backup_max_plaintext_bytes']
        total=sum(p.stat().st_size for p in stage.rglob('*') if p.is_file() and not p.is_symlink())
        if total>maximum:raise RuntimeError('Configuration snapshot exceeds the local backup size budget')
        for volume in backup_volumes():
            name=volume['name']
            present=run(['docker','volume','inspect',name],capture=True,check=False)
            if not isinstance(present,str):raise RuntimeError('Required backup volume is missing: '+name)
            output=snapshots/name;output.mkdir()
            paused=[]
            try:
                if volume['mode']=='cold':
                    for container in volume.get('containers',[volume.get('container')]):
                        if container and inspect_container(container)['State']['Running']:
                            run(['docker','stop','--time','20',container]);paused.append(container)
                print('Snapshot:',name,flush=True)
                summary=json.loads(helper('snapshot',name,output,exclude=volume.get('exclude',[]),budget=maximum-total,capture=True))
                size=sum(p.stat().st_size for p in output.rglob('*') if p.is_file() and not p.is_symlink())
                total+=size
                if total>maximum:raise RuntimeError('Local backup size budget exceeded')
                inventory.append({'name':name,'mode':volume['mode'],**summary,'bytes':size})
            finally:
                for container in reversed(paused):run(['docker','start',container])
        write(stage/'inventory.json',{'volumes':inventory,'created_at':time.time(),'excluded':['Ollama weight blobs','Prometheus/Loki/Tempo history']})
        name=dt.datetime.now(dt.timezone.utc).strftime('mindul-%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:6]
        temporary_backup=BACKUPS/(name+'.partial')
        destination=BACKUPS/(name+'.tar.gz.enc')
        try:
            encrypt(stage,temporary_backup)
            temporary_backup.replace(destination)
            sha,signature=archive_signature(destination)
            write(destination.with_suffix('.json'),{'version':1,'created_at':time.time(),'bytes':destination.stat().st_size,
                'sha256':sha,'hmac_sha256':signature,'volumes':inventory})
            verify_backup(destination)
        except BaseException:
            temporary_backup.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            destination.with_suffix('.json').unlink(missing_ok=True)
            raise
        update_metrics(backup_last_success=time.time(),backup_last_attempt_success=1,backup_bytes=destination.stat().st_size)
        valid=sorted(BACKUPS.glob('mindul-*.tar.gz.enc'),key=lambda p:p.stat().st_mtime,reverse=True)
        for old in valid[config()['backup_keep_count']:]:old.unlink();old.with_suffix('.json').unlink(missing_ok=True)
        print('Encrypted backup complete:',destination,flush=True)
        return destination


def latest_backup():
    files=list(BACKUPS.glob('mindul-*.tar.gz.enc'))
    if not files:raise RuntimeError('No encrypted backup exists yet')
    return max(files,key=lambda p:p.stat().st_mtime)


def verify_backup(path):
    with decrypted(path) as (stage,manifest):
        count=0
        for item in manifest['volumes']:
            snapshot=stage/'volumes'/item['name']
            if not (snapshot/'owners.json').exists():raise RuntimeError('Incomplete volume snapshot')
            # Check SQLite from an isolated read-only Docker mount, never live volumes.
            expected=read(snapshot/'sqlite.json',{})
            if expected:
                result=run(['docker','run','--rm','--network','none','--user','0',
                    '-v',str(snapshot/'data')+':/source:ro','-v',str(snapshot)+':/snapshot:ro',
                    '-v',str(HELPER)+':/tools/snapshot.py:ro',config()['helper_image'],
                    'python','/tools/snapshot.py','verify'],capture=True)
                count+=json.loads(result)['verified_sqlite_files']
        print(f'Archive authentication and SQLite verification: {count} databases OK',flush=True)


def restore_check(path):
    with decrypted(path) as (stage,manifest):
        count=0
        for item in manifest['volumes']:
            if not item['sqlite_files']:continue
            volume='mindul-restore-check-'+uuid.uuid4().hex
            run(['docker','volume','create','--label','mindul.restore-test=true',volume],capture=True)
            try:
                result=json.loads(helper('restore',volume,stage/'volumes'/item['name'],write_volume=True,capture=True))
                count+=result['verified_sqlite_files']
            finally:run(['docker','volume','rm',volume],capture=True)
        if not count:raise RuntimeError('Backup contains no SQLite databases to restore-test')
        update_metrics(backup_last_restore_check=time.time())
        print(f'Isolated-volume restore verification: {count} databases OK',flush=True)


def restore_files(path,destination):
    destination=Path(destination).expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):raise RuntimeError('Restore directory must be empty')
    with decrypted(path) as (stage,_):shutil.copytree(stage,destination,dirs_exist_ok=True,symlinks=True)
    destination.chmod(0o700)
    print('Restored files to:',destination,'(running services and volumes unchanged)')


def health():
    def probe(item):
        service,url=item;parsed=urllib.parse.urlsplit(url)
        success=0;expiry=0
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'mindul-infrastructure-health/1.0'}),timeout=5) as response:success=int(response.status==200)
            with socket.create_connection((parsed.hostname,443),timeout=5) as connection:
                with ssl.create_default_context().wrap_socket(connection,server_hostname=parsed.hostname) as secure:
                    expiry=ssl.cert_time_to_seconds(secure.getpeercert()['notAfter'])
        except OSError:pass
        labels=f'service="{service}",host="{parsed.hostname}"'
        return [f'mindul_https_probe_success{{{labels}}} {success}',f'mindul_tls_certificate_expiry_timestamp_seconds{{{labels}}} {expiry}']
    lines=['# TYPE mindul_https_probe_success gauge','# TYPE mindul_tls_certificate_expiry_timestamp_seconds gauge']
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(probe,config()['probes'].items()):lines.extend(result)
    lines.append(f'mindul_health_last_probe_timestamp_seconds {time.time()}')
    states=dict(line.split('\t',1) for line in run(['docker','ps','-a','--format','{{.Names}}\t{{.State}}'],capture=True).splitlines())
    for name in config().get('required_containers',[]):
        lines.append(f'mindul_container_running{{container="{name}"}} {int(states.get(name)=="running")}')
    atomic(STATE/'metrics/https.prom','\n'.join(lines)+'\n',0o644)
    print('HTTPS/TLS probes updated')


def maintenance():
    available=run(['docker','buildx','prune','--help'],capture=True)
    if '--max-used-space' in available:
        run(['docker','buildx','prune','--force','--max-used-space',config()['build_cache_max_used']])
    else:run(['docker','builder','prune','--force','--keep-storage',config()['build_cache_max_used']])
    status=read(STATE/'status.json',{})
    if time.time()-status.get('backup_last_success',0)<36*3600:
        run(['docker','image','prune','--force','--filter','until='+config()['image_prune_age']])
    update_metrics(maintenance_last_success=time.time())
    print('Maintenance complete; no Docker volumes were deleted')


def update_checkout(commit):
    if not re.fullmatch(r'[a-f0-9]{40}',commit):raise RuntimeError('Deploy requires a full commit SHA')
    if run(['git','-C',ROOT,'status','--porcelain'],capture=True):raise RuntimeError('Working tree has local changes; refusing to overwrite them')
    run(['git','-C',ROOT,'fetch','origin','main'])
    head=run(['git','-C',ROOT,'rev-parse','HEAD'],capture=True)
    if head!=commit:
        ancestor=run(['git','-C',ROOT,'merge-base','--is-ancestor',commit,head],check=False)
        if ancestor.returncode==0:
            print('A newer revision is already present; stale deployment skipped')
            return False
        run(['git','-C',ROOT,'merge','--ff-only',commit])
    return True


def deploy(commit=None):
    if commit and not update_checkout(commit):return
    # Re-exec after updating source so the deployed revision owns deployment logic.
    if commit and os.environ.get('MINDUL_DEPLOY_REEXEC')!=commit:
        os.environ['MINDUL_DEPLOY_REEXEC']=commit
        os.execv(sys.executable,[sys.executable,str(ROOT/'ops/mindul_ops.py'),'deploy','--commit',commit])
    base('config','--quiet');monitoring('config','--quiet')
    monitoring('build','telemetry-gateway')
    migrate_storage()
    base('up','-d')
    monitoring('up','-d','--build','--force-recreate')
    run([sys.executable,ROOT/'scripts/install-paseo.py'])
    run([ROOT/'scripts/install-mindul.sh'])
    run([ROOT/'scripts/install-ops.sh'],env={**os.environ,'MINDUL_OPS_INITIALIZED':'1'})
    run(['docker','compose','--project-directory',ROOT,'exec','-T','caddy','caddy','validate','--config','/etc/caddy/Caddyfile','--adapter','caddyfile'],capture=True)
    run(['docker','compose','--project-directory',ROOT,'exec','-T','caddy','caddy','reload','--config','/etc/caddy/Caddyfile','--adapter','caddyfile'],capture=True)
    # File-provisioned alerting resources need a restart (dashboards auto-refresh).
    monitoring('restart','grafana')
    for _ in range(30):
        try:
            with urllib.request.urlopen('https://mindul-server.duckdns.org/grafana/api/health',timeout=3) as response:
                if response.status==200:break
        except OSError:time.sleep(2)
    else:raise RuntimeError('Grafana failed its post-deploy health check')
    health();update_metrics(deployment_last_success=time.time())
    write(STATE/'deployment.json',{'commit':run(['git','-C',ROOT,'rev-parse','HEAD'],capture=True),'deployed_at':time.time()})
    initial=read(STATE/'status.json',{})
    if not initial.get('backup_last_success'):
        try:create_backup()
        except BaseException:update_metrics(backup_last_attempt_success=0);raise
    if not initial.get('backup_last_restore_check'):restore_check(latest_backup())
    if not initial.get('maintenance_last_success'):maintenance()
    print('Infrastructure deployment complete')


def main():
    def terminate(_signal,_frame):raise KeyboardInterrupt('Operation interrupted')
    signal.signal(signal.SIGTERM,terminate)
    parser=argparse.ArgumentParser(description='Mindul infrastructure operations')
    commands=parser.add_subparsers(dest='command',required=True)
    for command in ('init','migrate-storage','backup','maintenance','health','status'):commands.add_parser(command)
    p=commands.add_parser('deploy');p.add_argument('--commit')
    for command in ('verify','restore-check'):
        p=commands.add_parser(command);p.add_argument('backup',nargs='?')
    p=commands.add_parser('restore');p.add_argument('backup');p.add_argument('--directory',required=True)
    args=parser.parse_args()
    try:
        if args.command=='status':
            print(json.dumps(read(STATE/'status.json',{}),indent=2));return 0
        with locked(apps=args.command in ('backup','deploy')):
            if args.command=='init':pass
            elif args.command=='deploy':deploy(args.commit)
            elif args.command=='migrate-storage':migrate_storage()
            elif args.command=='backup':
                try:create_backup()
                except BaseException:update_metrics(backup_last_attempt_success=0);raise
            elif args.command=='verify':verify_backup(args.backup or latest_backup())
            elif args.command=='restore-check':restore_check(args.backup or latest_backup())
            elif args.command=='restore':restore_files(args.backup,args.directory)
            elif args.command=='maintenance':maintenance()
            elif args.command=='health':health()
        return 0
    except (Exception,KeyboardInterrupt) as error:
        print('mindul-ops:',error,file=sys.stderr);return 1


if __name__=='__main__':sys.exit(main())
