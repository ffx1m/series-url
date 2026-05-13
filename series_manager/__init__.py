import os
import threading

from flask import Flask

from .api.routes import api_bp
from .web.routes import web_bp
from .db import connect_mongodb
from .jobs.worker import worker_loop


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

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    if os.environ.get("START_EMBEDDED_WORKER", "true").lower() == "true":
        thread = threading.Thread(target=worker_loop, name="job-worker", daemon=True)
        thread.start()

    return app
