import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from document_validation import validate_content
from tooling.parser import parse_text_tool_calls

@pytest.mark.parametrize('text', [
    'document_create(format="docx", content={"blocks": [{"text": "Report"}, {"type": "table", "rows": [["A", "B"], ["1", "2"]]}]})',
    '<function=document_create><parameter=format>docx</parameter><parameter=content>{"blocks": [{"text": "Report"}, {"type": "table", "rows": [["A", "B"], ["1", "2"]]}]}</parameter></function>',
    '{"name":"document_create","arguments":{"format":"docx","content":{"blocks":[{"text":"Report"},{"type":"table","rows":[["A","B"],["1","2"]]}]}}}',
])
def test_fallback_preserves_nested_document_tables(text):
    calls = parse_text_tool_calls(text, {'document_create'})
    assert len(calls) == 1
    args = calls[0]['function']['arguments']
    content = validate_content(args['format'], args['content'])
    assert content['blocks'][1]['rows'][1] == ['1', '2']


def test_python_fallback_does_not_execute_expressions(tmp_path):
    target = tmp_path / 'never-created'
    text = f'document_create(format="docx", content=__import__("pathlib").Path({str(target)!r}).touch())'
    assert not parse_text_tool_calls(text, {'document_create'})
    assert not target.exists()


@pytest.mark.parametrize('wrap', ['python', 'json', 'xml'])
def test_markdown_fallback_preserves_full_report_text(wrap):
    import json
    report = '# Title\n| A | B |\n| --- | --- |\n| one | two |\n\n## Last section\nComplete.'
    if wrap == 'python':
        text = f'document_create(format="docx", markdown={report!r})'
    elif wrap == 'json':
        text = json.dumps({'name': 'document_create', 'arguments': {'format': 'docx', 'markdown': report}})
    else:
        text = f'<function=document_create><parameter=format>docx</parameter><parameter=markdown>{report}</parameter></function>'
    calls = parse_text_tool_calls(text, {'document_create'})
    assert calls[0]['function']['arguments']['markdown'] == report

@pytest.mark.parametrize('fmt,content,path', [
    ('docx', {'blocks': [{'type': 'image'}]}, 'image_artifact_id'),
    ('pptx', {'slides': [{'title': 'Title', 'elements': [{'type': 'table'}]}]}, 'rows'),
    ('xlsx', {'sheets': [{'name': 'Data', 'cells': {'A1': {'unexpected': True}}}]}, 'A1'),
])
def test_nested_validation_explains_field(fmt, content, path):
    with pytest.raises(ValueError, match=path):
        validate_content(fmt, content)


def test_worksheet_rows_may_be_blank_or_uneven():
    """Found 2026-11-17: a budget sheet with a heading row and a blank spacer row was refused
    ("rows[2] has an invalid number of items") because every row needed at least one cell."""
    budget = {'sheets': [{'name': 'Budget', 'rows': [
        ['Monthly budget'], [], ['Item', 'Planned', 'Actual'], ['Rent', 1200, 1200], ['Food', 400],
        [], ['Total', '=SUM(B4:B5)', '=SUM(C4:C5)']]}]}
    assert validate_content('xlsx', budget) == budget
    # Word and slide tables still need cells in every row.
    with pytest.raises(ValueError, match=r'rows\[1\] has 0 items; it needs 1 to'):
        validate_content('docx', {'blocks': [{'type': 'table', 'rows': [['A'], []]}]})
    with pytest.raises(ValueError, match=r'content\.sheets\[0\]\.rows has 0 items; it needs 1 to'):
        validate_content('xlsx', {'sheets': [{'name': 'Empty', 'rows': []}]})


def test_invalid_arguments_allow_one_correction_then_disable_only_that_tool():
    import json
    from document_tools import DOCUMENT_TOOLS, apply_argument_retry
    names = {'document_create', 'document_read'}
    definitions = [DOCUMENT_TOOLS[n] for n in sorted(names)]
    failures = {}
    error = json.dumps({'status': 'failed', 'error_code': 'invalid_arguments', 'error': 'content.blocks is required'})
    first = json.loads(apply_argument_retry('document_create', error, failures, names, definitions))
    assert 'retry once' in first['instruction']
    assert 'document_create' in names
    second = json.loads(apply_argument_retry('document_create', error, failures, names, definitions))
    assert 'Do not retry' in second['instruction']
    assert names == {'document_read'}
    assert [x['function']['name'] for x in definitions] == ['document_read']
    assert second['status'] == 'failed'
    assert second['retry_exhausted'] is True
