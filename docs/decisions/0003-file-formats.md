# 0003: GeoPackage for deliverables, GeoParquet/Parquet for silver, zips read in place

**Context.** Consumers use GIS tools and spreadsheets; the pipeline needs fast, type-preserving intermediate tables; the OS
downloads are ~190 MB of zipped shapefiles.

**Decision.** Deliver GeoPackage (spatial) and CSV (tabular). Store silver tables as GeoParquet/Parquet, which preserve
types exactly (checked: reusing silver reproduces identical outputs). Keep OS and Code-Point zips as downloaded and read
the shapefiles straight from the zip (GDAL `zip://`), so bronze stays raw and there is no extracted copy to manage.

**Consequences.** Smaller data folder (1.4 GB to under 0.5 GB), bronze is exactly what was downloaded, and silver can be
deleted and rebuilt. A GeoPackage column cannot be called `Geom` (the CSV keeps the WKT text; the GeoPackage holds the
real geometry).
