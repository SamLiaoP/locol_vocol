"""locol_vocol: recognise people across recordings with reference clips.

How it works (see models/reference_clip.py for the data side):

1. When a user names a diarization label (SPEAKER_03 -> "Max"), up to 30 s of
   that person's cleanest speech in the recording is cut into a clip file.
2. Before a new recording is transcribed, the clips of known people are
   concatenated in front of the audio, separated by a second of silence, and
   the composite is what the engine receives.
3. The engine diarizes the whole file. Whatever label it gives a person's
   clip is that person's label in the meeting, so those segments get the
   name. Prefix segments are dropped and all timestamps shifted back.

Measured with ElevenLabs Scribe v2: 30 s clips recorded on another day are
clustered with the right person; 10 s clips are not (they get absorbed by
the nearest big cluster), hence the clip length.

Safety rules: a label that two different people's clips land on is left
unnamed (ambiguous), and a person whose clip got no words is ignored.
"""

import json
import logging
import os
import shutil
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

from src.database import db
from src.models import Recording, Speaker, SpeakerReferenceClip
from src.utils.ffmpeg_utils import _run_ffmpeg_command

logger = logging.getLogger(__name__)

CLIP_TARGET_SECONDS = 30.0       # audio per clip file (one person, one recording)
CLIP_MAX_PIECE_SECONDS = 15.0    # longest piece taken from a single segment
CLIP_MIN_PIECE_SECONDS = 3.0     # segments shorter than this are ignored
CLIP_EDGE_TRIM_SECONDS = 0.3     # shaved off both ends of a segment (turn-taking bleed)
CLIP_MIN_TOTAL_SECONDS = 8.0     # no clip at all when a person has less than this
PREFIX_PER_PERSON_SECONDS = 30.0
PREFIX_MAX_PEOPLE = 8
PREFIX_GAP_SECONDS = 1.0
MAX_CLIPS_PER_SPEAKER = 6
CLIP_DIR = 'voice_clips'
SAMPLE_RATE = 16000


# ----------------------------------------------------------------- helpers

def _upload_root() -> str:
    try:
        root = current_app.config.get('UPLOAD_FOLDER')
    except RuntimeError:
        root = None
    return root or os.environ.get('UPLOAD_FOLDER', '/data/uploads')


def clip_abs_path(clip: SpeakerReferenceClip) -> str:
    return os.path.join(_upload_root(), *clip.file_key.split('/'))


def is_raw_label(value: Optional[str]) -> bool:
    from src.services.speaker import DEFAULT_LABEL
    return bool(value) and bool(DEFAULT_LABEL.match(str(value).strip()))


def _segments_of(recording: Recording) -> List[Dict[str, Any]]:
    try:
        data = json.loads(recording.transcription or '[]')
    except (TypeError, ValueError):
        return []
    return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []


def _get_or_create_speaker(user_id: int, name: str) -> Optional[Speaker]:
    from src.services.speaker import find_user_speaker
    name = (name or '').strip()
    if not name or is_raw_label(name):
        return None
    speaker = find_user_speaker(user_id, name)
    if speaker is None:
        now = datetime.utcnow()
        speaker = Speaker(name=name, user_id=user_id, use_count=0, created_at=now, last_used=now)
        db.session.add(speaker)
        db.session.flush()
    return speaker


def _audio_filters(idx: int) -> str:
    return f"[{idx}:a]aresample={SAMPLE_RATE},aformat=sample_fmts=s16:channel_layouts=mono[a{idx}]"


# ------------------------------------------------------------ clip cutting

