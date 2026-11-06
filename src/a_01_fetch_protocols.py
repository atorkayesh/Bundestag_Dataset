"""Fetch new and revised Plenarprotokoll PDFs from the Bundestag document server.

Discovery probes WWSSS.pdf forward from the last session held locally. A
Wahlperiode is only treated as exhausted after a full window of consecutive
misses, so one unpublished session does not halt the scan.

Revision detection HEADs the most recent sessions and compares the server's
ETag against the manifest. The ETag encodes mtime and size, so a protocol
replaced by its corrected final version is detected without downloading it.

Manifest: <raw-dir>/_fetch_manifest.json — per session the ETag, MD5, first
fetch time, last change time and a revision counter. The counters are what let
REVISION_WINDOW be tuned from observed behaviour rather than guessed.
"""

# defaults — override via CLI flags
_DEFAULT_RAW = r"C:\Users\adama\Sciebo\Adama-Ali\Germany & Politics\bundestag-project\data\raw"

import argparse
import hashlib
import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://dserver.bundestag.de/btp"

# Consecutive misses before a Wahlperiode is considered exhausted. Guards
# against a gap caused by one protocol being published out of order.
PROBE_WINDOW = 20

# How many of the most recent sessions to re-check for revision each run.
# HEAD-only, so the cost is negligible; consult the revision counters in the
# manifest before reducing this.
REVISION_WINDOW = 30

REQUEST_DELAY = 0.4      # seconds between requests, to be a polite client
REQUEST_TIMEOUT = 60
USER_AGENT = "bundestag-corpus-pipeline/1.0 (academic research)"

MANIFEST_NAME = "_fetch_manifest.json"
_PDF_RE = re.compile(r"^(\d{2})(\d{3})\.pdf$", re.IGNORECASE)


@dataclass
class FetchReport:
    started_at: str = ""
    finished_at: str = ""
    wp: int = 0
    new_sessions: list = field(default_factory=list)
    revised_sessions: list = field(default_factory=list)
    backfilled_pdfs: list = field(default_factory=list)
    checked_for_revision: int = 0
    notes: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    dry_run: bool = False

    @property
    def changed(self) -> list:
        """Sessions whose text must be (re)processed downstream."""
        return self.new_sessions + self.revised_sessions

    @property
    def downloaded(self) -> list:
        return self.new_sessions + self.revised_sessions + self.backfilled_pdfs


def session_id(wp: int, sess: int) -> str:
    return f"{wp:02d}{sess:03d}"


def session_url(wp: int, sess: int) -> str:
    return f"{BASE_URL}/{wp:02d}/{session_id(wp, sess)}.pdf"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_manifest(raw_dir: Path) -> dict:
    path = raw_dir / MANIFEST_NAME
    if not path.exists():
        return {"sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"WARNING: manifest unreadable ({exc}); starting a new one.")
        return {"sessions": {}}
    data.setdefault("sessions", {})
    return data


def save_manifest(raw_dir: Path, manifest: dict) -> None:
    manifest["updated_at"] = _now()
    path = raw_dir / MANIFEST_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def local_sessions(raw_dir: Path, wp: int) -> set:
    """Session numbers of this WP already present on disk."""
    found = set()
    for pdf in raw_dir.glob("*.pdf"):
        m = _PDF_RE.match(pdf.name)
        if m and int(m.group(1)) == wp:
            found.add(int(m.group(2)))
    return found


def corpus_sessions(preprocessed_dir, wp: int) -> set:
    """Session numbers of this WP already processed into the corpus.

    The raw PDFs may not be held locally (the backfill ran on the cluster), so
    the preprocessed outputs, not data/raw, are the authority on what the
    corpus already contains.
    """
    if not preprocessed_dir:
        return set()
    wp_dir = Path(preprocessed_dir) / f"WP{wp:02d}"
    if not wp_dir.exists():
        return set()
    rx = re.compile(rf"^WP{wp:02d}_Session(\d{{3}})_speakers\.json$")
    found = set()
    for path in wp_dir.glob("*/*_speakers.json"):
        m = rx.match(path.name)
        if m:
            found.add(int(m.group(1)))
    return found


def detect_current_wp(raw_dir: Path, preprocessed_dir=None) -> int:
    """Highest Wahlperiode present locally, in raw PDFs or in the corpus."""
    wps = set()
    for pdf in raw_dir.glob("*.pdf"):
        m = _PDF_RE.match(pdf.name)
        if m:
            wps.add(int(m.group(1)))
    if preprocessed_dir:
        for d in Path(preprocessed_dir).glob("WP[0-9][0-9]"):
            wps.add(int(d.name[2:4]))
    if not wps:
        raise SystemExit(
            f"ERROR: no sessions found in {raw_dir} or the preprocessed folder.")
    return max(wps)


def head(url: str, http: requests.Session):
    """Return {etag, last_modified, content_length}, or None when absent (404)."""
    try:
        r = http.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        raise RuntimeError(f"HEAD failed: {exc}") from exc
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"HEAD returned {r.status_code}")
    length = r.headers.get("Content-Length", "")
    return {
        "etag": r.headers.get("ETag"),
        "last_modified": r.headers.get("Last-Modified"),
        "content_length": int(length) if length.isdigit() else None,
    }


