"""Same-name media routing and containment use disposable fixture files only."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import dashboard

class ImportedMediaPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='doubao-media-paths-')
        self.root=Path(self.tmp.name)
        self.products=self.root/'workspace/feishu/products'
        self.sources=self.root/'workspace/feishu/sources'
        for name,content in [('a',b'fixture-a'),('b',b'fixture-b')]:
            p=self.products/name/'same.png';p.parent.mkdir(parents=True);p.write_bytes(content)
        self.sources.mkdir(parents=True)
        (self.root/'private.yaml').write_text('fixture-private')
        self.patches=[patch.object(dashboard,'DATA_ROOT',self.root),
                      patch.object(dashboard,'PRODUCTS_DIR',self.products),
                      patch.object(dashboard,'SOURCES_DIR',self.sources)]
        for p in self.patches:p.start()
        self.client=dashboard.app.test_client()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def test_equal_filenames_return_their_own_product_images(self):
        for name,expected in [('a',b'fixture-a'),('b',b'fixture-b')]:
            with self.client.get('/media-file',query_string={'path':f'workspace/feishu/products/{name}/same.png'}) as r:
                self.assertEqual(r.status_code,200)
                self.assertEqual(r.data,expected)
    def test_absolute_imported_paths_remain_supported(self):
        with self.client.get('/media-file',query_string={'path':str(self.products/'b/same.png')}) as r:
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.data,b'fixture-b')
    def test_private_files_traversal_and_symlink_escape_are_rejected(self):
        link=self.products/'escape.png';link.symlink_to(self.root/'private.yaml')
        for value in ['private.yaml',str(self.root/'private.yaml'),'../../private.yaml',str(link),'']:
            with self.subTest(value=value):
                r=self.client.get('/media-file',query_string={'path':value})
                self.assertEqual(r.status_code,404)

if __name__=='__main__':unittest.main()
