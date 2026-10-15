"""
locol_vocol: deterministic clean-up for Chinese transcripts.

- to_traditional(): Simplified -> Taiwan Traditional via OpenCC (s2tw).
  Glyph conversion only: no phrase/vocabulary substitution, so the speaker's
  own wording is never replaced (e.g. 權限 stays 權限, never 許可權).
- merge_fragments(): fold one/two-character throw-away turns ("嘛", "對", "喔")
  back into the neighbouring turn of the same speaker so a sentence is not
  split across three speaker rows.
"""
import re
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

_converter = None


def _get_converter():
    global _converter
    if _converter is None:
        try:
            import opencc  # opencc-python-reimplemented
            _converter = opencc.OpenCC('s2tw')
        except Exception as e:  # pragma: no cover
            logger.warning("OpenCC unavailable, Traditional conversion disabled: %s", e)
            _converter = False
    return _converter or None


_HAS_CJK = re.compile(r'[一-鿿]')
_PUNCT = re.compile(r'[\s,.，。、！!？?；;：:「」『』()（）\-—…·]+')


def to_traditional(text: Optional[str]) -> Optional[str]:
    if not text or not _HAS_CJK.search(text):
        return text
    conv = _get_converter()
    return conv.convert(text) if conv else text


def _core_len(text: str) -> int:
    return len(_PUNCT.sub('', text or ''))


def merge_fragments(segments: List[Dict[str, Any]], max_fragment_chars: int = 2,
                    max_gap_seconds: float = 2.0) -> List[Dict[str, Any]]:
    """
    segments: list of dicts with keys speaker, sentence, start_time, end_time.
    A fragment (<= max_fragment_chars real characters) is merged into the
    previous segment when the previous segment has the same speaker, or when
    the segments before AND after it share a speaker (the fragment is an
    interjection that broke someone else's sentence; it is then attached to
    the previous segment and keeps the surrounding speaker).
    """
    if not segments:
        return segments
    out: List[Dict[str, Any]] = []
    for i, seg in enumerate(segments):
        sent = (seg.get('sentence') or '').strip()
        is_frag = _core_len(sent) <= max_fragment_chars
        prev = out[-1] if out else None
        nxt = segments[i + 1] if i + 1 < len(segments) else None

        def _gap_ok(a, b):
            try:
                return (float(b.get('start_time') or 0) - float(a.get('end_time') or 0)) <= max_gap_seconds
            except (TypeError, ValueError):
                return True

        if prev is not None and seg.get('speaker') == prev.get('speaker') and _gap_ok(prev, seg):
            # same speaker back-to-back: always join
            prev['sentence'] = _join(prev['sentence'], sent)
            prev['end_time'] = seg.get('end_time', prev.get('end_time'))
            continue
        if is_frag and prev is not None and nxt is not None \
                and prev.get('speaker') == nxt.get('speaker') and prev.get('speaker') != seg.get('speaker'):
            # Someone else's short interjection in the middle of a sentence.
            # Kept as its own row with its own speaker: nothing a participant
            # said is ever dropped or re-attributed.
            out.append(dict(seg))
            continue
        out.append(dict(seg))
    return out


_BACKCHANNEL = {'嗯', '嗯嗯', '對', '對對', '是', '好', '好好', '哦', '喔', '噢', '欸', '誒', '呃', '啊', '嘿',
                'ok', 'okay', 'yeah', 'yes', 'mm', 'hmm', 'uh', 'um', 'oh', '了解', '瞭解', '理解', '懂'}


def _is_backchannel(text: str) -> bool:
    core = _PUNCT.sub('', text or '').strip().lower()
    return core in _BACKCHANNEL


def _join(a: str, b: str) -> str:
    a = (a or '').rstrip()
    b = (b or '').strip()
    if not a:
        return b
    if not b:
        return a
    # no space between CJK, a space between latin words
    if _HAS_CJK.search(a[-1]) or _HAS_CJK.search(b[0]):
        return a + b
    return a + ' ' + b
