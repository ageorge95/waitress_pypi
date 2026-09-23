import os
import sys
import time
import json
import logging
import hashlib
import ssl
import re
import base64
import multiprocessing
import urllib.request
import urllib.parse
import urllib.error
from html.parser import HTMLParser

logger = logging.getLogger('mirror_wheels')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

SUPPORTED_EXTENSIONS = (
    '.whl',
    '.tar.gz',
    '.zip',
    '.tar.bz2',
    '.tar.xz',
    '.tgz'
)


def load_config(config_path=CONFIG_FILE):
    """Load configuration directly from config.json as the single source of truth."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    if not isinstance(config, dict):
        raise ValueError(f"Configuration file {config_path} must contain a JSON object")

    # Resolve relative paths against BASE_DIR
    if 'output_dir' in config and not os.path.isabs(config['output_dir']):
        config['output_dir'] = os.path.abspath(os.path.join(BASE_DIR, config['output_dir']))
    if 'wheels_dir' in config and not os.path.isabs(config['wheels_dir']):
        config['wheels_dir'] = os.path.abspath(os.path.join(BASE_DIR, config['wheels_dir']))
    elif 'wheels_dir' not in config:
        config['wheels_dir'] = os.path.abspath(os.path.join(BASE_DIR, 'wheels'))

    return config


def canonicalize_name(name):
    """Normalize package name per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


class SimpleIndexParser(HTMLParser):
    """HTML parser to extract download links from a PEP 503 simple repository page."""

    def __init__(self):
        super().__init__()
        self.links = []
        self._current_href = None
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'a':
            attr_dict = dict(attrs)
            self._current_href = attr_dict.get('href')
            self._current_text = []

    def handle_data(self, data):
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == 'a' and self._current_href is not None:
            self.links.append({
                'href': self._current_href,
                'text': ''.join(self._current_text).strip()
            })
            self._current_href = None
            self._current_text = []


def parse_hash_fragment(fragment):
    """Extract hash algorithm and value from URL fragment (e.g. #sha256=abcdef...)."""
    if not fragment:
        return None, None
    for part in fragment.split('&'):
        if '=' in part:
            algo, val = part.split('=', 1)
            algo = algo.lower()
            if algo in ('sha256', 'sha512', 'sha384', 'sha224', 'sha1', 'md5'):
                return algo, val.lower()
    return None, None


def parse_link_info(page_url, href, link_text=''):
    """Resolve link and extract filename, hash, and whether it is a supported distribution."""
    resolved_url = urllib.parse.urljoin(page_url, href)
    split_url = urllib.parse.urlsplit(resolved_url)
    filename = urllib.parse.unquote(os.path.basename(split_url.path))
    if not filename and link_text:
        filename = link_text

    lower_fn = filename.lower()
    if not any(lower_fn.endswith(ext) for ext in SUPPORTED_EXTENSIONS):
        return None

    hash_algo, hash_value = parse_hash_fragment(split_url.fragment)
    clean_url = urllib.parse.urlunsplit((split_url.scheme, split_url.netloc, split_url.path, split_url.query, ''))

    return {
        'url': clean_url,
        'filename': filename,
        'hash_algo': hash_algo,
        'hash_value': hash_value
    }


def normalize_source_config(source):
    """Normalize a source string or dictionary into a standard dictionary structure."""
    if isinstance(source, str):
        url = source
        username = None
        password = None
        token = None
        verify_ssl = True
    elif isinstance(source, dict):
        url = source.get('url', '')
        username = source.get('username') or None
        password = source.get('password') or None
        token = source.get('token') or None
        verify_ssl = source.get('verify_ssl', True)
    else:
        raise ValueError(f"Invalid source configuration: {source}")

    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        username = username or parsed.username
        password = password or parsed.password
        netloc = parsed.hostname
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        url = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    return {
        'url': url.rstrip('/'),
        'username': username,
        'password': password,
        'token': token,
        'verify_ssl': verify_ssl
    }


def get_ssl_context(verify_ssl=True):
    """Create an SSL context, disabling verification if requested for corporate environments."""
    if verify_ssl:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def build_request(url, source_info):
    """Build an HTTP Request with required headers and authentication."""
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'pip/24.0 mirror_wheels/1.0',
            'Accept': 'application/vnd.pypi.simple.v1+json, text/html;q=0.9, */*;q=0.8'
        }
    )
    if source_info.get('username') and source_info.get('password'):
        user_pass = f"{source_info['username']}:{source_info['password']}"
        encoded = base64.b64encode(user_pass.encode('utf-8')).decode('ascii')
        req.add_header('Authorization', f'Basic {encoded}')
    elif source_info.get('token'):
        req.add_header('Authorization', f"Bearer {source_info['token']}")
    return req


