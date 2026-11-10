#!/usr/bin/env python3
"""Link a NeuroLibre preprint to its published version in Crossref.

Used by .github/workflows/link-published-version.yml. Standard library only.

  build   Check both DOIs on Crossref, then write a resource-only deposit
          (doi_resources schema 5.4.0) that adds one relation to the preprint:
          isPreprintOf -> published DOI.
  check   After the deposit, read Crossref's submission log until it is
          complete and fail unless the record was accepted. Then look at the
          preprint's public record and fail if any relation it had before the
          deposit has disappeared.

Crossref documents that resource-only metadata is appended to the existing
record, so the deposit carries only the new relation. `check` exists because
that is the assumption a mistake would break: the preprint's isSupplementedBy
links (repository, data, book, container) must survive.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from xml.etree import ElementTree
from xml.sax.saxutils import escape

PREPRINT_PREFIX = "10.55458/neurolibre."
API = "https://api.crossref.org/works/"
MAILTO = os.environ.get("CROSSREF_EMAIL", "admin@neurolibre.org")
DEPOSIT_HOSTS = {"": "https://doi.crossref.org", "test": "https://test.crossref.org"}

# Same depositor block as inara's neurolibre.crossref template.
TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<doi_batch xmlns="http://www.crossref.org/doi_resources_schema/5.4.0"
           xmlns:rel="http://www.crossref.org/relations.xsd"
           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
           xsi:schemaLocation="http://www.crossref.org/doi_resources_schema/5.4.0 https://www.crossref.org/schemas/doi_resources5.4.0.xsd"
           version="5.4.0">
  <head>
    <doi_batch_id>{batch_id}</doi_batch_id>
    <depositor>
      <depositor_name>NeuroLibre Admin</depositor_name>
      <email_address>admin@neurolibre.org</email_address>
    </depositor>
  </head>
  <body>
    <doi_relations>
      <doi>{preprint_doi}</doi>
      <rel:program>
        <rel:related_item>
          <rel:description>Published version</rel:description>
          <rel:intra_work_relation relationship-type="isPreprintOf" identifier-type="doi">{published_doi}</rel:intra_work_relation>
        </rel:related_item>
      </rel:program>
    </doi_relations>
  </body>
</doi_batch>
"""


def fail(message):
    print(f"::error::{message}")
    sys.exit(1)


def normalize_doi(value):
    doi = value.strip()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    if not re.match(r"^10\.\d{4,9}/\S+$", doi):
        fail(f"'{value}' is not a DOI")
    return doi


def crossref_work(doi):
    url = API + urllib.parse.quote(doi, safe="/") + "?" + urllib.parse.urlencode({"mailto": MAILTO})
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)["message"]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def relation_set(work):
    """Relations as (type, id) pairs, lower-cased so DOI case does not matter."""
    pairs = set()
    for kind, items in (work.get("relation") or {}).items():
        for item in items:
            pairs.add((kind, str(item.get("id", "")).lower()))
    return pairs


