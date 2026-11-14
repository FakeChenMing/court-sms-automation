import unittest,tempfile,json,asyncio
from pathlib import Path
from unittest.mock import patch
import workflow_worker as w
class FakeLaws:
 def __init__(self):self.audit=[];self.errors=[]
 async def __aenter__(self):return self
 async def __aexit__(self,*args):pass
 def schemas(self):return []
class ImageBatches(unittest.TestCase):
 def run_case(self,last_approved):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);source=root/'fixture.pdf'
   pdf=w.pymupdf.open()
   for i in range(33):pdf.new_page().insert_text((30,40),'Fixture page '+str(i+1))
   pdf.save(source);pdf.close()
   docs=[{'id':'a','source_sha256':'fixture-hash','source_path':str(source),'pages':[{'page':i+1,'text':'Fixture page '+str(i+1),'needs_visual_review':True} for i in range(33)]}]
   result={'case_number':'（2026）测01民初123号','documents':[{'id':'a','reviewed_pages':list(range(1,34)),'date_inventory':[],'conclusion':'fixture'}],'events':[],'parties':[]}
   image_counts=[]
   def model(request):
    if 'tools' in request:value=result
    else:
     image_counts.append(len(json.loads(request['messages'][0]['content'][0]['text'])['source_crosschecks']))
     value={'approved':last_approved if json.loads(request['messages'][0]['content'][0]['text'])['batch_number']==2 else True,'issues':[] if last_approved else ['unreadable final image']}
    return {'content':[{'type':'text','text':json.dumps(value)}]}
   out=root/'run';out.mkdir()
   with patch.object(w,'LegalSources',FakeLaws),patch.object(w,'model',model),patch.object(w,'SKILLS',[]),patch.object(w,'same_case_messages',return_value={'messages':[], 'source_status':{}, 'errors':[]}),patch.object(w,'read_page',side_effect=lambda d,p:{'doc_id':d['id'],'page':p,'readable_text':True,'manual_source_review':None,'text':'fixture second OCR','engine':'fixture second engine'}):
    if last_approved:asyncio.run(w.review_case(result['case_number'],docs,['a'],[],out))
    else:
     with self.assertRaisesRegex(ValueError,'独立复核'):asyncio.run(w.review_case(result['case_number'],docs,['a'],[],out))
   report=json.loads((out/'verification.json').read_text())
   self.assertEqual(image_counts,[32,1] if last_approved else [32,1]*3);self.assertEqual(len(report['required_source_pages']),33);self.assertEqual(report['approved'],last_approved)
 def test_partial_page_cannot_use_second_ocr_as_visual_review(self):
  with tempfile.TemporaryDirectory() as td:
   out=Path(td);docs=[{'id':'a','uncertain_pages':[1],'pages':[{'page':1,'text':'partial','needs_visual_review':True}]}]
   with patch.object(w,'read_page',return_value={'readable_text':True,'manual_source_review':None}),patch.object(w,'model') as model:
    with self.assertRaisesRegex(ValueError,'须实际原图复核'):asyncio.run(w.verify_sources('case',docs,{'events':[],'parties':[]},[],{},out))
    model.assert_not_called()
 def test_continuation_deadline_page_is_crosschecked(self):
  with tempfile.TemporaryDirectory() as td:
   docs=[{'id':'a','pages':[{'page':1,'text':'notice'},{'page':2,'text':'deadline continuation'}]}]
   result={'documents':[{'id':'a','date_inventory':[{'source_page':2,'status':'anchor_unverified'}]}],'events':[],'parties':[]}
   with patch.object(w,'read_page',return_value={'readable_text':True,'manual_source_review':None}) as reader,patch.object(w,'model',return_value={'content':[{'type':'text','text':'{"approved":true,"issues":[]}'}]}):
    asyncio.run(w.verify_sources('case',docs,result,[],{},Path(td)))
    self.assertEqual(reader.call_args.args[1],2)
 def test_all_pages_reviewed(self):self.run_case(True)
 def test_last_batch_failure_blocks(self):self.run_case(False)
