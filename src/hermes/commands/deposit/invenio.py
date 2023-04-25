# SPDX-FileCopyrightText: 2023 Helmholtz-Zentrum Dresden-Rossendorf (HZDR)
#
# SPDX-License-Identifier: Apache-2.0

# SPDX-FileContributor: David Pape
# SPDX-FileContributor: Oliver Bertuch
# SPDX-FileContributor: Michael Meinel

import json
import logging
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import click
import requests

from hermes import config
from hermes.commands.deposit.error import DepositionUnauthorizedError
from hermes.error import MisconfigurationError
from hermes.model.context import CodeMetaContext
from hermes.model.path import ContextPath
from hermes.utils import hermes_user_agent

_DEFAULT_DEPOSITIONS_API_PATH = "api/deposit/depositions"
_DEFAULT_RECORD_SCHEMA_PATH = "api/schemas/records/record-v1.0.0.json"


# TODO: It turns out that the schema downloaded here can not be used. Figure out what to
# do with this. Maybe the code can be removed.
def prepare_deposit(click_ctx: click.Context, ctx: CodeMetaContext):
    """Prepare the Invenio deposit.

    In this case, "prepare" means download the record schema that is required
    by Invenio instances. This is the basis that will be used for metadata
    mapping in the next step.
    """

    invenio_path = ContextPath.parse("deposit.invenio")
    invenio_config = config.get("deposit").get("invenio", {})
    rec_id, rec_meta = _resolve_latest_invenio_id(ctx)
    ctx.update(invenio_path['latestRecord'], {'id': rec_id, 'metadata': rec_meta})

    site_url = invenio_config.get("site_url")
    if site_url is None:
        raise MisconfigurationError("deposit.invenio.site_url is not configured")

    record_schema_path = invenio_config.get("schema_paths", {}).get(
        "record", _DEFAULT_RECORD_SCHEMA_PATH
    )
    record_schema_url = f"{site_url}/{record_schema_path}"

    # TODO: cache this download in HERMES cache dir
    # TODO: ensure to use from cache instead of download if not expired (needs config)
    response = requests.get(
        record_schema_url, headers={"User-Agent": hermes_user_agent}
    )
    response.raise_for_status()
    record_schema = response.json()
    ctx.update(invenio_path["requiredSchema"], record_schema)


def map_metadata(click_ctx: click.Context, ctx: CodeMetaContext):
    """Map the harvested metadata onto the Invenio schema."""

    deposition_metadata = _codemeta_to_invenio_deposition(ctx["codemeta"])

    metadata_path = ContextPath.parse("deposit.invenio.depositionMetadata")
    ctx.update(metadata_path, deposition_metadata)

    # Store a snapshot of the mapped data within the cache, useful for analysis, debugging, etc
    with open(ctx.get_cache("deposit", "invenio", create=True), 'w') as invenio_json:
        json.dump(deposition_metadata, invenio_json, indent='  ')


def deposit(click_ctx: click.Context, ctx: CodeMetaContext):
    """Make a deposition on an Invenio-based platform.

    This function can:

    - Create a new record without any previous versions.

    Functionality to be added in the future:

    - Update the metadata of an existing record
    - Update the metadata and files of an existing record by creating a new version
    """

    _log = logging.getLogger("cli.deposit.invenio")

    invenio_config = config.get("deposit").get("invenio", {})
    invenio_path = ContextPath.parse("deposit.invenio")
    invenio_ctx = ctx[invenio_path]

    if not click_ctx.params["auth_token"]:
        raise DepositionUnauthorizedError("No auth token given for deposition platform")

    session = requests.Session()
    session.headers = {
        "User-Agent": hermes_user_agent,
        "Authorization": f"Bearer {click_ctx.params['auth_token']}",
    }

    site_url = invenio_config.get("site_url")
    if site_url is None:
        raise MisconfigurationError("deposit.invenio.site_url is not configured")

    depositions_api_path = invenio_config.get("api_paths", {}).get(
        "depositions", _DEFAULT_DEPOSITIONS_API_PATH
    )
    deposit_url = f"{site_url}/{depositions_api_path}"

    deposition_metadata = invenio_ctx["depositionMetadata"]
    try:
        latest_metadata = invenio_ctx["latestRecord"]["metadata"]
        if deposition_metadata.get("version") == latest_metadata.get("version"):
            raise ValueError("Version already deposited.")

        record_id = invenio_ctx["latestRecord"]["id"]
    except KeyError:
        record_id = None

    if record_id is not None:
        deposit_url += f'/{record_id}/actions/newversion'
        response = session.post(deposit_url)
        old_deposit = response.json()
        response = session.put(
            old_deposit['links']['latest_draft'],
            json={"metadata": deposition_metadata}
        )
    else:
        response = session.post(
            deposit_url,
            json={"metadata": deposition_metadata}
        )

    if not response.ok:
        _log.error(f"Could not update metadata of deposit {deposit_url!r}")
        click_ctx.exit(1)

    deposit = response.json()
    _log.debug("Created deposit: %s", deposit["links"]["html"])

    # Upload the files. We'll use the bucket API rather than the files API as it
    # supports file sizes above 100MB.
    bucket_url = deposit["links"]["bucket"]

    files: list[click.Path] = click_ctx.params["file"]
    for path_arg in files:
        path = Path(path_arg)

        # This should not happen, as Click shall not accept dirs as arguments already. Zero trust anyway.
        if not path.is_file():
            raise ValueError("Any given argument to be included in the deposit must be a file.")

        with open(path, "rb") as file_content:
            response = session.put(
                f"{bucket_url}/{path.name}",
                data=file_content
            )
            if not response.ok:
                _log.error(f"Could not upload file {path.name!r} into bucket {bucket_url!r}")
                click_ctx.exit(1)

    # This can potentially be used to verify the checksum
    # file_resource = response.json()

    publish_url = deposit["links"]["publish"]
    response = session.post(publish_url)
    if not response.ok:
        _log.error(f"Could not publish deposit via {publish_url!r}")
        _log.debug(response.text)
        click_ctx.exit(1)

    record = response.json()
    _log.info("Published record: %s", record["links"]["record_html"])


