import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import shutil
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('mindul',Path(__file__).resolve().parents[1]/'scripts/mindul.py')
mindul=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mindul)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.directory=Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_repository_names_and_argument_validation(self):
        self.assertEqual(mindul.repository_name('https://github.com/org/My-App.git'),'my-app')
        self.assertEqual(mindul.project_name('demo','qa'),'mindul-demo-qa')
        mindul.validate_repo('git@github.com:org/app.git')
        with self.assertRaises(mindul.DeployError):mindul.validate_repo('https://token:secret@github.com/org/repo')
        with self.assertRaises(mindul.DeployError):mindul.validate_repo('--upload-pack=bad')

    def test_domain_cannot_inject_caddy_configuration(self):
        self.assertEqual(mindul.validate_domain('app.mindul-server.duckdns.org'),'app.mindul-server.duckdns.org')
        for invalid in ['https://example.org','example.org:8080','example.org/path','example.org\n{','*.example.org']:
            with self.subTest(invalid=invalid),self.assertRaises(mindul.DeployError):mindul.validate_domain(invalid)

    def test_source_detection_and_path_boundaries(self):
        (self.directory/'deploy').mkdir()
        (self.directory/'deploy/Dockerfile').write_text('FROM python\nEXPOSE 8080\n')
        mode,path=mindul.detect_source(self.directory,{})
        self.assertEqual(mode,'dockerfile')
        self.assertEqual(path,self.directory/'deploy/Dockerfile')
        with self.assertRaises(mindul.DeployError):mindul.source_path(self.directory,'../../outside')

    def test_manifest_rejects_unknown_settings(self):
        (self.directory/'mindul.json').write_text('{"version":1,"shell":"do something"}')
        with self.assertRaises(mindul.DeployError):mindul.manifest_config(self.directory)

    def test_automatic_detection_rejects_dockerfile_symlink_outside_repository(self):
        source=self.directory/'repository'
        source.mkdir()
        outside=self.directory/'outside'
        outside.write_text('FROM python\n')
        (source/'Dockerfile').symlink_to(outside)
        with self.assertRaises(mindul.DeployError):mindul.detect_source(source,{})

    def test_select_web_service_and_target_port(self):
        services={'db':{'image':'postgres'},'frontend':{'ports':[{'target':3000,'published':'9999'}]}}
        self.assertEqual(mindul.select_service(services),'frontend')
        self.assertEqual(mindul.detect_port(services['frontend'],{}),3000)
        self.assertEqual(mindul.detect_port({}, {'port':8080}),8080)
        with self.assertRaises(mindul.DeployError):mindul.detect_port({}, {'port':70000})
        with self.assertRaises(mindul.DeployError):mindul.select_service({'one':{},'two':{}})

    def test_compose_normalization_isolates_names_volumes_and_networks(self):
        raw={'services':{'web':{'build':{'context':'/repo'},'container_name':'collision',
              'ports':[{'target':3000,'published':'3000'}],
              'environment':{'KEEP':'value'},'networks':{'shared':{'aliases':['production-app']}}},
              'db':{'image':'postgres:17'}},
             'volumes':{'data':{'name':'shared-production-data'}}}
        result=mindul.normalize_compose(raw,'mindul-demo-qa','web','r123',{},self.directory/'.env','https://qa.example.org')
        web=result['services']['web']
        self.assertNotIn('ports',web)
        self.assertNotIn('container_name',web)
        self.assertEqual(web['image'],'mindul-demo-qa-web:r123')
        self.assertEqual(web['environment']['APP_ORIGIN'],'https://qa.example.org')
        self.assertEqual(web['environment']['KEEP'],'value')
        self.assertEqual(result['volumes']['data']['name'],'mindul-demo-qa-data')
        self.assertEqual(result['services']['db']['networks'],{'default':None})
        self.assertNotIn('production-app',json.dumps(result))

    def test_unsafe_or_nonportable_compose_modes_have_actionable_errors(self):
        for service in [{'privileged':True},{'network_mode':'host'},
                        {'volumes':[{'type':'bind','source':'/tmp','target':'/data'}]},
                        {'profiles':['development']}]:
            with self.subTest(service=service),self.assertRaises(mindul.DeployError):
                mindul.normalize_compose({'services':{'web':service}},'demo','web','r1',{},self.directory/'.env','https://app.example.org')
        with self.assertRaises(mindul.DeployError):
            mindul.normalize_compose({'services':{'web':{}},'volumes':{'data':{'external':True}}},'demo','web','r1',{},self.directory/'.env','https://app.example.org')

    def test_env_file_retains_literal_values_and_is_private(self):
        source=self.directory/'secrets'
        source.write_text('PASSWORD=literal$(do-not-run)\nAPP_ORIGIN=old\nAPP_RELEASE=old\n')
        app=self.directory/'app'
        app.mkdir()
        path=mindul.prepare_env(app,str(source),'https://qa.example.org','r1')
        self.assertIn('PASSWORD=literal$(do-not-run)',path.read_text())
        self.assertEqual(path.read_text().count('APP_ORIGIN='),1)
        self.assertIn('APP_ORIGIN=https://qa.example.org',path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_failed_caddy_reload_restores_previous_route(self):
        route=self.directory/'app.caddy'
        route.write_text('old route')
        with patch.object(mindul,'caddy_reload',side_effect=[mindul.DeployError('invalid'),None]):
            with self.assertRaises(mindul.DeployError):mindul.update_route(route,'new route')
        self.assertEqual(route.read_text(),'old route')

    @unittest.skipUnless(shutil.which('docker'),'Docker Compose client required')
    def test_dockerfile_and_compose_snapshots_preserve_literal_dollar_secrets(self):
        source=self.directory/'source'
        source.mkdir()
        (source/'Dockerfile').write_text('FROM python:3.12-slim\nEXPOSE 8080\n')
        (self.directory/'.env').write_text("PASSWORD='literal$PASSWORD'\n")
        config={'project':'mindul-unit-qa','domain':'unit.example.org','settings':{}}
        for mode in ['dockerfile','compose']:
            if mode=='compose':
                (source/'compose.yaml').write_text('services:\n  web:\n    build: .\n    ports: ["8080:8080"]\n    environment:\n      PASSWORD: ${PASSWORD}\n')
            release=self.directory/mode
            release.mkdir()
            mindul.build_definition(source,release,self.directory,config,'r123')
            actual=json.loads(mindul.run(['docker','compose','-p','mindul-unit-qa','-f',release/'compose.json','config','--format','json'],capture=True))
            # `config` serializes literal dollars as $$ so its output is reusable.
            self.assertEqual(actual['services']['web']['environment']['PASSWORD'],'literal$$PASSWORD')

    def test_failed_new_route_does_not_leave_an_invalid_file(self):
        route=self.directory/'app.caddy'
        with patch.object(mindul,'caddy_reload',side_effect=[mindul.DeployError('invalid'),None]):
            with self.assertRaises(mindul.DeployError):mindul.update_route(route,'invalid')
        self.assertFalse(route.exists())

    def test_retention_preserves_in_use_release_and_removes_only_owned_images(self):
        releases=self.directory/'releases'
        for name in ['current','previous','old','recovery']:
            directory=releases/name
            directory.mkdir(parents=True)
            (directory/'release.json').write_text(json.dumps({'images':['demo-web:'+name]}))
        def fake_run(args,**kwargs):
            return 'container-id' if args[-1]=='label=mindul.release=recovery' else ''
        with patch.object(mindul,'run',side_effect=fake_run) as command:
            mindul.prune_releases(self.directory,{'history':['current','previous']})
        self.assertTrue((releases/'recovery').exists())
        self.assertFalse((releases/'old').exists())
        self.assertEqual([c.args[0] for c in command.call_args_list if c.args[0][1:3]==['image','rm']],
                         [['docker','image','rm','demo-web:old']])


if __name__=='__main__':unittest.main()
