"""Legacy Dashboard route: forwards to the unified Home & Dashboard page."""

import streamlit as st

st.switch_page("views/home.py")
