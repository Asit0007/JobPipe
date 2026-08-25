import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from jobpipe.normalize import canon_company, canon_title, fingerprint, is_near_duplicate


def test_company_suffixes_collapse():
    assert canon_company("Northwind Ltd.") == canon_company("Northwind Private Limited")
    assert canon_company("Acme Technologies Pvt Ltd") == "acme"


def test_seniority_stripped_from_title():
    assert canon_title("Senior DevOps Engineer") == canon_title("DevOps Engineer")
    assert canon_title("Sr. Site Reliability Engineer II") == canon_title("Site Reliability Engineer")


def test_noise_stripped():
    assert "urgent" not in canon_title("URGENT Hiring DevOps Engineer 3-5 yrs")


def test_bengaluru_matches_bangalore():
    a = fingerprint("Acme", "DevOps Engineer", "Bengaluru, KA")
    b = fingerprint("Acme", "DevOps Engineer", "Bangalore, KA")
    assert a == b


def test_near_duplicate_catches_reworded_repost():
    a = {"company": "Acme Pvt Ltd", "title": "DevOps Engineer - Cloud"}
    b = {"company": "Acme Limited", "title": "Cloud DevOps Engineer"}
    assert is_near_duplicate(a, b)


def test_different_companies_never_duplicate():
    a = {"company": "Acme", "title": "DevOps Engineer"}
    b = {"company": "Globex", "title": "DevOps Engineer"}
    assert not is_near_duplicate(a, b)


def test_different_locations_are_not_duplicates():
    # A Toronto req and a New York req are two jobs, however alike the titles.
    a = {"company": "Acme", "title": "Account Executive, Canada", "location": "Toronto, ON"}
    b = {"company": "Acme", "title": "Account Executive | Commercial", "location": "New York, NY"}
    assert not is_near_duplicate(a, b)


def test_location_is_ignored_when_one_side_lacks_it():
    a = {"company": "Acme", "title": "DevOps Engineer - Cloud", "location": ""}
    b = {"company": "Acme", "title": "Cloud DevOps Engineer", "location": "Bangalore"}
    assert is_near_duplicate(a, b)


def test_same_location_reworded_title_is_still_a_duplicate():
    a = {"company": "Acme Pvt Ltd", "title": "DevOps Engineer - Cloud", "location": "Bengaluru"}
    b = {"company": "Acme Limited", "title": "Cloud DevOps Engineer", "location": "Bangalore, KA"}
    assert is_near_duplicate(a, b)
