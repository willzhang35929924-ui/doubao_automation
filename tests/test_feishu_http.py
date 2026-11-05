"""Offline HTTP and atomic-download regressions; all credentials/media are fixtures."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import requests
from doubao_automation import feishu_client as module
from doubao_automation.feishu_client import FeishuClient, FeishuError


def config():
    return {'feishu': {'app_id':'fixture-app','app_secret':'fixture-secret','app_token':'fixture-base',
                       'products_table':{'table_id':'fixture-products'},
                       'scripts_table':{'table_id':'fixture-scripts'}}}


class Response:
    def __init__(self, status=200, payload=None, body=b'fixture', headers=None, stream_error=None):
        self.status_code=status
        self.payload=payload
        self.body=body
        self.headers=headers or {}
        self.stream_error=stream_error
        self.closed=False
    def json(self):
        if self.payload is None: raise ValueError('fixture non-JSON')
        return self.payload
    def iter_content(self, chunk_size):
        yield self.body
        if self.stream_error: raise self.stream_error
    def close(self): self.closed=True
    def __enter__(self): return self
    def __exit__(self,*args): self.close()


class FeishuHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='feishu-http-test-')
        self.root=Path(self.tmp.name)
        self.logs=patch.object(module,'LOGS_DIR',self.root/'logs');self.logs.start()
        self.sleep=patch.object(module.time,'sleep');self.sleep_mock=self.sleep.start()
        self.client=FeishuClient(config())
        self.client._token='fixture-token';self.client._token_expire=time.time()+7000
        self.request=Mock();self.client.session.request=self.request
    def tearDown(self):
        self.client.close();self.sleep.stop();self.logs.stop();self.tmp.cleanup()

    def test_bitable_extra_survives_and_only_canonical_api_receives_bearer(self):
        extra=json.dumps({'bitablePerm':{'tableId':'fixture-table','rev':32}},separators=(',',':'))
        attachment={'file_token':'fixture-file','size':5,
                    'url':'https://open.feishu.cn/open-apis/drive/v1/medias/fixture-file/download?'+urlencode({'extra':extra})}
        self.request.return_value=Response(body=b'fresh',headers={'Content-Type':'image/png'})
        dest=self.root/'result.png'
        self.client.download_media('fixture-file',dest,attachment=attachment)
        args,kwargs=self.request.call_args
        self.assertEqual(args,('GET','https://open.feishu.cn/open-apis/drive/v1/medias/fixture-file/download'))
        self.assertEqual(kwargs['params'],{'extra':extra})
        self.assertEqual(kwargs['headers']['Authorization'],'Bearer fixture-token')
        self.assertNotIn('verify',kwargs,'TLS verification must not be overridden')
        self.assertTrue(self.client.session.verify)
        self.assertEqual(dest.read_bytes(),b'fresh')

    def test_external_metadata_url_is_never_requested_or_given_credentials(self):
        self.request.return_value=Response(body=b'fresh')
        self.client.download_media('fixture-file',self.root/'result.bin',
                                   attachment={'url':'https://attacker.example/file?extra=private','size':5})
        args,kwargs=self.request.call_args
        self.assertTrue(args[1].startswith('https://open.feishu.cn/'))
        self.assertEqual(kwargs['params'],{})
        self.assertNotIn('attacker',str(self.request.call_args))

    def test_tmp_url_extra_and_invalid_token_handling(self):
        self.request.return_value=Response(body=b'fresh')
        extra='{"bitablePerm":{"tableId":"fixture"}}'
        self.client.download_media('fixture-file',self.root/'result.bin',attachment={
            'tmp_url':'https://open.feishu.cn/open-apis/drive/v1/medias/batch_get_tmp_download_url?'+urlencode({'extra':extra,'file_tokens':'fixture-file'})})
        self.assertEqual(self.request.call_args.kwargs['params'],{'extra':extra})
        count=self.request.call_count
        for token in ('fixture-file,','../fixture','fixture/file','fixture?query'):
            with self.assertRaises(FeishuError):self.client.download_media(token,self.root/'bad')
        self.assertEqual(self.request.call_count,count)

    def test_empty_body_400_is_not_retried_and_has_request_id(self):
        self.request.return_value=Response(400,body=b'',headers={'X-Tt-Logid':'request-fixture-400'})
        dest=self.root/'existing';dest.write_bytes(b'keep')
        with self.assertRaises(FeishuError) as caught:self.client.download_media('fixture-file',dest)
        self.assertEqual(self.request.call_count,1)
        self.assertEqual(caught.exception.http_status,400)
        self.assertIn('request-fixture-400',str(caught.exception))
        self.assertNotIn('fixture-file',str(caught.exception))
        self.assertEqual(dest.read_bytes(),b'keep')

    def test_auth_ssl_eof_retries_then_reuses_token(self):
        self.client._token=''
        self.request.side_effect=[requests.exceptions.SSLError('[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol'),
                                  Response(payload={'code':0,'tenant_access_token':'fixture-new','expire':7200})]
        self.assertEqual(self.client.token(),'fixture-new')
        self.assertEqual(self.client.token(),'fixture-new')
        self.assertEqual(self.request.call_count,2)
        self.assertEqual(self.sleep_mock.call_count,1)
        self.assertEqual(self.request.call_args.args[0],'POST')

    def test_certificate_failure_is_not_retried_or_disabled(self):
        self.client._token=''
        self.request.side_effect=requests.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED fixture-secret')
        with self.assertRaises(FeishuError) as caught:self.client.token()
        self.assertEqual(self.request.call_count,1)
        self.assertFalse(caught.exception.retryable)
        self.assertIn('证书验证失败',str(caught.exception))
        self.assertNotIn('fixture-secret',str(caught.exception))
        self.assertTrue(self.client.session.verify)

    def test_auth_read_timeout_is_not_blindly_replayed(self):
        self.client._token=''
        self.request.side_effect=requests.exceptions.ReadTimeout('fixture-secret')
        with self.assertRaises(FeishuError) as caught:self.client.token()
        self.assertEqual(self.request.call_count,1)
        self.assertFalse(caught.exception.retryable)

    def test_auth_business_error_is_not_retried_and_body_is_not_exposed(self):
        self.client._token=''
        self.request.return_value=Response(400,payload={'code':10003,'msg':'fixture-secret https://private.example/?extra=private'},
                                           headers={'X-Tt-Logid':'request-fixture'})
        with self.assertRaises(FeishuError) as caught:self.client.token()
        self.assertEqual(self.request.call_count,1)
        self.assertEqual(caught.exception.api_code,10003)
        self.assertNotIn('fixture-secret',str(caught.exception))
        self.assertNotIn('private.example',str(caught.exception))
        self.assertNotIn('fixture-secret',(self.root/'logs/feishu_sync.log').read_text())

    def test_get_503_retries_with_bounded_retry_after(self):
        first=Response(503,headers={'Retry-After':'3600'})
        self.request.side_effect=[first,Response(payload={'code':0,'data':{'items':[]}})]
        self.assertEqual(self.client.list_fields('fixture-table'),[])
        self.assertEqual(self.request.call_count,2)
        self.assertTrue(first.closed)
        self.assertLessEqual(self.sleep_mock.call_args.args[0],5)

    def test_interrupted_download_preserves_existing_file_and_cleans_parts(self):
        dest=self.root/'existing';dest.write_bytes(b'keep')
        self.request.side_effect=[Response(body=b'partial',stream_error=requests.exceptions.ChunkedEncodingError('fixture-url-secret')) for _ in range(3)]
        with self.assertRaises(FeishuError) as caught:self.client.download_media('fixture-file',dest)
        self.assertEqual(self.request.call_count,3)
        self.assertEqual(dest.read_bytes(),b'keep')
        self.assertEqual(list(self.root.glob('*.part')),[])
        self.assertIn('3 次',str(caught.exception))
        self.assertNotIn('fixture-url-secret',str(caught.exception))

    def test_download_restarts_partial_transfer_then_atomically_publishes(self):
        dest=self.root/'existing';dest.write_bytes(b'keep')
        self.request.side_effect=[Response(body=b'bad',stream_error=requests.exceptions.ChunkedEncodingError()),
                                  Response(body=b'fresh',headers={'Content-Length':'5'})]
        self.client.download_media('fixture-file',dest,attachment={'size':5})
        self.assertEqual(dest.read_bytes(),b'fresh')
        self.assertEqual(self.request.call_count,2)
        self.assertEqual(list(self.root.glob('*.part')),[])

    def test_short_body_never_replaces_existing_attachment(self):
        dest=self.root/'existing';dest.write_bytes(b'keep')
        self.request.side_effect=[Response(body=b'short',headers={'Content-Length':'10'}) for _ in range(3)]
        with self.assertRaises(FeishuError):self.client.download_media('fixture-file',dest,attachment={'size':10})
        self.assertEqual(dest.read_bytes(),b'keep')

    def test_partial_content_is_rejected_even_without_metadata_size(self):
        dest=self.root/'existing';dest.write_bytes(b'keep')
        self.request.return_value=Response(206,body=b'only-a-range')
        with self.assertRaises(FeishuError) as caught:self.client.download_media('fixture-file',dest)
        self.assertEqual(caught.exception.http_status,206)
        self.assertEqual(self.request.call_count,1)
        self.assertEqual(dest.read_bytes(),b'keep')

    def test_html_proxy_response_is_not_published_with_or_without_content_type(self):
        for headers in ({'Content-Type':'text/html; charset=utf-8'},{}):
            with self.subTest(headers=headers):
                dest=self.root/'existing';dest.write_bytes(b'keep')
                self.request.return_value=Response(body=b'<!DOCTYPE HTML><html>fixture login page</html>',headers=headers)
                with self.assertRaisesRegex(FeishuError,'HTML'):self.client.download_media('fixture-file',dest,attachment={'size':0})
                self.assertEqual(dest.read_bytes(),b'keep')
                self.assertEqual(list(self.root.glob('*.part')),[])

    def test_https_cdn_redirect_does_not_receive_api_bearer(self):
        first=Response(302,headers={'Location':'https://cdn.example/download?signature=fixture'})
        self.request.side_effect=[first,Response(body=b'fresh')]
        self.client.download_media('fixture-file',self.root/'result')
        self.assertEqual(self.request.call_args_list[0].kwargs['headers']['Authorization'],'Bearer fixture-token')
        self.assertEqual(self.request.call_args_list[1].kwargs['headers'],{})
        self.assertTrue(first.closed)

    def test_insecure_redirect_is_rejected_before_following(self):
        self.request.return_value=Response(302,headers={'Location':'http://cdn.example/download'})
        with self.assertRaises(FeishuError):self.client.download_media('fixture-file',self.root/'result')
        self.assertEqual(self.request.call_count,1)
        self.assertFalse((self.root/'result').exists())


if __name__=='__main__':unittest.main()
