import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main

client = TestClient(main.app)
INDEX = (Path(main.__file__).parent / "static" / "index.html").read_text()
SCRIPT = INDEX[INDEX.rindex("<script>") + 8 : INDEX.rindex("</script>")]


def _fn(name):
    i = SCRIPT.index(f"function {name}(")
    j = SCRIPT.index("{", i)
    depth, k = 0, j
    while True:
        if SCRIPT[k] == "{":
            depth += 1
        elif SCRIPT[k] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[j : k + 1]
        k += 1


def ndjson(*frames):
    return "".join(json.dumps(f) + "\n" for f in frames).encode()


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def stub(handler):
    class _Client(_REAL_ASYNC_CLIENT):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    return _Client


def ok_stream(_request):
    return httpx.Response(
        200,
        content=ndjson(
            {"message": {"role": "assistant", "content": "hello "}, "done": False},
            {"message": {"role": "assistant", "content": "there"}, "done": False},
            {
                "message": {"content": ""},
                "done": True,
                "eval_count": 2,
                "eval_duration": 100_000_000,
            },
        ),
    )


def frames_of(response_text):
    return [
        json.loads(line[6:]) for line in response_text.splitlines() if line.startswith("data: ")
    ]


def chat(session, message):
    with client.stream("POST", "/chat", json={"session": session, "message": message}) as r:
        return frames_of("".join(r.iter_text()))


@pytest.fixture(autouse=True)
def clean_state():
    main.SESSIONS.clear()
    main.LINK.update(rtt_ms=None, model_ip=None, ttft_ms=None, tok_per_s=None, error=None)
    yield
    main.SESSIONS.clear()


def test_page_is_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache"


def test_head_on_page_is_allowed():
    assert client.head("/").status_code == 200


@pytest.mark.parametrize(
    "asset",
    ["marked.min.js", "purify.min.js", "highlight.min.js", "github-dark.min.css"],
)
def test_vendored_assets_are_served(asset):
    assert client.get(f"/static/vendor/{asset}").status_code == 200


def test_healthz_is_alive_and_leaks_no_topology():
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["instance_id"] == main.INSTANCE_ID
    assert not {"node", "pod", "pod_ip", "model_url"} & set(body)


def test_whereami_matches_what_the_page_reads():
    body = client.get("/whereami").json()
    assert set(body) == {
        "site",
        "platform",
        "region",
        "node",
        "pod",
        "pod_ip",
        "instance_id",
        "model",
        "model_url",
        "link",
        "uptime_s",
    }
    assert set(body["link"]) == {"rtt_ms", "model_ip", "ttft_ms", "tok_per_s", "error"}

    read = set(re.findall(r"\bd\.([a-z_]+)\b", _fn("paint"))) - {"link"}
    assert read <= set(body), f"page reads fields /whereami does not return: {read - set(body)}"


def test_chat_streams_and_reports_its_own_timings(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", stub(ok_stream))
    frames = chat("s", "hi")
    assert [f["token"] for f in frames if "token" in f] == ["hello ", "there"]
    done = frames[-1]
    assert done["done"] is True
    assert done["turns"] == 2
    assert done["tok_per_s"] == 20.0
    assert done["ttft_ms"] is not None


def test_terminal_frame_never_carries_the_probe_error(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", stub(ok_stream))
    main.LINK["error"] = "ConnectError"
    done = chat("s", "hi")[-1]
    assert "error" not in done


def test_page_reads_the_terminal_frame_before_the_error_frame():
    assert SCRIPT.index("frame.done") < SCRIPT.index("frame.error")


def test_a_failed_chat_leaves_no_dangling_turn(monkeypatch):
    def dies(_request):
        raise httpx.ReadTimeout("cut")

    monkeypatch.setattr(main.httpx, "AsyncClient", stub(ok_stream))
    chat("s", "first")
    monkeypatch.setattr(main.httpx, "AsyncClient", stub(dies))
    chat("s", "second")
    monkeypatch.setattr(main.httpx, "AsyncClient", stub(ok_stream))
    done = chat("s", "third")[-1]

    roles = [m["role"] for m in main.SESSIONS["s"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert done["turns"] % 2 == 0


def test_rate_survives_missing_or_zero_counters():
    assert main._rate({"eval_count": 2, "eval_duration": 100_000_000}) == 20.0
    assert main._rate({"eval_count": 0, "eval_duration": 1}) is None
    assert main._rate({"eval_count": 2, "eval_duration": 0}) is None
    assert main._rate({}) is None


def test_every_lookup_in_the_page_resolves():
    used = set(re.findall(r"\$\('([^']+)'\)", SCRIPT))
    have = set(re.findall(r'id="([^"]+)"', INDEX))
    assert used <= have, f"no element with id: {sorted(used - have)}"


def test_session_id_never_calls_a_secure_context_only_api():
    assert not re.search(r"(?<!\?)\.\s*randomUUID\s*\(", SCRIPT)


def test_theme_cannot_drift():
    style = INDEX[INDEX.index("<style>") : INDEX.index("</style>")]
    declared = re.findall(r"^\s*(--[a-z0-9-]+)\s*:", style, re.M)
    dupes = {n for n in declared if declared.count(n) > 1}
    assert not dupes, f"declared more than once, so they can drift: {sorted(dupes)}"
    assert "prefers-color-scheme" not in style


def test_buttons_do_not_fall_back_to_native_chrome():
    style = INDEX[INDEX.index("<style>") : INDEX.index("</style>")]
    rule = re.search(r"\n\t+button \{(.*?)\n\t+\}", style, re.S).group(1)
    for prop in ("background", "border", "color"):
        assert re.search(rf"\n\s*{prop}:", rule), f"button rule does not reset {prop}"


def test_all_three_theme_states_are_switchable():
    style = INDEX[INDEX.index("<style>") : INDEX.index("</style>")]
    assert "color-scheme: light dark;" in style
    assert re.search(r":root\[data-theme='light'\] \{\s*color-scheme: light;", style)
    assert re.search(r":root\[data-theme='dark'\] \{\s*color-scheme: dark;", style)


def test_every_css_variable_used_is_defined():
    used = set(re.findall(r"var\((--[a-z0-9-]+)", INDEX))
    defined = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", INDEX, re.M))
    assert used <= defined, f"undefined: {sorted(used - defined)}"
