"""locol_vocol: reference voice clips used to recognise people across recordings.

The transcription engines this fork uses (ElevenLabs Scribe and friends) diarize
within one file but never return voice embeddings. Instead of a local
embedding model, a short clip of every known person is prepended to the audio
before it is sent for transcription; whichever diarization label the engine
gives the clip is then the label of that person in the meeting.

One row = one clip file: up to ~30 s of one person's speech assembled from the
cleanest segments of one recording, stored as 16 kHz mono WAV under
UPLOAD_FOLDER/voice_clips/<user_id>/. A person may own several clips (one per
recording they were named in) so different rooms, microphones and days are all
represented; the newest clips are preferred when building the prefix.
"""

from datetime import datetime

from src.database import db


class SpeakerReferenceClip(db.Model):
    __tablename__ = 'speaker_reference_clip'
    __table_args__ = (
        db.UniqueConstraint('speaker_id', 'recording_id', name='uq_reference_clip_speaker_recording'),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    speaker_id = db.Column(db.Integer, db.ForeignKey('speaker.id', ondelete='CASCADE'), nullable=False, index=True)
    # The recording the clip was cut from. NULL once that recording is deleted:
    # the clip file is a separate copy and keeps working.
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id', ondelete='SET NULL'), nullable=True, index=True)
    label = db.Column(db.String(100), nullable=True)      # diarization label the person had in that recording
    file_key = db.Column(db.String(512), nullable=False)  # path relative to UPLOAD_FOLDER
    duration = db.Column(db.Float, nullable=False)        # seconds of audio in the clip file
    pieces = db.Column(db.JSON, nullable=True)            # [[start, end], ...] in the source recording
    source = db.Column(db.String(20), nullable=False, default='confirmed')  # confirmed | backfill
    hit_count = db.Column(db.Integer, nullable=False, default=0)   # times this clip identified the person
    last_hit_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    speaker = db.relationship('Speaker', backref=db.backref('reference_clips', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        return {
            'id': self.id,
            'speaker_id': self.speaker_id,
            'recording_id': self.recording_id,
            'label': self.label,
            'duration': round(self.duration or 0.0, 1),
            'pieces': self.pieces or [],
            'source': self.source,
            'hit_count': self.hit_count or 0,
            'last_hit_at': self.last_hit_at.isoformat() if self.last_hit_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"SpeakerReferenceClip(id={self.id}, speaker_id={self.speaker_id}, recording_id={self.recording_id}, {self.duration}s)"
