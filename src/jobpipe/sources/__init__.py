from .greenhouse import fetch as fetch_greenhouse
from .lever import fetch as fetch_lever
from .ashby import fetch as fetch_ashby
from .adzuna import fetch as fetch_adzuna
from .gmail_alerts import fetch as fetch_gmail_alerts
from .remotive import fetch as fetch_remotive
from .remoteok import fetch as fetch_remoteok
from .arbeitnow import fetch as fetch_arbeitnow
from .jobicy import fetch as fetch_jobicy
from .himalayas import fetch as fetch_himalayas

# The five below greenhouse/lever/ashby are GLOBAL feeds, not company-scoped:
# no slug to maintain, and nothing to add to companies.yaml. They are also
# aggregators rather than first-party boards, so unlike the three ATS sources
# they are NOT in AUTHORITATIVE_SOURCES -- the fuzzy repost check runs over
# them, which is exactly what it was written for.
ALL_SOURCES = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "adzuna": fetch_adzuna,
    "gmail_alert": fetch_gmail_alerts,
    "remotive": fetch_remotive,
    "remoteok": fetch_remoteok,
    "arbeitnow": fetch_arbeitnow,
    "jobicy": fetch_jobicy,
    "himalayas": fetch_himalayas,
}
