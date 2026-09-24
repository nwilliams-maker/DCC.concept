from __future__ import annotations

import json
import os
from typing import Any

import requests
import sqlalchemy as sa
from playwright.sync_api import sync_playwright

TB_GQL = "https://be-terraboost-v3.terraboost.com/graphql"
MONDAY_GQL = "https://api.monday.com/v2"
MONDAY_FILE_GQL = "https://api.monday.com/v2/file"

PRINT_STATUS_BOARD_ID = int(os.environ.get("PRINT_STATUS_BOARD_ID", "6920657806"))
PRINT_PACKING_LIST_COLUMN_ID = os.environ.get("PRINT_PACKING_LIST_COLUMN_ID", "files__1")
POD_FILTER = (os.environ.get("POD_FILTER") or "Orange").strip()
NOT_BEFORE = (os.environ.get("PACKING_SYNC_NOT_BEFORE") or "").strip()
TEST_WO = (os.environ.get("PACKING_SYNC_TEST_WO") or "").strip()


class PortalSSORequired(RuntimeError):
    """The portal requires an interactive SSO login for this account."""


def _clean(v: Any) -> str:
    return "" if v is None else str(v).strip()


def tb_login() -> str:
    email = _clean(os.environ.get("TERRABOOST_EMAIL"))
    password = _clean(os.environ.get("TERRABOOST_PASSWORD"))
    if not email or not password:
        raise RuntimeError("Terraboost credentials are not configured")
    q = """mutation Login($email: String!, $password: String!) {
      login(input: {email: $email, password: $password}) { token }
    }"""
    r = requests.post(TB_GQL, json={"query": q, "variables": {"email": email, "password": password}}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError("Terraboost login failed")
    token = _clean((((body.get("data") or {}).get("login") or {}).get("token")))
    if not token:
        raise RuntimeError("Terraboost login returned no token")
    return token


def tb_query(token: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    r = requests.post(
        TB_GQL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=45,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError("; ".join(_clean(e.get("message") or e) for e in body["errors"]))
    return body.get("data") or {}


def monday_query(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    token = _clean(os.environ.get("MONDAY_API_TOKEN"))
    if not token:
        raise RuntimeError("MONDAY_API_TOKEN is not configured")
    r = requests.post(
        MONDAY_GQL,
        headers={"Authorization": token, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=45,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError("; ".join(_clean(e.get("message") or e) for e in body["errors"]))
    return body.get("data") or {}


def get_orange_accepted_routes() -> list[dict[str, Any]]:
    """Read accepted Orange routes from Railway Postgres only."""
    # A named work order permits one supervised legacy-route test after the
    # Postgres cutover; it never scans a Sheet or processes other old routes.
    if TEST_WO:
        return [{"wo": TEST_WO}]
    db_url = _clean(os.environ.get("DATABASE_URL"))
    if not db_url:
        raise RuntimeError("DATABASE_URL is not configured")

    where_time = ""
    where_test = ""
    params: dict[str, Any] = {"pod": POD_FILTER.lower()}
    if NOT_BEFORE:
        where_time = " AND r.updated_at >= :not_before "
        params["not_before"] = NOT_BEFORE
    if TEST_WO:
        where_test = " AND r.wo = :test_wo "
        params["test_wo"] = TEST_WO

    engine = sa.create_engine(db_url, pool_pre_ping=True)
    sql = sa.text(f"""
        SELECT
          r.id,
          r.wo,
          r.contractor_name,
          r.contractor_id,
          r.updated_at,
          r.payload,
          r.stop_data,
          c.pod_color,
          (
            SELECT max(e.created_at)
            FROM route_events e
            WHERE e.route_id = r.id
              AND e.action = 'processDecision'
              AND lower(coalesce(e.payload->>'decision','')) = 'accept'
          ) AS accepted_at
        FROM routes r
        LEFT JOIN contractors c ON c.id = r.contractor_id
          OR (r.contractor_id IS NULL AND lower(c.email) = lower(r.payload->>'ice'))
        WHERE r.status::text = 'accepted'
          AND lower(coalesce(nullif(trim(r.payload->>'pod'), ''), nullif(trim(c.pod_color), ''), nullif(trim(r.payload->>'pod_name'), ''), '')) = :pod
          {where_time}
          {where_test}
        ORDER BY coalesce((
            SELECT max(e.created_at)
            FROM route_events e
            WHERE e.route_id = r.id
              AND e.action = 'processDecision'
              AND lower(coalesce(e.payload->>'decision','')) = 'accept'
          ), r.updated_at) ASC
    """)
    with engine.connect() as conn:
        return [dict(x) for x in conn.execute(sql, params).mappings().all()]


def monday_board_meta() -> dict[str, Any]:
    q = """query($ids:[ID!]!) {
      boards(ids:$ids) {
        id
        name
        groups { id title position }
        columns { id title type }
      }
    }"""
    boards = monday_query(q, {"ids": [PRINT_STATUS_BOARD_ID]}).get("boards") or []
    if not boards:
        raise RuntimeError("Print Status board not found")
    return boards[0]


def choose_target_group(board: dict[str, Any]) -> str | None:
    forced = _clean(os.environ.get("PRINT_STATUS_GROUP_ID"))
    if forced:
        return forced
    groups = board.get("groups") or []
    for g in groups:
        title = _clean(g.get("title")).lower()
        if "ready" in title and "print" in title:
            return _clean(g.get("id"))
    for g in groups:
        if "print" in _clean(g.get("title")).lower():
            return _clean(g.get("id"))
    return _clean(groups[0].get("id")) if groups else None


def monday_find_item_by_name(name: str) -> dict[str, Any] | None:
    cursor = None
    while True:
        q = """query($board:[ID!]!, $cursor:String) {
          boards(ids:$board) {
            items_page(limit:500, cursor:$cursor) {
              cursor
              items {
                id
                name
                group { id }
                column_values { id text value }
              }
            }
          }
        }"""
        data = monday_query(q, {"board": [PRINT_STATUS_BOARD_ID], "cursor": cursor})
        page = (((data.get("boards") or [{}])[0]).get("items_page") or {})
        for item in page.get("items") or []:
            if _clean(item.get("name")) == name:
                return item
        cursor = page.get("cursor")
        if not cursor:
            return None


def monday_create_item(name: str, group_id: str | None) -> dict[str, Any]:
    if group_id:
        q = """mutation($board:ID!, $group:String!, $name:String!) {
          create_item(board_id:$board, group_id:$group, item_name:$name) { id name }
        }"""
        return monday_query(q, {"board": PRINT_STATUS_BOARD_ID, "group": group_id, "name": name})["create_item"]
    q = """mutation($board:ID!, $name:String!) {
      create_item(board_id:$board, item_name:$name) { id name }
    }"""
    return monday_query(q, {"board": PRINT_STATUS_BOARD_ID, "name": name})["create_item"]


def monday_item_has_file(item: dict[str, Any]) -> bool:
    for cv in item.get("column_values") or []:
        if cv.get("id") != PRINT_PACKING_LIST_COLUMN_ID:
            continue
        value = _clean(cv.get("value"))
        text = _clean(cv.get("text"))
        if value not in ("", "null", "[]", "{}") or text:
            return True
    return False


def monday_upload_file(item_id: str, filename: str, pdf_bytes: bytes) -> None:
    token = _clean(os.environ.get("MONDAY_API_TOKEN"))
    query = """mutation($item:ID!, $column:String!, $file:File!) {
      add_file_to_column(item_id:$item, column_id:$column, file:$file) { id }
    }"""
    operations = {
        "query": query,
        "variables": {
            "item": str(item_id),
            "column": PRINT_PACKING_LIST_COLUMN_ID,
            "file": None,
        },
    }
    files = {
        "variables[file]": (filename, pdf_bytes, "application/pdf"),
    }
    r = requests.post(
        MONDAY_FILE_GQL,
        headers={"Authorization": token},
        data={"query": query, "variables": json.dumps(operations["variables"])},
        files=files,
        timeout=90,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Monday file upload failed ({r.status_code}): {r.text[:400]}")
    body = r.json()
    if body.get("errors"):
        raise RuntimeError("; ".join(_clean(e.get("message") or e) for e in body["errors"]))
    if not (((body.get("data") or {}).get("add_file_to_column") or {}).get("id")):
        raise RuntimeError("Monday upload returned no asset ID")


def tb_find_work_order(token: str, wo: str) -> dict[str, Any]:
    q = """query SearchWorkOrders($first:Int!, $search:String) {
      workOrders(first:$first, search:$search) {
        nodes { id installerName onfleetRoutePlanName }
      }
    }"""
    nodes = (tb_query(token, q, {"first": 50, "search": wo}).get("workOrders") or {}).get("nodes") or []
    exact = [n for n in nodes if _clean(n.get("onfleetRoutePlanName")) == wo]
    if exact:
        return exact[0]
    raise RuntimeError(f"Terraboost work order '{wo}' was not uniquely matched")


def download_portal_packing_list(page: Any, work_order_id: int, wo: str) -> tuple[str, bytes]:
    """Download the PDF produced by Terraboost's own Work Order button."""
    page.goto(f"https://manage.terraboost.com/workorders/{work_order_id}", wait_until="domcontentloaded")
    email_box = page.get_by_role("textbox", name="Email Address")
    button = page.get_by_role("button", name="Download Packing List")
    email_box.or_(button).first.wait_for(state="visible", timeout=45000)
    if email_box.is_visible():
        email = _clean(os.environ.get("TERRABOOST_EMAIL"))
        if email.lower().endswith("@terraboost.biz"):
            raise PortalSSORequired("Terraboost portal requires Microsoft SSO for this account")
        password = _clean(os.environ.get("TERRABOOST_PASSWORD"))
        if not email or not password:
            raise RuntimeError("Terraboost portal credentials are not configured")
        email_box.fill(email)
        page.get_by_role("textbox", name="Password").fill(password)
        page.get_by_role("button", name="Sign in").click()
        email_box.wait_for(state="hidden", timeout=30000)
        page.goto(f"https://manage.terraboost.com/workorders/{work_order_id}", wait_until="domcontentloaded")

    try:
        button.wait_for(state="visible", timeout=45000)
    except Exception as exc:
        if email_box.is_visible():
            raise RuntimeError("Terraboost portal remained on the sign-in screen") from exc
        raise RuntimeError(f"Terraboost packing-list button unavailable at {page.url}") from exc
    with page.expect_download(timeout=90000) as download_event:
        button.click()
    download = download_event.value
    pdf_bytes = open(download.path(), "rb").read()
    if not pdf_bytes.startswith(b"%PDF-") or len(pdf_bytes) < 1000:
        raise RuntimeError(f"Terraboost returned an invalid packing-list PDF for '{wo}'")
    filename = download.suggested_filename
    if not filename.lower().endswith(".pdf"):
        raise RuntimeError("Terraboost download did not have a PDF filename")
    return filename, pdf_bytes


def sync_one(route: dict[str, Any], group_id: str | None, token: str, page: Any) -> dict[str, Any]:
    wo_name = _clean(route.get("wo"))
    if not wo_name:
        return {"status": "skipped", "reason": "blank WO"}
    item = monday_find_item_by_name(wo_name)
    if item is not None and monday_item_has_file(item):
        if TEST_WO == wo_name:
            match = tb_find_work_order(token, wo_name)
            filename, pdf_bytes = download_portal_packing_list(page, int(match["id"]), wo_name)
            return {"wo": wo_name, "item_id": item.get("id"), "created": False,
                    "status": "verified_portal_pdf_existing_item", "filename": filename,
                    "bytes": len(pdf_bytes)}
        return {"wo": wo_name, "item_id": item.get("id"), "created": False, "status": "already_has_pdf"}

    match = tb_find_work_order(token, wo_name)
    filename, pdf_bytes = download_portal_packing_list(page, int(match["id"]), wo_name)
    created = item is None
    if created:
        item = monday_create_item(wo_name, group_id)
    monday_upload_file(str(item["id"]), filename, pdf_bytes)
    return {"wo": wo_name, "item_id": item["id"], "created": created,
            "status": "uploaded", "filename": filename, "bytes": len(pdf_bytes)}


def main() -> None:
    result: dict[str, Any] = {
        "board_id": PRINT_STATUS_BOARD_ID,
        "pod": POD_FILTER,
        "not_before": NOT_BEFORE or None,
        "test_wo": TEST_WO or None,
        "processed": [],
        "errors": [],
    }
    board = monday_board_meta()
    group_id = choose_target_group(board)
    result["board_name"] = board.get("name")
    result["group_id"] = group_id
    routes = get_orange_accepted_routes()
    result["candidate_count"] = len(routes)
    if routes:
        token = tb_login()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            try:
                page = context.new_page()
                for route in routes:
                    try:
                        result["processed"].append(sync_one(route, group_id, token, page))
                    except Exception as exc:
                        # Playwright errors can echo filled form values in their
                        # call log. Never print exception text from this path.
                        result["errors"].append({"wo": _clean(route.get("wo")),
                                                 "error_type": type(exc).__name__})
            finally:
                browser.close()
    print("PACKING_SYNC=" + json.dumps(result, separators=(",", ":"), default=str), flush=True)
    if result["errors"]:
        raise RuntimeError(f"Packing sync failed for {len(result['errors'])} work order(s)")


if __name__ == "__main__":
    main()
