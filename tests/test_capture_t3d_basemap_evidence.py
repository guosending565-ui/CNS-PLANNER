from tools.capture_t3d_basemap_evidence import redact_url, summarize_requests


def test_redact_url_never_records_query_or_fragment():
    url = "http://127.0.0.1:8808/api/tile/12/1/2.jpg?token=secret#part"
    assert redact_url(url) == "http://127.0.0.1:8808/api/tile/12/1/2.jpg"


def test_request_summary_counts_non_get_without_storing_secrets():
    rows = [
        {"method": "GET", "url": "http://127.0.0.1:8808/a?token=secret"},
        {"method": "POST", "url": "http://127.0.0.1:8808/b?token=secret"},
    ]
    summary = summarize_requests(rows)
    assert summary == {
        "count": 2,
        "non_get_count": 1,
        "by_method": {"GET": 1, "POST": 1},
        "urls": ["http://127.0.0.1:8808/a", "http://127.0.0.1:8808/b"],
    }
    assert "secret" not in repr(summary)
