import sqlite3
import unittest

from workflowy_importer import chatgpt_live, chatgpt_records, chatgpt_storage


class ChatGPTDataCapsulesTests(unittest.TestCase):
    def test_legacy_live_exports_share_canonical_helpers(self):
        for item in ('ChatRecord','record_from_payload','_timestamp','_conversation_url'):
            self.assertIs(getattr(chatgpt_live,item),getattr(chatgpt_records,item))
        for item in ('CHATGPT_SCHEMA','ensure_chatgpt_schema','upsert_records','classify_statuses'):
            self.assertIs(getattr(chatgpt_live,item),getattr(chatgpt_storage,item))

    def test_no_browser_required_for_sqlite_statuses(self):
        db=sqlite3.connect(':memory:')
        try:
            db.row_factory=sqlite3.Row
            chatgpt_storage.ensure_chatgpt_schema(db)
            chatgpt_storage.ensure_chatgpt_schema(db)
            rec=chatgpt_records.record_from_payload(
                {'id':'fake123','title':'Synthetic','create_time':1000,'update_time':2000},
                source='synthetic',inventory={},
            )
            self.assertEqual(rec.url,'https://chatgpt.com/c/fake123')
            chatgpt_storage.upsert_records(db,[rec],now=2000)
            counts=chatgpt_storage.classify_statuses(db,now=2001)
            self.assertEqual(sum(counts.values()),1)
        finally:
            db.close()


if __name__=='__main__':
    unittest.main()
