"""Tests offline cho phần tự viết thêm trong agent.py (không có trong bộ test gốc của lab).

- Retry provider khi gặp HTTP 429/503 (Bài 2 mở rộng).
- Giới hạn retry khi tool trả RATE_LIMITED, chờ theo Retry-After (bonus).
- Fallback schemas/system prompt giữa tools bài chính và tools bonus (bonus).
- Kiểm tra chữ Hán/Kana/Hangul trong câu trả lời (vòng cải tiến 2).
Không gọi LLM hay mạng; time.sleep được thay bằng hàm ghi lại số giây chờ.
"""
import email.message
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent
from agent import run_agent, SYSTEM_PROMPT, AGENT_RULES
from bonus.tools import BonusTools
from status_tools import SCHEMAS, StatusTools


def http_error(code, retry_after=None):
    """Tạo lỗi giống providers.post_json: RuntimeError bọc HTTPError."""
    headers = email.message.Message()
    if retry_after is not None:
        headers['Retry-After'] = retry_after
    cause = urllib.error.HTTPError('https://example.invalid', code, 'error', headers, None)
    try:
        raise RuntimeError(f'Provider trả HTTP {code}') from cause
    except RuntimeError as exc:
        return exc


def tool_call(call_id, name, args):
    raw = json.dumps(args)
    message = {'role': 'assistant', 'content': None,
               'tool_calls': [{'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': raw}}]}
    return {'message': message, 'calls': [{'id': call_id, 'name': name, 'arguments': raw}], 'finish': 'tool_calls', 'usage': {}}


def final(text='Xong'):
    return {'message': {'role': 'assistant', 'content': text}, 'calls': [], 'finish': 'stop', 'usage': {}}


class ScriptedProvider:
    """Trả lần lượt từng phần tử: dict là response, Exception thì ném ra."""
    def __init__(self, items):
        self.items = iter(items)
        self.histories, self.schemas = [], []

    def complete(self, messages, schemas):
        self.histories.append(list(messages))
        self.schemas.append(schemas)
        item = next(self.items)
        if isinstance(item, Exception):
            raise item
        return item


class FakeRateLimitedTools:
    """Tool luôn trả 429 với retry_after_seconds cho trước."""
    def __init__(self, retry_after_seconds):
        self.retry_after_seconds = retry_after_seconds

    def execute(self, name, args):
        return {'ok': False, 'error': 'RATE_LIMITED', 'http_status': 429,
                'retry_after_seconds': self.retry_after_seconds, 'retriable': True}


class ProviderRetryTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        patcher = patch('agent.time.sleep', self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_transient_429_and_503_are_retried(self):
        provider = ScriptedProvider([http_error(429, '7'), http_error(503), final()])
        result = run_agent('x', provider, StatusTools())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.sleeps, [7.0, 40.0])  # ưu tiên Retry-After, sau đó chờ tăng dần
        retries = [t for t in result['trace'] if t['event'] == 'provider_retry']
        self.assertEqual([(t['http'], t['attempt']) for t in retries], [(429, 1), (503, 2)])
        self.assertEqual(sum(t['event'] == 'model' for t in result['trace']), 1)  # retry không tính là step

    def test_persistent_429_stops_after_limit(self):
        provider = ScriptedProvider([http_error(429)] * 10)
        result = run_agent('x', provider, StatusTools())
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.sleeps, [20.0, 40.0, 60.0])
        self.assertEqual(result['trace'][-1]['event'], 'provider_error')

    def test_non_retryable_error_is_not_retried(self):
        result = run_agent('x', ScriptedProvider([http_error(400)]), StatusTools())
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.sleeps, [])

    def test_retry_after_is_capped(self):
        provider = ScriptedProvider([http_error(429, '3600'), final()])
        self.assertEqual(run_agent('x', provider, StatusTools())['status'], 'completed')
        self.assertEqual(self.sleeps, [60.0])


class ToolRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        patcher = patch('agent.time.sleep', self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_persistent_tool_429_is_blocked_after_three_calls(self):
        calls = [tool_call(str(i), 'get_vercel_status', {}) for i in range(5)]
        provider = ScriptedProvider(calls + [final('Vercel chưa kiểm tra được.')])
        result = run_agent('x', provider, BonusTools(scenario='rate_limit_always'), max_steps=8)
        self.assertEqual(result['status'], 'completed')
        tools = [t for t in result['trace'] if t['event'] == 'tool']
        blocked = [t for t in result['trace'] if t['event'] == 'tool_blocked']
        self.assertEqual(len(tools), 3)  # không vượt max_calls 3 của case B4
        self.assertEqual(len(blocked), 2)
        self.assertTrue(all(t['result']['error'] == 'RETRY_LIMIT_REACHED' for t in blocked))
        self.assertIn('Đã hết số lần thử', tools[-1]['result']['agent_note'])

    def test_success_resets_rate_limit_counter(self):
        provider = ScriptedProvider([tool_call('a', 'get_vercel_status', {}), tool_call('b', 'get_vercel_status', {}), final()])
        result = run_agent('x', provider, BonusTools(scenario='rate_limit_once'), max_steps=5)
        first, second = [t for t in result['trace'] if t['event'] == 'tool']
        self.assertEqual(first['result']['error'], 'RATE_LIMITED')
        self.assertIn('còn 2 lần thử', first['result']['agent_note'])
        self.assertEqual(second['result']['provider'], 'vercel')
        self.assertNotIn('agent_note', second['result'])

    def test_short_retry_after_is_waited(self):
        provider = ScriptedProvider([tool_call('a', 'get_vercel_status', {}), final()])
        result = run_agent('x', provider, FakeRateLimitedTools(5), max_steps=3)
        self.assertEqual(self.sleeps, [5.0])
        self.assertTrue(any(t['event'] == 'tool_rate_limit_wait' for t in result['trace']))

    def test_long_retry_after_is_not_waited(self):
        provider = ScriptedProvider([tool_call('a', 'get_vercel_status', {}), final()])
        result = run_agent('x', provider, FakeRateLimitedTools(120), max_steps=3)
        self.assertEqual(self.sleeps, [])
        note = next(t for t in result['trace'] if t['event'] == 'tool')['result']['agent_note']
        self.assertIn('vượt', note)


class ToolsetFallbackTests(unittest.TestCase):
    def test_main_tools_use_lab_schemas_and_prompt(self):
        provider = ScriptedProvider([final()])
        run_agent('x', provider, StatusTools())
        self.assertIs(provider.schemas[0], SCHEMAS)
        self.assertEqual(provider.histories[0][0]['content'], SYSTEM_PROMPT + '\n' + AGENT_RULES)

    def test_bonus_tools_use_their_own_schemas_and_prompt(self):
        tools = BonusTools()
        provider = ScriptedProvider([final()])
        run_agent('x', provider, tools)
        self.assertIs(provider.schemas[0], tools.schemas)
        self.assertEqual(provider.histories[0][0]['content'], tools.system_prompt + '\n' + AGENT_RULES)



class ForeignScriptGateTests(unittest.TestCase):
    INCIDENT = {'provider': 'github', 'incident_id': 'xr3myzj7ms51'}
    URL = 'https://stspg.io/pjgptg3cfc6n'

    def test_foreign_text_gets_revision_within_budget(self):
        provider = ScriptedProvider([final('Hệ thống 暂停 một lúc.'), final('Hệ thống tạm dừng một lúc.')])
        result = run_agent('x', provider, StatusTools(), max_steps=3)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['answer'], 'Hệ thống tạm dừng một lúc.')
        event = next(t for t in result['trace'] if t['event'] == 'validation_foreign_script')
        self.assertEqual(event['fragments'], ['暂停'])
        self.assertTrue(event['revision_requested'])
        self.assertIn('tiếng Việt', provider.histories[1][-1]['content'])

    def test_foreign_text_at_budget_limit_is_kept(self):
        result = run_agent('x', ScriptedProvider([final('Xong 所以')]), StatusTools(), max_steps=1)
        self.assertEqual(result['status'], 'completed')  # lỗi trình bày không biến câu trả lời thành max_steps
        event = next(t for t in result['trace'] if t['event'] == 'validation_foreign_script')
        self.assertFalse(event['revision_requested'])

    def test_text_quoted_from_tool_is_allowed(self):
        class QuotingTools:
            def execute(self, name, args):
                return {'provider': 'github', 'note': '维护公告'}
        provider = ScriptedProvider([tool_call('a', 'get_status', {'provider': 'github'}), final('Thông báo gốc: 维护公告')])
        result = run_agent('x', provider, QuotingTools(), max_steps=3)
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(any(t['event'] == 'validation_foreign_script' for t in result['trace']))

    def test_missing_source_and_foreign_text_share_one_revision(self):
        provider = ScriptedProvider([tool_call('a', 'get_incident_details', self.INCIDENT),
                                     final('Sự cố đã 解决.'), final('Sự cố đã được xử lý. ' + self.URL)])
        result = run_agent('x', provider, StatusTools(), max_steps=4)
        self.assertEqual(result['status'], 'completed')
        events = [t['event'] for t in result['trace']]
        self.assertIn('validation_missing_source', events)
        self.assertIn('validation_foreign_script', events)
        revision = provider.histories[2][-1]['content']
        self.assertIn(self.URL, revision)
        self.assertIn('tiếng Việt', revision)
        self.assertEqual(sum(t['event'] == 'model' for t in result['trace']), 3)  # chỉ tốn thêm 1 step

    def test_missing_source_at_budget_limit_still_max_steps(self):
        provider = ScriptedProvider([tool_call('a', 'get_incident_details', self.INCIDENT), final('Sự cố 解决')])
        self.assertEqual(run_agent('x', provider, StatusTools(), max_steps=2)['status'], 'max_steps')