def download(url: str, dest: Path, http: requests.Session) -> str:
    """Download to dest atomically; return the MD5 of the content."""
    r = http.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(r.content)
    tmp.replace(dest)
    return hashlib.md5(r.content).hexdigest()


def _is_changed(remote: dict, record: dict) -> bool:
    """ETag is authoritative; fall back to Last-Modified plus size."""
    if remote.get("etag") and record.get("etag"):
        return remote["etag"] != record["etag"]
    return (
        remote.get("last_modified") != record.get("last_modified")
        or remote.get("content_length") != record.get("content_length")
    )


def _record(manifest: dict, sid: str, wp: int, sess: int,
            remote: dict, md5: str, revised: bool) -> None:
    sessions = manifest["sessions"]
    prev = sessions.get(sid)
    sessions[sid] = {
        "wp": wp,
        "session": sess,
        "etag": remote.get("etag"),
        "last_modified": remote.get("last_modified"),
        "content_length": remote.get("content_length"),
        "md5": md5,
        "first_fetched": prev.get("first_fetched", _now()) if prev else _now(),
        "last_changed": _now() if (revised or not prev) else prev.get("last_changed"),
        "revisions": (prev.get("revisions", 0) if prev else 0) + (1 if revised else 0),
    }


def run(raw_dir=None,
        wp: int = None,
        preprocessed_dir=None,
        probe_window: int = PROBE_WINDOW,
        revision_window: int = REVISION_WINDOW,
        skip_revision_check: bool = False,
        backfill_pdfs: bool = True,
        dry_run: bool = False) -> FetchReport:

    raw = Path(raw_dir or _DEFAULT_RAW)
    raw.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(raw)
    wp = wp or detect_current_wp(raw, preprocessed_dir)
    report = FetchReport(started_at=_now(), wp=wp, dry_run=dry_run)

    http = requests.Session()
    http.headers["User-Agent"] = USER_AGENT

    in_raw = local_sessions(raw, wp)
    in_corpus = corpus_sessions(preprocessed_dir, wp)
    known = in_raw | in_corpus
    last_known = max(known) if known else 0

    print(f"WP{wp:02d}: {len(in_corpus)} session(s) in corpus, "
          f"{len(in_raw)} PDF(s) local, highest known = {last_known:03d}")
    if dry_run:
        print("[dry run — nothing will be written]")

    # ---- backfill PDFs the corpus has but this machine does not --------- #
    # The original extraction ran on the cluster, so the raw PDFs for already
    # processed sessions may be absent here. They are needed as the baseline
    # for revision detection and for any reprocessing.
    missing = sorted(in_corpus - in_raw)
    if backfill_pdfs and missing:
        print(f"\nBackfilling {len(missing)} PDF(s) held in the corpus but "
              f"not on this machine ...")
        for sess in missing:
            sid = session_id(wp, sess)
            url = session_url(wp, sess)
            try:
                remote = head(url, http)
            except RuntimeError as exc:
                report.errors.append(f"{sid}: {exc}")
                time.sleep(REQUEST_DELAY)
                continue
            time.sleep(REQUEST_DELAY)
            if remote is None:
                report.errors.append(f"{sid}: in corpus but 404 on the server")
                continue
            report.backfilled_pdfs.append(sid)
            if not dry_run:
                md5 = download(url, raw / f"{sid}.pdf", http)
                _record(manifest, sid, wp, sess, remote, md5, revised=False)
                in_raw.add(sess)
                time.sleep(REQUEST_DELAY)
        print(f"  backfilled {len(report.backfilled_pdfs)}")

    # ---- revision check on the most recent sessions --------------------- #
    if not skip_revision_check and in_raw:
        recent = sorted(in_raw)[-revision_window:]
        print(f"\nChecking {len(recent)} most recent session(s) for revision ...")
        for sess in recent:
            sid = session_id(wp, sess)
            url = session_url(wp, sess)
            try:
                remote = head(url, http)
            except RuntimeError as exc:
                report.errors.append(f"{sid}: {exc}")
                time.sleep(REQUEST_DELAY)
                continue
            time.sleep(REQUEST_DELAY)

            report.checked_for_revision += 1
            if remote is None:
                report.errors.append(f"{sid}: held locally but 404 on the server")
                continue

            record = manifest["sessions"].get(sid)
            if record is None:
                # On disk but never recorded (pre-manifest backfill). Adopt the
                # current server state as the baseline rather than call it a change.
                if not dry_run:
                    local_pdf = raw / f"{sid}.pdf"
                    md5 = hashlib.md5(local_pdf.read_bytes()).hexdigest() \
                        if local_pdf.exists() else ""
                    _record(manifest, sid, wp, sess, remote, md5, revised=False)
                continue

            if _is_changed(remote, record):
                n = record.get("revisions", 0) + 1
                print(f"  REVISED  {sid}  (revision #{n})")
                report.revised_sessions.append(sid)
                if not dry_run:
                    md5 = download(url, raw / f"{sid}.pdf", http)
                    _record(manifest, sid, wp, sess, remote, md5, revised=True)
                    time.sleep(REQUEST_DELAY)

    # ---- discovery of new sessions -------------------------------------- #
    print(f"\nProbing forward from {last_known + 1:03d} (window {probe_window}) ...")
    misses = 0
    sess = last_known
    while misses < probe_window:
        sess += 1
        sid = session_id(wp, sess)
        url = session_url(wp, sess)
        try:
            remote = head(url, http)
        except RuntimeError as exc:
            report.errors.append(f"{sid}: {exc}")
            misses += 1
            time.sleep(REQUEST_DELAY)
            continue
        time.sleep(REQUEST_DELAY)

        if remote is None:
            misses += 1
            continue

        misses = 0                      # a hit resets the run of misses
        if sess in known:
            continue
        print(f"  NEW      {sid}")
        report.new_sessions.append(sid)
        if not dry_run:
            md5 = download(url, raw / f"{sid}.pdf", http)
            _record(manifest, sid, wp, sess, remote, md5, revised=False)
            time.sleep(REQUEST_DELAY)

    # ---- has the next Wahlperiode started? ------------------------------ #
    try:
        if head(session_url(wp + 1, 1), http) is not None:
            note = (f"WP{wp + 1:02d} has begun: session 001 is published. Re-run with "
                    f"--wp {wp + 1}, and review the party and role reference tables "
                    f"before trusting attribution for the new period.")
            print(f"\nNOTE: {note}")
            report.notes.append(note)
    except RuntimeError:
        pass

    if not dry_run:
        save_manifest(raw, manifest)

    report.finished_at = _now()
    return report