def set_outputs(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{key}={value}" for key, value in values.items()]
    if path:
        with open(path, "a") as handle:
            handle.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def build(args):
    preprint = normalize_doi(args.preprint_doi)
    published = normalize_doi(args.published_doi)

    if not preprint.lower().startswith(PREPRINT_PREFIX):
        fail(f"{preprint} is not a NeuroLibre preprint DOI ({PREPRINT_PREFIX}...)")
    if published.lower().startswith("10.55458/"):
        fail(f"{published} is a NeuroLibre DOI, not the published version")

    preprint_work = crossref_work(preprint)
    if preprint_work is None:
        fail(f"{preprint} is not registered with Crossref")
    published_work = crossref_work(published)
    if published_work is None:
        fail(f"{published} is not registered with Crossref")

    journal = (published_work.get("container-title") or [""])[0] or published_work.get("publisher", "")
    title = (published_work.get("title") or [""])[0]

    already = ("is-preprint-of", published.lower()) in relation_set(preprint_work)

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = preprint.split("/", 1)[1]
    # Crossref tracks the submission by file name, so it must be unique.
    file_name = f"relation-{suffix}-{stamp}.xml"
    xml_path = os.path.join(args.out_dir, file_name)
    with open(xml_path, "w") as handle:
        handle.write(TEMPLATE.format(
            batch_id=escape(f"{suffix}-relation-{stamp}"),
            preprint_doi=escape(preprint),
            published_doi=escape(published),
        ))

    snapshot_path = os.path.join(args.out_dir, "relations-before.json")
    with open(snapshot_path, "w") as handle:
        json.dump(sorted(relation_set(preprint_work)), handle)

    print(f"Preprint:  {preprint} ({(preprint_work.get('title') or [''])[0]})")
    print(f"Published: {published} ({title}) in {journal}")
    if already:
        print("Crossref already lists this isPreprintOf relation; the deposit will not be needed.")

    set_outputs(
        preprint_doi=preprint,
        published_doi=published,
        journal=journal.replace("\n", " "),
        xml_path=xml_path,
        file_name=file_name,
        snapshot_path=snapshot_path,
        already_linked=str(already).lower(),
    )


def submission_log(host, file_name, username, password):
    query = urllib.parse.urlencode({
        "usr": username, "pwd": password, "file_name": file_name, "type": "result",
    })
    # The URL carries the credentials, so never print it.
    with urllib.request.urlopen(f"{host}/servlet/submissionDownload?{query}", timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def check(args):
    host = DEPOSIT_HOSTS.get(args.mode.strip().lower())
    if host is None:
        fail(f"Unknown crossref mode '{args.mode}'")
    username = os.environ.get("CROSSREF_USERNAME", "")
    password = os.environ.get("CROSSREF_PASSWORD", "")
    if not username or not password:
        fail("CROSSREF_USERNAME and CROSSREF_PASSWORD must be set")

    # 1. Crossref's own verdict on the submission.
    deadline = time.time() + args.log_timeout
    log = ""
    while True:
        try:
            log = submission_log(host, args.file_name, username, password)
        except urllib.error.URLError as error:
            print(f"Could not read the submission log yet ({error}); retrying")
            log = ""
        if 'status="completed"' in log:
            break
        if time.time() > deadline:
            fail(f"Crossref had not finished processing {args.file_name} after {args.log_timeout}s. "
                 "Check the submission queue in the Crossref admin tool.")
        time.sleep(30)

    try:
        root = ElementTree.fromstring(log)
    except ElementTree.ParseError:
        fail("Crossref returned a submission log that is not XML:\n" + log[:2000])
    failures = int(root.findtext(".//failure_count", "0") or 0)
    successes = int(root.findtext(".//success_count", "0") or 0)
    messages = [m.text.strip() for m in root.iter("msg") if m.text]
    print(f"Crossref submission log: {successes} succeeded, {failures} failed")
    for message in messages:
        print(f"  {message}")
    if failures or not successes:
        fail("Crossref rejected the deposit: " + "; ".join(messages))

    # 2. The public record. Only production deposits show up in the REST API.
    if host != DEPOSIT_HOSTS[""]:
        print("Test deposit: the public REST API only reflects production, so the relation check is skipped.")
        return

    with open(args.snapshot) as handle:
        before = {tuple(pair) for pair in json.load(handle)}
    wanted = ("is-preprint-of", args.published_doi.lower())
    deadline = time.time() + args.api_timeout
    while True:
        after = relation_set(crossref_work(args.preprint_doi) or {})
        if wanted in after:
            break
        if time.time() > deadline:
            print(f"::warning::Crossref accepted the deposit, but {args.preprint_doi} does not show the relation "
                  f"in the public API yet. This can take a while; check https://api.crossref.org/works/{args.preprint_doi}")
            return
        time.sleep(60)

    lost = sorted(before - after)
    if lost:
        fail("The deposit removed relations the preprint had before. Restore them with a full redeposit "
             "(reaccept): " + ", ".join(f"{kind} {doi}" for kind, doi in lost))
    print(f"{args.preprint_doi} now lists isPreprintOf {args.published_doi}; all {len(before)} earlier relations are still there.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    b = commands.add_parser("build")
    b.add_argument("--preprint-doi", required=True)
    b.add_argument("--published-doi", required=True)
    b.add_argument("--out-dir", required=True)
    b.set_defaults(func=build)

    c = commands.add_parser("check")
    c.add_argument("--preprint-doi", required=True)
    c.add_argument("--published-doi", required=True)
    c.add_argument("--file-name", required=True)
    c.add_argument("--snapshot", required=True)
    c.add_argument("--mode", default="")
    c.add_argument("--log-timeout", type=int, default=900)
    c.add_argument("--api-timeout", type=int, default=600)
    c.set_defaults(func=check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
