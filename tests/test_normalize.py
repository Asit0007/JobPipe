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
