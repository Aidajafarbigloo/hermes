# SPDX-FileCopyrightText: 2022 German Aerospace Center (DLR)
#
# SPDX-License-Identifier: Apache-2.0

# SPDX-FileContributor: Stephan Druskat
# SPDX-FileContributor: Michael Meinel

import json
import logging
from importlib import metadata

import click

from hermes.model.context import HermesContext, HermesHarvestContext, CodeMetaContext
from hermes.model.errors import MergeError


@click.group(invoke_without_command=True)
@click.pass_context
def harvest(click_ctx: click.Context):
    """
    Automatic harvest of metadata
    """
    _log = logging.getLogger('cli.harvest')
    audit_log = logging.getLogger('audit')
    audit_log.info("# Metadata harvesting")

    # Create Hermes context (i.e., all collected metadata for all stages...)
    ctx = HermesContext()

    # Get all harvesters
    harvesters = metadata.entry_points(group='hermes.harvest')
    for harvester in harvesters:
        _log.info("- Running harvester %s", harvester.name)

        _log.debug(". Loading harvester from %s", harvester.value)
        harvest = harvester.load()

        with HermesHarvestContext(ctx, harvester) as harvest_ctx:
            harvest(click_ctx, harvest_ctx)
            for _key, ((_value, _tag), *_trace) in harvest_ctx._data.items():
                if any(v != _value and t == _tag for v, t in _trace):
                    raise MergeError(_key, None, _value)
        _log.info('')
    audit_log.info('')


@click.group(invoke_without_command=True)
def process():
    """
    Process metadata and prepare it for deposition
    """
    _log = logging.getLogger('cli.process')

    audit_log = logging.getLogger('audit')
    audit_log.info("# Metadata processing")

    ctx = CodeMetaContext()

    harvesters = metadata.entry_points(group='hermes.harvest')
    for harvester in harvesters:
        audit_log.info("## Process data from %s", harvester.name)

        harvest_context = HermesHarvestContext(ctx, harvester)
        harvest_context.load_cache()

        processors = metadata.entry_points(group='hermes.preprocess', name=harvester.name)
        for processor in processors:
            _log.debug(". Loading context processor %s", processor.value)
            process = processor.load()

            _log.debug(". Apply processor %s", processor.value)
            process(ctx, harvest_context)

        ctx.merge_from(harvest_context)
        _log.info('')
    audit_log.info('')

    if ctx._errors:
        audit_log.error('!!! warning "Errors during merge"')

        for ep, error in ctx._errors:
            audit_log.info('    - %s: %s', ep.name, error)

    tags_path = ctx.get_cache('process', 'tags', create=True)
    with tags_path.open('w') as tags_file:
        json.dump(ctx.tags, tags_file, indent='  ')

    with open('codemeta.json', 'w') as codemeta_file:
        json.dump(ctx._data, codemeta_file, indent='  ')

    logging.shutdown()


@click.group(invoke_without_command=True)
def deposit():
    """
    Deposit processed (and curated) metadata
    """
    click.echo("Metadata deposition")


@click.group(invoke_without_command=True)
def postprocess():
    """
    Postprocess metadata after deposition
    """
    click.echo("Post-processing")


@click.group(invoke_without_command=True)
def clean():
    """
    Remove cached data.
    """
    audit_log = logging.getLogger('audit')
    audit_log.info("# Cleanup")

    # Create Hermes context (i.e., all collected metadata for all stages...)
    ctx = HermesContext()
    ctx.purge_caches()
