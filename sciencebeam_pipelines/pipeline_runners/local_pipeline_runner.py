from __future__ import absolute_import

import argparse
import logging
import concurrent.futures
from urllib.parse import parse_qsl
from functools import partial
from mimetypes import guess_type
from typing import Callable, List

import requests
from werkzeug.datastructures import MultiDict

from sciencebeam_utils.beam_utils.io import (
    read_all_from_path,
    save_file_content
)

from sciencebeam_utils.utils.tqdm import tqdm_with_logging_redirect

from sciencebeam_pipelines.utils.formatting import format_size
from sciencebeam_pipelines.utils.logging import configure_logging
from sciencebeam_pipelines.utils.requests import RetrySession, METHOD_WHITELIST_WITH_POST

from sciencebeam_pipelines.config.app_config import get_app_config


from sciencebeam_pipelines.pipelines import Pipeline, RequestsPipelineStep

from sciencebeam_pipelines.pipelines import (
    get_pipeline_for_configuration_and_args,
    add_pipeline_args
)

from sciencebeam_pipelines.pipeline_runners.simple_pipeline_runner import (
    SimplePipelineRunner
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


def add_num_workers_argument(parser: argparse.ArgumentParser):
    parser.add_argument(
        '--num-workers', '--num_workers',
        default=1,
        type=int,
        help='The number of workers.'
    )
    parser.add_argument(
        '--max-retries',
        default=10,
        type=int,
        help='The number of times to attempt to retry http requests.'
    )
    parser.add_argument(
        '--retry-backoff',
        default=0.1,
        type=float,
        help='The backoff factor to use.'
    )
    parser.add_argument(
        '--fail-on-error',
        action='store_true',
        help='Fail process on conversion error (rather than logging a warning and resuming).'
    )


def _parse_request_args(request_args_str: str) -> MultiDict:
    LOGGER.debug('request_args_str: %s', request_args_str)
    return MultiDict(parse_qsl(request_args_str.strip('"')))


def add_request_args_argument(parser: argparse.ArgumentParser):
    parser.add_argument(
        '--request-args',
        type=_parse_request_args,
        help=''.join([
            'The request args to use as if they were passed in to the URL',
            ' (in the query string format).',
            ' e.g. \'remove_line_no=n&remove_redline=n\''
        ])
    )


def parse_args(pipeline, config, argv=None):
    LOGGER.debug('argv=%s', argv)
    parser = argparse.ArgumentParser()
    add_pipeline_args(parser)
    add_batch_args(parser)
    add_num_workers_argument(parser)
    add_request_args_argument(parser)
    pipeline.add_arguments(parser, config, argv)

    args = parser.parse_args(argv)

    if args.debug:
        logging.getLogger().setLevel('DEBUG')

    process_batch_args(args)

    return args


def process_file(
        file_url: str, simple_runner: SimplePipelineRunner,
        get_output_file_for_source_url: Callable[[str], str],
        session: requests.Session,
        request_args: MultiDict = None):
    output_file_url = get_output_file_for_source_url(
        file_url
    )
    file_content = read_all_from_path(file_url)
    LOGGER.info('read source content: %s (%s)', file_url, format_size(len(file_content)))
    data_type = guess_type(file_url)[0]
    LOGGER.debug('data_type: %s', data_type)
    LOGGER.debug('session: %s', session)
    context = {RequestsPipelineStep.REQUESTS_SESSION_KEY: session}
    if request_args:
        context['request_args'] = request_args
    result = simple_runner.convert(
        file_content, file_url, data_type,
        context=context
    )
    LOGGER.debug('result.keys: %s', result.keys())
    output_content = encode_if_text_type(result[DataProps.CONTENT])
    save_file_content(output_file_url, output_content)
    LOGGER.info('saved output to: %s (%s)', output_file_url, format_size(len(output_content)))


def process_with_pool_executor(
        executor: concurrent.futures.Executor,
        file_list: List[str],
        process_file_url: callable,
        fail_on_error: bool):
    error_count = 0
    success_count = 0

    def log_summary(name: str):
        LOGGER.info(
            '%s: %d success, %d failures (total: %d)',
            name, success_count, error_count, len(file_list)
        )

    with tqdm_with_logging_redirect(total=len(file_list)) as pbar:
        future_to_url = {
            executor.submit(process_file_url, url): url
            for url in file_list
        }
        LOGGER.debug('future_to_url: %s', future_to_url)
        for future in concurrent.futures.as_completed(future_to_url):
            pbar.update(1)
            url = future_to_url[future]
            try:
                future.result()
                success_count += 1
                log_summary('progress')
            except Exception as exc:  # pylint: disable=broad-except
                error_count += 1
                log_summary('progress')
                LOGGER.warning('%r generated an exception: %s', url, exc)
                if fail_on_error:
                    raise
    log_summary('done')


def run(args, config, pipeline: Pipeline):
    LOGGER.info('args: %s', args)
    file_list = get_remaining_file_list_for_args(args)
    LOGGER.debug('file_list: %s', file_list)

    if not file_list:
        LOGGER.info('no files to process')
        return

    simple_runner = SimplePipelineRunner(pipeline.get_steps(config, args))

    retry_args = dict(
        method_whitelist=METHOD_WHITELIST_WITH_POST,
        max_retries=args.max_retries,
        backoff_factor=args.retry_backoff
    )
    with RetrySession(**retry_args) as session:
        process_file_url = partial(
            process_file,
            simple_runner=simple_runner,
            get_output_file_for_source_url=get_output_file_for_source_file_fn(args),
            session=session,
            request_args=args.request_args
        )

        num_workers = args.num_workers
        LOGGER.info('using %d workers', num_workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            process_with_pool_executor(
                executor=executor,
                file_list=file_list,
                process_file_url=process_file_url,
                fail_on_error=args.fail_on_error
            )


def main(argv=None):
    config = get_app_config()

    pipeline = get_pipeline_for_configuration_and_args(config, argv=argv)

    args = parse_args(pipeline, config, argv)
    run(args, config=config, pipeline=pipeline)


if __name__ == '__main__':
    configure_logging()

    main()
