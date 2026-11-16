import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import dashboard
import observatory


@pytest.fixture
def report(tmp_path):
    source = tmp_path / 'report.json'
    row = observatory.empty_record('grok')
    row['model'] = '<script>private</script>'
    report = observatory.collect_report([])
    report['records'] = [row]
    report['rawConversation'] = 'DO NOT EXPORT THIS'
    source.write_text(json.dumps(report), encoding='utf-8')
    return SimpleNamespace(observatory_report=str(source)), source


def test_observatory_get_is_sanitized_read_only_and_versioned(report):
    config, source = report
    before = source.read_bytes()
    files = set(source.parent.iterdir())
    status, _, body = dashboard.route(config, '/observatory')
    assert status == 200 and b'<meter ' in body
    assert b'unknown' in body and b'&lt;script&gt;private&lt;/script&gt;' in body
    assert b'<script>private</script>' not in body
    status, content_type, body = dashboard.route(config, '/observatory.json?path=/forbidden')
    assert status == 200 and content_type.startswith('application/json')
    assert b'DO NOT EXPORT' not in body
    assert json.loads(body)['records'][0]['credits'] is None
    status, _, body = dashboard.route(config, '/observatory.csv')
    assert status == 200 and b'recordType' in body
    assert source.read_bytes() == before and set(source.parent.iterdir()) == files


@pytest.mark.parametrize('quality', [[1], [{'reviews': [None]}], [{'verificationAttempts': [1]}]])
def test_corrupt_quality_returns_validation_error(report, quality):
    config, source = report
    value = json.loads(source.read_text())
    value['quality'] = quality
    source.write_text(json.dumps(value))
    assert dashboard.route(config, '/observatory')[0] == 422


def test_actual_http_server_only_serves_reads(report):
    config, _ = report
    server = dashboard.Server(config, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base + '/observatory.json', timeout=3) as response:
            assert response.status == 200 and json.load(response)['schema'] == 2
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + '/observatory', data=b'approve=true', method='POST'), timeout=3)
        assert error.value.code == 501
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
