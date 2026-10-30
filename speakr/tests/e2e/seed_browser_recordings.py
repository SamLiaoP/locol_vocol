"""Seed synthetic audio/transcripts in a disposable browser-test database.

The guard runs before importing the app: never point this script at real data.
run_speakr.py calls it with its temporary SQLite database. The image smoke job
can also call it with SPEAKR_BROWSER_FIXTURES=1 in a disposable container.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import wave


def main():
    uri = os.environ.get('SQLALCHEMY_DATABASE_URI', '')
    if os.environ.get('SPEAKR_BROWSER_FIXTURES') != '1' or not uri.startswith('sqlite:///'):
        raise SystemExit('Synthetic fixtures require SPEAKR_BROWSER_FIXTURES=1 and an explicit SQLite database')
    database = Path(uri.removeprefix('sqlite:///')).resolve()
    temporary_roots = (Path(tempfile.gettempdir()).resolve(), Path('/tmp').resolve())
    if not any(database.is_relative_to(root) for root in temporary_roots):
        raise SystemExit('Synthetic fixtures must live under the temporary directory, outside production data')
    uploads = os.environ.get('UPLOAD_FOLDER')
    if not uploads or not any(Path(uploads).resolve().is_relative_to(root) for root in temporary_roots):
        raise SystemExit('Synthetic audio uploads must also live under the temporary directory')

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.app import app, db
    from src.models import User, Recording, Speaker

    with app.app_context():
        user = User.query.filter_by(email=os.environ.get('ADMIN_EMAIL')).one()
        user.ui_language = 'zh'
        audio = Path(app.config['UPLOAD_FOLDER']) / 'ui-smoke.wav'
        audio.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(audio), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b'\0\0' * 16000 * 40)
        segments = [
            {'speaker': '小林', 'sentence': '今天先確認一下產品的使用流程。', 'start_time': 0, 'end_time': 8},
            {'speaker': '小陳', 'sentence': '上傳錄音後，就能閱讀、播放與校訂逐字稿。', 'start_time': 8, 'end_time': 18},
            {'speaker': '小林', 'sentence': '我們也需要保留說話者的名字，方便下次辨識。', 'start_time': 18, 'end_time': 29},
            {'speaker': '阿哲', 'sentence': '好，下週再一起看看使用回饋。', 'start_time': 29, 'end_time': 39},
        ]
        for number in (1, 2):
            title = f'UI smoke fixture {number}'
            recording = Recording.query.filter_by(user_id=user.id, title=title).first()
            if recording is None:
                db.session.add(Recording(
                    user_id=user.id, title=title, title_source='user', status='COMPLETED',
                    audio_path=str(audio), original_filename='ui-smoke.wav', mime_type='audio/wav',
                    audio_duration_seconds=40, is_inbox=False,
                    transcription=json.dumps(segments, ensure_ascii=False),
                ))
        for name in ('小林', '小陳', '阿哲'):
            if not Speaker.query.filter_by(user_id=user.id, name=name).first():
                db.session.add(Speaker(user_id=user.id, name=name, use_count=2))
        db.session.commit()


if __name__ == '__main__':
    main()
