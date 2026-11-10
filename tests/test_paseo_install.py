import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

spec=importlib.util.spec_from_file_location('paseo_install',Path(__file__).resolve().parents[1]/'scripts/install-paseo.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


class InstallationTests(unittest.TestCase):
    def test_reconcile_enabled_router_does_not_dispatch_without_its_service_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'.env').write_text('PASEO_POSTGRES_PASSWORD=existing\n')
            (root/'.router.json').write_text('{"enabled":true}')
            (root/'.router.env').write_text('')
            response=MagicMock();response.__enter__.return_value.status=200
            with patch.object(installer,'PASEO',root),patch.object(installer,'compose'), \
                 patch.object(installer.urllib.request,'urlopen',return_value=response), \
                 patch.object(installer.subprocess,'run') as run:
                installer.install()
            run.assert_not_called()
            self.assertIn('existing',(root/'.env').read_text())

    def test_bootstrap_password_is_private_removed_from_runtime_and_not_rotated_on_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'router').mkdir()
            (root/'.env.example').write_text('PASEO_POSTGRES_PASSWORD=replace-with-random-hex\n')
            (root/'router/config.example.json').write_text('{"enabled":false}')
            (root/'router/.env.example').write_text('PROJECTS_GITHUB_TOKEN=\n')
            response=MagicMock();response.__enter__.return_value.status=200
            with patch.object(installer,'PASEO',root),patch.object(installer,'compose'), \
                 patch.object(installer.urllib.request,'urlopen',return_value=response), \
                 patch.object(installer.subprocess,'run'):
                installer.install();credentials=(root/'.operator-credentials').read_text()
                self.assertNotIn('PASEO_BOOTSTRAP_', (root/'.env').read_text())
                installer.install()
            self.assertEqual((root/'.operator-credentials').read_text(),credentials)
            self.assertEqual((root/'.operator-credentials').stat().st_mode & 0o777,0o600)
