"""The columns of the property table, in output order, each with a group and a plain-English description.

Building the table stops if it produces a column that is not described here, as the buildings file does (dictionary.py).
"""
COLUMNS = [
    # (name, group, description)
    ("uprn", "Identity", "Unique Property Reference Number from OS Open UPRN: one per addressable location (a house, a flat, a shop, a "
                         "bus shelter). OS Open UPRN does not say which are homes."),
    ("easting", "Location", "British National Grid easting of the UPRN point, in metres (EPSG:27700)."),
    ("northing", "Location", "British National Grid northing of the UPRN point, in metres."),
    ("latitude", "Location", "WGS84 latitude of the UPRN point."),
    ("longitude", "Location", "WGS84 longitude of the UPRN point."),
    ("os_tile", "Location", "The 100 km OS grid square the point is in (e.g. TM). The table is stored one folder per square."),

    ("building_id", "Building", "ID of the OS Open Map Local building polygon the UPRN point lies inside. Blank when the point is not inside "
                                "any footprint (a garden, a caravan, a bus shelter)."),
    ("building_area_m2", "Building", "Footprint area of that building, in square metres."),
    ("uprns_in_building", "Building", "How many UPRNs share that building (in the same grid square). 1 suggests a house; many suggests flats "
                                      "or a building with several occupiers. 0 when the UPRN is not inside a building."),

    ("postcode", "Postcode", "The nearest Code-Point Open postcode point to the UPRN. A guide, not the address's own postcode: the nearest "
                             "postcode centre is often a neighbouring postcode. OS Open UPRN carries no addresses, so this is the best "
                             "open link; the EPC register (not yet added) has real address postcodes."),
    ("postcode_distance_m", "Postcode", "Distance from the UPRN to that postcode point, in metres. Blank when none is within 250 m."),

    ("lsoa_code", "Area", "Census 2021 Lower layer Super Output Area (about 1,500 people) the point is in, from the generalised ONS outlines."),
    ("lsoa_name", "Area", "Name of that LSOA."),
    ("lsoa_method", "Area", "inside = the point is inside the LSOA outline. nearest = it fell just outside the generalised outlines (the coast, "
                            "a boundary gap) and took the nearest LSOA within 300 m."),
    ("lad_code", "Area", "Local authority district (2024) of the LSOA, from the deprivation file. Blank for areas outside England."),
    ("lad_name", "Area", "Name of that local authority district."),

    ("imd_score", "Deprivation", "English Index of Multiple Deprivation 2025 score of the LSOA (higher = more deprived). An area measure, "
                                 "not a measure of the household."),
    ("imd_rank", "Deprivation", "Rank of the LSOA among England's 33,755 (1 = most deprived)."),
    ("imd_decile", "Deprivation", "Decile of the LSOA (1 = most deprived 10%)."),
    ("income_decile", "Deprivation", "Income deprivation domain decile (1 = most deprived)."),
    ("employment_decile", "Deprivation", "Employment deprivation domain decile."),
    ("education_decile", "Deprivation", "Education, skills and training domain decile."),
    ("health_decile", "Deprivation", "Health deprivation and disability domain decile."),
    ("crime_decile", "Deprivation", "Crime domain decile."),
    ("housing_barriers_decile", "Deprivation", "Barriers to housing and services domain decile."),
    ("living_environment_decile", "Deprivation", "Living environment domain decile (housing quality, air quality, road traffic accidents)."),
    ("idaci_decile", "Deprivation", "Income Deprivation Affecting Children Index decile."),
    ("idaopi_decile", "Deprivation", "Income Deprivation Affecting Older People Index decile."),
    ("lsoa_population", "Deprivation", "Total population of the LSOA, mid-2022."),
    ("lsoa_children_0_15", "Deprivation", "Dependent children aged 0 to 15 in the LSOA, mid-2022."),
    ("lsoa_aged_60_plus", "Deprivation", "People aged 60 and over in the LSOA, mid-2022."),

    ("conservation_area", "Planning", "True when the point is inside a conservation area on planning.data.gov.uk."),
    ("conservation_area_name", "Planning", "Name of that conservation area."),
    ("article_4_area", "Planning", "True when the point is inside an Article 4 direction area (permitted-development rights withdrawn; often "
                                   "used to control conversion to HMOs). Only areas councils have published are included."),
    ("listed_in_outline", "Planning", "True when the point is inside a listed building outline from Historic England."),
    ("listed_grade", "Planning", "Grade (I, II*, II) of the nearest Historic England listing point within 10 m (a setting). Listing points are "
                                 "rough, so this can name a neighbouring listed building and can miss a listed one whose point is further "
                                 "off; listed_in_outline is the sure case where an outline exists."),
    ("listed_distance_m", "Planning", "Distance to that listing point, in metres (10 m or less)."),

    ("flood_zone", "Flood", "Environment Agency Flood Map for Planning zone from rivers and the sea: 3 = high (1 in 100 a year from rivers, "
                            "1 in 200 from the sea), 2 = medium (1 in 1,000), 1 = low or none. Ignores flood defences. Not surface water "
                            "or groundwater."),
    ("flood_zone_type", "Flood", "For zone 2 or 3, how the flood zone was modelled (e.g. Fluvial Models, Tidal Models); blank for zone 1."),

    ("critical_site_id", "Critical sites", "Site ID of the critical-buildings site whose outline contains the point, from the current "
                                          "critical-buildings run. Blank when the point is in no critical site."),
    ("critical_site_type", "Critical sites", "Site Type of that site (or, when not inside one, of the nearest within 1 km)."),
    ("critical_site_importance", "Critical sites", "Operational Importance of that site (High, Medium, Low)."),
    ("critical_site_distance_m", "Critical sites", "0 when the point is inside the site outline; otherwise the distance in metres to the nearest "
                                                   "critical site within 1 km; blank when there is none. The ID, type and importance "
                                                   "above are those of that nearest site."),
]

NAMES = [c[0] for c in COLUMNS]
DESCRIPTION = {name: (group, text) for name, group, text in COLUMNS}
