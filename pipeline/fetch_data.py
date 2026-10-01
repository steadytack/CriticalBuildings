"""Download the source data for the pilot area (Suffolk) into data/. Skips files already present."""
import zipfile
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

OSM_URL = "https://download.geofabrik.de/europe/united-kingdom/england/suffolk-latest.osm.pbf"
OS_AREAS = ["TL", "TM", "TG"]   # 100 km grid squares covering Suffolk
OS_URL = ("https://api.os.uk/downloads/v1/products/OpenMapLocal/downloads"
          "?area={area}&format=" + quote("ESRI® Shapefile") + "&redirect")


def download(url, dest):
    if dest.exists():
        print("have", dest.name)
        return
    print("downloading", dest.name)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


if __name__ == "__main__":
    download(OSM_URL, DATA / "suffolk-latest.osm.pbf")
    for a in OS_AREAS:
        z = DATA / f"opmplc_essh_{a.lower()}.zip"
        download(OS_URL.format(area=a), z)
        out = DATA / f"opmplc_{a.lower()}"
        if not out.exists():
            with zipfile.ZipFile(z) as zf:
                zf.extractall(out)
    print("done")
