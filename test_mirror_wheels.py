import os
import sys
import time
import json
import base64
import hashlib
import tempfile
import unittest
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse

from mirror_wheels import (
    load_config,
    canonicalize_name,
    parse_hash_fragment,
    parse_link_info,
    normalize_source_config,
    build_request,
    verify_file_hash,
    SimpleIndexParser,
    fetch_package_links,
    download_distribution,
    perform_mirroring,
    start_mirror_process,
    is_parent_alive,
    SUPPORTED_EXTENSIONS,
)


class MockSimpleHandler(BaseHTTPRequestHandler):
    """Mock HTTP server simulating a PEP 503 simple repository."""

    def log_message(self, format, *args):
        # Suppress logging to keep test output clean
        pass

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path in ('/simple/adas-tsf/', '/simple/adas-tsf'):
            # Return PEP 503 HTML with a wheel and a tar.gz
            wheel_content = b"fake-wheel-binary-data"
            wheel_sha = hashlib.sha256(wheel_content).hexdigest()
            html = f"""<!DOCTYPE html>
<html>
<body>
  <a href="/files/adas_tsf-1.0.0-py3-none-any.whl#sha256={wheel_sha}">adas_tsf-1.0.0-py3-none-any.whl</a>
  <a href="/files/adas_tsf-1.0.0.tar.gz">adas_tsf-1.0.0.tar.gz</a>
  <a href="/files/readme.txt">readme.txt</a>
</body>
</html>"""
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html)))
            self.end_headers()
            self.wfile.write(html.encode('utf-8'))

        elif path == '/simple/json-pkg/':
            # Return PEP 691 JSON
            wheel_content = b"fake-json-pkg-content"
            wheel_sha = hashlib.sha256(wheel_content).hexdigest()
            data = {
                "files": [
                    {
                        "filename": "json_pkg-0.1.0-py3-none-any.whl",
                        "url": "/files/json_pkg-0.1.0-py3-none-any.whl",
                        "hashes": {"sha256": wheel_sha}
                    }
                ]
            }
            body = json.dumps(data).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.pypi.simple.v1+json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == '/files/adas_tsf-1.0.0-py3-none-any.whl':
            content = b"fake-wheel-binary-data"
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        elif path == '/files/adas_tsf-1.0.0.tar.gz':
            content = b"fake-tar-gz-binary-data"
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        elif path == '/files/json_pkg-0.1.0-py3-none-any.whl':
            content = b"fake-json-pkg-content"
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        elif path == '/auth-simple/adas-tsf/':
            # Require Basic Auth
            auth_header = self.headers.get('Authorization')
            expected = 'Basic ' + base64.b64encode(b"myuser:mypassword").decode('ascii')
            if auth_header != expected:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Test"')
                self.end_headers()
                return

            html = """<!DOCTYPE html><html><body>
  <a href="/files/adas_tsf-2.0.0-py3-none-any.whl">adas_tsf-2.0.0-py3-none-any.whl</a>
</body></html>"""
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(html.encode('utf-8'))

        elif path == '/files/adas_tsf-2.0.0-py3-none-any.whl':
            content = b"fake-auth-wheel"
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        else:
            self.send_response(404)
            self.end_headers()


