"""Fetch the authoritative registers: DfE GIAS, CQC active locations, NHS ODS."""
import datetime as dt
import logging
import re

import requests

from critical_buildings.bronze import download, manifest

log = logging.getLogger(__name__)


def _reachable(url):
    """GET (not HEAD: the DfE server answers HEAD with an error) and drop the body."""
    try:
        with requests.get(url, stream=True, timeout=60, headers=download.HEADERS) as r:
            return r.status_code == 200
    except requests.RequestException:
        return False


def fetch_dfe(dry_run=False, today=None):
    """GIAS publishes a date-stamped file; try today and the previous days."""
    cat = manifest.load_sources()["dfe_gias"]
    today = today or dt.date.today()
    for back in range(4):
        url = cat["url"].format(yyyymmdd=(today - dt.timedelta(days=back)).strftime("%Y%m%d"))
        if dry_run or _reachable(url):
            return download.snapshot("dfe_gias", [{"name": "edubasealldata.csv", "url": url}], dry_run=dry_run)
    raise RuntimeError("No GIAS file found for the last 4 days")


def resolve_cqc_url(page_html, base="https://www.cqc.org.uk", pattern=r"HSCA_Active_Locations\.ods"):
    """The CQC file link changes every month: find it on the 'using CQC data' page."""
    m = re.search(rf'href="([^"]*{pattern})"', page_html)
    if not m:
        raise RuntimeError("Could not find the CQC HSCA active locations link on the CQC page")
    link = m.group(1)
    return link if link.startswith("http") else base + link


def fetch_cqc(dry_run=False):
    cat = manifest.load_sources()["cqc_hsca"]
    html = requests.get(cat["page"], timeout=60, headers=download.HEADERS).text
    url = resolve_cqc_url(html, pattern=cat["link_pattern"])
    return download.snapshot("cqc_hsca", [{"name": "active_locations.ods", "url": url}], dry_run=dry_run)


def fetch_nhs(dry_run=False):
    cat = manifest.load_sources()["nhs_ods"]
    specs = [{"name": f"{r}.csv", "url": cat["url"].format(report=r)} for r in cat["reports"]]
    return download.snapshot("nhs_ods", specs, dry_run=dry_run)
