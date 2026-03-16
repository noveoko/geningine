Module 0 (The Harvester): Archive PDF & Scan Download Pipeline
Input/Output

Input File: data/config/harvest_queue.json (A user-curated list of API endpoints, collection IDs, or OAI-PMH queries).

Output Folder: data/input_1_raw_scans/ (Contains the downloaded .pdf or directory of raw .jpg files).

State Tracking Output: data/input_1_raw_scans/harvest_manifest.json (An append-only registry of everything downloaded so far, used to prevent duplicate fetching).

Processing Logic
This module uses a Python-based worker script that reads the queue, identifies the source type, routes it to the correct fetcher function, and handles rate-limiting to prevent IP bans.

Archive.org: Use the official internetarchive Python library. Use the download() function targeting format Text PDF or Single Page Processed JP2 ZIP to grab highest quality scans.

Polona.pl: Interface with Polona's JSON API (https://api.polona.pl/). Fetch the object's metadata, extract the IIIF manifest URL, and download either the bundled PDF (if available) or iterate through the IIIF image endpoints to download high-res JPGs sequentially.

Polish Digital Libraries (FBC/dLibra): Use the Sickle library to parse OAI-PMH metadata endpoints. For actual files, utilize standard requests with BeautifulSoup to scrape the presentation page for the direct PDF/DjVu download link. (Note: DjVu files will need a local conversion step to PDF/PNG using djvulibre).

Szukaj w Archiwach (SzWA): Use the SzWA API (https://www.szukajwarchiwach.gov.pl/api/). Authenticate (free key), query by zespół (fonds) or jednostka (unit), and pull the associated digital scan URLs.

Resiliency: All requests calls must be wrapped in Tenacity for automatic retries with exponential backoff. Files must be written to a .tmp extension until fully downloaded, then renamed to prevent corrupted partial files in the pipeline.

The Data Contract (Harvest Manifest)
Every successful download must append a record to harvest_manifest.json. This metadata will eventually follow the document through to Meilisearch so users know where the original record lives.

JSON
{
  "document_id": "polona_12345678",
  "source_system": "polona",
  "original_url": "https://polona.pl/item/12345678",
  "title": "Ksiegi metrykalne parafii rzymskokatolickiej",
  "fetch_timestamp": "2026-03-16T09:00:00Z",
  "local_path": "data/input_1_raw_scans/polona_12345678.pdf",
  "file_type": "pdf",
  "page_count": 142,
  "checksum_md5": "d41d8cd98f00b204e9800998ecf8427e"
}
The "LLM Context Snippet"
"Write a Python script that acts as 'Module 0' for a genealogy pipeline, reading target IDs from data/config/harvest_queue.json and downloading them to data/input_1_raw_scans/. It must include specific API routing logic for Archive.org (via the internetarchive package), Polona.pl (via API/IIIF), and Polish dLibra libraries (via direct URL fetching). The script must use exponential backoff, write to temporary files during download to prevent corruption, and append a JSON metadata contract to harvest_manifest.json upon completion."