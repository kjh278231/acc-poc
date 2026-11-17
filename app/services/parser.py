"""Deterministic Korean time / duration parser shared by both engines.

Turns driver phrases into minutes from now:
  "30분" "삼십 분" "한 시간 반" "1시간 20분" "두 시간쯤" "20~30분" "3시 반쯤" "오후 4시"
Vague answers ("곧", "금방", "잘 모르겠어요") return vague=True so the caller can re-ask
instead of guessing a number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

NATIVE = {
    "한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3, "석": 3, "네": 4, "넷": 4, "넉": 4,
    "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10, "열한": 11, "열두": 12,
}
SINO_DIGIT = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}

_NUM = r"(?:\d+(?:\.\d+)?|열한|열두|다섯|여섯|일곱|여덟|아홉|하나|한|두|세|석|네|넉|열|[일이삼사오육칠팔구십]+)"
_RANGE = re.compile(rf"({_NUM})\s*(분|시간)?\s*(?:~|-|에서|내지|or|또는)\s*({_NUM})\s*(분|시간)")
_HOUR_MIN = re.compile(rf"({_NUM})\s*시간\s*(?:(반)|({_NUM})\s*분)?")
_MIN = re.compile(rf"({_NUM})\s*분")
_CLOCK = re.compile(rf"(오전|오후|아침|저녁|밤)?\s*({_NUM})\s*시(?!간)\s*(?:(반)|({_NUM})\s*분)?")

VAGUE_PATTERNS = [
    "모르겠", "모르", "글쎄", "곧", "금방", "조만간", "확실하지", "애매", "가봐야", "봐야 알",
    "정확히는", "얼마나 걸릴지", "언제 될지", "모름",
]
ARRIVED_PATTERNS = ["도착했", "도착 했", "도착해 있", "다 왔어요", "도착 완료"]


@dataclass
class ParsedTime:
    minutes: int | None
    vague: bool = False
    source: str = ""  # duration | clock | range | arrived | none

    @property
    def ok(self) -> bool:
        return self.minutes is not None


def ko_to_int(tok: str | None) -> float | None:
    tok = (tok or "").strip()
    if not tok:
        return None
    if len(tok) == 1 and tok in SINO_DIGIT:
        # single sino digits collide with common words ("이 분" = this person, "구분"); require 2+ chars
        return None
    try:
        return float(tok)
    except ValueError:
        pass
    if tok in NATIVE:
        return NATIVE[tok]
    # Sino-Korean: 삼십오, 사십, 십오, 이십
    if all(c in SINO_DIGIT or c == "십" for c in tok):
        total, cur = 0, 0
        for c in tok:
            if c == "십":
                total += (cur or 1) * 10
                cur = 0
            else:
                cur = SINO_DIGIT[c]
        return total + cur
    return None


def _hm(h: str | None, half: str | None, m: str | None) -> float | None:
    hv = ko_to_int(h) if h else 0
    if hv is None:
        return None
    mv = 30 if half else (ko_to_int(m) if m else 0)
    if mv is None:
        return None
    return hv * 60 + mv


def parse_duration(text: str, now: datetime | None = None) -> ParsedTime:
    """Minutes from now described by the utterance (ETA or delay)."""
    t = text.replace(",", " ")
    if any(p in t for p in ARRIVED_PATTERNS) and "거의" not in t:
        return ParsedTime(0, source="arrived")

    m = _RANGE.search(t)
    if m:
        lo, lo_unit, hi, hi_unit = m.groups()
        lo_unit = lo_unit or hi_unit
        lo_v, hi_v = ko_to_int(lo), ko_to_int(hi)
        if lo_v is not None and hi_v is not None:
            mul_lo = 60 if lo_unit == "시간" else 1
            mul_hi = 60 if hi_unit == "시간" else 1
            # conservative: use the upper bound for ETA planning
            return ParsedTime(int(round(max(lo_v * mul_lo, hi_v * mul_hi))), source="range")

    m = _HOUR_MIN.search(t)
    if m:
        v = _hm(m.group(1), m.group(2), m.group(3))
        if v is not None:
            return ParsedTime(int(round(v)), source="duration")

    m = _CLOCK.search(t)
    if m:
        mins = _clock_to_minutes(m, now or datetime.now())
        if mins is not None:
            return ParsedTime(mins, source="clock")

    m = _MIN.search(t)
    if m:
        v = ko_to_int(m.group(1))
        if v is not None:
            return ParsedTime(int(round(v)), source="duration")

    if "반 시간" in t or "30분" in t:
        return ParsedTime(30, source="duration")

    vague = any(p in t for p in VAGUE_PATTERNS) or "거의" in t
    return ParsedTime(None, vague=vague, source="none")


def _clock_to_minutes(m: re.Match, now: datetime) -> int | None:
    ampm, h, half, mm = m.groups()
    hv = ko_to_int(h)
    if hv is None or not 0 <= hv <= 24:
        return None
    minute = 30 if half else int(ko_to_int(mm) or 0)
    hour = int(hv)
    if ampm in ("오후", "저녁", "밤") and hour < 12:
        hour += 12
    target = now.replace(hour=hour % 24, minute=minute % 60, second=0, microsecond=0)
    diff = (target - now).total_seconds() / 60
    if ampm is None and diff < 0:
        # "3시" without 오전/오후: next occurrence within 12h
        diff += 12 * 60
    if diff < 0:
        diff += 24 * 60
    if diff > 12 * 60:
        return None
    return int(round(diff))


def has_number(text: str) -> bool:
    return parse_duration(text).ok


def is_negative_issue(text: str) -> bool:
    """Plain 'no issue' answers. Used only as a hint; engines make the final call."""
    pats = ["없어요", "없습니다", "없음", "괜찮", "문제 없", "이상 없", "특별한 건 없", "특이사항 없", "순조", "잘 가고", "정상"]
    return any(p in text for p in pats)


_ANY_TIME = re.compile(rf"{_NUM}\s*(?:시간|분|시(?!간))")


def count_time_mentions(text: str) -> int:
    """Number of separate time expressions ("1시간 20분" and "20~30분" count once)."""
    t = _RANGE.sub(" R ", text)
    t = _HOUR_MIN.sub(lambda m: " H " if ko_to_int(m.group(1)) is not None else m.group(0), t)
    t = _CLOCK.sub(lambda m: " C " if ko_to_int(m.group(2)) is not None else m.group(0), t)
    n = len(re.findall(r" [RHC] ", t))
    n += sum(1 for m in _MIN.finditer(t) if ko_to_int(m.group(1)) is not None)
    return n