def _resolve_latest_invenio_id(ctx: CodeMetaContext) -> (str, dict):
    """
    Using the given configuration and metadata, figure out the latest record id.

    If a record id is present as configuration ``deposit.invenio.record_id`` this one will be used to identify the
    latest version of the record. Otherwise, if there is a doi present (either as configuration with key
    ``deposit.invenio.doi``  or as a codemeta identifier, the DOI will be used to resolve the base record id.

    Anyway, the record id will always be used to resolve the latest version.

    If any of the resolution steps fail or produce an unexpected result, a ValueError will be thrown.

    :param ctx: The context for which the record id should be resolved.
    :return: The Invenio record id and the metadata of the record
    """

    invenio_config = config.get('deposit').get('invenio', {})
    site_url = invenio_config.get('site_url')
    if site_url is None:
        raise MisconfigurationError("deposit.invenio.site_url is not configured")

    # Check if we configured an Invenio record ID (of the concept...)
    record_id = invenio_config.get('record_id')
    if record_id is None:
        doi = invenio_config.get('doi')
        if doi is None:
            try:
                # TODO: There might be more semantic in the codemeta.identifier... (also see schema.org)
                identifier = ctx['codemeta.identifier']
                if identifier.startswith('https://doi.org/'):
                    doi = identifier[16:]
                elif identifier.startswith('http://dx.doi.org/'):
                    doi = identifier[18:]
            except KeyError:
                pass

        if doi is not None:
            # If we got a DOI, resolve it (using doi.org) into a Invenio URL ... and extract the record id.
            record_id = _invenio_resolve_doi(site_url, doi)

    if record_id is not None:
        # If we got a record id by now, resolve it using the Invenio API to the latests record.
        return _invenio_resolve_record_id(site_url, record_id)

    return None, {}


def _invenio_resolve_doi(site_url, doi) -> str:
    """
    Resolve an DOI to a Invenio URL and extract the record id.

    :param site_url: Root URL for the Invenio instance to use.
    :param doi: The DOI to be resolved (only the identifier *without* the ``https://doi.org/`` prefix).
    :return: The record ID on the respective instance.
    """

    res = requests.get(f'https://doi.org/{doi}')

    # This is a mean hack due to DataCite answering a 404 with a 200 status
    if res.url == 'https://datacite.org/404.html':
        raise ValueError(f"Invalid DOI: {doi}")

    # Ensure the resolved record is on the correct instance
    if not res.url.startswith(site_url):
        raise ValueError(f"{res.url} is not on configured host {site_url}.")

    # Extract the record id as last part of the URL path
    page_url = urlparse(res.url)
    *_, record_id = page_url.path.split('/')
    return record_id


def _invenio_resolve_record_id(site_url: str, record_id: str) -> (str, dict):
    """
    Find the latest version of a given record.

    :param site_url: Root URL for the Invenio instance to use.
    :param record_id: The record that sould be resolved.
    :return: The record id of the latest version for the requested record.
    """
    res = requests.get(f"{site_url}/api/records/{record_id}")
    if res.status_code != 200:
        raise ValueError(f"Could not retrieve record from {res.url}: {res.text}")

    res_json = res.json()
    res = requests.get(res_json['links']['latest'])
    if res.status_code != 200:
        raise ValueError(f"Could not retrieve record from {res.url}: {res.text}")

    res_json = res.json()
    return res_json['id'], res_json['metadata']


