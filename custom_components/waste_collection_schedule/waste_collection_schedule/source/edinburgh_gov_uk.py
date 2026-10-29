"""Waste collection schedules for the City of Edinburgh Council."""

import calendar
import re
from collections.abc import Iterable
from datetime import date
from html import unescape
from typing import ClassVar, final
from urllib.parse import urljoin

from waste_collection_schedule import parsers, preprocessors, response_shape, retrievers
from waste_collection_schedule import waste_types as wt
from waste_collection_schedule.base_source import BaseSource
from waste_collection_schedule.config_params import street
from waste_collection_schedule.exceptions import (
    SourceArgumentNotFoundWithSuggestions,
)
from waste_collection_schedule.transformers import ICSTransformer

_BASE_URL = "https://www.edinburgh.gov.uk"
_SEARCH_URL = f"{_BASE_URL}/directory/search"
_DIRECTORY_ID = "10251"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    "Accept-Language": "en-GB,en;q=0.9",
}

_RECORD_RE = re.compile(
    r'href=["\'](?P<url>/directory-record/\d+/[^"\']+)["\'][^>]*>'
    r"(?P<name>[^<]+)</a>",
    re.IGNORECASE,
)

_CALENDAR_RE = re.compile(
    r'href=["\'](?P<url>https://www\.edinburgh\.gov\.uk/'
    r'downloads/file/\d+/[^"\']+)["\']',
    re.IGNORECASE,
)

_YEAR_RE = re.compile(r"\b(20\d{2})\b")

_TYPE_MAP = {
    "Green recycling": wt.RECYCLABLES,
    "Grey waste and glass": wt.GENERAL_WASTE,
}

_MONTH_GRIDS = (
    (1, 44.04, 210.84, 443.16),
    (2, 234.60, 416.28, 443.16),
    (3, 440.04, 607.32, 443.16),
    (4, 631.08, 798.36, 443.16),
    (5, 44.04, 210.84, 277.08),
    (6, 234.60, 416.28, 277.08),
    (7, 440.04, 607.32, 277.08),
    (8, 631.08, 798.36, 277.08),
    (9, 44.04, 210.84, 112.32),
    (10, 234.60, 416.28, 112.32),
    (11, 440.04, 607.32, 112.32),
    (12, 631.08, 798.36, 112.32),
)

_COLUMN_WIDTH = 23.88
_ROW_HEIGHT = 19.68


def _normalise(value: str) -> str:
    """Normalise a street name for matching."""
    return " ".join(value.split()).casefold()


def _record_url(response, *_, street: str, **__) -> str:
    """Resolve the supplied street name to an Edinburgh directory record."""
    matches = []

    for match in _RECORD_RE.finditer(response.text):
        name = unescape(" ".join(match.group("name").split()))
        url = urljoin(
            _BASE_URL,
            unescape(match.group("url")),
        )
        matches.append((name, url))

    wanted = _normalise(street)

    exact = next(
        (item for item in matches if _normalise(item[0]) == wanted),
        None,
    )

    if exact is not None:
        return exact[1]

    suggestions = [
        name
        for name, _url in matches
        if wanted in _normalise(name) or _normalise(name) in wanted
    ]

    if not suggestions:
        suggestions = [name for name, _url in matches]

    raise SourceArgumentNotFoundWithSuggestions(
        "street",
        street,
        suggestions,
    )


def _calendar_url(response, *_, **__) -> str:
    """Extract the kerbside calendar PDF URL from a street record."""
    match = _CALENDAR_RE.search(response.text)

    response_shape.expect(
        match is not None,
        source_name="City of Edinburgh Council",
        detail="street record returned no kerbside calendar URL",
        raw=response.text[:500],
    )

    assert match is not None

    return match.group("url")


def _calendar_date_from_vector(
    vector: parsers.PdfVector,
    year: int,
) -> date | None:
    """Convert a calendar marker position into its printed date."""
    centre_x = (vector.x0 + vector.x1) / 2
    centre_y = (vector.y0 + vector.y1) / 2

    for month, left, right, top_row_y in _MONTH_GRIDS:
        if not left <= centre_x <= right:
            continue

        row = round((top_row_y - centre_y) / _ROW_HEIGHT)

        column = round((centre_x - (left + _COLUMN_WIDTH / 2)) / _COLUMN_WIDTH)

        if not 0 <= row <= 5:
            continue

        if not 0 <= column <= 6:
            continue

        first_weekday, days_in_month = calendar.monthrange(
            year,
            month,
        )

        day = row * 7 + column - first_weekday + 1

        if not 1 <= day <= days_in_month:
            return None

        return date(
            year,
            month,
            day,
        )

    return None


