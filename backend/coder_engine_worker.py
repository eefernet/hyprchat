"""Separate modern worker process; never imports the legacy SDK adapter at startup."""
import importlib.metadata
import os
from pathlib import Path

from fastapi import FastAPI
from coder_worker_runtime import install_routes
from coder_engine_runtime import ENGINE, SDK_VERSION
from coder_worker_runtime import WorkerStore
from coder_preview import install_routes as install_preview_routes

app = FastAPI(title='Daedalus engine worker')
projects = Path(os.environ.get('DAEDALUS_PROJECTS_DIR', '/opt/daedalus-engine/projects'))
install_routes(app, projects, engine=ENGINE)
install_preview_routes(app, WorkerStore(os.environ.get('DAEDALUS_STATE_DIR', str(projects.parent / '.daedalus'))))


@app.get('/health')
def health():
    actual = importlib.metadata.version('openhands-sdk')
    return {'status': 'ok' if actual == SDK_VERSION else 'incompatible',
            'engine': ENGINE, 'sdk_version': actual}

# Explicitly enabled only by the isolated release unit.
from coder_engine_resources import initialize
initialize()