def _codemeta_to_invenio_deposition(metadata: dict) -> dict:
    """The mapping logic.

    Functionality similar to this exists in the ``convert_codemeta`` package which uses
    the crosswalk tables to do the mapping:

    .. code-block:: python

       invenio_metadata = convert_codemeta.crosswalk(
           metadata, "codemeta", "Zenodo"
       )

    Unfortunately, this doesn't work well with additional metadata in the same dict, so
    it is safer to provide our own implementation.

    Currently, this function handles a lot of cases which we want to be able to
    configure. A simple mapping from one JSON path to another is not enough.

    The metadata expected by Zenodo is described in the `Zenodo Developers guide
    <https://developers.zenodo.org/#representation>`_. Unfortunately, there doesn't seem
    to be a schema one can download in order to validate these metadata. There might be
    differences between Invenio-based platforms.
    """

    creators = [
        # TODO: Distinguish between @type "Person" and others
        {
            k: v for k, v in {
                # TODO: This is ugly
                "affiliation": author.get("affiliation", {"legalName": None}).get("legalName"),
                # Invenio wants "family, given". author.get("name") might not have this format.
                "name": f"{author.get('familyName')}, {author.get('givenName')}"
                if author.get("familyName") and author.get("givenName")
                else author.get("name"),
                # Invenio expects the ORCID without the URL part
                "orcid": author.get("@id", "").replace("https://orcid.org/", "") or None,
            }.items() if v is not None
        }
        for author in metadata["author"]
    ]

    # This is not used at the moment. See comment below in `deposition_metadata` dict.
    contributors = [  # noqa: F841
        # TODO: Distinguish between @type "Person" and others
        {
            k: v for k, v in {
                # TODO: This is ugly
                "affiliation": contributor.get("affiliation", {"legalName": None}).get("legalName"),
                # Invenio wants "family, given". contributor.get("name") might not have this format.
                "name": f"{contributor.get('familyName')}, {contributor.get('givenName')}"
                if contributor.get("familyName") and contributor.get("givenName")
                else contributor.get("name"),
                # Invenio expects the ORCID without the URL part
                "orcid": contributor.get("@id", "").replace("https://orcid.org/", "") or None,
                # TODO: Many possibilities here. Get from config
                "type": "ProjectMember",
            }.items() if v is not None
        }
        # TODO: Filtering out "GitHub" should be done elsewhere
        for contributor in metadata["contributor"] if contributor.get("name") != "GitHub"
    ]

    # TODO: Use the fields currently set to `None`.
    # Some more fields are available but they most likely don't relate to software
    # publications targeted by hermes.
    deposition_metadata = {k: v for k, v in {
        # If upload_type is "publication"/"image", a publication_type/image_type must be
        # specified. Since hermes targets software publications, this can be ignored and
        # upload_type can be hard-coded to "software".
        # TODO: Make this a constant maybe.
        "upload_type": "software",
        # IS0 8601-formatted date
        # TODO: Maybe we want a different date? Then make this configurable. If not,
        # this can be removed as it defaults to today.
        "publication_date": date.today().isoformat(),
        "title": metadata["name"],
        "creators": creators,
        # TODO: Use a real description here. Possible sources could be
        # `tool.poetry.description` from pyproject.toml or `abstract` from
        # CITATION.cff. This should then be stored in codemeta description field.
        "description": metadata["name"],
        # TODO: Get from config. This needs to be specified; we can not guess this.
        # TODO: Needs some more logic:
        # Possible options are: open, embargoed, restricted, closed. open and
        # restricted should come with a `license`, embargoed with an `embargo_date`,
        # restricted with `access_conditions`.
        "access_right": "open",
        # TODO: Get this from config/codemeta/GitHub API/...
        "license": "Apache-2.0",
        "embargo_date": None,
        "access_conditions": None,
        # TODO: If a publisher already has assigned a DOI to the files we want to
        # upload, it should be used here. In this case, Invenio will not give us a new
        # one. Set "prereserve_doi" accordingly.
        "doi": None,
        # This prereserves a DOI that can then be added to the files before publishing
        # them.
        # TODO: Use the DOI we get back from this.
        "prereserve_doi": True,
        # TODO: A good source for this could be `tool.poetry.keywords` in pyproject.toml.
        "keywords": None,
        "notes": None,
        "related_identifiers": None,
        # TODO: Use `contributors`. In the case of the hermes workflow itself, the
        # contributors are currently all in `creators` already. So for now, we set this
        # to `None`. Change this when relationship between authors and contributors can
        # be specified in the processing step.
        "contributors": None,
        "references": None,
        # TODO: This has to come from config.
        "communities": None,
        "grants": None,
        "subjects": None,
        # TODO: Get this from config
        "version": metadata.get('version'),
    }.items() if v is not None}

    return deposition_metadata
