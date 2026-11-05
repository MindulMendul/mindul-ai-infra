#!/usr/bin/env python3
"""Small Git → Docker Compose → Caddy deployer; Python standard library only."""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

INFRA = Path(os.environ.get('MINDUL_INFRA_DIR', Path(__file__).resolve().parents[1])).resolve()
STATE = Path(os.environ.get('MINDUL_STATE_DIR', Path.home() / '.local/share/mindul-deploy')).resolve()
ROUTES = INFRA / 'deployments/caddy'
BASE_DOMAIN = os.environ.get('MINDUL_BASE_DOMAIN', 'mindul-server.duckdns.org')
NETWORK = os.environ.get('MINDUL_NETWORK', 'mindul-net')
CADDY = os.environ.get('MINDUL_CADDY_CONTAINER', 'mindul-caddy')


class DeployError(Exception):
    pass


def run(args, *, capture=False, check=True, cwd=None, env=None):
    try:
        result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None)
    except FileNotFoundError as error:
        raise DeployError(f'필요한 명령어가 없습니다: {args[0]}') from error
    if check and result.returncode:
        # Captured Compose config can contain secrets. Never dump its stdout.
        detail = result.stderr.strip()[:1500] if capture else ''
        raise DeployError(f'{args[0]} 실행 실패 ({result.returncode})' + (f'\n{detail}' if detail else ''))
    return result.stdout.strip() if capture and result.returncode == 0 else result


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_atomic(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text)
    temporary.chmod(mode)
    temporary.replace(path)


def write_json(path, value):
    write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def slug(value):
    value = re.sub(r'[^a-z0-9-]+', '-', value.lower()).strip('-')
    if not value or len(value) > 40:
        raise DeployError('앱 이름과 환경은 영문 소문자·숫자·하이픈으로 1~40자여야 합니다.')
    return value


def repository_name(repo):
    return slug(repo.rstrip('/').rsplit('/', 1)[-1].removesuffix('.git'))


def validate_repo(repo):
    if repo.startswith('https://'):
        url = urllib.parse.urlsplit(repo)
        if not url.hostname or url.username or url.password:
            raise DeployError('Git URL에 비밀번호나 토큰을 넣지 마세요. 기존 SSH 인증 또는 Git credential을 사용하세요.')
    elif repo.startswith('git@') or repo.startswith('ssh://'):
        pass
    elif Path(repo).is_absolute() and Path(repo).is_dir():
        pass  # Also supports local Git repositories for bootstrap and testing.
    else:
        raise DeployError('HTTPS/SSH Git 주소 또는 로컬 Git 저장소의 절대 경로가 필요합니다.')


def validate_domain(domain):
    domain = domain.lower().rstrip('.')
    if len(domain) > 253 or '.' not in domain or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
        for label in domain.split('.')
    ):
        raise DeployError('도메인은 https://, 경로, 포트 없이 입력하세요.')
    return domain


def app_directory(name, environment):
    return STATE / 'apps' / (slug(name) + '-' + slug(environment))


def project_name(name, environment):
    return 'mindul-' + slug(name) + '-' + slug(environment)


def source_path(source, relative):
    path = (source / relative).resolve()
    if not path.is_relative_to(source.resolve()):
        raise DeployError(f'저장소 밖의 경로는 사용할 수 없습니다: {relative}')
    return path


def manifest_config(source):
    path = source_path(source,'mindul.json')
    data = read_json(path, {})
    if not isinstance(data, dict) or data.get('version', 1) != 1:
        raise DeployError('mindul.json은 version 1 JSON 객체여야 합니다.')
    allowed = {'version', 'compose', 'service', 'dockerfile', 'context', 'port',
               'health_path', 'volumes', 'build_args', 'memory', 'cpus'}
    unknown = set(data) - allowed
    if unknown:
        raise DeployError(f'mindul.json의 지원하지 않는 키: {", ".join(sorted(unknown))}')
    return data


