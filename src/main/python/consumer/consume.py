import asyncio
import json
from urllib.request import Request, urlopen
from datetime import date, timedelta

import aiohttp as aiohttp
from bs4 import BeautifulSoup

from common.config.app_config import Config
from common.constant.consume import FORM_4_FILTER, API, ENCODING, SEC_PAYLOAD, ARCHIVE_API, BEGIN_DATE, END_DATE
from consumer.consume_txt import consume_and_save_txt_form_4_filing
from consumer.consume_xml import consume_and_save_xml_form_4_filing
from domain.filings_metadata.filings_metadata import FilingsMetadata


def create_request(bytes_length: int) -> Request:
    request = Request(API)
    request.add_header('Authorization', Config.SEC_API_KEY)
    request.add_header('Content-Type', 'application/json; charset=' + ENCODING)
    request.add_header('Content-Length', bytes_length)
    return request


def construct_payload(begin: date, filings_filter: str, forms_to_request: int, start_from: int) -> dict:
    SEC_PAYLOAD.get("query").get("query_string")["query"] = filings_filter.format(begin, begin)
    SEC_PAYLOAD["size"] = forms_to_request
    SEC_PAYLOAD["from"] = start_from
    return SEC_PAYLOAD


def call_sec_api(request_body: dict) -> FilingsMetadata:
    json_payload = json.dumps(request_body)
    payload_bytes = json_payload.encode(ENCODING)
    request = create_request(len(payload_bytes))
    response = urlopen(request, payload_bytes)
    response_body = response.read()
    return FilingsMetadata(**json.loads(response_body.decode(ENCODING)))


async def download_file(session: aiohttp.ClientSession, url: str, retry: bool = True) -> BeautifulSoup or str:
    async with session.get(url) as response:
        content = await response.text()
    if isinstance(content, (aiohttp.ClientConnectionError, aiohttp.ClientTimeout)):
        if retry:  # retry once to see if connection resets, raise exception if it does not
            await asyncio.sleep(1)
            return await download_file(session, url, False)
        raise Exception(f"Error occurred while retrieving filing. {content}")
    if ".txt" in url:
        return content
    else:  # .xml & .htm
        return BeautifulSoup(content, "lxml")


async def download_all_files(urls: list):
    auth_headers = {"Authorization": Config.SEC_API_KEY,
                    "User-Agent": Config.SEC_USER_AGENT,
                    "content-encoding": "gzip",
                    "accept": "text/html,application/xhtml+xml,application/xml"}
    tasks, requests_made = [], 0
    async with aiohttp.ClientSession(headers=auth_headers) as session:
        for url in urls:
            requests_made += 1
            task = asyncio.ensure_future(download_file(session, url))
            tasks.append(task)
            if requests_made == 9:
                await asyncio.sleep(1)
                requests_made = 0
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return results


async def download_form_4_filings(begin: date, end: date, start_from: int = 0, existing_filing_urls: list = None) -> None:
    if begin < end:  # Base Case: date to start query from is before date to stop at (moving backwards in time)
        return

    # Recursive Case: There are filings yet to be downloaded, continue consuming them

    # instantiate arrays to store urls for ensuring forms are not downloaded multiple times
    if existing_filing_urls is None:
        existing_filing_urls = []
    filing_urls_from_this_iteration = []

    # create payload to retrieve filing metadata
    size = 200  # 200 seems to be the max the SEC's api will return
    payload = construct_payload(begin, FORM_4_FILTER, size, start_from)

    # make request to SEC api with custom payload
    sec_response = call_sec_api(payload)

    # iterate over filings returned for the date range specified
    if len(sec_response.filings) > 0:
        for filing in sec_response.filings:
            # there should be some overlap of previous query end date and new query begin date, to ensure no filings are missed
            if filing.linkToFilingDetails is not None and filing.linkToFilingDetails not in existing_filing_urls:
                if filing.ticker is not None and len(filing.ticker) > 0:
                    if any(extension == filing.linkToFilingDetails[-4:] for extension in [".xml", ".htm"]):
                        link_to_filing_details = ARCHIVE_API + "/" + filing.linkToFilingDetails.split("Archives/edgar/data/")[1]
                    else:
                        link_to_filing_details = ARCHIVE_API + "/" + filing.linkToTxt.split("Archives/edgar/data/")[1]
                    existing_filing_urls.append(link_to_filing_details)
                    filing_urls_from_this_iteration.append(link_to_filing_details)
        responses = await download_all_files(filing_urls_from_this_iteration)
        for response in responses:
            if isinstance(response, (BeautifulSoup, str)):
                consumer = consume_and_save_xml_form_4_filing if isinstance(response, BeautifulSoup) else consume_and_save_txt_form_4_filing
                consumer(response, begin)
            else:
                raise Exception(f"Error: Response was of type {type(response)} rather than BeautifulSoup or str")

    # recursive call
    if len(sec_response.filings) < size:  # all filings for this date have been processed
        print(f"{begin} complete")
        new_begin_date = begin - timedelta(days=1)
        await download_form_4_filings(new_begin_date, end, 0, filing_urls_from_this_iteration)
    else:  # more filings left to process for this date
        await download_form_4_filings(begin, end, start_from + size, filing_urls_from_this_iteration)


if __name__ == "__main__":
    asyncio.run(download_form_4_filings(BEGIN_DATE, END_DATE))
