import os
import sys
import sqlite3
import pytest
from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Fixtures compartidos (BD en memoria, app Flask)

# '''
#  Fixture app → instancia Flask configurada para testing
# - Fixture client → app.test_client() listo para requests HTTP
# - Fixture db → SQLite en memoria con tablas creadas y datos de prueba
# '''


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def app():
    from dashboard import app as flask_app
    flask_app.config["TESTING"] = True
    flask_app.config["SERVER_NAME"] = "localhost"
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()