def verify_file_hash(file_path, algo, expected_hash):
    """Verify that a local file matches an expected cryptographic hash."""
    try:
        h = hashlib.new(algo)
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest().lower() == expected_hash.lower()
    except Exception as e:
        logger.warning(f"Error computing {algo} hash for {file_path}: {e}")
        return False


def fetch_package_links(source_info, package_name, timeout=30):
    """
    Query the simple index repository for distribution files belonging to package_name.
    Supports PEP 503 HTML and PEP 691 JSON.
    """
    base_url = source_info['url']
    norm_pkg = canonicalize_name(package_name)
    ssl_ctx = get_ssl_context(source_info.get('verify_ssl', True))

    candidate_paths = [
        f"{base_url}/{norm_pkg}/",
        f"{base_url}/{norm_pkg}",
        f"{base_url}/{package_name}/",
        f"{base_url}/{package_name}"
    ]
    seen = set()
    candidates = [p for p in candidate_paths if not (p in seen or seen.add(p))]

    resp_data = None
    content_type = ''
    final_page_url = None

    for cand_url in candidates:
        req = build_request(cand_url, source_info)
        try:
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=timeout) as response:
                resp_data = response.read()
                content_type = response.headers.get('Content-Type', '')
                final_page_url = response.geturl()
                break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            logger.warning(f"HTTP {e.code} while querying {cand_url}: {e.reason}")
            return []
        except Exception as e:
            logger.warning(f"Connection error accessing {cand_url}: {e}")
            return []

    if resp_data is None:
        logger.debug(f"Package '{package_name}' not found at {base_url}")
        return []

    links = []

    # Check for PEP 691 JSON response
    if 'application/vnd.pypi.simple.v1+json' in content_type or content_type.startswith('application/json'):
        try:
            data = json.loads(resp_data.decode('utf-8'))
            files = data.get('files', [])
            for item in files:
                fn = item.get('filename')
                u = item.get('url')
                if not fn or not u:
                    continue
                lower_fn = fn.lower()
                if not any(lower_fn.endswith(ext) for ext in SUPPORTED_EXTENSIONS):
                    continue
                resolved = urllib.parse.urljoin(final_page_url, u)
                hashes = item.get('hashes', {})
                hash_algo = None
                hash_val = None
                if 'sha256' in hashes:
                    hash_algo, hash_val = 'sha256', hashes['sha256']
                elif hashes:
                    hash_algo, hash_val = next(iter(hashes.items()))
                links.append({
                    'url': resolved,
                    'filename': fn,
                    'hash_algo': hash_algo,
                    'hash_value': hash_val
                })
            return links
        except Exception as e:
            logger.warning(f"Failed to parse PEP 691 JSON from {final_page_url}: {e}")

    # Standard PEP 503 HTML parsing
    try:
        html_text = resp_data.decode('utf-8', errors='replace')
        parser = SimpleIndexParser()
        parser.feed(html_text)
        for item in parser.links:
            info = parse_link_info(final_page_url, item['href'], item['text'])
            if info:
                links.append(info)
    except Exception as e:
        logger.warning(f"Failed to parse HTML from {final_page_url}: {e}")

    return links


