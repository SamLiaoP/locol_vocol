"""
locol_vocol: ElevenLabs Scribe transcription connector.

POST https://api.elevenlabs.io/v1/speech-to-text (multipart, synchronous):
the whole file is sent in one request, ElevenLabs diarizes up to 32 speakers
and returns word-level timestamps with speaker ids. No app-side chunking, so
speaker labels stay consistent for the entire recording.

Config (from env / admin settings via the registry):
- api_key:  TRANSCRIPTION_API_KEY      (required; sent as `xi-api-key`)
- api_keys: ELEVENLABS_API_KEYS        (optional, comma-separated extra keys; when a
            request is refused for lack of credits the next key is tried, and the key
            that worked last is preferred afterwards)
- base_url: TRANSCRIPTION_BASE_URL     (default https://api.elevenlabs.io)
- model:    TRANSCRIPTION_MODEL        (default scribe_v2)

Post-processing (Chinese-oriented, deterministic):
- Simplified -> Taiwan Traditional (OpenCC s2twp)
- consecutive words of one speaker are grouped into a segment; a new segment
  starts on a speaker change, a pause > 1.5 s, or after sentence punctuation
  once the segment is long enough
- tiny interjection fragments are folded back (see src/utils/zh_postprocess)
"""
import io
import re
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Set, Optional, List

import httpx

from ..base import (
    BaseTranscriptionConnector,
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
    ConnectorSpecifications,
)
from ..exceptions import TranscriptionError, ConfigurationError, ProviderError

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r'[。！？!?]$')
_PAUSE_SECONDS = 1.5
_MAX_SEGMENT_SECONDS = 25.0
_MIN_SEGMENT_CHARS_BEFORE_SPLIT = 12


