"""Offline integration checks. Run with python -m unittest test_server.py."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['API_TOKEN'] = 'offline-test-only-' + 'x' * 40
os.environ['OUTPUT_DIR'] = tempfile.mkdtemp(prefix='velora-api-test-')
import server
from fastapi.testclient import TestClient

AUTH = {'Authorization': 'Bearer ' + os.environ['API_TOKEN']}

class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.context = TestClient(server.app)
        self.client = self.context.__enter__()
    def tearDown(self):
        self.context.__exit__(None, None, None)
        server.JOBS.clear()
    def test_authentication(self):
        self.assertEqual(self.client.get('/health').status_code, 401)
        self.assertEqual(self.client.get('/health', headers=AUTH).status_code, 200)
    def test_url_allowlist(self):
        accepted = [
            ('https://youtu.be/abcdefghijk', 'https://www.youtube.com/watch?v=abcdefghijk'),
            ('https://www.youtube.com/shorts/abcdefghijk', 'https://www.youtube.com/watch?v=abcdefghijk'),
            ('https://m.youtube.com/watch?v=abcdefghijk&feature=share', 'https://www.youtube.com/watch?v=abcdefghijk')]
        for raw, expected in accepted:
            self.assertEqual(server.normalize_url(raw), expected)
        for raw in ['http://youtube.com/watch?v=abcdefghijk', 'https://youtube.com.evil.test/watch?v=abcdefghijk', 'https://evil.test/abcdefghijk', 'https://user:pass@youtube.com/watch?v=abcdefghijk', 'https://youtube.com:8443/watch?v=abcdefghijk', 'https://youtube.com/watch?v=short', 'https://youtube.com/channel/abcdefghijk']:
            with self.assertRaises(ValueError):
                server.normalize_url(raw)
    def test_private_request_and_no_arbitrary_url(self):
        r = self.client.post('/resolve', headers=AUTH, json={'url':'https://127.0.0.1/internal','mode':'video'})
        self.assertEqual(r.status_code, 400)
        with patch('server.resolve_media', side_effect=ValueError('private video sign in')):
            r = self.client.post('/resolve', headers=AUTH, json={'url':'https://youtu.be/abcdefghijk','mode':'video'})
            self.assertEqual(r.status_code, 422)
            self.assertIn('authorized access', r.json()['error'])
    def test_job_validation(self):
        base = {'url':'https://youtu.be/abcdefghijk','mode':'video','quality':1080,'rights':False}
        self.assertEqual(self.client.post('/jobs',headers=AUTH,json=base).status_code,400)
        base.update(rights=True,quality=8000)
        self.assertEqual(self.client.post('/jobs',headers=AUTH,json=base).status_code,400)
        base.update(quality=1080,mode='playlist',url='https://www.youtube.com/playlist?list=PLabcdefghijk',entries=['abcdefghijk']*2)
        self.assertEqual(self.client.post('/jobs',headers=AUTH,json=base).status_code,400)
    def test_formats_are_from_source(self):
        info = {'id':'abcdefghijk','title':'Actual source','uploader':'Owner','formats':[{'height':720,'vcodec':'h264'},{'height':1080,'vcodec':'av1'},{'height':4320,'vcodec':'av1'},{'height':1080,'vcodec':'h264'}]}
        with patch('server.YoutubeDL') as y:
            y.return_value.__enter__.return_value.extract_info.return_value = info
            result = server.resolve_media('https://youtu.be/abcdefghijk','video')
        self.assertEqual([f['height'] for f in result['formats']],[1080,720])
    def test_file_and_expiry(self):
        key='a'*32;folder=server.ROOT/key;folder.mkdir();file=folder/'sample.mp4';file.write_bytes(b'test-media')
        now=server.time.time()
        server.JOBS[key]={'id':key,'status':'ready','progress':100,'file':str(file),'created':now,'updated':now}
        r=self.client.get('/jobs/'+key+'/file',headers=AUTH)
        self.assertEqual(r.status_code,200);self.assertEqual(r.content,b'test-media')
        self.assertNotIn('file',self.client.get('/jobs/'+key,headers=AUTH).json())
        server.JOBS[key]['updated']=now-server.TTL-1
        self.assertEqual(self.client.get('/jobs/'+key,headers=AUTH).status_code,404)
        self.assertFalse(folder.exists())
    def test_progress_does_not_move_backwards(self):
        key='b'*32;server.JOBS[key]={'progress':45,'updated':server.time.time()}
        server.mutate(key,progress=20)
        self.assertEqual(server.JOBS[key]['progress'],45)

if __name__=='__main__':
    unittest.main()
