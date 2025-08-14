import streamlit as st
import importlib.metadata

# Latest dhanhq version on PyPI (update this value to latest known as needed)
LATEST_DHANHQ_VERSION = "2.0.2"

def get_installed_version():
    try:
        return importlib.metadata.version('dhanhq')
    except importlib.metadata.PackageNotFoundError:
        return None

def is_version_newer(installed, latest):
    from packaging.version import parse as parse_version
    if installed is None:
        return True
    return parse_version(latest) > parse_version(installed)

# Fetch installed version
installed_version = get_installed_version()

st.markdown("### 📦 DhanHQ Package Version Info")

if installed_version is None:
    st.warning("⚠️ DhanHQ package is not installed!")
elif is_version_newer(installed_version, LATEST_DHANHQ_VERSION):
    st.warning(f"⚠️ Your installed dhanhq version `{installed_version}` is older than the latest available `{LATEST_DHANHQ_VERSION}`. Please upgrade!")
else:
    st.success(f"✅ Your dhanhq package is up to date: `{installed_version}`")

# ... rest of your Streamlit bot code ...
