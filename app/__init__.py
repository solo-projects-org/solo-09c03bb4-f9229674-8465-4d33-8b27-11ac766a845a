"""Flask 应用工厂。"""
from __future__ import annotations

import os

from flask import Flask


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-quality-control-secret"),
        DB_PATH=os.environ.get(
            "DB_PATH", os.path.join(os.getcwd(), "data", "qc.db")
        ),
    )
    if test_config:
        app.config.update(test_config)

    from . import db

    db.init_app(app)

    from .routes import bp

    app.register_blueprint(bp)
    return app