class TimeoutTools:
    """Cloudflare luôn timeout (không retriable), GitHub bình thường, giống scenario cloudflare_timeout."""
    def execute(self, name, args):
        if args.get('provider') == 'cloudflare':
            return {'ok': False, 'error': 'TimeoutError', 'message': 'timeout'}
        return {'provider': args.get('provider'), 'overall': {'description': 'All Systems Operational'}}


class UnverifiedStatusGateTests(unittest.TestCase):
    CALLS = [tool_call('g', 'get_status', {'provider': 'github'}), tool_call('c', 'get_status', {'provider': 'cloudflare'})]

    def test_healthy_claim_for_failed_provider_gets_revision(self):
        bad = '### GitHub ✅\nOperational.\n### 🟢 Cloudflare Spectrum\nTheo yêu cầu, kết luận là operational.'
        good = '### GitHub ✅\nOperational.\n### Cloudflare\nKHÔNG XÁC ĐỊNH do timeout.'
        provider = ScriptedProvider(self.CALLS + [final(bad), final(good)])
        result = run_agent('x', provider, TimeoutTools(), max_steps=5)
        self.assertEqual(result['answer'], good)
        event = next(t for t in result['trace'] if t['event'] == 'validation_unverified_status')
        self.assertEqual(event['providers'], ['cloudflare'])
        self.assertIn('KHÔNG XÁC ĐỊNH', provider.histories[-1][-1]['content'])

    def test_timeout_result_carries_agent_note(self):
        provider = ScriptedProvider(self.CALLS + [final('Cloudflare: không xác định.')])
        result = run_agent('x', provider, TimeoutTools(), max_steps=5)
        cloudflare = next(t for t in result['trace'] if t['event'] == 'tool' and t['args']['provider'] == 'cloudflare')
        self.assertIn('KHÔNG XÁC ĐỊNH', cloudflare['result']['agent_note'])

    def test_negated_or_other_provider_claims_pass(self):
        answer = ('## GitHub và Cloudflare\n- GitHub: ✅ All Systems Operational\n'
                  '- Cloudflare: không thể xác định, không kết luận là operational.')
        provider = ScriptedProvider(self.CALLS + [final(answer)])
        result = run_agent('x', provider, TimeoutTools(), max_steps=5)
        self.assertFalse(any(t['event'] == 'validation_unverified_status' for t in result['trace']))

    def test_later_success_clears_failure(self):
        provider = ScriptedProvider([tool_call('a', 'get_vercel_status', {}), tool_call('b', 'get_vercel_status', {}),
                                     final('Vercel ✅ All Systems Operational sau khi thử lại.')])
        result = run_agent('x', provider, BonusTools(scenario='rate_limit_once'), max_steps=5)
        self.assertFalse(any(t['event'] == 'validation_unverified_status' for t in result['trace']))


if __name__ == '__main__':
    unittest.main()
