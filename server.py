import os
import sys
import logging
from pypiserver import app
from waitress import serve
from backup import start_backup_process

# Enable ALL logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)

# Enable Waitress logging
waitress_logger = logging.getLogger('waitress')
waitress_logger.setLevel(logging.INFO)

backup_logger = logging.getLogger('backup')
backup_logger.setLevel(logging.INFO)

if not os.path.isdir('wheels'):
    os.makedirs('wheels')

application = app(
    roots=["wheels"],
    verbosity=3,
    disable_fallback=True,
    password_file='.htpasswd',
    authenticate=['update']
)

if __name__ == '__main__':
    print("=" * 60)
    print("PyPI Server starting on http://localhost:8080")
    print("=" * 60)

    backup_proc, stop_event = start_backup_process()
    try:
        serve(application, host='0.0.0.0', port=8080, threads=6)
    except KeyboardInterrupt:
        print("\nPyPI Server interrupted, shutting down...")
    finally:
        stop_event.set()
        backup_proc.join(timeout=3)
        if backup_proc.is_alive():
            backup_proc.terminate()

