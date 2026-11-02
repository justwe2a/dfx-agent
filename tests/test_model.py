import io
import json
import os
import unittest
from unittest.mock import patch
import urllib.error

from dfx_agent.model import DeepSeekModel, ModelError, NoRedirect


class ModelTests(unittest.TestCase):
    def envelope(self, content='{"action":"search","term":"distance"}', finish='stop'):
        return {'id':'test-id','model':'deepseek-flash','usage':{'prompt_tokens':12,'completion_tokens':8,'total_tokens':20},
                'choices':[{'finish_reason':finish,'message':{'content':content,'reasoning_content':'DO_NOT_SAVE'}}]}

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY':'TEST_SECRET'})
    @patch('urllib.request.build_opener')
    def test_transport_reads_env_and_preserves_usage_without_key_or_reasoning(self, opener):
        opener.return_value.open.return_value=io.BytesIO(json.dumps(self.envelope()).encode())
        result=DeepSeekModel('deepseek-flash', thinking='disabled').query({'instructions':'Return JSON','case_id':'synthetic'})
        self.assertEqual(result['_provider']['usage']['total_tokens'],20)
        self.assertNotIn('TEST_SECRET',json.dumps(result));self.assertNotIn('DO_NOT_SAVE',json.dumps(result))
        request=opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://api.deepseek.com/chat/completions')
        self.assertEqual(request.get_header('Authorization'),'Bearer TEST_SECRET')
        self.assertNotIn('TEST_SECRET',request.data.decode())

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY':'TEST_SECRET'})
    @patch('urllib.request.build_opener')
    def test_truncated_or_invalid_output_never_becomes_patch(self, opener):
        for envelope in [self.envelope(finish='length'),self.envelope(content='not JSON'),self.envelope(content='[]')]:
            opener.return_value.open.return_value=io.BytesIO(json.dumps(envelope).encode())
            with self.assertRaises(ModelError) as caught:DeepSeekModel('deepseek-flash', thinking='disabled').query({})
            self.assertEqual(caught.exception.metadata['usage']['total_tokens'],20)
            self.assertEqual(caught.exception.response_content,envelope['choices'][0]['message']['content'])
            self.assertNotIn('DO_NOT_SAVE',json.dumps(caught.exception.metadata))

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY':'TEST_SECRET'})
    @patch('urllib.request.build_opener')
    def test_invalid_envelope_does_not_save_http_body_as_model_content(self, opener):
        opener.return_value.open.return_value=io.BytesIO(b'not an envelope')
        with self.assertRaises(ModelError) as caught:DeepSeekModel('deepseek-flash').query({})
        self.assertIsNone(caught.exception.response_content)
        self.assertEqual(opener.return_value.open.call_count,1)

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY':'TEST_SECRET'})
    @patch('urllib.request.build_opener')
    def test_http_error_not_retried_and_body_not_logged(self, opener):
        opener.return_value.open.side_effect=urllib.error.HTTPError('https://api.deepseek.com',401,'contains secret',{},io.BytesIO(b'TEST_SECRET'))
        with self.assertRaises(ModelError) as caught:DeepSeekModel('deepseek-flash').query({})
        self.assertEqual(opener.return_value.open.call_count,1)
        self.assertNotIn('TEST_SECRET',str(caught.exception))
        self.assertEqual(caught.exception.metadata['http_status'],401)
        self.assertIsNone(caught.exception.response_content)

    def test_redirect_does_not_forward_bearer_token(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'redirect',{},'https://untrusted.example'))

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY':'TEST_SECRET'})
    @patch('urllib.request.build_opener')
    def test_returned_model_change_stops_without_retry(self, opener):
        value=self.envelope();value['model']='other-model'
        opener.return_value.open.return_value=io.BytesIO(json.dumps(value).encode())
        with self.assertRaisesRegex(ModelError,'differs') as caught:
            DeepSeekModel('deepseek-flash').query({})
        self.assertEqual(caught.exception.metadata['returned_model'],'other-model')
        self.assertEqual(opener.return_value.open.call_count,1)

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_key_and_missing_model_are_configuration_errors(self):
        with self.assertRaises(ValueError):DeepSeekModel('deepseek-flash')
        with self.assertRaises(ValueError):DeepSeekModel(None)
