# PythonAnywhere WSGI entry point
import sys, os

# Make sure the app folder is on the path
path = os.path.dirname(os.path.abspath(__file__))
if path not in sys.path:
    sys.path.insert(0, path)

# Set environment variables here (PythonAnywhere doesn't have a .env)
os.environ.setdefault('SECRET_KEY', 'CHANGE-THIS-TO-SOMETHING-RANDOM-AND-LONG')
os.environ.setdefault('DB_PATH', os.path.join(path, 'portfolio.db'))

from app import app as application  # noqa
