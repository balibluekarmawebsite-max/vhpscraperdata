"""
uploader.py — POST a downloaded report file to the analytics dashboard.

The dashboard runs its existing flexible column mapping and writes the rows to
Postgres, so we don't re-parse the file or touch the DB directly here. The
`property` field tells the dashboard which hotel the rows belong to.
"""
import os

import requests

import config


def upload(path, property_code, report_name, cfg=None):
    """Upload one file. Returns the HTTP status code on success; raises on
    network error or a non-2xx response.
    """
    if cfg is None:
        cfg = config.UPLOAD

    headers = {}
    if cfg.get("token"):
        headers["Authorization"] = "Bearer {}".format(cfg["token"])

    with open(path, "rb") as fh:
        files = {cfg["field"]: (os.path.basename(path), fh)}
        data = {"property": property_code, "report": report_name}
        resp = requests.post(
            cfg["url"],
            files=files,
            data=data,
            headers=headers,
            timeout=cfg.get("timeout", 120),
        )
    resp.raise_for_status()
    return resp.status_code