def _is_green_marker(
    vector: parsers.PdfVector,
) -> bool:
    """Return True for an outlined green-recycling calendar marker."""
    width = vector.x1 - vector.x0
    height = vector.y1 - vector.y0

    if not (23.5 <= width <= 24.5 and 19 <= height <= 20.5):
        return False

    return vector.stroke_color == (0.0, 0.0, 0.0) and vector.fill_color == (0.0,)


def _is_grey_marker(
    vector: parsers.PdfVector,
) -> bool:
    """Return True for a filled grey-waste calendar marker."""
    width = vector.x1 - vector.x0
    height = vector.y1 - vector.y0

    if not (23.5 <= width <= 24.5 and 19 <= height <= 20.5):
        return False

    return vector.stroke_color == (0.0,) and vector.fill_color == (0.0, 0.0, 0.0)


def _calendar_rows(
    records: parsers.PdfDocumentLayout,
    source: BaseSource | None = None,
) -> Iterable[tuple[date, str]]:
    """Extract Edinburgh collection dates from the kerbside calendar."""
    layout = records

    year_match = _YEAR_RE.search(layout.text)

    source_name = response_shape.source_name(source)

    response_shape.expect(
        year_match is not None,
        source_name=source_name,
        detail="could not determine year from Edinburgh calendar PDF",
        raw=layout.text[:500],
    )

    assert year_match is not None

    year = int(year_match.group(1))

    green_dates: set[date] = set()
    grey_dates: set[date] = set()

    for vector in layout.vectors:
        collection_date = _calendar_date_from_vector(
            vector,
            year,
        )

        if collection_date is None:
            continue

        if _is_green_marker(vector):
            green_dates.add(collection_date)

        elif _is_grey_marker(vector):
            grey_dates.add(collection_date)

    response_shape.expect(
        bool(green_dates),
        source_name=source_name,
        detail="no green recycling dates found in Edinburgh calendar PDF",
        raw=layout.text[:500],
    )

    response_shape.expect(
        bool(grey_dates),
        source_name=source_name,
        detail="no grey waste dates found in Edinburgh calendar PDF",
        raw=layout.text[:500],
    )

    for collection_date in sorted(green_dates):
        yield collection_date, "Green recycling"

    for collection_date in sorted(grey_dates):
        yield collection_date, "Grey waste and glass"


@final
class Source(BaseSource):
    TITLE = "City of Edinburgh Council"
    DESCRIPTION = "Waste collection schedules for the City of Edinburgh Council."
    URL = "https://www.edinburgh.gov.uk/bin-collection-days"
    COUNTRY = "uk"
    RAISE_ON_EMPTY = True

    WASTE_TYPES: ClassVar[list] = [
        wt.RECYCLABLES,
        wt.GENERAL_WASTE,
    ]

    TEST_CASES: ClassVar[dict] = {
        "Ellen's Glen Road": {
            "street": "Ellen's Glen Road",
        },
        "Silverknowes Gardens": {
            "street": "Silverknowes Gardens",
        },
    }

    PARAMS = (street(),)

    HOWTO: ClassVar[dict[str, str]] = {
        "en": (
            "Enter the street name exactly as listed by the "
            "City of Edinburgh Council bin collection service."
        )
    }

    retrieve = retrievers.LookupChainRetriever(
        steps=(
            retrievers.Lookup(
                _SEARCH_URL,
                params=lambda street, **_: {
                    "directoryID": _DIRECTORY_ID,
                    "showInMap": "",
                    "keywords": street,
                    "search": "Search directory",
                },
                headers=_HEADERS,
                raise_for_status=True,
                pick=_record_url,
            ),
            retrievers.Lookup(
                lambda record_url, **_: record_url,
                headers=_HEADERS,
                raise_for_status=True,
                pick=_calendar_url,
            ),
        ),
        url=lambda _record_url, calendar_url, **_: calendar_url,
        headers=_HEADERS,
        raise_for_status=True,
        timeout=60,
    )

    parse = parsers.PdfLayoutParser(
        min_fragments=50,
        min_vectors=40,
    )

    preprocess = preprocessors.Compose(
        _calendar_rows,
        preprocessors.Deduplicate(),
        preprocessors.SortRows(),
    )

    transform = ICSTransformer(type_value_map=_TYPE_MAP)
