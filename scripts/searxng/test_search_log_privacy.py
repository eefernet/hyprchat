"""Run in the staged SearXNG environment after search-log-privacy.patch."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from searx.network.network import Network


class SearchLogPrivacyTests(unittest.TestCase):
    def test_http_failure_preserves_status_without_search_or_credentials(self):
        network = object.__new__(Network)
        network._logger = Mock()
        response = SimpleNamespace(request=SimpleNamespace(method='GET',
            url='https://name:password@search.example/private-phrase?q=private-query'), status_code=429)
        error = RuntimeError('rate limited')
        with patch('searx.network.network.raise_for_httperror', side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                network.patch_response(response, True)
        self.assertIs(raised.exception, error)
        message, *args = network._logger.warning.call_args.args
        self.assertEqual(message % tuple(args), 'HTTP Request failed: GET search.example (HTTP 429)')

    def test_success_has_no_request_log(self):
        network = object.__new__(Network)
        network._logger = Mock()
        response = SimpleNamespace(status_code=200)
        self.assertIs(network.patch_response(response, False), response)
        network._logger.warning.assert_not_called()


if __name__ == '__main__':
    unittest.main()
