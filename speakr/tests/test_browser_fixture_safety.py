"""The browser-test seeder must reject production paths before importing the app."""
from pathlib import Path
import runpy

import pytest


@pytest.fixture
def seed():
    script = Path(__file__).parent / 'e2e' / 'seed_browser_recordings.py'
    return runpy.run_path(str(script))['main']


def test_fixtures_require_explicit_opt_in(seed, monkeypatch, tmp_path):
    monkeypatch.delenv('SPEAKR_BROWSER_FIXTURES', raising=False)
    monkeypatch.setenv('SQLALCHEMY_DATABASE_URI', f'sqlite:///{tmp_path}/test.db')
    with pytest.raises(SystemExit, match='SPEAKR_BROWSER_FIXTURES=1'):
        seed()


def test_fixtures_reject_a_production_database(seed, monkeypatch, tmp_path):
    monkeypatch.setenv('SPEAKR_BROWSER_FIXTURES', '1')
    monkeypatch.setenv('SQLALCHEMY_DATABASE_URI', 'sqlite:////data/instance/transcriptions.db')
    monkeypatch.setenv('UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    with pytest.raises(SystemExit, match='outside production data'):
        seed()


def test_fixtures_reject_production_uploads(seed, monkeypatch, tmp_path):
    monkeypatch.setenv('SPEAKR_BROWSER_FIXTURES', '1')
    monkeypatch.setenv('SQLALCHEMY_DATABASE_URI', f'sqlite:///{tmp_path}/test.db')
    monkeypatch.setenv('UPLOAD_FOLDER', '/data/uploads')
    with pytest.raises(SystemExit, match='audio uploads must also live under the temporary directory'):
        seed()
