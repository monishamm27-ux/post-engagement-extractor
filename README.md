# Twitter/X URL Engagement Extractor

Streamlit app that takes a CSV or Excel of tweet URLs and appends **Views,
Likes, Comments, Retweets** columns by calling the Apify Tweet Scraper.

## Layout

```
twitter-engagement-extractor/
├── app.py           # Streamlit UI
├── config.py        # env/token + actor fallback list
├── file_io.py       # read/merge/write (CSV + XLSX)
├── url_utils.py     # tweet URL parsing & validation
├── scraper.py       # Apify calls + automatic actor fallback
├── requirements.txt
├── .env.example
└── sample_urls.csv
```

## Setup

```bash
cd twitter-engagement-extractor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and paste your Apify token
```

Get an Apify token: https://console.apify.com/account/integrations

## Run

```bash
streamlit run app.py
```

Then in the browser:

1. Upload a `.xlsx` or `.csv` file containing a **URL** column.
2. Click **Fetch engagement metrics**.
3. Download the enriched Excel or CSV.

## How it works

- **Validation** — each URL is matched against
  `https://(x|twitter).com/<user>/status/<id>`. Invalid rows are flagged in
  the `Status` column; duplicates share metrics with their first occurrence.
- **Fallback** — the primary actor (`apidojo/tweet-scraper`) is tried first.
  If it fails or omits some tweets, the app automatically retries the missing
  URLs on the next actor in `config.DEFAULT_ACTORS`.
- **Unavailable tweets** — deleted / private / suspended tweets leave the
  metric cells blank and record the reason in the `Note` column.

## Output columns

Original columns are preserved. Appended:

| Column   | Meaning                                        |
|----------|------------------------------------------------|
| Views    | Impression / view count                        |
| Likes    | Like / favorite count                          |
| Comments | Reply count                                    |
| Retweets | Retweet + repost count                         |
| Status   | `ok`, `invalid_url`, `not_found`, `duplicate`  |
| Note     | Reason when Status is not `ok`                 |