def detect_source(source, settings):
    if settings.get('compose') and settings.get('dockerfile'):
        raise DeployError('compose와 dockerfile 중 하나만 지정하세요.')
    if settings.get('compose'):
        path = source_path(source, settings['compose'])
        if not path.is_file(): raise DeployError(f'Compose 파일이 없습니다: {path}')
        return 'compose', path
    if settings.get('dockerfile'):
        path = source_path(source, settings['dockerfile'])
        if not path.is_file(): raise DeployError(f'Dockerfile이 없습니다: {path}')
        return 'dockerfile', path
    for relative in ('compose.yaml', 'compose.yml', 'docker-compose.yml', 'docker-compose.yaml',
                     'deploy/compose.yaml', 'deploy/docker-compose.yml'):
        path=source_path(source,relative)
        if path.is_file(): return 'compose', path
    for relative in ('Dockerfile', 'deploy/Dockerfile'):
        path=source_path(source,relative)
        if path.is_file(): return 'dockerfile', path
    raise DeployError('Dockerfile 또는 Compose 파일이 필요합니다. --dockerfile/--compose로 경로를 지정할 수도 있습니다.')


def select_service(services, requested=None):
    if requested:
        if requested not in services: raise DeployError(f'웹 서비스가 없습니다: {requested}')
        return requested
    if len(services) == 1: return next(iter(services))
    published = [name for name, service in services.items() if service.get('ports')]
    if len(published) == 1: return published[0]
    for name in ('web', 'app', 'server', 'frontend'):
        if name in services: return name
    raise DeployError('서비스가 여러 개입니다. --service 또는 mindul.json의 service로 웹 서비스를 지정하세요.')


def detect_port(service, settings, dockerfile=None):
    port = settings.get('port')
    if port is None:
        ports = service.get('ports') or service.get('expose') or []
        if ports:
            value = ports[0]
            port = value.get('target') if isinstance(value, dict) else str(value).split(':')[-1].split('/')[0]
    if port is None and dockerfile:
        exposed = re.findall(r'^\s*EXPOSE\s+(\d+)', dockerfile.read_text(), flags=re.MULTILINE | re.IGNORECASE)
        if exposed: port = exposed[-1]
    try: port = int(port)
    except (ValueError, TypeError): raise DeployError('내부 HTTP 포트를 찾지 못했습니다. --port 3000처럼 지정하세요.')
    if not 1 <= port <= 65535: raise DeployError('포트는 1~65535 범위여야 합니다.')
    return port


def normalize_compose(data, project, web_service, release, settings, env_file, origin):
    """Give each deployment its own names and volumes; publish HTTP through Caddy."""
    services = data.get('services', {})
    if not services or web_service not in services:
        raise DeployError('배포할 Compose 서비스가 없습니다.')
    result = {'services': {}, 'networks': {
        'default': {}, 'mindul-public': {'external': True, 'name': NETWORK},
    }, 'volumes': {}}
    for name, declaration in data.get('volumes', {}).items():
        declaration = declaration or {}
        if declaration.get('external'):
            raise DeployError(f'외부 볼륨 {name}은 자동 배포에서 공유하지 않습니다. 앱 전용 named volume을 사용하세요.')
        if declaration.get('driver_opts') or declaration.get('driver','local') != 'local':
            raise DeployError(f'{name}: 호스트 경로를 공유하는 볼륨 대신 기본 local named volume을 사용하세요.')
        result['volumes'][name] = {**declaration, 'name': project + '-' + name}
    for section in ('configs', 'secrets'):
        if data.get(section): result[section] = data[section]
    for name, definition in services.items():
        service = dict(definition)
        if service.get('network_mode') or service.get('privileged') or service.get('pid') == 'host':
            raise DeployError(f'{name}: host network/PID 또는 privileged 설정은 이 배포 도구에서 지원하지 않습니다.')
        if service.get('profiles'):
            raise DeployError(f'{name}: profiles 없이 실행 가능한 배포용 Compose를 사용하세요.')
        for mount in service.get('volumes', []):
            if isinstance(mount, str):
                raise DeployError('Compose 볼륨은 docker compose config로 정규화해야 합니다.')
            if mount.get('type') == 'bind' and not mount.get('read_only'):
                raise DeployError(f'{name}: 쓰기 가능한 bind mount 대신 named volume을 사용하세요: {mount.get("target")}')
        for key in ('container_name', 'ports', 'networks', 'profiles', 'develop', 'pull_policy'):
            service.pop(key, None)
        service['networks'] = {'default': None}
        service.setdefault('restart', 'unless-stopped')
        limits=service.get('deploy',{}).get('resources',{}).get('limits',{})
        service.setdefault('mem_limit', settings.get('memory', limits.get('memory','1g')))
        service.setdefault('cpus', settings.get('cpus', limits.get('cpus',1.0)))
        service.setdefault('logging', {'driver':'json-file', 'options':{'max-size':'10m','max-file':'2'}})
        labels = service.get('labels') or {}
        if not isinstance(labels, dict): raise DeployError('Compose labels는 객체여야 합니다.')
        service['labels'] = {**labels, 'mindul.managed':'true', 'mindul.app':project, 'mindul.release':release}
        if service.get('build'):
            service['image'] = f'{project}-{name}:{release}'
        if name == web_service:
            service['networks']['mindul-public'] = {'aliases':[project + '-web']}
            environment = service.get('environment') or {}
            if not isinstance(environment, dict): raise DeployError('Compose environment는 객체여야 합니다.')
            service['environment'] = {**environment, 'APP_ORIGIN':origin, 'APP_RELEASE':release}
        result['services'][name] = service
    return result