class TestMirrorWheels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), MockSimpleHandler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.base_path = self.test_dir.name
        self.wheels_dir = os.path.join(self.base_path, 'wheels')
        self.config_path = os.path.join(self.base_path, 'config.json')
        os.makedirs(self.wheels_dir, exist_ok=True)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_canonicalize_name(self):
        self.assertEqual(canonicalize_name('adas-tsf'), 'adas-tsf')
        self.assertEqual(canonicalize_name('adas_tsf'), 'adas-tsf')
        self.assertEqual(canonicalize_name('ADAS.TSF'), 'adas-tsf')
        self.assertEqual(canonicalize_name('adas---tsf'), 'adas-tsf')

    def test_parse_hash_fragment(self):
        algo, val = parse_hash_fragment('sha256=abcdef123456')
        self.assertEqual(algo, 'sha256')
        self.assertEqual(val, 'abcdef123456')

        algo, val = parse_hash_fragment('md5=0123456789abcdef&other=1')
        self.assertEqual(algo, 'md5')
        self.assertEqual(val, '0123456789abcdef')

        algo, val = parse_hash_fragment('')
        self.assertIsNone(algo)
        self.assertIsNone(val)

    def test_parse_link_info(self):
        base_page = 'https://example.com/simple/pkg/'
        info = parse_link_info(base_page, '../../files/pkg-1.0.whl#sha256=1234')
        self.assertIsNotNone(info)
        self.assertEqual(info['filename'], 'pkg-1.0.whl')
        self.assertEqual(info['url'], 'https://example.com/files/pkg-1.0.whl')
        self.assertEqual(info['hash_algo'], 'sha256')
        self.assertEqual(info['hash_value'], '1234')

        # Unsupported extension should return None
        bad_info = parse_link_info(base_page, 'pkg-1.0.exe')
        self.assertIsNone(bad_info)

    def test_normalize_source_config(self):
        # Plain string
        s1 = normalize_source_config('https://pypi.org/simple/')
        self.assertEqual(s1['url'], 'https://pypi.org/simple')
        self.assertIsNone(s1['username'])
        self.assertIsNone(s1['password'])
        self.assertTrue(s1['verify_ssl'])

        # String with credentials in URL
        s2 = normalize_source_config('https://usr:pwd@artifactory.local:8080/simple/')
        self.assertEqual(s2['url'], 'https://artifactory.local:8080/simple')
        self.assertEqual(s2['username'], 'usr')
        self.assertEqual(s2['password'], 'pwd')

        # Dictionary format
        s3 = normalize_source_config({
            'url': 'https://myrepo.local/simple',
            'username': 'bob',
            'password': 'secret',
            'verify_ssl': False
        })
        self.assertEqual(s3['url'], 'https://myrepo.local/simple')
        self.assertEqual(s3['username'], 'bob')
        self.assertEqual(s3['password'], 'secret')
        self.assertFalse(s3['verify_ssl'])

    def test_build_request_auth(self):
        src_auth = {'username': 'testuser', 'password': 'testpass'}
        req = build_request('https://example.com', src_auth)
        expected_auth = 'Basic ' + base64.b64encode(b"testuser:testpass").decode('ascii')
        self.assertEqual(req.get_header('Authorization'), expected_auth)

        src_token = {'token': 'mytoken123'}
        req_token = build_request('https://example.com', src_token)
        self.assertEqual(req_token.get_header('Authorization'), 'Bearer mytoken123')

        src_user_token = {'username': 'testuser', 'token': 'mytoken123'}
        req_user_token = build_request('https://example.com', src_user_token)
        expected_user_token = 'Basic ' + base64.b64encode(b"testuser:mytoken123").decode('ascii')
        self.assertEqual(req_user_token.get_header('Authorization'), expected_user_token)

    def test_verify_file_hash(self):
        file_path = os.path.join(self.wheels_dir, 'sample.txt')
        content = b"sample content for hashing"
        with open(file_path, 'wb') as f:
            f.write(content)

        expected_sha = hashlib.sha256(content).hexdigest()
        self.assertTrue(verify_file_hash(file_path, 'sha256', expected_sha))
        self.assertFalse(verify_file_hash(file_path, 'sha256', 'wrong_hash'))

    def test_load_config_project_defaults(self):
        cfg = load_config()
        self.assertTrue(cfg.get('mirror_enabled'))
        self.assertTrue(isinstance(cfg.get('mirror_packages'), list))
        self.assertTrue(len(cfg.get('mirror_sources', [])) >= 1)
        self.assertTrue(os.path.isdir(cfg['wheels_dir']))

    def test_fetch_package_links_pep503_and_pep691(self):
        # PEP 503 HTML
        source_info = normalize_source_config(f'http://127.0.0.1:{self.port}/simple')
        links = fetch_package_links(source_info, 'adas-tsf')
        filenames = [l['filename'] for l in links]
        self.assertIn('adas_tsf-1.0.0-py3-none-any.whl', filenames)
        self.assertIn('adas_tsf-1.0.0.tar.gz', filenames)
        self.assertNotIn('readme.txt', filenames)

        # PEP 691 JSON
        json_links = fetch_package_links(source_info, 'json-pkg')
        json_filenames = [l['filename'] for l in json_links]
        self.assertIn('json_pkg-0.1.0-py3-none-any.whl', json_filenames)

    def test_fetch_and_download_authenticated(self):
        source_info = normalize_source_config({
            'url': f'http://127.0.0.1:{self.port}/auth-simple',
            'username': 'myuser',
            'password': 'mypassword'
        })
        links = fetch_package_links(source_info, 'adas-tsf')
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['filename'], 'adas_tsf-2.0.0-py3-none-any.whl')

        downloaded = download_distribution(links[0], source_info, self.wheels_dir)
        self.assertTrue(downloaded)

        saved_path = os.path.join(self.wheels_dir, 'adas_tsf-2.0.0-py3-none-any.whl')
        self.assertTrue(os.path.isfile(saved_path))
        with open(saved_path, 'rb') as f:
            self.assertEqual(f.read(), b"fake-auth-wheel")

        # Second download should skip since file already exists
        downloaded_again = download_distribution(links[0], source_info, self.wheels_dir)
        self.assertFalse(downloaded_again)

    def test_perform_mirroring(self):
        config = {
            'wheels_dir': self.wheels_dir,
            'mirror_packages': ['adas-tsf'],
            'mirror_sources': [f'http://127.0.0.1:{self.port}/simple'],
            'mirror_enabled': True
        }
        count = perform_mirroring(config=config)
        self.assertEqual(count, 2)  # 1 wheel + 1 tar.gz

        whl_path = os.path.join(self.wheels_dir, 'adas_tsf-1.0.0-py3-none-any.whl')
        tar_path = os.path.join(self.wheels_dir, 'adas_tsf-1.0.0.tar.gz')
        self.assertTrue(os.path.isfile(whl_path))
        self.assertTrue(os.path.isfile(tar_path))

        # Re-running should download 0 new files
        count_second = perform_mirroring(config=config)
        self.assertEqual(count_second, 0)

    def test_start_mirror_process(self):
        custom_config = {
            'wheels_dir': self.wheels_dir,
            'mirror_packages': ['adas-tsf'],
            'mirror_sources': [f'http://127.0.0.1:{self.port}/simple'],
            'mirror_enabled': True,
            'mirror_interval_seconds': 5,
            'mirror_run_on_startup': False
        }
        with open(self.config_path, 'w') as f:
            json.dump(custom_config, f)

        proc, stop_event = start_mirror_process(config_path=self.config_path)
        self.assertIsNotNone(proc)
        self.assertIsNotNone(stop_event)
        self.assertTrue(proc.is_alive())

        time.sleep(0.5)
        stop_event.set()
        proc.join(timeout=3)
        self.assertFalse(proc.is_alive())

    def test_parent_alive_detection(self):
        self.assertTrue(is_parent_alive(os.getpid()))
        self.assertFalse(is_parent_alive(999999))


if __name__ == '__main__':
    unittest.main()
