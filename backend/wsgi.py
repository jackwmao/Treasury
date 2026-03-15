from a2wsgi import ASGIMiddleware

from app.main import app as asgi_app

# WSGI entrypoint exposing the same FastAPI routes.
app = ASGIMiddleware(asgi_app)

