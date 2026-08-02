"""Client construction and explicit test injection for management pages."""

from __future__ import annotations

from collections.abc import Callable

import streamlit as st

from .client import ManagementAPIClient

_client_factory: Callable[[], ManagementAPIClient] = ManagementAPIClient


def set_management_client_factory(factory: Callable[[], ManagementAPIClient]) -> None:
    global _client_factory
    _client_factory = factory


def get_management_client():
    if "management_api_client" not in st.session_state:
        st.session_state.management_api_client = _client_factory()
    return st.session_state.management_api_client
