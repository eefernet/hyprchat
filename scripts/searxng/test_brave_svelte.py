"""Offline regressions for brave-svelte-data.patch (pinned upstream backport)."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from searx.engines import brave


def payload(result, category='search'):
    response = {'web' if category == 'search' else 'news': {'results': [result]}}
    data = {'body': {'response': response}} if category == 'search' else {'response': response}
    return '<script>data: [(function(a){return {type:"data",data:a}})({}), ' + json.dumps({'type':'data','data':data}) + '],\nform: null</script>'


class BraveSvelteTests(unittest.TestCase):
    def test_request_preserves_transport_encoding_defaults(self):
        params = {'headers': {}, 'cookies': {}, 'time_range': '', 'safesearch': 0,
                  'searxng_locale': 'en', 'pageno': 1}
        traits = SimpleNamespace(get_region=lambda *args: 'US', custom={'ui_lang': {}})
        with patch.object(brave, 'traits', traits, create=True), \
                patch.object(brave.locales, 'get_engine_locale', return_value='en-us'):
            brave.request('python list documentation', params)
        self.assertNotIn('Accept-Encoding', params['headers'])
        self.assertEqual(params['cookies']['useLocation'], '0')
        self.assertEqual(params['cookies']['summarizer'], '0')

    def test_mixed_wrapped_and_plain_objects_return_web_results(self):
        result = {'title':'Python reference','url':'https://docs.python.org/3/','description':'Python documentation'}
        with patch.object(brave, 'brave_category', 'search'):
            results = brave.response(SimpleNamespace(text=payload(result)))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, result['url'])
        self.assertEqual(results[0].content, result['description'])

    def test_news_without_rendered_html_results(self):
        result = {'title':'Science update','url':'https://example.org/news','description':'A new discovery.'}
        with patch.object(brave, 'brave_category', 'news'):
            results = brave.response(SimpleNamespace(text=payload(result, 'news')))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, result['title'])

    def test_wrapped_data_handles_brackets_commas_and_identifiers_in_strings(self):
        text = 'data: [(function(a,b){return {type:"data",data:{value:a,items:[a,b],text:"a, ] }"}})("first, value",false))],\n'
        data = brave.extract_json_data(text)['data'][0]['data']
        self.assertEqual(data, {'value':'first, value','items':['first, value',False],'text':'a, ] }'})

    def test_original_plain_object_payload(self):
        self.assertEqual(brave.extract_json_data('data: [{type:"data",data:{value:1}}],\n')['data'][0]['data'], {'value':1})

    def test_arbitrary_javascript_and_unknown_pages_are_rejected(self):
        for text in ['<h1>Challenge</h1>', 'data: [alert("not executed")],',
                     'data: [(function(a){return {data:a}})(fetch("https://example.org"))],',
                     'data: [{data:"unterminated}]:']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                brave.extract_json_data(text)

    def test_explicit_no_results_is_not_a_parser_failure(self):
        text='data: [{type:"data",data:{}},{type:"data",data:{noResults:true}}],\n'
        with patch.object(brave, 'brave_category', 'search'):
            self.assertEqual(len(brave.response(SimpleNamespace(text=text))), 0)


if __name__ == '__main__':
    unittest.main()