def main() -> None:
    p = argparse.ArgumentParser(
        description="Fetch new and revised Bundestag Plenarprotokoll PDFs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--raw-dir", default=None,
                   help=f"PDF folder. Default: {_DEFAULT_RAW}")
    p.add_argument("--preprocessed-dir", default=None,
                   help="Preprocessed corpus, used to tell what is already "
                        "processed when the raw PDFs are not held locally.")
    p.add_argument("--wp", type=int, default=None,
                   help="Wahlperiode. Default: highest found locally.")
    p.add_argument("--probe-window", type=int, default=PROBE_WINDOW,
                   help="Consecutive misses before the WP is considered exhausted.")
    p.add_argument("--revision-window", type=int, default=REVISION_WINDOW,
                   help="How many recent sessions to re-check for revision.")
    p.add_argument("--skip-revision-check", action="store_true",
                   help="Only look for new sessions.")
    p.add_argument("--no-backfill", action="store_true",
                   help="Do not download PDFs for sessions already in the corpus.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be fetched without downloading.")
    p.add_argument("--json", default=None, metavar="PATH",
                   help="Also write the report as JSON to this path.")
    args = p.parse_args()

    rep = run(raw_dir=args.raw_dir, wp=args.wp,
              preprocessed_dir=args.preprocessed_dir,
              probe_window=args.probe_window,
              revision_window=args.revision_window,
              skip_revision_check=args.skip_revision_check,
              backfill_pdfs=not args.no_backfill,
              dry_run=args.dry_run)

    print("\n" + "=" * 56)
    print(f"  New sessions       : {len(rep.new_sessions)}")
    print(f"  Revised sessions   : {len(rep.revised_sessions)}")
    print(f"  Backfilled PDFs    : {len(rep.backfilled_pdfs)}")
    print(f"  Checked for change : {rep.checked_for_revision}")
    print(f"  Errors             : {len(rep.errors)}")
    print("=" * 56)
    for n in rep.notes:
        print(f"  * {n}")
    for e in rep.errors:
        print(f"  ! {e}")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(asdict(rep), indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\nReport written to {out}")


if __name__ == "__main__":
    main()
