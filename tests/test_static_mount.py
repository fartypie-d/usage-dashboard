"""Tests for the static file mount and root route on the FastAPI app.

These tests verify that:
1. GET / serves the index.html file with the correct content type.
2. GET /static/vendor/chart.min.js serves the vendored Chart.js file.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import app

    return TestClient(app)


def test_root_serves_index_html() -> None:
    """GET / should return the dashboard index.html page."""
    client = _client()
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # The page must carry the product name so we know it's the real UI.
    assert "debt-radar" in r.text


def test_static_vendor_chart_is_served() -> None:
    """GET /static/vendor/chart.min.js should serve the vendored Chart.js."""
    client = _client()
    r = client.get("/static/vendor/chart.min.js")
    assert r.status_code == 200
    # Chart.js 4.x exposes a global `Chart` constructor.
    body = r.text
    assert "Chart" in body or "chart" in body


def test_index_html_has_the_delegation_timeline_section() -> None:
    response = _client().get("/")
    assert response.status_code == 200
    assert 'id="flow-timeline"' in response.text
    assert "위임 타임라인" in response.text


def test_support_js_renders_the_delegation_timeline() -> None:
    response = _client().get("/static/support.js")
    assert response.status_code == 200
    assert "renderDelegationTimeline" in response.text
    assert "state.delegation.flows" in response.text


def test_support_js_consumes_the_redefined_overhead_fields() -> None:
    """support.js must read the redefined overhead cost-allocation fields."""
    response = _client().get("/static/support.js")
    assert response.status_code == 200
    body = response.text
    assert "delegation_share" in body
    assert "setup_cost_usd" in body
    assert "work_cost_usd" in body
    assert "setup_share" in body


def test_support_js_no_longer_references_the_removed_overhead_fields() -> None:
    """Dead consumers of the removed overhead fields must not remain."""
    response = _client().get("/static/support.js")
    assert response.status_code == 200
    body = response.text
    assert "delegation_token_overhead" not in body
    assert "direct_cost_per_task_usd" not in body
    assert "delegated_cost_per_task_usd" not in body


def test_support_js_persists_the_range_and_source_filters() -> None:
    """페이지를 떠났다 돌아와도 사용량 필터가 유지되어야 한다 (localStorage)."""
    response = _client().get("/static/support.js")
    assert response.status_code == 200
    body = response.text
    assert "ud-range" in body
    assert "ud-source" in body
    assert "restoreFilters" in body


def test_support_js_consumes_the_flow_total_field() -> None:
    """support.js must read flows_total so 'more' counts survive the server cap."""
    response = _client().get("/static/support.js")
    assert response.status_code == 200
    assert "flows_total" in response.text


def test_support_js_requests_more_flows_with_the_limit_parameter() -> None:
    """Expanding the flow list must re-fetch /api/delegation with limit=."""
    response = _client().get("/static/support.js")
    assert response.status_code == 200
    assert "limit=" in response.text


def test_api_response_is_gzipped_when_the_client_accepts_it() -> None:
    client = _client()
    response = client.get(
        "/api/delegation?range=all",
        headers={"accept-encoding": "gzip"},
    )
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"


def test_api_response_is_not_gzipped_when_the_client_does_not_accept_it() -> None:
    client = _client()
    response = client.get(
        "/api/delegation?range=all",
        headers={"accept-encoding": "identity"},
    )
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert isinstance(response.json(), dict)


def test_gzip_does_not_change_the_json_contract() -> None:
    """Compressed and identity responses must decode to the same JSON.

    httpx auto-decompresses, so equality alone would still pass if GZip were
    removed.  Assert content-encoding to prove the gzip path was actually hit.
    """
    client = _client()
    gzipped_res = client.get(
        "/api/delegation?range=all",
        headers={"accept-encoding": "gzip"},
    )
    raw_res = client.get(
        "/api/delegation?range=all",
        headers={"accept-encoding": "identity"},
    )
    assert gzipped_res.status_code == 200
    assert raw_res.status_code == 200
    assert gzipped_res.headers.get("content-encoding") == "gzip"
    assert "content-encoding" not in raw_res.headers
    assert gzipped_res.json() == raw_res.json()


def test_sessions_page_is_served() -> None:
    response = _client().get("/static/sessions.html")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "세션 상세" in response.text


def test_sessions_js_wires_the_work_api() -> None:
    response = _client().get("/static/sessions.js")
    assert response.status_code == 200
    assert "/static/flow.html" in response.text
    assert "/api/work/session/" in response.text
    # 원문 삽입 안전 규칙: sessions.js는 innerHTML을 쓰지 않는다
    assert "innerHTML" not in response.text


def test_index_links_to_the_flow_page() -> None:
    response = _client().get("/")
    assert response.status_code == 200
    assert 'href="/static/flow.html"' in response.text


def test_sessions_page_links_back_to_the_dashboard() -> None:
    response = _client().get("/static/sessions.html")
    assert '<a href="/"' in response.text


def test_sessions_js_renders_the_actions_truncated_flag() -> None:
    """Task 2 반려로 신설된 절단 신호가 프런트에서 소비되는지의 최소 가드."""
    response = _client().get("/static/sessions.js")
    assert "actions_truncated" in response.text


def test_sessions_js_shows_warnings_on_both_views() -> None:
    """세션 상세 응답의 warnings가 경고 배너로 표시된다."""
    response = _client().get("/static/sessions.js")
    assert response.text.count("⚠") >= 1



def test_progress_page_is_served() -> None:
    response = _client().get("/static/progress.html")
    assert response.status_code == 200
    assert "진행내역" in response.text


def test_flow_page_is_served() -> None:
    response = _client().get("/static/flow.html")
    assert response.status_code == 200
    assert "작업 흐름" in response.text


def test_flow_js_is_served() -> None:
    response = _client().get("/static/flow.js")
    assert response.status_code == 200
    assert "/static/sessions.html#/session/" in response.text
    assert "innerHTML" not in response.text


def test_markdown_renderer_is_served() -> None:
    response = _client().get("/static/markdown.js")
    assert response.status_code == 200
    assert "renderMarkdown" in response.text


def test_markdown_renderer_never_assigns_inner_html() -> None:
    """createElement 전용 규율의 회귀 가드 — innerHTML 대입이 곧 XSS 경로다.

    단어 자체는 주석("innerHTML을 쓰지 않는다")에 나오므로 대입만 막는다.
    """
    text = _client().get("/static/markdown.js").text
    assert not re.search(r"innerHTML\s*=", text)


def test_dashboard_and_sessions_link_to_progress() -> None:
    client = _client()
    for page in ("/static/index.html", "/static/sessions.html"):
        assert "/static/progress.html" in client.get(page).text, page


def test_progress_js_renders_warnings_on_both_views() -> None:
    """목록·상세 양쪽에 경고 렌더 경로가 있어야 한다 (sessions.js 와 같은 회귀 가드)."""
    text = _client().get("/static/progress.js").text
    assert text.count("renderWarnings(") >= 3   # 정의 1 + 목록 1 + 상세 1


def test_progress_js_guards_against_stale_responses() -> None:
    """연타 시 옛 응답이 새 화면을 덮지 않도록 요청 시퀀스 가드가 있어야 한다."""
    text = _client().get("/static/progress.js").text
    assert "detailReqSeq" in text


def test_progress_js_wires_keyboard_activation() -> None:
    """div[role=button] 은 Enter/Space 가 자동 동작하지 않는다."""
    text = _client().get("/static/progress.js").text
    assert "onkeydown" in text
    assert "Enter" in text


def test_progress_js_consumes_the_projects_field() -> None:
    """진행내역 프로젝트 선택자는 목록 응답의 projects 필드를 소비해야 한다."""
    response = _client().get("/static/progress.js")
    assert response.status_code == 200
    assert "projects" in response.text


def test_progress_js_requests_with_the_project_parameter() -> None:
    """선택한 프로젝트의 목록·상세 요청은 project= 질의를 포함해야 한다."""
    response = _client().get("/static/progress.js")
    assert response.status_code == 200
    assert "project=" in response.text


def test_sessions_diff_js_is_served() -> None:
    response = _client().get("/static/sessions_diff.js")

    assert response.status_code == 200
    assert "renderFilesPanel" in response.text


def test_sessions_diff_js_never_injects_raw_html() -> None:
    text = _client().get("/static/sessions_diff.js").text

    assert "innerHTML" not in text
    assert "insertAdjacentHTML" not in text
    assert "document.write" not in text


def test_sessions_html_loads_the_diff_renderer_before_the_page_script() -> None:
    text = _client().get("/static/sessions.html").text

    assert "/static/sessions_diff.js" in text
    assert text.index("/static/sessions_diff.js") < text.index("/static/sessions.js")


def test_sessions_diff_js_wires_keyboard_activation() -> None:
    """div[role=button] 은 Enter/Space 가 자동 동작하지 않는다."""
    text = _client().get("/static/sessions_diff.js").text
    assert "onKeydown" in text
    assert "Enter" in text


def test_sessions_html_defines_the_diff_line_styles() -> None:
    text = _client().get("/static/sessions.html").text

    for token in (".diff-add", ".diff-del", ".diff-ctx", ".diff-hunk-header"):
        assert token in text


def test_sessions_js_routes_the_two_detail_tabs() -> None:
    text = _client().get("/static/sessions.js").text

    assert "turns|files" in text  # 해시 정규식이 두 탭을 인식한다
    assert "renderFilesPanel" in text
    assert "detailKey" in text


def test_sessions_js_links_actions_to_files() -> None:
    text = _client().get("/static/sessions.js").text

    assert "file_index" in text
    assert "jumpToFile" in text


def test_sessions_js_still_never_injects_raw_html() -> None:
    text = _client().get("/static/sessions.js").text

    assert "innerHTML" not in text
    assert "insertAdjacentHTML" not in text
    assert "document.write" not in text


BRANDED_PAGES = ("index.html", "progress.html", "sessions.html", "flow.html")


def test_every_page_shows_the_kit_name_as_the_main_title() -> None:
    for page in BRANDED_PAGES:
        text = _client().get(f"/static/{page}").text
        assert "debt-radar" in text, page


def test_page_names_survive_as_subtitles() -> None:
    assert "세션 상세" in _client().get("/static/sessions.html").text
    assert "진행내역" in _client().get("/static/progress.html").text
    assert "사용량" in _client().get("/static/index.html").text


def test_the_old_service_name_is_gone() -> None:
    for page in BRANDED_PAGES:
        assert "LLM 워크플로우" not in _client().get(f"/static/{page}").text, page
