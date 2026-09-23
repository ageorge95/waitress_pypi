import os
import sys
import time
import json
import logging
import zipfile
import multiprocessing
from datetime import datetime

logger = logging.getLogger('backup')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')


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

    return config


def create_zip_archive(source_dir, output_dir):
    """Archive source_dir into a zip file named with current timestamp."""
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(source_dir, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    archive_name = f"wheels_backup_{timestamp}.zip"
    final_path = os.path.join(output_dir, archive_name)
    temp_path = f"{final_path}.tmp"

    logger.info(f"Creating backup archive: {final_path}")
    with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(source_dir):
            for file in files:
                file_path = os.path.join(root, file)
                # Keep directory structure relative to parent of source_dir
                arcname = os.path.relpath(file_path, os.path.dirname(source_dir))
                zf.write(file_path, arcname)

    os.replace(temp_path, final_path)
    logger.info(f"Backup created successfully: {final_path}")
    return final_path


def cleanup_old_backups(output_dir, max_backups):
    """Retain only the latest max_backups files in output_dir, deleting older ones."""
    if not os.path.isdir(output_dir) or max_backups <= 0:
        return

    files = [
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if os.path.isfile(os.path.join(output_dir, f)) and not f.endswith('.tmp')
    ]

    # Sort files by modification time, oldest first
    files.sort(key=lambda p: os.path.getmtime(p))

    excess = len(files) - max_backups
    if excess > 0:
        for old_file in files[:excess]:
            try:
                os.remove(old_file)
                logger.info(f"Deleted old backup: {old_file}")
            except Exception as e:
                logger.error(f"Failed to delete old backup {old_file}: {e}")


def perform_backup():
    """Execute a single backup run and cleanup."""
    config = load_config()
    wheels_dir = config['wheels_dir']
    output_dir = config['output_dir']
    max_backups = int(config['max_backups'])

    try:
        create_zip_archive(wheels_dir, output_dir)
        cleanup_old_backups(output_dir, max_backups)
    except Exception as e:
        logger.error(f"Backup execution failed: {e}", exc_info=True)


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
            return ret != 0  # 0 indicates process has terminated
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
            logger.info("Parent process has exited. Stopping backup worker.")
            return True
        remaining = max(0.0, deadline - time.time())
        step = min(1.0, remaining)
        if stop_event is not None:
            if stop_event.wait(timeout=step):
                return True
        else:
            time.sleep(step)
    return False


def backup_worker(stop_event=None, parent_pid=None):
    """Worker loop running periodic backups."""
    if parent_pid is None:
        parent_pid = os.getppid()

    logger.info(f"Backup worker process started (monitoring parent PID {parent_pid}).")
    config = load_config()

    if config.get('run_on_startup', False):
        perform_backup()

    while True:
        config = load_config()
        # Support backup_interval_seconds for testing or backup_interval_hours
        interval_seconds = config.get('backup_interval_seconds')
        if interval_seconds is None:
            interval_hours = float(config['backup_interval_hours'])
            interval_seconds = max(1.0, interval_hours * 3600)

        should_stop = wait_interval(stop_event, interval_seconds, parent_pid)
        if should_stop:
            break

        perform_backup()

    logger.info("Backup worker process stopped.")


def start_backup_process():
    """Start the backup worker as a separate multiprocessing Process."""
    stop_event = multiprocessing.Event()
    parent_pid = os.getpid()
    process = multiprocessing.Process(
        target=backup_worker,
        args=(stop_event, parent_pid),
        name='BackupProcess',
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
    backup_worker()
