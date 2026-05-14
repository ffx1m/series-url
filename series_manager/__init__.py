import os
import threading
from hmac import compare_digest

from flask import Flask, Response, request

from .api.routes import api_bp
from .web.routes import web_bp
from .db import connect_mongodb
from .jobs.worker import worker_loop


def require_admin_auth(app):
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        return

    username = os.environ.get("ADMIN_USERNAME", "admin")

    @app.before_request
    def check_admin_auth():
        if request.endpoint == "static":
            return None
        auth = request.authorization
        if (
            auth
            and compare_digest(auth.username or "", username)
            and compare_digest(auth.password or "", password)
        ):
            return None
        return Response(
            "Authentication required",
            401,
            {"WWW-Authenticate": 'Basic realm="Series Manager"'},
        )


def create_app():
    app = Flask(
        __name__,
        template_folder=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "templates")),
    )
    app.config["UPLOAD_FOLDER"] = os.environ.get("UPLOAD_FOLDER", "data/uploads")
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", 16 * 1024 * 1024))

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs("data", exist_ok=True)

    connect_mongodb()
    require_admin_auth(app)

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    if os.environ.get("START_EMBEDDED_WORKER", "true").lower() == "true":
        thread = threading.Thread(target=worker_loop, name="job-worker", daemon=True)
        thread.start()

    return app
