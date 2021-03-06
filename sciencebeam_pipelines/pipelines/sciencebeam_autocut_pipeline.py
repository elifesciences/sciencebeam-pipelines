import argparse  # pylint: disable=unused-import
import logging

from lxml import etree

import requests

from sciencebeam_utils.utils.collection import extend_dict
from sciencebeam_utils.utils.xml import get_text_content

from sciencebeam_pipelines.utils.mime_type_constants import MimeTypes

from . import Pipeline, RequestsPipelineStep


LOGGER = logging.getLogger(__name__)


def apply_revised_value(node, revised_value):
    for child in node:
        node.remove(child)
    node.text = revised_value


class ScienceBeamAutocutApiStep(RequestsPipelineStep):
    def __init__(self, api_url, xpath):
        super().__init__(api_url=api_url)
        self._xpath = xpath

    def get_supported_types(self):
        return {MimeTypes.JATS_XML, MimeTypes.TEI_XML, MimeTypes.XML}

    def process_request(self, data: dict, session: requests.Session, context: dict = None):
        root = etree.fromstring(data['content'])
        matching_nodes = root.xpath(self._xpath)
        if not matching_nodes:
            LOGGER.info('xpath not matching any element: %s', self._xpath)
            return data
        for node in matching_nodes:
            value = get_text_content(node)
            LOGGER.debug('node for xpath %s: %s (text: %s)', self._xpath, node, value)
            response = session.post(
                self._api_url,
                data=value.encode('utf-8'),
                timeout=self.get_default_request_timeout(context=context)
            )
            response.raise_for_status()
            revised_value = response.text
            LOGGER.debug('revised_value: %s (was: %s)', revised_value, value)
            if revised_value != value:
                apply_revised_value(node, revised_value)
        return extend_dict(data, {
            'content': etree.tostring(root)
        })

    def __str__(self):
        return 'ScienceBeam Autocut API'

    def __repr__(self):
        return '%s(%s, %s)' % (type(self).__name__, self._api_url, self._xpath)


class ScienceBeamAutocutPipeline(Pipeline):
    def add_arguments(self, parser, config, argv=None):
        # type: (argparse.ArgumentParser, dict, object) -> None
        sciencebeam_autocut_group = parser.add_argument_group('ScienceBeam Autocut')
        sciencebeam_autocut_group.add_argument(
            '--sciencebeam-autocut-url', required=True,
            help='URL to the ScienceBeam Autocut service (e.g. http://localhost:8080/api/autocut)'
        )
        sciencebeam_autocut_group.add_argument(
            '--sciencebeam-autocut-xpath', required=True,
            default='front/article-meta/title-group/article-title',
            help='The xPath for the element to send to ScienceBeam Autocut'
        )

    def get_steps(self, config, args):
        # type: (dict, object) -> list
        steps = [
            ScienceBeamAutocutApiStep(
                args.sciencebeam_autocut_url,
                args.sciencebeam_autocut_xpath
            )
        ]
        return steps


PIPELINE = ScienceBeamAutocutPipeline()
