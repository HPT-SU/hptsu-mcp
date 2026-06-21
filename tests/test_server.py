"""Smoke checks for the FastMCP server wiring."""
from __future__ import annotations

import pytest

from hptsu_mcp.server import PAGE_SIZE_MAX, REGISTRY_KINDS, mcp


def test_registry_kinds_documented() -> None:
    assert {"cert", "decl", "otts", "sbkts", "otch", "sout"} <= REGISTRY_KINDS.keys()


def test_mcp_app_constructed() -> None:
    assert mcp.name == "hpt-su"


def test_page_size_max_is_50() -> None:
    assert PAGE_SIZE_MAX == 50


@pytest.mark.asyncio
async def test_all_15_tools_registered() -> None:
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "search_documents", "get_document",
        "search_certificates", "search_declarations",
        "search_type_approvals", "search_safety_reports",
        "search_by_vin", "fulltext_search",
        "list_document_files", "download_document_file",
        "list_brands", "list_vehicle_models",
        "list_test_labs", "list_certification_bodies",
        "list_tnved_codes", "list_registry_kinds",
    }
    assert expected <= names, f"Missing tools: {expected - names}"
