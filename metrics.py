"""Read-only Codex rollout metrics. No prompts, credentials, or message text stored."""
from __future__ import annotations
import csv
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError, AttributeError):
        return None


@dataclass
class Turn:
    id: str
    start: float
    status: str = 'running'
    end: float | None = None
    duration: float | None = None
    ttft: float | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    model: str = ''

    @property
    def cache_hit_rate(self):
        if self.input_tokens is None or self.cached_input_tokens is None or self.input_tokens <= 0:
            return None
        return min(1.0, max(0.0, self.cached_input_tokens / self.input_tokens))

    def seconds(self, now=None):
        if self.duration is not None:
            return self.duration
        return max(0., (self.end or now or datetime.now(timezone.utc).timestamp()) - self.start)

    def rate_at(self, now=None):
        seconds = self.seconds(now=now)
        if self.output_tokens is None or seconds <= 0:
            return None
        return self.output_tokens / seconds

    @property
    def rate(self):
        return self.rate_at()


class Session:
    def __init__(self, path):
        self.path = Path(path)
        self.id = self.path.stem[-36:]
        self.cwd = ''
        self.turns: list[Turn] = []
        self.active: Turn | None = None
        self.last_output = None
        self.last_reasoning = None
        self.baseline_output = 0
        self.baseline_reasoning = 0
        self.has_turn_usage = False
        self.offset = 0
        self.pending = b''
        self.signature = None
        self.warning = ''
        self.model = ''
        self.last_activity = 0.

    def poll(self):
        try:
            stat = self.path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            if signature == self.signature:
                return False
            if stat.st_size < self.offset or (stat.st_size == self.offset and self.signature is not None):
                self.__init__(self.path)
            with self.path.open('rb') as stream:
                stream.seek(self.offset)
                chunk = stream.read()
                self.offset = stream.tell()
            lines = (self.pending + chunk).split(b'\n')
            self.pending = lines.pop()
            for line in lines:
                if not line.strip():
                    continue
                try:
                    self.consume(json.loads(line))
                except (ValueError, TypeError, KeyError, AttributeError):
                    self.warning = '日志包含无法解析的记录，已跳过'
            self.signature = signature
            return True
        except OSError as exc:
            self.warning = '日志暂不可读：' + str(exc)
            return False

    def consume(self, row):
        p = row.get('payload') or {}
        kind = row.get('type')
        t = timestamp(row.get('timestamp'))
        if kind == 'session_meta':
            self.id = p.get('id') or self.id
            self.cwd = p.get('cwd') or ''
            return
        if kind == 'turn_context':
            self.model = p.get('model') or self.model
            if self.active:
                self.active.model = self.model
            return
        if kind == 'event_msg' and p.get('type') in ('task_started', 'turn_started'):
            attribution = p.get('turn_attribution') or {}
            trigger = attribution.get('turn_trigger')
            # Exclude guardian reviews, background workers, and other non-user turns.
            if trigger and trigger not in ('composer', 'user', 'user_message'):
                return
            identity = p.get('turn_id') or str(t)
            if any(v.id == identity for v in self.turns):
                return
            if self.active:
                self.active.status = 'incomplete'
                self.active.end = t
            self.active = Turn(identity, t if t is not None else float(p.get('started_at', 0)), model=self.model)
            self.turns.append(self.active)
            self.baseline_output = self.last_output
            self.baseline_reasoning = self.last_reasoning
            self.has_turn_usage = False
            self.last_activity = self.active.start
            return
        if kind == 'token_usage_record':
            total = p.get('thread_token_usage') or {}
            self.last_output = total.get('output_tokens', self.last_output)
            self.last_reasoning = total.get('reasoning_output_tokens', self.last_reasoning)
            if self.active and p.get('turn_id') == self.active.id:
                usage = p.get('turn_token_usage') or {}
                if 'output_tokens' in usage:
                    self.active.output_tokens = max(0, int(usage['output_tokens']))
                    self.active.reasoning_tokens = max(0, int(usage.get('reasoning_output_tokens', 0)))
                    if 'input_tokens' in usage and 'cached_input_tokens' in usage:
                        self.active.input_tokens = max(0, int(usage['input_tokens']))
                        self.active.cached_input_tokens = max(0, int(usage['cached_input_tokens']))
                    self.has_turn_usage = True
            return
        if kind != 'event_msg':
            return
        event = p.get('type')
        if event == 'token_count':
            info = p.get('info') or {}
            usage = info.get('total_token_usage') or {}
            out = usage.get('output_tokens')
            reason = usage.get('reasoning_output_tokens')
            if out is not None:
                if self.active and not self.has_turn_usage:
                    # Legacy cumulative counters: first session starts from zero.
                    base = self.baseline_output or 0
                    self.active.output_tokens = max(0, int(out) - base)
                    self.active.reasoning_tokens = max(0, int(reason or 0) - (self.baseline_reasoning or 0))
                self.last_output = int(out)
                self.last_reasoning = int(reason or 0)
        elif event in ('task_complete', 'turn_completed', 'turn_aborted') and self.active:
            identity = p.get('turn_id')
            if identity and identity != self.active.id:
                return
            turn = self.active
            turn.end = t if t is not None else float(p.get('completed_at', turn.start))
            duration_ms = p.get('duration_ms')
            turn.duration = max(0., float(duration_ms) / 1000.) if duration_ms is not None else max(0., turn.end-turn.start)
            first = p.get('time_to_first_token_ms')
            turn.ttft = max(0., float(first)/1000.) if first is not None else None
            turn.status = 'interrupted' if event == 'turn_aborted' else 'complete'
            self.last_activity = turn.end
            self.active = None

    def summary(self):
        complete = [v for v in self.turns if v.status == 'complete']
        first = [v.ttft for v in complete if v.ttft is not None]
        measured = [v for v in complete if v.output_tokens is not None and v.seconds() > 0]
        total_seconds = sum(v.seconds() for v in measured)
        cache_measured = [v for v in complete if v.input_tokens is not None and
                          v.cached_input_tokens is not None and v.input_tokens > 0]
        total_input = sum(v.input_tokens for v in cache_measured)
        return {
            'completed': len(complete),
            'cache_hit_rate': (sum(v.cached_input_tokens for v in cache_measured) / total_input
                               if total_input else None),
            'cache_input_tokens': total_input,
            'cached_input_tokens': sum(v.cached_input_tokens for v in cache_measured),
            'avg_duration': sum(v.seconds() for v in complete) / len(complete) if complete else None,
            'avg_ttft': sum(first) / len(first) if first else None,
            'avg_rate': sum(v.output_tokens for v in measured) / total_seconds if total_seconds else None,
            'tokens': sum(v.output_tokens for v in measured),
        }

    def snapshot(self):
        return {'id': self.id, 'source': str(self.path), 'turns': [asdict(t) for t in self.turns], 'summary': self.summary()}


