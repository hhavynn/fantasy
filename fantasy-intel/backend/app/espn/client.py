"""Raw ESPN fantasy API access.

Uses `espn_api`'s request layer (endpoint selection, cookie auth, error
handling) but returns the raw JSON so we keep every source field and ID.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests
from espn_api.requests.espn_requests import EspnFantasyRequests

from .constants import (PLAYER_COUNT_HEADER, PROJECTION_LIMIT, PROJECTION_STATUSES, RESPONSE_META_KEY,
                        TRANSACTION_TYPES)

log = logging.getLogger(__name__)


class EspnClient:
    def __init__(self, league_id: int, season: int, espn_s2: str | None, swid: str | None):
        cookies = {"espn_s2": espn_s2, "SWID": swid} if espn_s2 and swid else None
        self.req = EspnFantasyRequests(sport="nfl", year=season, league_id=league_id, cookies=cookies)
        self.league_id = league_id
        self.season = season

    @staticmethod
    def _filter(obj: dict) -> dict:
        return {"x-fantasy-filter": json.dumps(obj)}

    def league(self) -> dict:
        return self.req.league_get(params={"view": ["mTeam", "mRoster", "mMatchup", "mSettings", "mStandings"]})

    def roster_for_period(self, scoring_period: int) -> dict:
        return self.req.league_get(params={"view": ["mRoster", "mTeam"], "scoringPeriodId": scoring_period})

    def transactions(self, scoring_period: int) -> dict:
        return self.req.league_get(
            params={"view": "mTransactions2", "scoringPeriodId": scoring_period},
            headers=self._filter({"transactions": {"filterType": {"value": TRANSACTION_TYPES}}}),
        )

    def activity(self, offset: int = 0, size: int = 50) -> dict:
        filters = {"topics": {
            "filterType": {"value": ["ACTIVITY_TRANSACTIONS"]},
            "limit": size,
            "limitPerMessageSet": {"value": 25},
            "offset": offset,
            "sortMessageDate": {"sortPriority": 1, "sortAsc": False},
            "sortFor": {"sortPriority": 2, "sortAsc": False},
            "filterIncludeMessageTypeIds": {"value": [178, 180, 179, 239, 181, 244]},
        }}
        return self.req.league_get(
            extend="/communication/", params={"view": "kona_league_communication"},
            headers=self._filter(filters),
        )

    def player_pool(self, scoring_period: int, limit: int = 500) -> dict:
        """Free agents + players on waivers, highest-owned first."""
        filters = {"players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "limit": limit,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }}
        return self.req.league_get(
            params={"view": "kona_player_info", "scoringPeriodId": scoring_period},
            headers=self._filter(filters),
        )

    def player_projections(self, scoring_period: int, limit: int = PROJECTION_LIMIT) -> dict:
        """The league's whole player universe (rostered + free agents + waivers) for one scoring
        period, in one request. Each player's `stats` carries ESPN's projection for that period
        (statSourceId 1, statSplitTypeId 1), including future weeks. `filterStatus` is ESPN's
        supported status filter; a combined ONTEAM + FREEAGENT + WAIVERS request returns exactly
        the union of the separate requests (verified against the real league, 2026 week 6).

        ESPN reports the total number of players matching the filter (independent of `limit` /
        `offset`) in the X-Fantasy-Filter-Player-Count response header. espn_api's league_get
        drops headers, so this request is made directly; the count is attached to the payload
        under RESPONSE_META_KEY and decides whether the response is provably complete."""
        filters = {"players": {
            "filterStatus": {"value": list(PROJECTION_STATUSES)},
            "limit": limit,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }}
        params = {"view": "kona_player_info", "scoringPeriodId": scoring_period}
        headers = self._filter(filters)
        r = self._get(params, headers)
        if r.status_code != 200:
            # espn_api raises for auth / missing league / other errors, and on a 401 may switch to
            # its alternate endpoint; retry once there so the count header is still captured.
            self.req.checkRequestStatus(r.status_code, params=params, headers=headers)
            r = self._get(params, headers)
            if r.status_code != 200:
                raise RuntimeError(f"ESPN returned HTTP {r.status_code}")
        data = r.json()
        data = data[0] if isinstance(data, list) else data
        data[RESPONSE_META_KEY] = {"filter_player_count": _int_or_none(r.headers.get(PLAYER_COUNT_HEADER))}
        return data

    def _get(self, params: dict, headers: dict) -> requests.Response:
        return requests.get(self.req.LEAGUE_ENDPOINT, params=params, headers=headers, cookies=self.req.cookies,
                            timeout=60)

    def pro_players(self) -> Any:
        return self.req.get_pro_players()

    def player_cards(self, player_ids: list[int], max_scoring_period: int) -> dict:
        return self.req.get_player_card(player_ids, max_scoring_period)

    def draft(self) -> dict:
        return self.req.get_league_draft()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
