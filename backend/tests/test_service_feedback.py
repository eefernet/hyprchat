"""Feedback is actionable without changing failed checks into verification."""
import pytest

from coder_project_runtime import service_endpoint_feedback
from coder_engine_ops import summarize


def feedback(log, *, ready=True, service='app'):
    url = 'http://127.0.0.1:49152'
    return service_endpoint_feedback(log, {'DAEDALUS_' + service.upper() + '_URL': url},
                                     [{'passed': ready, 'url': url}])


@pytest.mark.parametrize('error', ['net::ERR_CONNECTION_REFUSED', 'ECONNREFUSED', 'Connection refused'])
def test_wrong_loopback_endpoint_has_specific_repair_feedback(error):
    result = feedback(f'{error}\n navigating to "http://127.0.0.1:3001/"')
    assert result['category'] == 'test_service_endpoint'
    assert result['reported_urls'] == ['http://127.0.0.1:3001/']
    assert 'DAEDALUS_APP_URL' in result['reason']
    assert 'every navigation/request' in result['reason']
    summary = summarize([{'id': 'npm-test', 'passed': False, 'revision_id': 'rev',
                          'classification': 'application_defect', 'reason': result['reason'],
                          'service_diagnostic': result}], 'rev')
    assert not summary['checks_ok'] and not summary['checks_passed']
    assert summary['failures'][0]['service_diagnostic'] == result


@pytest.mark.parametrize('log', [
    'ERR_CONNECTION_REFUSED http://localhost:49152/',
    'ERR_CONNECTION_REFUSED http://[::1]:49152/',
    'ERR_CONNECTION_REFUSED http://127.0.0.1:bad/',
    'ERR_CONNECTION_REFUSED https://example.com/',
    'ERR_CONNECTION_REFUSED without a URL',
    'AssertionError expected Ready but got empty http://127.0.0.1:3001/',
])
def test_no_invented_endpoint_mismatch(log):
    assert feedback(log) is None


def test_only_ready_controller_endpoints_can_establish_expectation():
    log = 'ERR_CONNECTION_REFUSED http://127.0.0.1:3001/'
    assert feedback(log, ready=False) is None
    assert service_endpoint_feedback(log, {}, []) is None
    result = feedback(log, service='frontend')
    assert list(result['endpoints']) == ['DAEDALUS_FRONTEND_URL']
    assert 'DAEDALUS_APP_URL' not in result['reason']


def test_endpoint_feedback_is_bounded_and_accepts_multiple_services():
    env = {'DAEDALUS_APP_URL': 'http://127.0.0.1:49152', 'DAEDALUS_API_URL': 'http://127.0.0.1:49153'}
    launches = [{'passed': True, 'url': url} for url in env.values()]
    assert service_endpoint_feedback('ECONNREFUSED http://127.0.0.1:49153/', env, launches) is None
    log = 'ECONNREFUSED ' + ' '.join(f'http://127.0.0.1:{port}/' for port in range(3000, 3020))
    assert len(service_endpoint_feedback(log, env, launches)['reported_urls']) == 8