def select_pieces(segments: List[Dict[str, Any]], speaker_value: str) -> List[Tuple[float, float]]:
    """Up to CLIP_TARGET_SECONDS of (start, end) pieces of one speaker.

    Longest segments first, each trimmed at both ends and capped by taking
    its middle part, so the pieces are clean single-voice speech.
    """
    candidates = []
    for seg in segments:
        if seg.get('speaker') != speaker_value:
            continue
        try:
            start, end = float(seg.get('start_time')), float(seg.get('end_time'))
        except (TypeError, ValueError):
            continue
        start, end = start + CLIP_EDGE_TRIM_SECONDS, end - CLIP_EDGE_TRIM_SECONDS
        if end - start >= CLIP_MIN_PIECE_SECONDS:
            candidates.append((end - start, start, end))
    candidates.sort(reverse=True)
    pieces, total = [], 0.0
    for length, start, end in candidates:
        if total >= CLIP_TARGET_SECONDS:
            break
        take = min(length, CLIP_MAX_PIECE_SECONDS, CLIP_TARGET_SECONDS - total)
        if take < CLIP_MIN_PIECE_SECONDS and total > 0:
            break
        mid = (start + end) / 2.0
        pieces.append((round(mid - take / 2.0, 3), round(mid + take / 2.0, 3)))
        total += take
    pieces.sort()
    return pieces if total >= CLIP_MIN_TOTAL_SECONDS else []


def _cut_pieces(src_path: str, pieces: List[Tuple[float, float]], out_path: str) -> None:
    cmd = ['ffmpeg', '-y', '-v', 'error', '-nostdin']
    for start, end in pieces:
        cmd += ['-ss', f'{start:.3f}', '-t', f'{end - start:.3f}', '-i', src_path]
    chain = ';'.join(_audio_filters(i) for i in range(len(pieces)))
    inputs = ''.join(f'[a{i}]' for i in range(len(pieces)))
    cmd += ['-filter_complex', f"{chain};{inputs}concat=n={len(pieces)}:v=0:a=1[out]",
            '-map', '[out]', '-ar', str(SAMPLE_RATE), '-ac', '1', '-c:a', 'pcm_s16le', out_path]
    _run_ffmpeg_command(cmd, f"reference clip from {os.path.basename(src_path)}")


def _local_audio_path(recording: Recording):
    """Context manager yielding the recording's audio as a local file path."""
    from src.services.storage import get_storage_service
    return get_storage_service().materialize(recording.audio_path)


def delete_clip(clip: SpeakerReferenceClip) -> None:
    try:
        os.remove(clip_abs_path(clip))
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Reference clip file not removed (%s): %s", clip.file_key, e)
    db.session.delete(clip)


def _prune(speaker: Speaker) -> None:
    clips = sorted(SpeakerReferenceClip.query.filter_by(speaker_id=speaker.id).all(),
                   key=lambda c: c.created_at or datetime.min)
    while len(clips) > MAX_CLIPS_PER_SPEAKER:
        victim = next((c for c in clips if not c.hit_count), clips[0])
        clips.remove(victim)
        delete_clip(victim)


def create_clip(recording: Recording, speaker: Speaker, speaker_value: str, segments=None,
                label: Optional[str] = None, source: str = 'confirmed',
                local_path: Optional[str] = None) -> Optional[SpeakerReferenceClip]:
    """Cut (or replace) the clip of `speaker` from `recording`.

    speaker_value is what the transcript currently shows for the person
    (their name after a rename); label is the diarization label they had.
    Does not commit.
    """
    segments = _segments_of(recording) if segments is None else segments
    pieces = select_pieces(segments, speaker_value)
    if not pieces:
        return None
    rel_dir = f"{CLIP_DIR}/{recording.user_id}"
    os.makedirs(os.path.join(_upload_root(), rel_dir), exist_ok=True)
    file_key = f"{rel_dir}/{speaker.id}_{recording.id}_{uuid.uuid4().hex[:8]}.wav"
    out_path = os.path.join(_upload_root(), *file_key.split('/'))
    if local_path:
        _cut_pieces(local_path, pieces, out_path)
    else:
        with _local_audio_path(recording) as materialized:
            _cut_pieces(materialized.local_path, pieces, out_path)
    for old in SpeakerReferenceClip.query.filter_by(speaker_id=speaker.id, recording_id=recording.id).all():
        delete_clip(old)
    db.session.flush()
    clip = SpeakerReferenceClip(
        user_id=recording.user_id, speaker_id=speaker.id, recording_id=recording.id,
        label=label, file_key=file_key, duration=round(sum(e - s for s, e in pieces), 3),
        pieces=[[s, e] for s, e in pieces], source=source)
    db.session.add(clip)
    db.session.flush()
    _prune(speaker)
    logger.info("Reference clip: %s <- recording %s (%.1fs, %d pieces)", speaker.name, recording.id, clip.duration, len(pieces))
    return clip


