import os
import sys
import logging
from pypiserver import app
from waitress import serve

# Enable ALL logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)

# Enable Waitress logging
waitress_logger = logging.getLogger('waitress')
waitress_logger.setLevel(logging.INFO)

if not os.path.isdir('wheels'):
    os.makedirs('wheels')

application = app(
    roots=".wheels",
    verbosity=3,
    disable_fallback=True
)

print("=" * 60)
print("PyPI Server starting on http://localhost:8080")
print("=" * 60)

serve(application, host='0.0.0.0', port=8080, threads=6)
