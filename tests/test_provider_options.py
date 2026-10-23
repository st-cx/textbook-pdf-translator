"""Inference controls are explicit, validated and never replace request identity."""
import json,unittest
from unittest.mock import patch
from textbook_pdf.translation import _load_provider,_chat

class ProviderOptionTests(unittest.TestCase):
    def provider(self,**options):
        return _load_provider({'_default':'demo','providers':{'demo':{
            'base_url':'https://example.invalid/v1','api_key':'test-only',
            'model':'example-model',**options}}},None)

    def request(self,provider):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return json.dumps({'choices':[{'message':{'content':'{}'},'finish_reason':'stop'}],
                'usage':{'completion_tokens':7,'completion_tokens_details':{'reasoning_tokens':0}}}).encode()
        with patch('textbook_pdf.translation.urllib.request.build_opener')as factory:
            factory.return_value.open.return_value=Response()
            result,usage=_chat(provider,[{'role':'user','content':'self-authored test'}])
            request=factory.return_value.open.call_args.args[0]
            return json.loads(request.data),result,usage

    def test_omitted_controls_preserve_default_body(self):
        body,_,_=self.request(self.provider())
        self.assertNotIn('thinking',body);self.assertNotIn('reasoning_effort',body)

    def test_explicit_disabled_thinking_keeps_model_messages_and_limits(self):
        body,_,usage=self.request(self.provider(thinking='disabled',max_tokens=2048))
        self.assertEqual(body['thinking'],{'type':'disabled'})
        self.assertEqual(body['model'],'example-model')
        self.assertEqual(body['messages'],[{'role':'user','content':'self-authored test'}])
        self.assertEqual(body['max_tokens'],2048)
        self.assertEqual(usage['completion_tokens_details']['reasoning_tokens'],0)

    def test_low_effort_and_enabled_thinking_are_forwarded(self):
        body,_,_=self.request(self.provider(thinking='enabled',reasoning_effort='low'))
        self.assertEqual(body['thinking'],{'type':'enabled'})
        self.assertEqual(body['reasoning_effort'],'low')

    def test_invalid_controls_fail_before_network(self):
        for key,value in [('thinking',True),('thinking',{'type':'disabled'}),('thinking','off'),
                          ('reasoning_effort',{}),('reasoning_effort','unbounded')]:
            with self.subTest(key=key,value=value),patch('textbook_pdf.translation.urllib.request.build_opener')as network:
                with self.assertRaises(ValueError):self.provider(**{key:value})
                network.assert_not_called()

    def test_independent_review_provider_keeps_its_own_default(self):
        config={'providers':{'translation':{'base_url':'https://example.invalid/v1','api_key':'test',
            'model':'translator','thinking':'disabled'},'review':{'base_url':'https://example.invalid/v1',
            'api_key':'test','model':'reviewer'}}}
        self.assertEqual(_load_provider(config,'translation').thinking,'disabled')
        self.assertIsNone(_load_provider(config,'review').thinking)

if __name__=='__main__':unittest.main()