def move_clips(sources: List[Speaker], target: Speaker) -> None:
    """Give the target every clip of the sources (speaker merge)."""
    taken = {c.recording_id for c in SpeakerReferenceClip.query.filter_by(speaker_id=target.id).all()}
    for source in sources:
        for clip in SpeakerReferenceClip.query.filter_by(speaker_id=source.id).all():
            if clip.recording_id is not None and clip.recording_id in taken:
                delete_clip(clip)          # target already has this recording covered
            else:
                clip.speaker_id = target.id
                taken.add(clip.recording_id)
    db.session.flush()
    _prune(target)


def delete_clips_for_user(user_id: int) -> int:
    clips = SpeakerReferenceClip.query.filter_by(user_id=user_id).all()
    for clip in clips:
        delete_clip(clip)
    return len(clips)


# ------------------------------------------------------------- the prefix

@dataclass
class PrefixPlan:
    input_path: str                 # composite file handed to the connector
    total: float                    # seconds of prefix (clips + gaps) before the real audio
    slots: List[Dict[str, Any]] = field(default_factory=list)   # {speaker_id, name, clip_id, start, end}
    people: List[int] = field(default_factory=list)             # speaker ids in the prefix


def _choose_clips(user_id: int, exclude_recording_id: Optional[int]) -> List[Tuple[Speaker, List[SpeakerReferenceClip]]]:
    rows = (db.session.query(SpeakerReferenceClip, Speaker)
            .join(Speaker, Speaker.id == SpeakerReferenceClip.speaker_id)
            .filter(SpeakerReferenceClip.user_id == user_id).all())
    by_speaker: Dict[int, Tuple[Speaker, List[SpeakerReferenceClip]]] = {}
    for clip, speaker in rows:
        if exclude_recording_id and clip.recording_id == exclude_recording_id:
            continue
        if not os.path.exists(clip_abs_path(clip)):
            continue
        by_speaker.setdefault(speaker.id, (speaker, []))[1].append(clip)
    people = sorted(by_speaker.values(), key=lambda sc: sc[0].last_used or datetime.min, reverse=True)
    chosen = []
    for speaker, clips in people[:PREFIX_MAX_PEOPLE]:
        clips.sort(key=lambda c: (c.created_at or datetime.min), reverse=True)   # newest voice first
        picked, seconds = [], 0.0
        for clip in clips:
            if seconds >= PREFIX_PER_PERSON_SECONDS:
                break
            picked.append(clip)
            seconds += clip.duration or 0.0
        if picked:
            chosen.append((speaker, picked))
    return chosen


def prepare_input(recording: Recording, local_audio_path: str) -> Tuple[Optional[PrefixPlan], Optional[str]]:
    """Build the composite file for a recording. (None, None) when there is nothing to prefix."""
    chosen = _choose_clips(recording.user_id, recording.id)
    if not chosen:
        return None, None
    workdir = tempfile.mkdtemp(prefix='refclips_')
    try:
        cmd = ['ffmpeg', '-y', '-v', 'error', '-nostdin']
        slots, t, inputs = [], 0.0, []
        idx = 0
        for speaker, clips in chosen:
            for clip in clips:
                cmd += ['-i', clip_abs_path(clip)]
                slots.append({'speaker_id': speaker.id, 'name': speaker.name, 'clip_id': clip.id,
                              'start': round(t, 3), 'end': round(t + clip.duration, 3)})
                inputs.append(idx); idx += 1
                t += clip.duration + PREFIX_GAP_SECONDS
        n_clips = idx
        cmd += ['-i', local_audio_path]                                   # index n_clips
        cmd += ['-f', 'lavfi', '-t', f'{PREFIX_GAP_SECONDS}', '-i', f'anullsrc=r={SAMPLE_RATE}:cl=mono']  # index n_clips+1
        chain = ';'.join(_audio_filters(i) for i in range(n_clips + 1))
        chain += f";[{n_clips + 1}:a]asplit={n_clips}" + ''.join(f'[g{i}]' for i in range(n_clips))
        order = ''.join(f'[a{i}][g{i}]' for i in range(n_clips)) + f'[a{n_clips}]'
        chain += f";{order}concat=n={2 * n_clips + 1}:v=0:a=1[out]"
        out_path = os.path.join(workdir, 'with_references.flac')
        cmd += ['-filter_complex', chain, '-map', '[out]', '-ar', str(SAMPLE_RATE), '-ac', '1',
                '-c:a', 'flac', '-compression_level', '5', out_path]
        _run_ffmpeg_command(cmd, 'reference prefix assembly')
        plan = PrefixPlan(input_path=out_path, total=round(t, 3), slots=slots,
                          people=[s.id for s, _ in chosen])
        return plan, workdir
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def cleanup(workdir: Optional[str]) -> None:
    if workdir:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------- after transcription

