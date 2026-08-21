import sys
import os

path = '/home/sibirk/sibirk'
if path not in sys.path:
    sys.path.append(path)

from a2wsgi import ASGIMiddleware
from main import app as fastapi_app

# Обертка ASGI в WSGI
application = ASGIMiddleware(fastapi_app)