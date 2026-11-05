import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('dashboards', Path(__file__).resolve().parents[1]/'scripts/service_dashboards.py')
dashboards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboards)


class ServiceDashboardsTests(unittest.TestCase):
    def test_service_folders_group_environments_and_remove_retired_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'monitoring/grafana').mkdir(parents=True)
            state = root/'state'
            for name, env in [('shop','production'), ('shop','qa'), ('blog','production')]:
                path = state/'apps'/f'{name}-{env}'
                path.mkdir(parents=True)
                (path/'config.json').write_text(json.dumps({'name':name,'environment':env,'project':f'mindul-{name}-{env}','domain':f'{name}-{env}.example.org'}))
                (path/'state.json').write_text('{"history":["r1"]}')
            self.assertEqual(dashboards.sync(root,state),2)
            target=root/'monitoring/grafana/service-dashboards'
            shop=json.loads((target/'Web · shop/service.json').read_text())
            self.assertIn('shop-qa.example.org',json.dumps(shop))
            query=shop['panels'][1]['targets'][0]['expr']
            self.assertIn('mindul-shop-production',query)
            self.assertNotIn('blog',query)
            uid=shop['uid']
            (state/'apps/blog-production/state.json').write_text('{"history":[]}')
            self.assertEqual(dashboards.sync(root,state),1)
            self.assertFalse((target/'Web · blog').exists())
            self.assertEqual(json.loads((target/'Web · shop/service.json').read_text())['uid'],uid)