def resolve_matches(segments, plan: PrefixPlan) -> Tuple[Dict[str, str], Dict[str, List[str]], Dict[int, List[int]]]:
    """({label: name}, {label: [names]} ambiguous, {speaker_id: [clip_id]} hits).

    segments: TranscriptionSegment objects with absolute times (prefix included).
    """
    by_speaker_labels: Dict[int, Counter] = defaultdict(Counter)
    clips_of: Dict[int, set] = defaultdict(set)
    names: Dict[int, str] = {}
    for seg in segments or []:
        if seg.start_time is None or seg.end_time is None or not seg.speaker:
            continue
        if seg.start_time >= plan.total:
            continue
        # a segment can straddle two slots only across a 1 s gap; weigh by overlap
        for slot in plan.slots:
            overlap = min(seg.end_time, slot['end']) - max(seg.start_time, slot['start'])
            if overlap > 0.5:
                by_speaker_labels[slot['speaker_id']][seg.speaker] += overlap
                clips_of[slot['speaker_id']].add(slot['clip_id'])
                names[slot['speaker_id']] = slot['name']
    label_claims: Dict[str, set] = defaultdict(set)
    for sid, counter in by_speaker_labels.items():
        label, weight = counter.most_common(1)[0]
        if weight < 2.0:                      # practically no speech recognised in the clip
            continue
        label_claims[label].add(sid)
    matches, ambiguous, hits = {}, {}, {}
    for label, sids in label_claims.items():
        if len(sids) == 1:
            sid = next(iter(sids))
            matches[label] = names[sid]
            hits[sid] = sorted(clips_of[sid])
        else:
            ambiguous[label] = sorted(names[s] for s in sids)
    return matches, ambiguous, hits


def strip_prefix(response, total: float) -> None:
    """Drop prefix segments from a TranscriptionResponse and shift the rest back."""
    kept = []
    for seg in response.segments or []:
        if seg.start_time is None or seg.end_time is None:
            kept.append(seg)
            continue
        if seg.end_time <= total + 0.05:
            continue
        seg.start_time = round(max(0.0, seg.start_time - total), 3)
        seg.end_time = round(seg.end_time - total, 3)
        if seg.words:
            for w in seg.words:
                for key in ('start', 'end', 'start_time', 'end_time'):
                    if isinstance(w.get(key), (int, float)):
                        w[key] = round(max(0.0, w[key] - total), 3)
        kept.append(seg)
    response.segments = kept
    response.speakers = sorted({s.speaker for s in kept if s.speaker})
    if response.duration:
        response.duration = max(0.0, response.duration - total)
    response.text = ' '.join((s.text or '').strip() for s in kept).strip()


def finish_response(response, plan: PrefixPlan) -> Dict[str, Any]:
    """Resolve the prefix labels, then remove the prefix from the response."""
    matches, ambiguous, hits = resolve_matches(response.segments, plan)
    strip_prefix(response, plan.total)
    # keep only labels that actually occur in the meeting part
    present = {s.speaker for s in response.segments or [] if s.speaker}
    matches = {k: v for k, v in matches.items() if k in present}
    logger.info("Reference clips: matched %s; ambiguous %s", matches, ambiguous or '-')
    return {'matches': matches, 'ambiguous': ambiguous, 'hits': hits}


