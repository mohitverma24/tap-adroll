import sys
import backoff
import json
import requests
import singer
from typing import Optional, List, Tuple
from datetime import datetime, date, timedelta
from dateutil import parser
from ratelimit import limits, exception

LOGGER = singer.get_logger()
DELIVERIES_CHUNKS_WEEKS = 26  # 26 weeks is roughly 6 months


def date_chunks(
    start_date: date, increment: timedelta, maximum: date = None
) -> List[Tuple[date, date]]:
    start = start_date
    if not maximum:
        maximum = datetime.utcnow().date() - timedelta(days=1)

    while True:
        end_date = min(start + increment, maximum)
        if start == end_date:
            # API raises 400 if the start and end dates are identical.
            return
        yield (start, end_date)
        start = end_date
        if end_date == maximum:
            return


class AdRoll:
    BASE_URL = "https://services.adroll.com/"

    def __init__(self, config, state, limit=250):
        self.SESSION = requests.Session()
        self.limit = limit
        self.access_token = config["access_token"]
        self.config = config
        self.state = state
        self.advertisables = None
        self.active_campaigns = []

    def sync(self, streams):
        """ Sync data from tap source """
        # TODO: pass the streams
        for tap_stream_id in streams:
            LOGGER.info("Syncing stream:" + tap_stream_id)

            if tap_stream_id == "deliveries":
                self.sync_deliveries(tap_stream_id)
            else:
                self.sync_full_table_streams(tap_stream_id)

        singer.write_state(self.state)

    def sync_full_table_streams(self, tap_stream_id):
        for row in self.get_streams(tap_stream_id):
            singer.write_records(tap_stream_id, [row])

    def get_streams(self, tap_stream_id):
        if tap_stream_id == "advertisables":
            return self.get_advertisables()
        elif tap_stream_id == "campaigns":
            return self.get_campaigns()
        else:
            LOGGER.info(f"UNKNOWN STREAM: {tap_stream_id}")
            return []

    def get_advertisables(self):
        self.advertisables = self.call_api(url="api/v1/organization/get_advertisables",)
        return json.loads(
            json.dumps(self.advertisables), parse_int=str, parse_float=str
        )

    def get_campaigns(self):
        campaigns = []
        if self.advertisables and len(self.advertisables) > 0:
            for advertisable in self.advertisables:
                campaigns += self.call_api(
                    url="api/v1/advertisable/get_campaigns_fast",  # 🏎️ 💨 💨
                    params={"advertisable": advertisable["eid"]},
                )
        self.active_campaigns = [
            {
                "eid": campaign["eid"],
                "advertisable": campaign["advertisable"],
                "start_date": campaign["start_date"],
                "created_date": campaign["created_date"],
                "end_date": campaign["end_date"],
                "is_active": campaign["is_active"],
                "updated_date": campaign["updated_date"],
            }
            for campaign in campaigns
        ]
        return json.loads(json.dumps(campaigns), parse_int=str, parse_float=str)

    @backoff.on_exception(
        backoff.expo,
        (requests.exceptions.RequestException, exception.RateLimitException),
        max_tries=5,
        factor=2,
        giveup=lambda e: e.response.status_code in [429],  # too many requests
    )
    @limits(calls=100, period=10)
    def call_api(self, url, params={}):
        url = f"{self.BASE_URL}{url}"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        response = self.SESSION.get(url, headers=headers, params=params)

        LOGGER.info(response.url)
        response.raise_for_status()
        response_json = response.json()

        return response_json["results"]

    def sync_deliveries(self, tap_stream_id):
        state = self.state
        for campaign in self.active_campaigns:
            eid = campaign.get("eid")
            if not eid:
                LOGGER.error(f"{campaign} has no attribute 'eid'")
                continue
            # date of last sync, otherwise campaign start
            sync_start_date = self.get_campaign_sync_start_date(
                tap_stream_id, state, campaign
            )
            # date of campaign end if ended, otherwise None
            campaign_end_date = self.get_campaign_end_date(campaign)

            # everything synced and campaign ended (not active)
            if (
                campaign_end_date
                and (sync_start_date >= campaign_end_date)
                and not campaign["is_active"]
            ):
                LOGGER.info(
                    f"(skipping) campaign: {eid} start_date: {sync_start_date} end_date: {campaign_end_date}"
                )
                continue

            LOGGER.info(
                f"(syncing) campaign: {eid} start_date: {sync_start_date} end_date: {campaign_end_date}"
            )
            state = self.bulk_read_campaign_deliveries_from_dates(
                tap_stream_id=tap_stream_id,
                state=state,
                campaign=campaign,
                sync_start_date=sync_start_date,
                campaign_end_date=campaign_end_date,
            )
            self.state = state

    def bulk_read_campaign_deliveries_from_dates(
        self,
        tap_stream_id,
        state,
        campaign,
        sync_start_date,
        campaign_end_date: Optional[datetime],
    ):
        for start_date, end_date in date_chunks(
            start_date=sync_start_date,
            increment=timedelta(weeks=DELIVERIES_CHUNKS_WEEKS),
            maximum=campaign_end_date,
        ):
            eid = campaign["eid"]
            LOGGER.info(
                f"(advancing) campaign: {eid} start_date: {start_date} end_date: {end_date}"
            )
            api_result = self.get_campaign_deliveries(campaign, start_date, end_date)
            state = self.write_campaign_deliveries_records_and_advance_state(
                tap_stream_id, state, campaign, api_result
            )

        return state

    def get_campaign_sync_start_date(self, tap_stream_id, state, campaign):
        """
            If we are able to find the date in bookmarks, we add one day to that date.
            We add one day, because the start_dates are previous end_dates for which we already have data
            ex. the resulting payload contains 2018-01-01 as last date we keep that last date in state
            and the next day we use it as start date, but we already have data for that day,
            so we need to set it to 2018-01-02
        """
        if state and state.get("bookmarks", {}).get(tap_stream_id, None):
            synced_campaigns = state["bookmarks"][tap_stream_id]
            if synced_campaigns and synced_campaigns.get(campaign["eid"], None):
                return datetime.strptime(
                    synced_campaigns[campaign["eid"]], "%Y-%m-%dT%H:%M:%S"
                ).date() + timedelta(days=1)

        campaign_start_date = campaign.get("start_date") or campaign.get("created_date")
        campaign_start_date = datetime.strptime(
            campaign_start_date, "%Y-%m-%dT%H:%M:%S%z"
        ).replace(tzinfo=None)
        return campaign_start_date.date()

    def get_campaign_end_date(self, campaign):
        campaign_end_date = campaign.get("end_date")
        if campaign_end_date:
            return (
                datetime.strptime(campaign_end_date, "%Y-%m-%dT%H:%M:%S%z")
                .replace(tzinfo=None)
                .date()
            )

    def write_campaign_deliveries_records_and_advance_state(
        self, tap_stream_id, state, campaign, api_result
    ):
        eid = campaign["eid"]
        advertisable_eid = campaign["advertisable"]
        for summary in api_result["date"]:
            row = {
                "campaign_eid": eid,
                "advertisable_eid": advertisable_eid,
                **summary,
            }
            singer.write_records(tap_stream_id, [row])

        last_date_from_payload = api_result["date"][-1]["date"]
        return self.__advance_bookmark(
            state=state,
            tap_stream_id=tap_stream_id,
            bookmark_key=eid,
            bookmark_value=datetime.strptime(
                last_date_from_payload, "%Y-%m-%d"
            ).isoformat(),
        )

    def get_campaign_deliveries(self, campaign, start_date, end_date):
        try:
            return self.call_api(
                url="uhura/v1/deliveries/campaign",
                params={
                    "breakdowns": "date",
                    "currency": "USD",
                    "advertisable_eid": campaign["advertisable"],
                    "campaign_eids": campaign["eid"],
                    "start_date": start_date.strftime("%Y-%m-%d"),
                    "end_date": end_date.strftime("%Y-%m-%d"),
                },
            )
        except requests.exceptions.HTTPError as exc:
            if exc.response.status_code in [429]:
                LOGGER.error(exc)
                LOGGER.info(self.state)
                singer.write_state(self.state)
                sys.exit(0)
            raise

    def __advance_bookmark(self, state, tap_stream_id, bookmark_key, bookmark_value):
        if not bookmark_value:
            singer.write_state(state)
            return state

        state = singer.write_bookmark(
            state, tap_stream_id, bookmark_key, bookmark_value
        )
        singer.write_state(state)
        return state
