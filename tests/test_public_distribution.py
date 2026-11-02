"""Public distribution checks using local files and synthetic HTTP requests."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from fastapi.testclient import TestClient
from dfx_agent.workbench import create_app

ROOT = Path(__file__).resolve().parents[1]


class PublicDistributionTests(unittest.TestCase):
    def test_empty_task_catalog_and_generic_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            with TestClient(create_app(Path(temporary)), base_url='http://127.0.0.1') as client:
                page = client.get('/')
                self.assertEqual(page.status_code, 200)
                self.assertIn('DFX Agent', page.text)
                response = client.get('/api/assignments')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['tasks'], [])
                config = client.get('/api/config').json()
                self.assertFalse(config['allow_live'])
                self.assertFalse(config['allow_device'])

    def test_tokenizer_install_from_local_archive(self):
        from tokenizers import Tokenizer
        from tokenizers.models import WordLevel
        spec = importlib.util.spec_from_file_location('public_tokenizer', ROOT/'scripts/prepare-tokenizer.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        model = Tokenizer(WordLevel({'[UNK]': 0, 'hello': 1}, unk_token='[UNK]'))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('official/tokenizer.json', model.to_str())
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)/'tokenizer.json'
            module.install(buffer.getvalue(), destination)
            self.assertIsInstance(json.loads(destination.read_text()), dict)
            self.assertEqual(Tokenizer.from_file(str(destination)).get_vocab_size(), 2)
