#!/usr/bin/env python3
"""Generate per-service Grafana dashboards from the deployment registry.

No Grafana credentials or API access required; file provisioning refreshes automatically.
Application RUM instrumentation remains an explicit application integration.
"""
import hashlib
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def build_dashboard(name, configs):
    uid = 'web-' + hashlib.sha256(name.encode()).hexdigest()[:20]
    environments = sorted({c['environment'] for c in configs})
    projects = '|'.join(re.escape(c['project']).replace(r'\-', '-') for c in configs)
    containers = '^(' + projects + ')-.*'
    def panel(number, title, expr, unit='short'):
        return {'id': number, 'title': title, 'type': 'timeseries',
                'gridPos': {'x': ((number-2) % 2)*12, 'y': 4+((number-2)//2)*8, 'w': 12, 'h': 8},
                'datasource': {'type': 'prometheus', 'uid': 'prometheus'},
                'targets': [{'refId': 'A', 'expr': expr, 'legendFormat': '{{name}}'}],
                'fieldConfig': {'defaults': {'unit': unit}, 'overrides': []}}
    selector = '{name=~' + json.dumps(containers) + '}'
    links = '\n'.join('- **'+c['environment']+'**: https://'+c['domain'] for c in configs)
    return {'uid': uid, 'title': name+' · Service', 'schemaVersion': 39, 'version': 1,
            'tags': ['mindul', 'web', name], 'editable': False, 'refresh': '30s',
            'time': {'from': 'now-1h', 'to': 'now'}, 'timezone': 'browser',
            'panels': [
                {'id': 1, 'title': 'Service / environments', 'type': 'text',
                 'gridPos': {'x': 0, 'y': 0, 'w': 24, 'h': 4},
                 'options': {'mode': 'markdown', 'content': links+'\n\nContainer metrics cover: '+', '.join(environments)+'. RUM, API metrics, logs and traces require application instrumentation; deploying alone does not enable collection.'}},
                panel(2, 'Container CPU', 'sum by (name) (rate(container_cpu_usage_seconds_total'+selector+'[$__rate_interval]))', 'percentunit'),
                panel(3, 'Container memory', 'container_memory_working_set_bytes'+selector, 'bytes'),
                panel(4, 'Network receive', 'sum by (name) (rate(container_network_receive_bytes_total'+selector+'[$__rate_interval]))', 'Bps'),
                panel(5, 'Network transmit', 'sum by (name) (rate(container_network_transmit_bytes_total'+selector+'[$__rate_interval]))', 'Bps')],
            'links': [{'title': 'Infrastructure', 'url': '/grafana/d/mindul-infra'}]}


def sync(root=ROOT, state=None):
    state = Path(state or os.environ.get('MINDUL_STATE_DIR', Path.home()/'.local/share/mindul-deploy'))
    target = root/'monitoring/grafana'
    groups = {}
    for path in sorted(state.glob('*/*/config.json')):
        config = json.loads(path.read_text())
        status_path = path.with_name('state.json')
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
        if status.get('history'):
            groups.setdefault(config['name'], []).append(config)
    generated = target/'service-dashboards'
    generated.mkdir(exist_ok=True)
    expected = set()
    for name, configs in groups.items():
        # Separate provider uses the immediate directory as the service folder.
        folder = generated/('Web · '+re.sub(r'[^a-zA-Z0-9_.-]', '-', name))
        folder.mkdir(exist_ok=True)
        file = folder/'service.json'
        temporary = file.with_suffix('.tmp')
        temporary.write_text(json.dumps(build_dashboard(name, configs), indent=2)+'\n')
        temporary.chmod(0o644)
        temporary.replace(file)
        expected.add(file)
    for file in generated.glob('*/service.json'):
        if file not in expected:
            file.unlink()
            file.parent.rmdir()
    return len(groups)


if __name__ == '__main__':
    print(f'Service dashboards synchronized: {sync()}')
