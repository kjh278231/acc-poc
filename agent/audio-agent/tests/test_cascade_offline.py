"""Offline checks for the cascade engine helpers (no AWS, no aiohttp)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge.engines.base import ssml_numbers  # noqa: E402
from bridge.engines.cascade import SentenceSplitter, ThinkingFilter, clean_for_tts  # noqa: E402
from bridge.engines.transcribe_ws import audio_event, decode_events, encode_event  # noqa: E402


def test_splitter():
    sp = SentenceSplitter()
    out = []
    for tok in ["안녕하", "세요. 오늘", "은 맑아요! 우산", "은 필요 없", "어요"]:
        out += sp.feed(tok)
    out += sp.flush()
    assert out == ["안녕하세요.", "오늘은 맑아요!", "우산은 필요 없어요"], out


def test_splitter_long_clause():
    sp = SentenceSplitter(soft_max=30)
    out = sp.feed("이 문장은 아주 길어서 중간에 쉼표가 있는데, 쉼표에서 잘려야 합니다 계속 이어지는 말")
    assert out and out[0].endswith(","), out


def test_thinking_filter():
    f = ThinkingFilter()
    s = "".join(f.feed(t) for t in ["<thin", "king>고객이 끝내려 함</thi", "nking>감사합니다. ", "안녕히 계세요."])
    assert s == "감사합니다. 안녕히 계세요.", repr(s)


def test_clean():
    assert clean_for_tts("**네!** 😀 - 좋아요") == "네!  좋아요" or "네!" in clean_for_tts("**네!** 😀")


def test_ssml_numbers():
    D = '<say-as interpret-as="digits">{}</say-as>'
    T = '<say-as interpret-as="telephone">{}</say-as>'
    assert ssml_numbers("네, 확인해 볼게요.") == "네, 확인해 볼게요."
    assert ssml_numbers("청구번호 20261120-0042 맞으세요?") == f"청구번호 {D.format('20261120-0042')} 맞으세요?"
    assert ssml_numbers("010-1234-5678로 연락드릴게요.") == f"{T.format('010-1234-5678')}로 연락드릴게요."
    for quantity in ["2026년 11월 20일", "50000원", "12345건", "1,500,000원", "3.14159"]:
        assert "say-as" not in ssml_numbers(quantity), quantity
    assert ssml_numbers("A&B 12345") == f"A&amp;B {D.format('12345')}"


def test_eventstream_roundtrip():
    msg = encode_event({":message-type": "event", ":event-type": "TranscriptEvent"}, b'{"a":1}')
    ev = list(decode_events(msg + audio_event(b"\x01\x02")))
    assert ev[0][0][":event-type"] == "TranscriptEvent" and ev[0][1] == b'{"a":1}'
    assert ev[1][0][":event-type"] == "AudioEvent" and ev[1][1] == b"\x01\x02"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
