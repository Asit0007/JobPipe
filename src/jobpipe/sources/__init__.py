from .greenhouse import fetch as fetch_greenhouse
from .lever import fetch as fetch_lever
from .ashby import fetch as fetch_ashby
from .adzuna import fetch as fetch_adzuna
from .gmail_alerts import fetch as fetch_gmail_alerts

ALL_SOURCES = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "adzuna": fetch_adzuna,
    "gmail_alert": fetch_gmail_alerts,
}
