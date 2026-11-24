"""NASA Earthdata search and download wrappers."""
from __future__ import annotations

import earthaccess


def search_aster(bbox, start_date, end_date, short_name="AST_L1A", provider="LPCLOUD", version="004"):
    temporal = (f"{start_date}T00:00:00", f"{end_date}T23:59:59")
    return earthaccess.search_data(
        short_name=short_name,
        bounding_box=tuple(float(v) for v in bbox),
        temporal=temporal,
        provider=provider,
        version=str(version),
    )


def authenticate():
    """Trigger earthaccess' configured interactive/persisted login flow."""
    return earthaccess.login()


def download_granules(granules, destination, threads=4):
    return earthaccess.download(granules, local_path=str(destination), threads=int(threads), show_progress=True)
