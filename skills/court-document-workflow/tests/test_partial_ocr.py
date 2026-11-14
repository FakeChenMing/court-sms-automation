import unittest,tempfile,json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import workflow_worker
ledger=workflow_worker.ledger
class PartialOCR(unittest.TestCase):
 def test_partial_requires_source_images(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);source=root/'source.pdf';source.write_bytes(b'fixture');text=root/'ocr.md';text.write_text('OCR incomplete page 2')
   doc={'id':'a','case_number':'（2026）测01民初123号','sha256':ledger.digest(source),'paths':[str(source)],'ocr_status':'partial','ocr':{'text_path':str(text),'text_sha256':ledger.digest(text),'uncertain_pages':[2]},'review_status':'pending'}
   target=root/'ledger.json';target.write_text(json.dumps({'version':1,'documents':{'a':doc},'cases':{},'events':{}}))
   decision={'source_sha256':doc['sha256'],'all_pages_reviewed':True,'date_inventory':[],'conclusion':'无未来事项','events':[]}
   with patch.object(ledger,'STATE_DIR',root),patch.object(ledger,'LEDGER',target):
    with self.assertRaisesRegex(AssertionError,'逐页复核'):ledger.record_review('a',decision)
    self.assertEqual(json.loads(target.read_text())['documents']['a']['review_status'],'pending')
    decision['visual_reviewed_pages']=[2];ledger.record_review('a',decision)
    actual=json.loads(target.read_text())['documents']['a']
    self.assertEqual(actual['review_status'],'complete');self.assertEqual(actual['ocr_status'],'partial')
    decision['context_revision']=0
    with ledger.transaction() as data:data['cases'][doc['case_number']]={'revision':1}
    ledger.record_review('a',decision)
    self.assertTrue(json.loads(target.read_text())['documents']['a']['context_refresh_needed'])
if __name__=='__main__':unittest.main()
