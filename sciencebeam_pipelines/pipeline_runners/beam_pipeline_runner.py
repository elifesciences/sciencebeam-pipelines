from __future__ import absolute_import

import argparse
import logging
import mimetypes

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions, SetupOptions
from apache_beam.metrics.metric import Metrics

from sciencebeam_utils.utils.collection import (
    extend_dict
)

from sciencebeam_utils.beam_utils.utils import (
    TransformAndCount,
    TransformAndLog,
    MapOrLog,
    PreventFusion
)

from sciencebeam_utils.beam_utils.io import (
    read_all_from_path,
    save_file_content
)

from sciencebeam_utils.beam_utils.main import (
    add_cloud_args,
    process_cloud_args
)

from sciencebeam_pipelines.config.app_config import get_app_config

from sciencebeam_pipelines.utils.logging import configure_logging

from sciencebeam_pipelines.pipelines import (
    get_pipeline_for_configuration_and_args,
    add_pipeline_args
)

from sciencebeam_pipelines.pipeline_runners.pipeline_runner_utils import (
    add_batch_args,
    process_batch_args,
    encode_if_text_type,
    get_output_file_for_source_file_fn,
    get_remaining_file_list_for_args,
    DataProps
)


LOGGER = logging.getLogger(__name__)


def get_logger():
    return logging.getLogger(__name__)


class MetricCounters:
    FILES = 'files'


def ReadFileContent():
    return "ReadFileContent" >> TransformAndCount(
        beam.Map(lambda file_url: {
            DataProps.SOURCE_FILENAME: file_url,
            DataProps.FILENAME: file_url,
            DataProps.CONTENT: read_all_from_path(file_url)
        }),
        MetricCounters.FILES
    )


def get_step_error_counter(step):
    return 'error_%s' % step


def get_step_ignored_counter(step):
    return 'ignored_%s' % step


def get_step_processed_counter(step):
    return 'processed_%s' % step


def execute_or_skip_step(step):
    supported_types = step.get_supported_types()
    processed_counter = Metrics.counter(
        'PipelineStep', get_step_processed_counter(step)
    )
    ignored_counter = Metrics.counter(
        'PipelineStep', get_step_ignored_counter(step)
    )

    def wrapper(x):
        data_type = x['type']
        if data_type in supported_types:
            get_logger().debug('excuting step %s: %s (%s)', step, x.keys(), data_type)
            result = extend_dict(x, step(x))
            get_logger().debug(
                'result of step %s: %s (%s)',
                step, result.keys(), result.get('type')
            )
            processed_counter.inc()
            return result

        get_logger().debug(
            'skipping step %s, %s not in supported types (%s)', step, data_type, supported_types
        )
        ignored_counter.inc()
        return x

    return wrapper


def get_step_transform(step):
    step_name = str(step)
    return step_name >> MapOrLog(
        execute_or_skip_step(step),
        log_fn=lambda e, v: (
            get_logger().warning(
                'caught exception (ignoring item): %s, source file: %s, step: %s',
                e, v[DataProps.SOURCE_FILENAME], step_name, exc_info=e
            )
        ), error_count=get_step_error_counter(step)
    )


def configure_pipeline(p, opt, pipeline, config):
    get_default_output_file_for_source_file = get_output_file_for_source_file_fn(opt)
    file_list = get_remaining_file_list_for_args(opt)
    LOGGER.debug('file_list: %s', file_list)

    if not file_list:
        LOGGER.info('no files to process')
        return

    steps = pipeline.get_steps(config, opt)

    LOGGER.info('steps: %s', steps)

    input_urls = (
        p |
        beam.Create(file_list) |
        PreventFusion()
    )

    input_data = (
        input_urls |
        ReadFileContent() |
        "Determine Type" >> beam.Map(lambda d: extend_dict(d, {
            DataProps.TYPE: mimetypes.guess_type(d[DataProps.SOURCE_FILENAME])[0]
        }))
    )

    result = input_data

    for step in steps:
        LOGGER.debug('step: %s', step)
        result |= get_step_transform(step)

    _ = (  # noqa: F841
        result |
        "WriteOutput" >> TransformAndLog(
            beam.Map(lambda v: save_file_content(
                get_default_output_file_for_source_file(
                    v[DataProps.SOURCE_FILENAME]
                ),
                encode_if_text_type(v[DataProps.CONTENT])
            )),
            log_fn=lambda x: get_logger().info('saved output to: %s', x)
        )
    )


def parse_args(pipeline, config, argv=None):
    parser = argparse.ArgumentParser()
    add_pipeline_args(parser)
    add_batch_args(parser)
    add_cloud_args(parser)
    pipeline.add_arguments(parser, config, argv)

    args = parser.parse_args(argv)

    if args.debug:
        logging.getLogger().setLevel('DEBUG')

    process_batch_args(args)
    process_cloud_args(
        args, args.output_path,
        name='sciencebeam_pipelines-convert'
    )

    get_logger().info('args: %s', args)

    return args


def run(args, config, pipeline, save_main_session):
    # We use the save_main_session option because one or more DoFn's in this
    # workflow rely on global context (e.g., a module imported at module level).
    pipeline_options = PipelineOptions.from_dictionary(vars(args))
    pipeline_options.view_as(SetupOptions).save_main_session = save_main_session

    with beam.Pipeline(args.runner, options=pipeline_options) as p:
        configure_pipeline(p, args, pipeline, config)

        # Execute the pipeline and wait until it is completed.


def main(argv=None, save_main_session=True):
    config = get_app_config()

    pipeline = get_pipeline_for_configuration_and_args(config, argv=argv)

    args = parse_args(pipeline, config, argv)

    run(args, config=config, pipeline=pipeline, save_main_session=save_main_session)


if __name__ == '__main__':
    configure_logging()

    main()
