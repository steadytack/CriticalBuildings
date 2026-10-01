import pytest

from critical_buildings.gold.merge import name_similarity, strict_similarity


@pytest.mark.parametrize("a,b,low,high", [
    ("Whatfield Church of England Voluntary Controlled Primary School",
     "Whatfield Church of England (Voluntary Controlled) Primary School", 0.99, 1.0),
    ("Ravensmere Infants School", "Ravensmere Infant School", 0.99, 1.0),       # plural / singular stop word
    ("Earl Soham Community Primary School", "Earl Soham Community Primary School", 1.0, 1.0),
    ("Wetheringsett Manor School", "Wetheringsett Church of England Primary School", 0.0, 0.94),  # shared village only
    # different villages that merely share an ending: must stay below the 0.6 'good name match' threshold
    ("Thurston Primary Academy", "Norton Primary School", 0.0, 0.59),
])
def test_name_similarity_ranges(a, b, low, high):
    assert low <= name_similarity(a, b) <= high


def test_missing_names_never_match():
    assert name_similarity("", "Anything") == 0.0
    assert name_similarity(None, None) == 0.0
    assert strict_similarity("", "x") == 0.0


def test_generic_words_alone_do_not_hide_distinct_places():
    # after dropping generic words both reduce to 'nayland', so the loose score is 1.0 ...
    assert name_similarity("Nayland Surgery", "Nayland House Care Home") == 1.0
    # ... which is exactly why cross-type matching uses the strict full-name comparison
    assert strict_similarity("Nayland Surgery", "Nayland House Care Home") < 0.9


def test_strict_similarity_ignores_case_and_punctuation():
    assert strict_similarity("St. Mary's  School", "st marys school") == 1.0
    assert strict_similarity("Glastonbury Court", "Glastonbury Court") == 1.0
    assert strict_similarity("Lothingland Hospital", "Lothingland") < 0.9
