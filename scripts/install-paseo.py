#!/usr/bin/env python3
"""Initialize and start the pinned Paseo Hub stack without exposing credentials."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PASEO = ROOT/'paseo'


def private_write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd,'w') as handle:
        os.fchmod(handle.fileno(),0o600)
        handle.write(text)


def compose(*args):
    subprocess.run(['docker','compose','--env-file',str(PASEO/'.env'),'-f',str(PASEO/'compose.yaml'),*args],check=True)


def install():
    file = PASEO/'.env'
    if not file.exists():
        email = 'admin@eniveil.com'
        password = secrets.token_urlsafe(30)
        private_write(file, (PASEO/'.env.example').read_text().replace('replace-with-random-hex',secrets.token_hex(32))+
                      f'PASEO_BOOTSTRAP_ORGANIZATION=MindulMendul\nPASEO_BOOTSTRAP_OWNER_EMAIL={email}\nPASEO_BOOTSTRAP_OWNER_PASSWORD={password}\n')
        private_write(PASEO/'.operator-credentials',f'email={email}\npassword={password}\n')
    file.chmod(0o600)
    state=Path.home()/'.local/share/mindul-paseo-router'
    state.mkdir(parents=True,exist_ok=True);state.chmod(0o700)
    compose('config','--quiet')
    compose('up','-d','--wait','--wait-timeout','240')
    with urllib.request.urlopen('http://127.0.0.1:3180/health',timeout=10) as response:
        if response.status != 200:
            raise RuntimeError('Hub health check failed')
    # Keep the bootstrap credential only in the private operator handoff file.
    lines = file.read_text().splitlines()
    if any(line.startswith('PASEO_BOOTSTRAP_') for line in lines):
        private_write(file,'\n'.join(line for line in lines if not line.startswith('PASEO_BOOTSTRAP_'))+'\n')
        compose('up','-d','--no-deps','--wait','--wait-timeout','120','hub')
    router=PASEO/'.router.json'
    if not router.exists():private_write(router,(PASEO/'router/config.example.json').read_text())
    router.chmod(0o600)
    environment=PASEO/'.router.env'
    if not environment.exists():private_write(environment,(PASEO/'router/.env.example').read_text())
    environment.chmod(0o600)
    if not json.loads(router.read_text()).get('enabled'):
        subprocess.run([sys.executable,str(PASEO/'router/runtime.py')],check=True)
    print('Paseo Hub installed. Operator credentials: paseo/.operator-credentials (0600).')
    print('Configure Projects read access and native cloud environments before enabling routing.')


if __name__ == '__main__':
    install()