def compose_args(project, release_dir):
    return ['docker', 'compose', '-p', project, '-f', release_dir / 'compose.json']


def check_setup():
    if not (ROUTES / '.ready').exists():
        raise DeployError('배포 도구 초기 연결이 필요합니다. scripts/install-mindul.sh를 먼저 실행하세요.')
    container=json.loads(run(['docker', 'inspect', CADDY], capture=True))[0]
    if not any(m['Destination']=='/etc/caddy/apps' and Path(m['Source'])==ROUTES for m in container.get('Mounts',[])):
        raise DeployError('Caddy 배포 경로가 연결되지 않았습니다. scripts/install-mindul.sh를 다시 실행하세요.')
    run(['docker', 'network', 'inspect', NETWORK], capture=True)


@contextlib.contextmanager
def lock():
    STATE.mkdir(parents=True, exist_ok=True)
    STATE.chmod(0o700)
    with (STATE / '.lock').open('a') as handle:
        try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise DeployError('다른 배포 작업이 진행 중입니다. 완료 후 다시 실행하세요.')
        yield


def caddy_reload():
    run(['docker', 'exec', CADDY, 'caddy', 'validate', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'], capture=True)
    run(['docker', 'exec', CADDY, 'caddy', 'reload', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'], capture=True)


def route_text(domain, project, port):
    return f'{domain} {{\n\thandle /metrics {{\n\t\trespond 404\n\t}}\n\thandle {{\n\t\treverse_proxy {project}-web:{port}\n\t}}\n}}\n'


def update_route(path, text):
    previous = path.read_text() if path.exists() else None
    if text is None: path.unlink(missing_ok=True)
    else: write_atomic(path, text, 0o644)
    try: caddy_reload()
    except DeployError:
        if previous is None: path.unlink(missing_ok=True)
        else: write_atomic(path, previous, 0o644)
        with contextlib.suppress(DeployError): caddy_reload()
        raise


def wait_healthy(project, port, path, domain, timeout):
    if not path.startswith('/') or any(c in path for c in '\r\n?#'):
        raise DeployError('health_path는 쿼리가 없는 /경로 형식이어야 합니다.')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = run(['docker','exec',CADDY,'wget','-q','-O','/dev/null','-T','4',
                      '--header',f'Host: {domain}','--header','X-Forwarded-Proto: https',
                      f'http://{project}-web:{port}{path}'], capture=True, check=False)
        if isinstance(result, str): return
        time.sleep(2)
    raise DeployError(f'웹 상태 검사 실패: {project}-web:{port}{path}')


def check_https(domain, path, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request('https://' + domain + path, headers={'User-Agent':'mindul-deploy/1'})
            with urllib.request.urlopen(request, timeout=min(5, max(1,timeout))) as response:
                if response.status < 400: return True
        except (OSError, urllib.error.URLError, TimeoutError): pass
        time.sleep(2)
    return False


def check_domain_available(domain, project):
    # Refuse both generated and manually maintained hostnames.
    for path in [INFRA / 'Caddyfile', *ROUTES.glob('*.caddy')]:
        if path.name == project + '.caddy': continue
        if re.search(r'(?<![a-z0-9.-])' + re.escape(domain) + r'\s*\{', path.read_text()):
            raise DeployError(f'다른 서비스가 이미 사용하는 도메인입니다: {domain}')
    try: socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
    except socket.gaierror: raise DeployError(f'DNS에 등록되지 않은 도메인입니다: {domain}')


def prepare_env(app_dir, supplied, origin, release):
    path = app_dir / '.env'
    if supplied:
        source = Path(supplied).expanduser().resolve()
        if not source.is_file(): raise DeployError(f'환경변수 파일이 없습니다: {source}')
        if source != path: write_atomic(path, source.read_text())
    if not path.exists(): write_atomic(path, '# App secrets/settings; never committed to Git.\n')
    # Always keep automatic settings consistent with the selected environment.
    original = [line for line in path.read_text().splitlines()
                if not re.match(r'^(?:export\s+)?(?:APP_ORIGIN|APP_RELEASE)=', line)]
    original += [f'APP_ORIGIN={origin}', f'APP_RELEASE={release}']
    write_atomic(path, '\n'.join(original) + '\n')
    return path


def build_definition(source, release_dir, app_dir, config, release):
    settings = {**manifest_config(source), **config.get('settings', {})}
    if config.get('settings',{}).get('dockerfile'): settings.pop('compose',None)
    if config.get('settings',{}).get('compose'): settings.pop('dockerfile',None)
    mode, path = detect_source(source, settings)
    env_file = app_dir / '.env'
    project, origin = config['project'], 'https://' + config['domain']
    if mode == 'compose':
        raw = run(['docker','compose','--env-file',env_file,'-f',path,'config','--format','json'], capture=True)
        data = json.loads(raw)
        service = select_service(data.get('services',{}), settings.get('service'))
        port = detect_port(data['services'][service], settings)
    else:
        context = source_path(source, settings.get('context','.'))
        build_args = {'APP_RELEASE':release, **settings.get('build_args',{})}
        volumes = settings.get('volumes', {})
        if not isinstance(volumes, dict): raise DeployError('mindul.json volumes는 {"data":"/data"} 형식이어야 합니다.')
        for name, target in volumes.items():
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*',name) or not isinstance(target,str) or not target.startswith('/'):
                raise DeployError('볼륨 이름과 컨테이너 내부 절대 경로를 확인하세요.')
        data = {'services':{'web':{
            'build':{'context':str(context),'dockerfile':str(path),'args':build_args},
            'env_file':[str(env_file)],
            'volumes':[{'type':'volume','source':name,'target':target} for name,target in volumes.items()],
        }}, 'volumes':{name:{} for name in volumes}}
        service = 'web'
        port = detect_port(data['services'][service], settings, path)
    normalized = normalize_compose(data,project,service,release,settings,env_file,origin)
    # A self-contained rollback must not reread an env file that may later change.
    provisional = release_dir / 'provisional.json'
    write_json(provisional,normalized)
    resolved = json.loads(run(['docker','compose','--env-file',env_file,'-p',project,'-f',provisional,'config','--format','json'],capture=True))
    # Compose config already escapes literal dollar signs for a safe round trip.
    write_json(release_dir / 'compose.json',resolved)
    provisional.unlink()
    images = [s['image'] for s in resolved['services'].values() if s.get('build')]
    health_path=settings.get('health_path','/')
    if not isinstance(health_path,str) or not health_path.startswith('/') or any(c in health_path for c in '\r\n?#'):
        raise DeployError('health_path는 쿼리가 없는 /경로 형식이어야 합니다.')
    return {'port':port,'health_path':health_path,'images':images,
            'mode':mode,'web_service':service}


def prune_releases(app_dir, state):
    retained = set(state.get('history',[])[:2])
    for directory in (app_dir / 'releases').iterdir():
        if directory.name in retained or not directory.is_dir(): continue
        in_use = run(['docker','ps','-aq','--filter','label=mindul.release='+directory.name],capture=True)
        if in_use: continue
        metadata = read_json(directory / 'release.json', {})
        for image in metadata.get('images',[]):
            run(['docker','image','rm',image],capture=True,check=False)
        shutil.rmtree(directory)


def deploy(args, saved=None):
    if args.timeout<1 or args.https_timeout<0: raise DeployError('timeout은 1 이상, https-timeout은 0 이상이어야 합니다.')
    repo = args.repo if getattr(args,'repo',None) else saved['repo']
    validate_repo(repo)
    name = slug(args.name or (saved or {}).get('name') or repository_name(repo))
    environment = slug(args.environment)
    app_dir = app_directory(name,environment)
    project = project_name(name,environment)
    previous_config = read_json(app_dir / 'config.json', {})
    state = read_json(app_dir / 'state.json', {'history':[]})
    if previous_config.get('repo') and previous_config['repo'] != repo:
        raise DeployError('같은 앱 이름에 다른 저장소를 연결할 수 없습니다. --name으로 새 이름을 지정하세요.')
    branch = args.branch or previous_config.get('branch') or ('qa' if environment == 'qa' else None)
    suffix = name if environment == 'production' else f'{name}-{environment}'
    domain = validate_domain(args.domain or previous_config.get('domain') or suffix + '.' + BASE_DOMAIN)
    check_domain_available(domain,project)
    if shutil.disk_usage(STATE).free < 2 * 1024**3:
        raise DeployError('배포 전에 디스크 여유 공간을 2 GiB 이상 확보하세요.')
    release = dt.datetime.now(dt.timezone.utc).strftime('r%Y%m%d%H%M%S') + '-' + os.urandom(2).hex()
    release_dir = app_dir / 'releases' / release
    source = release_dir / 'source'
    release_dir.mkdir(parents=True)
    config = {**previous_config,'name':name,'environment':environment,'repo':repo,'branch':branch,
              'domain':domain,'project':project,'settings':dict(previous_config.get('settings',{}))}
    for key in ('compose','service','dockerfile','context','port','health_path','memory','cpus'):
        value = getattr(args,key,None)
        if value is not None: config['settings'][key]=value
    if args.compose: config['settings'].pop('dockerfile',None)
    if args.dockerfile: config['settings'].pop('compose',None)
    current = state['history'][0] if state['history'] else None
    active_changed = False
    route_file = ROUTES / (project + '.caddy')
    old_route = route_file.read_text() if route_file.exists() else None
    env_backup = (app_dir / '.env').read_text() if (app_dir / '.env').exists() else None
    try:
        print(f'[{name}/{environment}] 저장소 다운로드',flush=True)
        clone = ['git','clone','--depth','1','--no-hardlinks']
        if branch: clone += ['--branch',branch]
        run([*clone,'--',repo,source])
        if not branch: config['branch']=run(['git','branch','--show-current'],cwd=source,capture=True)
        sha = run(['git','rev-parse','HEAD'],cwd=source,capture=True)
        prepare_env(app_dir,args.env_file,'https://'+domain,release)
        definition = build_definition(source,release_dir,app_dir,config,release)
        metadata = {**definition,'release':release,'commit':sha,'domain':domain,'branch':config['branch'],
                    'settings':config['settings']}
        write_json(release_dir / 'release.json',metadata)
        command = compose_args(project,release_dir)
        print(f'[{name}/{environment}] 이미지 빌드',flush=True)
        run([*command,'build'])
        print(f'[{name}/{environment}] 컨테이너 실행·상태 검사',flush=True)
        active_changed=True
        run([*command,'up','-d','--remove-orphans','--wait','--wait-timeout',str(args.timeout)])
        wait_healthy(project,definition['port'],definition['health_path'],domain,args.timeout)
        update_route(route_file,route_text(domain,project,definition['port']))
    except (Exception,KeyboardInterrupt) as error:
        print('배포 실패. 기존 배포를 복구합니다.',file=sys.stderr,flush=True)
        rollback_ok=True
        if active_changed:
            try:
                if current:
                    prior=app_dir/'releases'/current
                    run([*compose_args(project,prior),'up','-d','--remove-orphans','--wait','--wait-timeout',str(args.timeout)])
                else:
                    run([*compose_args(project,release_dir),'down'])
            except DeployError as recovery:
                rollback_ok=False
                print(f'컨테이너 복구 실패: {recovery}',file=sys.stderr)
        if active_changed:
            try: update_route(route_file,old_route)
            except DeployError as recovery:
                rollback_ok=False
                print(f'프록시 복구 실패: {recovery}',file=sys.stderr)
        if env_backup is not None: write_atomic(app_dir/'.env',env_backup)
        if rollback_ok:
            metadata=read_json(release_dir/'release.json',{})
            for image in metadata.get('images',[]): run(['docker','image','rm',image],capture=True,check=False)
            shutil.rmtree(release_dir)
        raise DeployError(str(error) if not isinstance(error,KeyboardInterrupt) else '사용자가 배포를 중단했습니다.') from error
    config['branch']=metadata['branch']
    write_json(app_dir/'config.json',config)
    state['history']=[release,*[r for r in state['history'] if r!=release]][:2]
    write_json(app_dir/'state.json',state)
    prune_releases(app_dir,state)
    print(f'[{name}/{environment}] HTTPS 확인',flush=True)
    verified=check_https(domain,definition['health_path'],args.https_timeout)
    state['https_verified']=verified
    write_json(app_dir/'state.json',state)
    print(f'배포 완료: https://{domain}\n커밋: {sha[:12]}\n상태: '+('HTTPS 정상' if verified else '앱 정상 / HTTPS 준비 중 (DNS·인증서 확인 필요)'))


def load_app(args):
    directory=app_directory(args.name,args.environment)
    config=read_json(directory/'config.json')
    state=read_json(directory/'state.json',{'history':[]})
    if not config: raise DeployError('등록된 앱이 없습니다. mindul list로 확인하세요.')
    return directory,config,state


def manage(args):
    directory,config,state=load_app(args)
    if args.command=='env':
        path=directory/'.env'
        if not path.exists(): write_atomic(path,'# KEY=value\n')
        run([*shlex.split(os.environ.get('EDITOR','nano')),path])
        path.chmod(0o600)
        print('저장 후 mindul redeploy 명령으로 적용하세요.')
        return
    if not state['history']: raise DeployError('현재 실행 가능한 배포 이력이 없습니다.')
    release_dir=directory/'releases'/state['history'][0]
    command=compose_args(config['project'],release_dir)
    if args.command=='status':
        run([*command,'ps','-a'])
        print('https://'+config['domain'])
    elif args.command=='logs':
        run([*command,'logs','--tail',str(args.tail),*(['--follow'] if args.follow else [])])
    elif args.command=='stop': run([*command,'stop'])
    elif args.command=='restart':
        run([*command,'restart'])
        meta=read_json(release_dir/'release.json')
        wait_healthy(config['project'],meta['port'],meta['health_path'],meta['domain'],args.timeout)
        print('재시작 정상')
    elif args.command=='check':
        meta=read_json(release_dir/'release.json')
        wait_healthy(config['project'],meta['port'],meta['health_path'],meta['domain'],args.timeout)
        if not check_https(config['domain'],meta['health_path'],args.timeout): raise DeployError('내부 앱은 정상이나 HTTPS 확인에 실패했습니다.')
        print('컨테이너·HTTPS 정상: https://'+config['domain'])
    elif args.command=='remove':
        run([*command,'down'])
        update_route(ROUTES/(config['project']+'.caddy'),None)
        state['history']=[]
        write_json(directory/'state.json',state)
        print('컨테이너와 도메인 연결을 제거했습니다. 데이터 볼륨과 환경변수는 보존했습니다.')
    elif args.command=='rollback':
        if len(state['history'])<2: raise DeployError('되돌릴 이전 배포가 없습니다.')
        previous=directory/'releases'/state['history'][1]
        meta=read_json(previous/'release.json')
        try:
            run([*compose_args(config['project'],previous),'up','-d','--remove-orphans','--wait','--wait-timeout',str(args.timeout)])
            wait_healthy(config['project'],meta['port'],meta['health_path'],meta['domain'],args.timeout)
            update_route(ROUTES/(config['project']+'.caddy'),route_text(meta['domain'],config['project'],meta['port']))
        except DeployError:
            run([*command,'up','-d','--remove-orphans','--wait','--wait-timeout',str(args.timeout)])
            raise
        state['history']=list(reversed(state['history']))
        config['domain']=meta['domain']
        config['branch']=meta['branch']
        config['settings']=meta.get('settings',{})
        write_json(directory/'config.json',config)
        write_json(directory/'state.json',state)
        print('롤백 완료: '+meta['commit'][:12]+' https://'+config['domain'])


def parser():
    root=argparse.ArgumentParser(prog='mindul',description='Git 저장소를 기존 Docker·Caddy에 배포합니다.')
    commands=root.add_subparsers(dest='command',required=True)
    for name in ('deploy','redeploy'):
        command=commands.add_parser(name,help='새 저장소 배포' if name=='deploy' else '저장된 설정으로 재배포')
        command.add_argument('repo' if name=='deploy' else 'name')
        if name=='deploy':command.add_argument('--name')
        command.add_argument('--environment','-e',default='production')
        for option in ('branch','domain','compose','service','dockerfile','context','health-path','env-file','memory'):
            command.add_argument('--'+option)
        command.add_argument('--port',type=int)
        command.add_argument('--cpus',type=float)
        command.add_argument('--timeout',type=int,default=120)
        command.add_argument('--https-timeout',type=int,default=60)
    commands.add_parser('list',help='등록된 앱 목록')
    for name in ('backup','restore-check','maintenance','health'):
        commands.add_parser(name,help='인프라 운영 작업: '+name)
    for name in ('status','logs','restart','stop','check','rollback','remove','env'):
        command=commands.add_parser(name)
        command.add_argument('name')
        command.add_argument('--environment','-e',default='production')
        command.add_argument('--timeout',type=int,default=120)
        if name=='logs':
            command.add_argument('--tail',type=int,default=100)
            command.add_argument('--follow','-f',action='store_true')
    return root


def main():
    args=parser().parse_args()
    try:
        if args.command in ('backup','restore-check','maintenance','health'):
            run([sys.executable,INFRA/'ops/mindul_ops.py',args.command])
            return 0
        if args.command=='list':
            apps=sorted((STATE/'apps').glob('*/config.json'))
            if not apps: print('이 도구로 등록된 앱이 없습니다. mindul deploy <Git 주소>로 배포하세요.')
            for path in apps:
                config=read_json(path)
                state=read_json(path.parent/'state.json',{'history':[]})
                active=state['history'][0] if state['history'] else '(removed)'
                print(f'{config["name"]}\t{config["environment"]}\t{config["branch"]}\t{active}\thttps://{config["domain"]}')
            return 0
        if args.command in ('status','logs','env'):
            manage(args)
            return 0
        with lock():
            check_setup()
            if args.command=='deploy': deploy(args)
            elif args.command=='redeploy':
                _,config,_=load_app(args)
                deploy(args,saved=config)
            else: manage(args)
        return 0
    except (DeployError,ValueError,OSError) as error:
        print(f'mindul: {error}',file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__=='__main__': sys.exit(main())