def apply_matches(recording: Recording, plan: PrefixPlan, result: Dict[str, Any]) -> Dict[str, str]:
    """Write the recognised names into the transcript and count the hits. Commits."""
    matches = result.get('matches') or {}
    if not matches:
        recording.speaker_label_map = None
        db.session.commit()
        return {}
    from src.services.speaker_embedding_matcher import apply_speaker_names_to_transcription
    if not apply_speaker_names_to_transcription(recording, matches):   # commits
        return {}
    recording.speaker_label_map = dict(matches)
    now = datetime.utcnow()
    matched_ids = {sid for sid, _ in result.get('hits', {}).items()
                   if any(slot['speaker_id'] == sid and slot['name'] in matches.values() for slot in plan.slots)}
    for sid in matched_ids:
        speaker = db.session.get(Speaker, sid)
        if speaker:
            speaker.use_count = (speaker.use_count or 0) + 1
            speaker.last_used = now
        for cid in result['hits'].get(sid, []):
            clip = db.session.get(SpeakerReferenceClip, cid)
            if clip:
                clip.hit_count = (clip.hit_count or 0) + 1
                clip.last_hit_at = now
    db.session.commit()
    return matches


# ------------------------------------------------------- after a rename

def on_names_applied(recording: Recording, label_to_name: Dict[str, str], user, local_path: Optional[str] = None) -> int:
    """After a user names speakers: cut clips for the people the prefix missed.

    label_to_name maps what the transcript showed before the save (a raw
    label, or a name given earlier) to the name now assigned. A raw label
    means the reference clips did not recognise the person -> new clip. A
    name corrected to another name moves the recording's clip to the right
    person. Does not commit.
    """
    if not label_to_name or not recording.audio_path:
        return 0
    segments = _segments_of(recording)
    if not segments:
        return 0
    created = 0
    label_map = dict(recording.speaker_label_map or {})
    for old_key, name in label_to_name.items():
        speaker = _get_or_create_speaker(recording.user_id, name)
        if speaker is None:
            continue
        if not is_raw_label(old_key):
            # a name corrected to another name: the old person's clip from
            # this recording described someone else
            old_speaker = _get_or_create_speaker(recording.user_id, old_key) if old_key else None
            if old_speaker and old_speaker.id != speaker.id:
                for clip in SpeakerReferenceClip.query.filter_by(speaker_id=old_speaker.id, recording_id=recording.id).all():
                    delete_clip(clip)
        for lab, val in list(label_map.items()):
            if val == old_key:
                label_map[lab] = name
        if is_raw_label(old_key):
            label_map[old_key] = name
        try:
            if create_clip(recording, speaker, name, segments, label=old_key if is_raw_label(old_key) else None,
                           source='confirmed', local_path=local_path):
                created += 1
        except Exception as e:
            logger.warning("Reference clip for %s from recording %s failed: %s", name, recording.id, e)
    recording.speaker_label_map = label_map or None
    return created


# ------------------------------------------------------------- backfill

def backfill_user(user_id: int) -> Dict[str, Any]:
    """Cut clips for every named person in the user's existing recordings. Commits."""
    stats = {'recordings': 0, 'clips_created': 0, 'skipped_existing': 0, 'errors': []}
    recordings = Recording.query.filter_by(user_id=user_id).filter(Recording.audio_path.isnot(None)).all()
    for recording in recordings:
        segments = _segments_of(recording)
        names = sorted({str(s.get('speaker')).strip() for s in segments
                        if s.get('speaker') and not is_raw_label(str(s.get('speaker')))})
        if not names:
            continue
        stats['recordings'] += 1
        try:
            with _local_audio_path(recording) as materialized:
                for name in names:
                    speaker = _get_or_create_speaker(user_id, name)
                    if speaker is None:
                        continue
                    if SpeakerReferenceClip.query.filter_by(speaker_id=speaker.id, recording_id=recording.id).first():
                        stats['skipped_existing'] += 1
                        continue
                    if create_clip(recording, speaker, name, segments, source='backfill', local_path=materialized.local_path):
                        stats['clips_created'] += 1
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            stats['errors'].append(f"recording {recording.id}: {e}")
            logger.warning("Reference clip backfill failed for recording %s: %s", recording.id, e)
    return stats
