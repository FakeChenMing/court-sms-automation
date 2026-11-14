import asyncio
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import workflow_worker as w


class AttachmentSyncWorker(unittest.TestCase):
 def test_pending_inspections_are_bounded_throttled_and_never_reapply_calendar(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'ledger.json'
   active={str(i):{'uid':str(i),'verified':True,'sync_pending':True} for i in range(12)}
   excluded={
    'recent':{'uid':'recent','verified':True,'sync_pending':True,'attachment_sync_checked_at':990},
    'merged':{'uid':'merged','verified':True,'sync_pending':True,'merged_into':'0'},
    'removed':{'uid':'removed','verified':True,'sync_pending':True,'calendar_removed_by_receipt_merge':True},
    'unverified':{'uid':'unverified','verified':False,'sync_pending':True},
    'uploaded':{'uid':'uploaded','verified':True,'sync_pending':False}}
   data={'documents':{},'events':{**active,**excluded}}
   path.write_text(json.dumps(data))
   @contextmanager
   def transaction():
    state=json.loads(path.read_text());yield state;path.write_text(json.dumps(state))
   def inspect(key):
    if key=='0':raise RuntimeError('source metadata changed')
    with transaction() as state:state['events'][key].update(sync_pending=False,attachment_sync_state='uploaded')
   with patch.object(w.ledger,'LEDGER',path),patch.object(w.ledger,'transaction',transaction),patch.object(w.ledger,'refresh_attachment_sync',side_effect=inspect) as refresh,patch.object(w.ledger,'apply_calendar') as apply,patch.object(w.ledger,'apply_receipt_group_calendar') as group_apply,patch.object(w.time,'time',return_value=1000):
    result=asyncio.run(w.refresh_pending_attachment_sync(data))
    self.assertEqual(refresh.call_count,10)
    self.assertTrue(result['events']['0']['sync_pending'])
    self.assertEqual(result['events']['0']['attachment_sync_last_error']['type'],'RuntimeError')
    refresh.reset_mock()
    asyncio.run(w.refresh_pending_attachment_sync(result))
    self.assertEqual([c.args[0] for c in refresh.call_args_list],['10','11'])
    apply.assert_not_called();group_apply.assert_not_called()
    final=json.loads(path.read_text())
    for key,original in excluded.items():self.assertEqual(final['events'][key],original)

 def test_model_cooldown_does_not_skip_attachment_inspection(self):
  with tempfile.TemporaryDirectory() as tmp:
   state=Path(tmp);data={'documents':{},'events':{}}
   ledger_path=state/'ledger.json';ledger_path.write_text(json.dumps(data))
   (state/'model_service.json').write_text(json.dumps({'available':False,'service':w.MODEL_SERVICE,'retry_at':'9999-01-01T00:00:00+08:00'}))
   refreshed=copy.deepcopy(data);refreshed['sync_inspected']=True
   with patch.object(w,'STATE',state),patch.object(w,'LOG',state/'log'),patch.object(w.ledger,'LEDGER',ledger_path),patch.object(w,'refresh_pending_attachment_sync',new=AsyncMock(return_value=refreshed)) as refresh,patch.object(w,'recover_receipt_groups',new=AsyncMock(return_value=refreshed)),patch.object(w,'write_health') as health,patch.object(w,'model') as model:
    asyncio.run(w.run())
    refresh.assert_awaited_once_with(data,False)
    self.assertEqual(health.call_args.args[0],refreshed)
    model.assert_not_called()


if __name__=='__main__':unittest.main()
