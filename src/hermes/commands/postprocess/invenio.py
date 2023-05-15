# SPDX-FileCopyrightText: 2022 German Aerospace Center (DLR)
#
# SPDX-License-Identifier: Apache-2.0

# SPDX-FileContributor: Michael Meinel
# SPDX-FileContributor: Stephan Druskat

import json
import logging

import toml
from ruamel import yaml

from hermes import config


_log = logging.getLogger('deposit.invenio')


def config_record_id(ctx):
    deposition_path = ctx.get_cache('deposit', 'deposit')
    with deposition_path.open("r") as deposition_file:
        deposition = json.load(deposition_file)
    conf = config.get('hermes')
    try:
        conf['deposit']['invenio']['record_id'] = deposition['record_id']
        toml.dump(conf, open('hermes.toml', 'w'))
    except KeyError as e:
        raise RuntimeError("No configuration for deposition on Invenio available to store record id") from e


def cff_doi(ctx):
    deposition_path = ctx.get_cache('deposit', 'deposit')
    with deposition_path.open("r") as deposition_file:
        deposition = json.load(deposition_file)
    try:
        cff = yaml.load(open('CITATION.cff', 'r'), yaml.Loader)
        new_identifier = {
                'description': f"DOI for the published version {deposition['metadata']['version']} [generated by hermes]",
                'type': 'doi',
                'value': deposition['doi']
            }
        if 'identifiers' in cff:
            cff['identifiers'].append(new_identifier)
        else:
            cff['identifiers'] = [new_identifier]
        yaml.dump(cff, open('CITATION.cff', 'w'),
                  indent=4, default_flow_style=False, block_seq_indent=2, allow_unicode=True)
    except IOError as e:
        raise RuntimeError("CITATION.cff not found.") from e