def default_home():
    configured = os.environ.get('CODEX_HOME')
    if configured:
        return Path(configured)
    default = Path.home() / '.codex'
    if default.exists():
        return default
    local = Path('D:/CodexHome')
    return local if local.exists() else default


def discover(home, limit=80):
    root = Path(home) / 'sessions'
    if not root.exists():
        return []
    files = []
    # Bounded discovery: newest date folders first; avoid walking every old rollout.
    for year in sorted(root.iterdir(), reverse=True):
        if not year.is_dir():
            continue
        for month in sorted(year.iterdir(), reverse=True):
            if not month.is_dir():
                continue
            for day in sorted(month.iterdir(), reverse=True):
                if day.is_dir():
                    files.extend(day.glob('*.jsonl'))
                    if len(files) >= limit:
                        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def export_csv(session, path):
    with Path(path).open('w', encoding='utf-8-sig', newline='') as stream:
        out = csv.writer(stream)
        out.writerow(['会话ID', '请求ID', '开始时间', '状态', '首token秒', '整轮耗时秒', '输出token', '推理token_输出子集', '整轮平均tok每秒', '输入token', '缓存输入token', '缓存命中率', '模型'])
        for t in session.turns:
            out.writerow([session.id, t.id, datetime.fromtimestamp(t.start).isoformat(timespec='seconds'), t.status,
                          t.ttft, round(t.seconds(), 3), t.output_tokens, t.reasoning_tokens,
                          round(t.rate, 3) if t.rate is not None else '', t.input_tokens,
                          t.cached_input_tokens, round(t.cache_hit_rate * 100, 2) if t.cache_hit_rate is not None else '', t.model])