class ElevenLabsTranscriptionConnector(BaseTranscriptionConnector):
    """Connector for ElevenLabs Scribe (batch) with diarization."""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.DIARIZATION,
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
        TranscriptionCapability.SPEAKER_COUNT_CONTROL,   # num_speakers (max)
        TranscriptionCapability.HOTWORDS,                # keyterms
    }
    PROVIDER_NAME = "elevenlabs"
    KNOWN_MODELS = frozenset({'scribe_v2', 'scribe_v1', 'scribe_v1_experimental'})

    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=None,
        handles_chunking_internally=True,
    )

    def __init__(self, config: Dict[str, Any]):
        # Key pool: the active key first (settings/env TRANSCRIPTION_API_KEY), then any
        # extra keys from ELEVENLABS_API_KEYS. Duplicates and blanks are dropped.
        raw_extra = str(config.get('api_keys') or '').replace('\n', ',').replace(';', ',')
        pool: List[str] = []
        for k in [config.get('api_key') or ''] + raw_extra.split(','):
            k = k.strip()
            if k and k not in pool:
                pool.append(k)
        self.api_keys: List[str] = pool
        self.api_key = pool[0] if pool else ''
        self._key_index = 0          # key tried first; moves to whichever key last succeeded
        self._key_lock = threading.Lock()
        base = (config.get('base_url') or 'https://api.elevenlabs.io').strip()
        self.base_url = base.rstrip('/')
        model = (config.get('model') or '').strip()
        self.model = model if model in self.KNOWN_MODELS else 'scribe_v2'
        # A 3-hour meeting can take several minutes server-side; be generous.
        self.timeout = float(config.get('timeout', 3600.0))
        self.traditional = bool(config.get('traditional_chinese', True))
        super().__init__(config)

    def _validate_config(self) -> None:
        if not self.api_keys:
            raise ConfigurationError("api_key is required for the ElevenLabs connector (set TRANSCRIPTION_API_KEY)")

    # ------------------------------------------------------------ key pool

    @staticmethod
    def mask_key(key: str) -> str:
        key = key or ''
        return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else (key[:4] + '…' if key else '')

    def _key_order(self) -> List[int]:
        """Indexes to try, starting from the preferred key and wrapping around."""
        n = len(self.api_keys)
        with self._key_lock:
            start = self._key_index if 0 <= self._key_index < n else 0
        return [(start + i) % n for i in range(n)]

    def _prefer_key(self, idx: int) -> None:
        with self._key_lock:
            if idx != self._key_index:
                logger.info("ElevenLabs: now preferring key #%d (%s)", idx + 1, self.mask_key(self.api_keys[idx]))
            self._key_index = idx

    @property
    def active_key_index(self) -> int:
        with self._key_lock:
            return self._key_index

    @staticmethod
    def _is_quota_error(status_code: int, body: str) -> bool:
        """ElevenLabs answers 401 with code `quota_exceeded` when credits run out."""
        b = (body or '').lower()
        return status_code in (401, 402, 429) and any(
            s in b for s in ('quota_exceeded', 'exceeds your quota', 'insufficient_quota', 'insufficient credits'))

    def usage_report(self, timeout: float = 10.0) -> List[Dict[str, Any]]:
        """Credit usage of every key in the pool via GET /v1/user/subscription.

        A key created without the `user_read` permission cannot read its own
        subscription; that key is reported with an `error` instead of numbers.
        """
        out: List[Dict[str, Any]] = []
        active = self.active_key_index
        for i, key in enumerate(self.api_keys):
            row: Dict[str, Any] = {'index': i, 'masked': self.mask_key(key), 'active': i == active,
                                   'used': None, 'limit': None, 'remaining': None, 'reset_at': None,
                                   'tier': None, 'error': None}
            try:
                with httpx.Client(base_url=self.base_url, headers={"xi-api-key": key},
                                  timeout=httpx.Timeout(timeout, connect=10.0)) as client:
                    resp = client.get("/v1/user/subscription")
                if resp.status_code != 200:
                    detail = {}
                    try:
                        detail = (resp.json() or {}).get('detail') or {}
                    except Exception:
                        pass
                    status = detail.get('status') if isinstance(detail, dict) else None
                    row['error'] = 'missing_permissions' if status == 'missing_permissions' else f"HTTP {resp.status_code}"
                else:
                    d = resp.json() or {}
                    used, limit = d.get('character_count'), d.get('character_limit')
                    row.update(used=used, limit=limit, tier=d.get('tier'),
                               remaining=(limit - used) if isinstance(used, int) and isinstance(limit, int) else None)
                    reset = d.get('next_character_count_reset_unix')
                    if isinstance(reset, (int, float)):
                        row['reset_at'] = datetime.fromtimestamp(reset, tz=timezone.utc).isoformat()
            except Exception as e:  # network error, timeout, bad JSON
                row['error'] = str(e)[:120]
            out.append(row)
        return out

    # ------------------------------------------------------------ request

    def _form(self, request: TranscriptionRequest) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "model_id": self.model,
            "timestamps_granularity": "word",
            "tag_audio_events": "false",
            "diarize": "true" if request.diarize else "false",
        }
        lang = (request.language or '').strip().lower()
        if lang and lang not in ('auto', 'any', 'detect'):
            data["language_code"] = lang
        if request.diarize and request.max_speakers:
            try:
                data["num_speakers"] = str(max(1, min(32, int(request.max_speakers))))
            except (TypeError, ValueError):
                pass
        return data

    @staticmethod
    def _keyterms(request: TranscriptionRequest) -> List[str]:
        raw = (request.hotwords or '').strip()
        if not raw:
            return []
        terms = [t.strip() for t in re.split(r'[,\n]', raw) if t.strip()]
        return terms[:100]  # >100 keyterms changes billing

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        try:
            audio = self._read_audio(request)
            form = self._form(request)
            files = [("file", (request.filename or "audio", audio, request.mime_type or "application/octet-stream"))]
            # keyterms is a list field -> httpx repeats the key for list values
            fields: Dict[str, Any] = dict(form)
            kt = self._keyterms(request)
            if kt:
                fields["keyterms"] = kt
            logger.info("ElevenLabs: submitting %s (%.1f MB) model=%s lang=%s diarize=%s num_speakers=%s",
                        request.filename, len(audio) / 1048576, self.model,
                        form.get('language_code', 'auto'), form.get('diarize'), form.get('num_speakers', '-'))
            last_error: Optional[ProviderError] = None
            order = self._key_order()
            for attempt, idx in enumerate(order):
                key = self.api_keys[idx]
                with httpx.Client(base_url=self.base_url, headers={"xi-api-key": key},
                                  timeout=httpx.Timeout(self.timeout, connect=30.0)) as client:
                    resp = client.post("/v1/speech-to-text", data=fields, files=files)
                if resp.status_code == 200:
                    self._prefer_key(idx)
                    return self._parse_result(resp.json(), diarized=bool(request.diarize))
                last_error = ProviderError(
                    f"ElevenLabs transcription failed ({resp.status_code}): {resp.text[:4000]}",
                    provider=self.PROVIDER_NAME, status_code=resp.status_code)
                # Out of credits on this key: fall through to the next one, if any.
                if self._is_quota_error(resp.status_code, resp.text) and attempt < len(order) - 1:
                    logger.warning("ElevenLabs: key #%d (%s) is out of credits, trying key #%d",
                                   idx + 1, self.mask_key(key), order[attempt + 1] + 1)
                    continue
                raise last_error
            raise last_error or ProviderError("ElevenLabs transcription failed: no API key configured",
                                              provider=self.PROVIDER_NAME)
        except (TranscriptionError, ConfigurationError):
            raise
        except Exception as e:
            logger.error("ElevenLabs transcription failed: %s", e)
            raise TranscriptionError(f"ElevenLabs transcription failed: {e}") from e

    @staticmethod
    def _read_audio(request: TranscriptionRequest) -> bytes:
        f = request.audio_file
        try:
            if hasattr(f, 'seek'):
                f.seek(0)
        except (OSError, io.UnsupportedOperation):
            pass
        data = f.read()
        if not data:
            raise TranscriptionError("ElevenLabs: empty audio file")
        return data

    # ------------------------------------------------------------- parsing

    def _parse_result(self, result: Dict[str, Any], diarized: bool) -> TranscriptionResponse:
        from src.utils.zh_postprocess import to_traditional, merge_fragments

        language = result.get('language_code')
        duration = result.get('audio_duration_secs')
        words = result.get('words') or []
        plain = result.get('text') or ''

        if not words:
            text = to_traditional(plain) if self.traditional else plain
            return TranscriptionResponse(text=text, language=language, duration=duration,
                                         provider=self.PROVIDER_NAME, model=self.model, raw_response=result)

        # 1. group words into raw segments
        raw: List[Dict[str, Any]] = []
        cur: Optional[Dict[str, Any]] = None
        for w in words:
            wtype = w.get('type')
            if wtype == 'audio_event':
                continue
            txt = w.get('text') or ''
            spk = _norm_speaker(w.get('speaker_id')) if diarized else None
            start, end = w.get('start'), w.get('end')
            if wtype == 'spacing':
                if cur is not None:
                    cur['sentence'] += txt
                continue
            new_seg = cur is None or (diarized and spk != cur['speaker'])
            if not new_seg and start is not None and cur.get('end_time') is not None:
                if start - cur['end_time'] > _PAUSE_SECONDS:
                    new_seg = True
                elif (cur.get('start_time') is not None and end is not None
                      and end - cur['start_time'] > _MAX_SEGMENT_SECONDS
                      and _SENTENCE_END.search(cur['sentence'].rstrip())):
                    new_seg = True
                elif (_SENTENCE_END.search(cur['sentence'].rstrip())
                      and len(cur['sentence'].strip()) >= _MIN_SEGMENT_CHARS_BEFORE_SPLIT * 3):
                    new_seg = True
            if new_seg:
                if cur is not None:
                    raw.append(cur)
                cur = {'speaker': spk, 'sentence': txt, 'start_time': start, 'end_time': end}
            else:
                cur['sentence'] += txt
                if end is not None:
                    cur['end_time'] = end
        if cur is not None:
            raw.append(cur)

        # 2. clean text + fold fragments
        for seg in raw:
            seg['sentence'] = _tidy(seg['sentence'])
            if self.traditional:
                seg['sentence'] = to_traditional(seg['sentence'])
        raw = [s for s in raw if s['sentence']]
        merged = merge_fragments(raw) if diarized else raw

        segments: List[TranscriptionSegment] = []
        speakers = set()
        parts = []
        for s in merged:
            spk = s.get('speaker')
            if spk:
                speakers.add(spk)
                parts.append(f"[{spk}]: {s['sentence']}")
            else:
                parts.append(s['sentence'])
            segments.append(TranscriptionSegment(
                text=s['sentence'], speaker=spk,
                start_time=_r(s.get('start_time')), end_time=_r(s.get('end_time')),
            ))
        logger.info("ElevenLabs: %d words -> %d segments (%d after merge), %d speakers, lang=%s",
                    len(words), len(raw), len(merged), len(speakers), language)
        return TranscriptionResponse(
            text='\n'.join(parts),
            segments=segments,
            speakers=sorted(speakers) if speakers else None,
            language=language,
            duration=duration,
            provider=self.PROVIDER_NAME,
            model=self.model,
            raw_response=result,
        )

    def health_check(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {"type": "string", "description": "ElevenLabs API key"},
                "base_url": {"type": "string", "default": "https://api.elevenlabs.io"},
                "model": {"type": "string", "default": "scribe_v2"},
            },
        }


def _norm_speaker(sid: Optional[str]) -> str:
    """speaker_0 -> SPEAKER_00 (matches the labels users already know)."""
    if not sid:
        return 'SPEAKER_00'
    m = re.match(r'^speaker_(\d+)$', str(sid))
    if m:
        return f"SPEAKER_{int(m.group(1)):02d}"
    return str(sid)


_CJK = re.compile(r'[一-鿿]')


def _tidy(text: str) -> str:
    """Collapse whitespace; drop spaces between CJK characters."""
    t = re.sub(r'\s+', ' ', text or '').strip()
    t = re.sub(r'(?<=[一-鿿])\s+(?=[一-鿿])', '', t)
    t = re.sub(r'(?<=[一-鿿])\s+(?=[，。、！？；：])', '', t)
    t = re.sub(r'(?<=[，。、！？；：])\s+(?=[一-鿿])', '', t)
    return t


def _r(v):
    try:
        return round(float(v), 3) if v is not None else None
    except (TypeError, ValueError):
        return None