def download_distribution(link_info, source_info, wheels_dir, timeout=60, stop_event=None):
    """
    Download a package distribution to wheels_dir.
    Skips if the file exists and is valid. Uses a temporary file and atomic replace.
    """
    filename = link_info['filename']
    dest_path = os.path.join(wheels_dir, filename)
    temp_path = f"{dest_path}.tmp"

    hash_algo = link_info.get('hash_algo')
    hash_value = link_info.get('hash_value')

    if os.path.isfile(dest_path):
        if hash_algo and hash_value:
            if verify_file_hash(dest_path, hash_algo, hash_value):
                logger.debug(f"Skipping {filename}: already exists with valid hash.")
                return False
            logger.warning(f"Existing file {filename} hash mismatch. Re-downloading...")
        else:
            if os.path.getsize(dest_path) > 0:
                logger.debug(f"Skipping {filename}: already exists locally.")
                return False

    os.makedirs(wheels_dir, exist_ok=True)

    url = link_info['url']
    source_netloc = urllib.parse.urlsplit(source_info['url']).netloc
    file_netloc = urllib.parse.urlsplit(url).netloc

    # Only pass auth header if host matches the source host
    download_source_info = source_info if source_netloc == file_netloc else {
        'url': url,
        'username': None,
        'password': None,
        'token': None,
        'verify_ssl': source_info.get('verify_ssl', True)
    }

    req = build_request(url, download_source_info)
    ssl_ctx = get_ssl_context(download_source_info.get('verify_ssl', True))

    logger.info(f"Downloading {filename} from {url}...")
    hasher = hashlib.new(hash_algo) if (hash_algo and hash_value) else None

    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=timeout) as response:
            with open(temp_path, 'wb') as f:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        logger.info(f"Download cancelled for {filename}.")
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                        return False
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    if hasher:
                        hasher.update(chunk)

        if hasher and hash_value:
            computed = hasher.hexdigest().lower()
            if computed != hash_value.lower():
                logger.error(f"Hash mismatch for {filename}! Expected {hash_value}, got {computed}")
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                return False

        os.replace(temp_path, dest_path)
        logger.info(f"Successfully saved {filename} to {wheels_dir}")
        return True
    except Exception as e:
        logger.error(f"Failed to download {filename} from {url}: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return False


def perform_mirroring(config=None, stop_event=None):
    """Run a single mirroring cycle across all configured sources and packages."""
    if config is None:
        config = load_config()

    wheels_dir = config.get('wheels_dir', os.path.join(BASE_DIR, 'wheels'))
    packages = config.get('mirror_packages', [])
    raw_sources = config.get('mirror_sources', [])

    if not packages:
        logger.info("No packages configured to mirror in mirror_packages.")
        return 0

    if not raw_sources:
        logger.info("No sources configured to mirror from in mirror_sources.")
        return 0

    sources = [normalize_source_config(s) for s in raw_sources]
    total_downloaded = 0

    logger.info(f"Starting mirroring run for {len(packages)} package(s) across {len(sources)} source(s)...")

    for pkg in packages:
        if stop_event is not None and stop_event.is_set():
            break
        for src in sources:
            if stop_event is not None and stop_event.is_set():
                break
            logger.info(f"Querying source {src['url']} for package '{pkg}'...")
            try:
                links = fetch_package_links(src, pkg)
                logger.info(f"Found {len(links)} distribution(s) for '{pkg}' at {src['url']}")
                for link in links:
                    if stop_event is not None and stop_event.is_set():
                        break
                    if download_distribution(link, src, wheels_dir, stop_event=stop_event):
                        total_downloaded += 1
            except Exception as e:
                logger.error(f"Error querying {src['url']} for '{pkg}': {e}", exc_info=True)

    logger.info(f"Mirroring run complete. Downloaded {total_downloaded} new distribution(s).")
    return total_downloaded


def is_parent_alive(parent_pid):
    """Check if the parent process is still alive."""
    if parent_pid is None or parent_pid <= 0:
        return True
    if sys.platform == 'win32':
        import ctypes
        synchronize = 0x00100000
        process_query_limited_info = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            synchronize | process_query_limited_info, False, parent_pid
        )
        if not handle:
            return False
        try:
            ret = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
            return ret != 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(parent_pid, 0)
            return True
        except OSError:
            return False


def wait_interval(stop_event, timeout_seconds, parent_pid=None):
    """Wait for timeout_seconds or until stop_event is set / parent process exits."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return True
        if parent_pid is not None and not is_parent_alive(parent_pid):
            logger.info("Parent process has exited. Stopping mirror worker.")
            return True
        remaining = max(0.0, deadline - time.time())
        step = min(1.0, remaining)
        if stop_event is not None:
            if stop_event.wait(timeout=step):
                return True
        else:
            time.sleep(step)
    return False


def mirror_worker(stop_event=None, parent_pid=None):
    """Worker loop running periodic mirroring."""
    if parent_pid is None:
        parent_pid = os.getppid()

    logger.info(f"Mirror worker process started (monitoring parent PID {parent_pid}).")
    config = load_config()

    if config.get('mirror_run_on_startup', True) and config.get('mirror_enabled', True):
        try:
            perform_mirroring(config=config, stop_event=stop_event)
        except Exception as e:
            logger.error(f"Initial mirroring execution failed: {e}", exc_info=True)

    while True:
        config = load_config()
        if not config.get('mirror_enabled', True):
            interval_seconds = 60.0
        else:
            interval_seconds = config.get('mirror_interval_seconds')
            if interval_seconds is None:
                interval_minutes = config.get('mirror_interval_minutes')
                if interval_minutes is not None:
                    interval_seconds = max(1.0, float(interval_minutes) * 60)
                else:
                    interval_hours = float(config.get('mirror_interval_hours', 1.0))
                    interval_seconds = max(1.0, interval_hours * 3600)

        should_stop = wait_interval(stop_event, interval_seconds, parent_pid)
        if should_stop:
            break

        if config.get('mirror_enabled', True):
            try:
                perform_mirroring(config=config, stop_event=stop_event)
            except Exception as e:
                logger.error(f"Periodic mirroring execution failed: {e}", exc_info=True)

    logger.info("Mirror worker process stopped.")


def start_mirror_process():
    """Start the mirror worker as a separate multiprocessing Process."""
    config = load_config()
    if not config.get('mirror_enabled', True):
        logger.info("Mirror worker is disabled in configuration.")
        return None, None

    stop_event = multiprocessing.Event()
    parent_pid = os.getpid()
    process = multiprocessing.Process(
        target=mirror_worker,
        args=(stop_event, parent_pid),
        name='MirrorProcess',
        daemon=True
    )
    process.start()
    return process, stop_event


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        stream=sys.stdout
    )
    mirror_worker()
