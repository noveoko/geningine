import json
import yaml
import logging
from pathlib import Path
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Iterable

def setup_logging(module_name="pipeline"):
    logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
    return logging.getLogger(module_name)

def load_config(config_path=None):
    path = config_path or Path(__file__).parent.parent / "config.yaml"
    with open(path, "r") as f:
        return yaml.safe_load(f)

def get_doc_id(original_filename: str) -> str:
    stem = Path(original_filename).name
    digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()
    return digest[:12]


def page_stem(doc_id, page_num):
    return f"{doc_id}_p{page_num:03d}"

def append_manifest(folder: str | Path, entry: dict) -> None:
    if "ts" not in entry:
        entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    mpath = Path(folder) / "_manifest.jsonl"
    with open(mpath, "a", encoding="utf-8") as f:
        f.write(line)

def read_manifest(directory):
    manifest_path = Path(directory) / "_manifest.jsonl"
    if not manifest_path.exists():
        return []
    with open(manifest_path, "r") as f:
        return [json.loads(line) for line in f]

def processed_set(directory):
    manifest = read_manifest(directory)
    return {(item["doc_id"], item["page"]) for item in manifest}

# This is the one specifically missing in your error log
def already_processed(out_dir, doc_id, page_num):
    done = processed_set(out_dir)
    return (doc_id, page_num) in done

from typing import Generator, Iterable

def chunked(iterable: Iterable, size: int) -> Generator[list, None, None]:
    """Yield successive fixed-size chunks from *iterable*."""
    buf: list = []
    for item in iterable:
        buf.append(item)
        if len(buf) == size:
            yield buf
            buf = []
    if buf:
        yield buf

def find_project_root() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in [here, here.parent, here.parent.parent]:
        if (candidate / "config.yaml").exists():
            return candidate
    raise FileNotFoundError("config.yaml not found. Run from project root.")


def manifest_path(folder: str | Path) -> Path:
    return Path(folder) / "_manifest.jsonl"