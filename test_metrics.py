import json
import tempfile
import unittest
from pathlib import Path
from metrics import Session, export_csv


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.s = Session('fake.jsonl')

    def event(self, kind, seconds=0, **extra):
        self.s.consume({'type':'event_msg','timestamp':f'2026-11-04T00:00:{seconds:02d}Z','payload':{'type':kind,**extra}})

    def start(self, turn='a', seconds=0, **extra):
        self.event('task_started', seconds, turn_id=turn, **extra)

    def tokens(self, count, seconds=1):
        self.event('token_count', seconds, info={'total_token_usage':{'output_tokens':count,'reasoning_output_tokens':count//2}})

    def finish(self, turn='a', seconds=10, **extra):
        self.event('task_complete',seconds,turn_id=turn,**extra)

    def test_duplicate_usage_and_weighted_average(self):
        self.start(); self.tokens(100); self.tokens(100)
        self.finish(duration_ms=10000,time_to_first_token_ms=1200)
        self.start('b',10); self.tokens(300,15); self.finish('b',30,duration_ms=20000,time_to_first_token_ms=800)
        self.assertEqual([t.output_tokens for t in self.s.turns],[100,200])
        self.assertEqual(self.s.summary()['avg_rate'],10)
        self.assertEqual(self.s.summary()['avg_duration'],15)
        self.assertEqual(self.s.summary()['avg_ttft'],1)

    def test_new_records_preferred_and_reasoning_not_added(self):
        self.start()
        record={'turn_id':'a','thread_token_usage':{'output_tokens':9000},'turn_token_usage':{'output_tokens':160,'reasoning_output_tokens':40}}
        self.s.consume({'type':'token_usage_record','payload':record})
        self.tokens(9999); self.finish()
        self.assertEqual(self.s.turns[0].output_tokens,160)
        self.assertEqual(self.s.turns[0].reasoning_tokens,40)
        self.assertEqual(self.s.turns[0].rate,16)
        self.assertIsNone(self.s.turns[0].input_tokens)

    def test_cache_hit_rate_and_weighted_session_rate(self):
        self.start()
        self.s.consume({'type':'token_usage_record','payload':{
            'turn_id':'a','turn_token_usage':{'output_tokens':20,'input_tokens':100,'cached_input_tokens':40}}})
        self.assertEqual(self.s.summary()['total_usage_tokens'], 120)
        self.finish()
        self.assertAlmostEqual(self.s.turns[0].cache_hit_rate, 0.4)
        self.start('b',10)
        self.s.consume({'type':'token_usage_record','payload':{
            'turn_id':'b','turn_token_usage':{'output_tokens':30,'input_tokens':300,'cached_input_tokens':240}}})
        self.finish('b',20)
        self.assertAlmostEqual(self.s.summary()['cache_hit_rate'], 0.7)
        self.assertEqual(self.s.summary()['cache_input_tokens'], 400)
        self.assertEqual(self.s.summary()['total_usage_tokens'], 450)

    def test_cache_rate_unknown_when_log_lacks_cache_fields(self):
        self.start()
        self.s.consume({'type':'token_usage_record','payload':{
            'turn_id':'a','turn_token_usage':{'output_tokens':20,'input_tokens':100}}})
        self.finish()
        self.assertIsNone(self.s.turns[0].cache_hit_rate)
        self.assertIsNone(self.s.summary()['cache_hit_rate'])

    def test_running_average_rate_updates_with_elapsed_time(self):
        self.start(); self.tokens(11162)
        started_at = self.s.active.start
        self.assertAlmostEqual(self.s.active.rate_at(now=started_at+428), 11162/428)
        self.assertAlmostEqual(self.s.active.rate_at(now=started_at+500), 11162/500)
        self.assertLess(self.s.active.rate_at(now=started_at+500), self.s.active.rate_at(now=started_at+428))
        self.assertIsNone(self.s.summary()['avg_rate'])
        self.assertIsNone(self.s.active.ttft)

    def test_unknown_output_rate_stays_unknown(self):
        self.start()
        self.assertIsNone(self.s.active.rate_at(now=50))
        self.assertIsNone(self.s.summary()['avg_rate'])

    def test_unknown_output_stays_blank_for_live_display(self):
        self.start()
        self.assertIsNone(self.s.active.rate)
        self.assertIsNone(self.s.summary()['avg_rate'])

    def test_interrupted_excluded(self):
        self.start(); self.tokens(50)
        self.event('turn_aborted',5,turn_id='a',duration_ms=5000)
        self.assertEqual(self.s.summary()['completed'],0)
        self.assertIsNone(self.s.summary()['avg_rate'])
        self.assertEqual(self.s.turns[0].status,'interrupted')

    def test_guardian_ignored(self):
        self.start(turn_attribution={'turn_trigger':'guardian_review'})
        self.tokens(100); self.finish()
        self.assertFalse(self.s.turns)

    def test_authoritative_duration(self):
        self.start(); self.tokens(100); self.finish(seconds=20,duration_ms=4000,time_to_first_token_ms=200)
        self.assertEqual(self.s.turns[0].seconds(),4)
        self.assertEqual(self.s.turns[0].rate,25)

    def test_missing_token_stays_unknown(self):
        self.start(); self.finish()
        self.assertIsNone(self.s.turns[0].output_tokens)
        self.assertIsNone(self.s.summary()['avg_rate'])

    def test_partial_utf8_and_incremental_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'session.jsonl'
            payload=json.dumps({'type':'session_meta','payload':{'cwd':'中文路径'}},ensure_ascii=False).encode()
            cut=payload.index('中'.encode())+1
            path.write_bytes(payload[:cut]); s=Session(path)
            s.poll(); self.assertEqual(s.cwd,'')
            with path.open('ab') as f: f.write(payload[cut:]+b'\n')
            s.poll(); self.assertEqual(s.cwd,'中文路径')
            self.assertFalse(s.poll())

    def test_csv_and_no_prompt_persisted(self):
        self.start(); self.tokens(100); self.finish()
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'out.csv'; export_csv(self.s,p)
            text=p.read_text(encoding='utf-8-sig')
            self.assertIn('100',text)
            self.assertNotIn('message',json.dumps(self.s.snapshot()))

    def test_duplicate_start_and_mismatched_end(self):
        self.start(); self.start(); self.finish('wrong')
        self.assertEqual(len(self.s.turns),1)
        self.assertEqual(self.s.active.id,'a')


if __name__=='__main__': unittest.main()
