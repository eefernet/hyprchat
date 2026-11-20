"""Offline privacy/failure regressions for the Brave link-resolution adapter."""
import asyncio
import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from searx import brave_urlcloak as cloak

RAW = base64.urlsafe_b64encode(bytes([1])+bytes(12)+bytes([1])+bytes(140)).decode().rstrip('=')
KEY = {'version':1, 'public_key':base64.b64encode(bytes([1])*32).decode(),
       'sig_public_key':base64.b64encode(bytes([2])*32).decode()}


class BraveUrlcloakTests(unittest.TestCase):
    def test_all_known_cloak_shapes(self):
        for url in ['/go/v1/'+RAW, '/go/v1#'+RAW,
                    'https://search.brave.com/go/v1/'+RAW,
                    'https://search.brave.com/go/v1#'+RAW]:
            self.assertEqual(cloak._token(url), RAW)

    def test_malformed_or_non_oblivious_redirects_rejected(self):
        for url in ['/go/v1/bad', '/go/v1/'+RAW+'?confirm',
                    'https://search.brave.com/go/v1/notice?url=example',
                    'http://search.brave.com/go/v1/'+RAW,
                    'https://name:secret@search.brave.com/go/v1/'+RAW]:
            with self.subTest(url=url), self.assertRaises(Exception):cloak._token(url)

    def test_direct_destinations_preserved_without_network(self):
        rows=[SimpleNamespace(url='https://docs.python.org/3/?q=one%20two#part'),SimpleNamespace(suggestion='related')]
        with patch.object(cloak, 'get_context_network', side_effect=AssertionError('network must not run')):
            self.assertIs(cloak.resolve_result_links(rows), rows)
        self.assertEqual(rows[0].url,'https://docs.python.org/3/?q=one%20two#part')

    def test_unsafe_or_unresolved_urls_never_pass(self):
        for url in ['http:///go/v1/bad','javascript:alert(1)','https://127.0.0.1/',
                    'https://[::1]/','https://localhost/','https://192.168.1.1/',
                    'https://user:secret@example.org/','https://example.org\\@localhost/',
                    'https://search.brave.com/go/v1/'+RAW]:
            with self.subTest(url=url),self.assertRaises(Exception):cloak._destination(url)

    def test_public_key_devalue_is_narrow_and_bounded(self):
        data={'nodes':[None, {'data':[{'cloakKey':1},{'version':2,'public_key':3,'sig_public_key':4},1,KEY['public_key'],KEY['sig_public_key']]}]}
        self.assertEqual(cloak._extract_key(data),KEY)
        for bad in [True,-1,100]:
            data['nodes'][1]['data'][0]['cloakKey']=bad
            with self.assertRaises(Exception):cloak._extract_key(data)

    def test_crypto_failure_is_sanitized(self):
        result=SimpleNamespace(returncode=1,stdout='',stderr='private query token or state')
        with patch.object(cloak.subprocess,'run',return_value=result),self.assertRaises(Exception) as caught:
            cloak._crypto('prepare',KEY,[RAW],cloak.default_timer()+2)
        self.assertNotIn('private',str(caught.exception));self.assertNotIn(RAW,str(caught.exception))

    def test_repeated_links_resolve_once_and_publish_together(self):
        rows=[SimpleNamespace(url='/go/v1/'+RAW),SimpleNamespace(url='/go/v1/'+RAW)]
        with patch.object(cloak,'get_context_network'),patch.object(cloak,'_public_key',return_value=KEY), \
             patch.object(cloak,'_crypto',side_effect=[[{'sid':'sid','state':'secret','blinded_element':'blind'}],['https://docs.python.org/3/']]) as crypto, \
             patch.object(cloak,'_evaluate',return_value=[{}]):
            cloak.resolve_result_links(rows)
        self.assertEqual(crypto.call_args_list[0].args[2],[RAW])
        self.assertTrue(all(r.url=='https://docs.python.org/3/' for r in rows))

    def test_failed_resolution_leaves_every_result_unpublished(self):
        rows=[SimpleNamespace(url='/go/v1/'+RAW)]
        with patch.object(cloak,'get_context_network'),patch.object(cloak,'_public_key',return_value=KEY), \
             patch.object(cloak,'_crypto',side_effect=[[{}],['https://127.0.0.1/']]), \
             patch.object(cloak,'_evaluate',return_value=[{}]),self.assertRaises(Exception):
            cloak.resolve_result_links(rows)
        self.assertEqual(rows[0].url,'/go/v1/'+RAW)

    def test_transport_sends_only_blinded_fields_with_no_redirects(self):
        calls=[]
        response={'evaluated_element':base64.b64encode(bytes(32)).decode(),'proof':base64.b64encode(bytes(64)).decode()}
        async def request(method,url,**kwargs):
            calls.append((method,url,kwargs))
            return SimpleNamespace(status_code=200,content=b'{}',json=lambda:response)
        item={'token':RAW,'state':'private-blinding-state','sid':'public-page-sid','blinded_element':'blind'}
        result=cloak._evaluate(SimpleNamespace(request=request),[item],cloak.default_timer()+2)
        self.assertEqual(calls[0][1],cloak.EVALUATE_URL)
        self.assertEqual(calls[0][2]['json'],{'sid':'public-page-sid','blinded_element':'blind'})
        self.assertFalse(calls[0][2]['allow_redirects'])
        self.assertTrue(calls[0][2]['raise_for_httperror'])
        self.assertEqual(calls[0][2]['headers']['Referer'],'')
        self.assertNotIn('cookies',calls[0][2])
        self.assertEqual(result[0]['state'],'private-blinding-state')

    def test_provider_denial_is_not_hidden_or_retried(self):
        calls=[]
        async def request(*args,**kwargs):
            calls.append(1)
            raise RuntimeError('provider denied')
        with self.assertRaisesRegex(RuntimeError,'provider denied'):
            cloak._evaluate(SimpleNamespace(request=request),[{'sid':'sid','blinded_element':'blind'}],cloak.default_timer()+2)
        self.assertEqual(len(calls),1)

    def test_timeout_cancels_pending_requests(self):
        cancelled=threading_event=__import__('threading').Event()
        async def request(*args,**kwargs):
            try:await asyncio.sleep(60)
            finally:cancelled.set()
        with self.assertRaises(Exception):
            cloak._evaluate(SimpleNamespace(request=request),[{'sid':'sid','blinded_element':'blind'}],cloak.default_timer()+.03)
        self.assertTrue(threading_event.wait(1))


if __name__=='__main__':unittest.main()
