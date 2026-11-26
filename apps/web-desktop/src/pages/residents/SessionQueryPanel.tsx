import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, StatusChip } from '@server-guardian/ui';
import type { Tone } from '@server-guardian/ui';
import {
  cancelResidentQuery,
  fetchResidentQuery,
  resetResidentSession,
  submitResidentQuery,
} from '../../api';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { useProjectStore } from '../../state/projectStore';
import { fmtDateTime } from '../../lib/format';
import type {
  ResidentAgent,
  ResidentQuery,
  ResidentQueryDetail,
  ResidentScopedSession,
} from '../../types';
import { tr } from '../../i18n';

const TERMINAL = new Set(['completed', 'failed', 'expired', 'cancelled']);
const POLL_MS = 2_000;
const TICK_MS = 1_000;
const MAX_CHAT_TURNS = 10;
const LABEL_RE = /^[a-z0-9][a-z0-9-]{0,23}$/;

function statusTone(status: string): Tone {
  if (status === 'completed') return 'ok';
  if (status === 'failed' || status === 'expired') return 'crit';
  if (status === 'running' || status === 'queued' || status === 'dispatched') return 'warn';
  return 'idle';
}

function elapsedSec(created: string, completed: string | null, nowMs: number): number | null {
  const start = Date.parse(created);
  if (!Number.isFinite(start)) return null;
  const end = completed ? Date.parse(completed) : nowMs;
  if (!Number.isFinite(end)) return null;
  return Math.max(0, Math.floor((end - start) / 1000));
}

/**
 * Hub-project-scoped multi-session chat console (`docs/design/resident-session-query.md` Q3+,
 * `docs/design/resident-multi-session.md` MS3).
 * Defaults to `project:<selected>` per MD1, supports named sub-sessions (`project:<id>:<label>`),
 * explicit `main`, scoped session reset, and `claude`-only read-only `mode: "fork"`.
 */
export function SessionQueryPanel({
  agent,
  queries,
  sessions = [],
  onSubmitted,
}: {
  agent: ResidentAgent;
  queries: ResidentQuery[];
  sessions?: ResidentScopedSession[];
  onSubmitted?: () => void;
}) {
  const { selected, projects } = useProjectStore();
  const project = projects.data?.projects.find((p) => p.id === selected) ?? null;
  const projectKind = project?.data.kind ?? 'code';
  const defaultKey = selected ? `project:${selected}` : 'main';

  const [sessionKey, setSessionKey] = useState(defaultKey);
  const [customKeys, setCustomKeys] = useState<string[]>([]);
  const [newLabel, setNewLabel] = useState('');
  const [mode, setMode] = useState<'in_place' | 'fork'>('in_place');
  const [prompt, setPrompt] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<Record<string, ResidentQueryDetail>>({});
  const [localIds, setLocalIds] = useState<string[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [showJumpBottom, setShowJumpBottom] = useState(false);

  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const stickToBottomRef = useRef(true);
  const loadedRef = useRef(loaded);
  loadedRef.current = loaded;

  // Reset active sessionKey whenever the selected hub project or agent changes.
  useEffect(() => {
    setSessionKey(selected ? `project:${selected}` : 'main');
    setMode('in_place');
    setError(null);
  }, [selected, agent.address]);

  const sessionReady = agent.state === 'ready' && Boolean(agent.session_ready);

  // Active sessions for this agent scoped to the selected project (plus `main`).
  const agentSessions = useMemo(() => {
    const out: Record<string, ResidentScopedSession> = {};
    for (const s of sessions) {
      if (s.address !== agent.address || s.status !== 'active') continue;
      if (s.session_key === 'main' || !selected || s.project === selected) {
        out[s.session_key] = s;
      }
    }
    return out;
  }, [sessions, agent.address, selected]);

  const sessionOptions = useMemo(() => {
    const keys = new Set<string>();
    if (selected) keys.add(`project:${selected}`);
    for (const key of Object.keys(agentSessions)) {
      if (key !== 'main') keys.add(key);
    }
    for (const key of customKeys) {
      if (selected && key.startsWith(`project:${selected}:`)) keys.add(key);
    }
    for (const q of queries) {
      if (q.address !== agent.address) continue;
      if (selected && q.project !== selected) continue;
      if (q.session_key && q.session_key !== 'main') keys.add(q.session_key);
    }
    const ordered = Array.from(keys).sort();
    ordered.push('main');
    return ordered;
  }, [selected, agentSessions, customKeys, queries, agent.address]);

  const activeSessionMeta = agentSessions[sessionKey] ?? null;
  const canFork =
    agent.provider === 'claude' &&
    (sessionKey === 'main' ? sessionReady : Boolean(activeSessionMeta?.session_ready));

  useEffect(() => {
    if (!canFork && mode === 'fork') setMode('in_place');
  }, [canFork, mode]);

  const canSubmit = Boolean(selected) && sessionReady && !submitting && prompt.trim().length > 0;
  const canReset =
    sessionKey !== 'main' &&
    Boolean(activeSessionMeta) &&
    !resetting;

  // Recent queries for this (agent, project, sessionKey), ordered oldest -> newest for chat display.
  const chatItems = useMemo(() => {
    const byId = new Map<string, ResidentQuery>();
    for (const q of queries) {
      if (q.address !== agent.address) continue;
      if (selected && q.project !== selected) continue;
      const qKey = q.session_key || 'main';
      if (qKey !== sessionKey) continue;
      byId.set(q.id, q);
    }
    for (const id of localIds) {
      const detail = loaded[id];
      if (!detail || detail.address !== agent.address) continue;
      if (selected && detail.project !== selected) continue;
      const qKey = detail.session_key || 'main';
      if (qKey !== sessionKey) continue;
      if (!byId.has(id)) byId.set(id, detail);
    }
    const list = Array.from(byId.values()).sort((a, b) => a.created.localeCompare(b.created));
    return list.slice(-MAX_CHAT_TURNS);
  }, [queries, localIds, loaded, agent.address, selected, sessionKey]);

  const pendingIds = useMemo(
    () =>
      chatItems
        .filter((item) => {
          const status = loaded[item.id]?.status ?? item.status;
          return !TERMINAL.has(status);
        })
        .map((item) => item.id),
    [chatItems, loaded],
  );
  const hasPending = pendingIds.length > 0;

  // Auto-load prompt & answer details for visible chat turns when missing or when snapshot status advances.
  const fetchSignature = chatItems.map((q) => `${q.id}:${q.status}`).join(',');
  useEffect(() => {
    if (chatItems.length === 0) return;
    const missing = chatItems.filter((item) => {
      const cached = loadedRef.current[item.id];
      return !cached || (cached.status !== item.status && TERMINAL.has(item.status));
    });
    if (missing.length === 0) return;
    let cancelled = false;
    setLoadingHistory(true);
    Promise.all(
      missing.map(async (item) => {
        try {
          return await fetchResidentQuery(item.id);
        } catch {
          return null;
        }
      }),
    ).then((results) => {
      if (cancelled) return;
      setLoaded((prev) => {
        const next = { ...prev };
        for (const detail of results) {
          if (detail) next[detail.id] = detail;
        }
        return next;
      });
      setLoadingHistory(false);
    });
    return () => {
      cancelled = true;
    };
  }, [fetchSignature, agent.address, selected, sessionKey]);

  // Poll in-flight queries every 2 s AND immediately when returning from a background tab (MD5).
  const pendingKey = pendingIds.join(',');
  useEffect(() => {
    if (!pendingKey) return;
    const ids = pendingKey.split(',');
    let cancelled = false;
    const poll = async () => {
      if (typeof document !== 'undefined' && document.hidden) return;
      const results = await Promise.all(
        ids.map(async (id) => {
          try {
            return await fetchResidentQuery(id);
          } catch {
            return null;
          }
        }),
      );
      if (cancelled) return;
      let finishedAny = false;
      setLoaded((prev) => {
        const next = { ...prev };
        for (const detail of results) {
          if (!detail) continue;
          const prevStatus = prev[detail.id]?.status;
          if (prevStatus && !TERMINAL.has(prevStatus) && TERMINAL.has(detail.status)) {
            finishedAny = true;
          }
          next[detail.id] = detail;
        }
        return next;
      });
      if (finishedAny && onSubmitted) onSubmitted();
    };
    const onVis = () => {
      if (typeof document !== 'undefined' && !document.hidden) {
        void poll();
      }
    };
    const timer = window.setInterval(poll, POLL_MS);
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVis);
    }
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVis);
      }
    };
  }, [pendingKey, onSubmitted]);

  // Live 1 s elapsed clock only while at least one query is in flight.
  useEffect(() => {
    if (!hasPending) return;
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, [hasPending]);

  // Smart auto-scroll (MD5): only scroll when already near bottom; otherwise offer Jump button.
  const lastId = chatItems[chatItems.length - 1]?.id ?? '';
  const lastStatus = lastId ? (loaded[lastId]?.status ?? chatItems[chatItems.length - 1]?.status ?? '') : '';
  const lastHasAnswer = Boolean(lastId && loaded[lastId]?.answer !== null);
  useEffect(() => {
    const el = transcriptRef.current;
    if (!el) return;
    if (stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
      setShowJumpBottom(false);
    } else if (chatItems.length > 0) {
      setShowJumpBottom(true);
    }
  }, [chatItems.length, lastId, lastStatus, lastHasAnswer]);

  const handleScroll = () => {
    const el = transcriptRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= 48;
    stickToBottomRef.current = nearBottom;
    if (nearBottom) setShowJumpBottom(false);
  };

  const jumpToBottom = () => {
    const el = transcriptRef.current;
    if (!el) return;
    stickToBottomRef.current = true;
    el.scrollTop = el.scrollHeight;
    setShowJumpBottom(false);
  };

  const handleAddSession = () => {
    const clean = newLabel.trim().toLowerCase();
    if (!selected || !LABEL_RE.test(clean)) return;
    const key = `project:${selected}:${clean}`;
    setCustomKeys((prev) => (prev.includes(key) ? prev : [...prev, key]));
    setSessionKey(key);
    setNewLabel('');
    setError(null);
  };

  const handleResetSession = async () => {
    if (!canReset) return;
    setResetting(true);
    setError(null);
    try {
      await resetResidentSession({ address: agent.address, session_key: sessionKey });
      if (onSubmitted) onSubmitted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setResetting(false);
    }
  };

  const handleSubmit = async () => {
    if (!canSubmit || !selected) return;
    setSubmitting(true);
    setError(null);
    stickToBottomRef.current = true;
    try {
      const created = await submitResidentQuery({
        address: agent.address,
        project: selected,
        prompt: prompt.trim(),
        session_key: sessionKey,
        mode,
      });
      setPrompt('');
      const detail = await fetchResidentQuery(created.id);
      setLoaded((prev) => ({ ...prev, [detail.id]: detail }));
      setLocalIds((prev) => (prev.includes(detail.id) ? prev : [...prev, detail.id]));
      if (onSubmitted) onSubmitted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = async (id: string) => {
    setError(null);
    try {
      await cancelResidentQuery(id);
    } catch (err) {
      // TOCTOU (MD5): if the query transitioned to `running` before cancel landed, sync fresh state.
      const msg = err instanceof Error ? err.message : String(err);
      if (!msg.includes('already_running') && !msg.includes('already_finished')) {
        setError(msg);
      }
    }
    try {
      const detail = await fetchResidentQuery(id);
      setLoaded((prev) => ({ ...prev, [detail.id]: detail }));
      if (onSubmitted) onSubmitted();
    } catch {
      // Ignore secondary refresh error.
    }
  };

  return (
    <Stack gap="var(--sg-s-3)">
      <Row style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 'var(--sg-s-2)' }}>
        <Row gap="var(--sg-s-2)" style={{ flexWrap: 'wrap' }}>
          {selected ? (
            <StatusChip tone="idle">{tr.residents.queryProject(selected, projectKind)}</StatusChip>
          ) : (
            <StatusChip tone="warn">{tr.residents.queryNoProject}</StatusChip>
          )}
          <StatusChip tone={sessionReady ? 'ok' : 'idle'}>
            {sessionReady ? tr.residents.querySessionReady : tr.residents.querySessionNotReady}
          </StatusChip>
          {activeSessionMeta ? (
            <StatusChip tone={activeSessionMeta.session_ready ? 'ok' : 'idle'}>
              {tr.residents.querySessionTurns(activeSessionMeta.turns)}
            </StatusChip>
          ) : null}
        </Row>
        {loadingHistory ? (
          <span style={{ ...muted, fontSize: 12 }}>{tr.residents.queryLoadingHistory}</span>
        ) : null}
      </Row>

      {/* Multi-session selector bar (MS3) */}
      <Row style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 'var(--sg-s-2)' }}>
        <Row gap="var(--sg-s-2)" style={{ flexWrap: 'wrap', alignItems: 'center' }}>
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
            <span style={muted}>{tr.residents.querySessionLabel}</span>
            <select
              value={sessionKey}
              onChange={(e) => setSessionKey(e.target.value)}
              disabled={!selected}
              style={{
                padding: '4px 8px',
                borderRadius: 'var(--sg-radius-sm, 6px)',
                border: '1px solid var(--sg-border-subtle)',
                background: 'var(--sg-bg-surface)',
                color: 'inherit',
                fontSize: 12,
                fontFamily: 'var(--sg-font-mono)',
              }}
            >
              {sessionOptions.map((key) => (
                <option key={key} value={key}>
                  {key === 'main'
                    ? tr.residents.querySessionMain
                    : selected && key === `project:${selected}`
                      ? tr.residents.querySessionDefault(selected)
                      : key}
                </option>
              ))}
            </select>
          </label>
          {selected ? (
            <Row gap="var(--sg-s-1)" style={{ flexWrap: 'nowrap' }}>
              <input
                type="text"
                value={newLabel}
                onChange={(e) => setNewLabel(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    handleAddSession();
                  }
                }}
                placeholder={tr.residents.queryNewLabelPlaceholder}
                style={{
                  width: 130,
                  padding: '4px 8px',
                  borderRadius: 'var(--sg-radius-sm, 6px)',
                  border: '1px solid var(--sg-border-subtle)',
                  background: 'var(--sg-bg-surface)',
                  color: 'inherit',
                  fontSize: 12,
                  fontFamily: 'var(--sg-font-mono)',
                }}
              />
              <Button
                size="sm"
                variant="outline"
                disabled={!LABEL_RE.test(newLabel.trim().toLowerCase())}
                onClick={handleAddSession}
              >
                {tr.residents.queryAddSession}
              </Button>
            </Row>
          ) : null}
        </Row>
        <Row gap="var(--sg-s-2)" style={{ flexWrap: 'wrap' }}>
          {canFork ? (
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value === 'fork' ? 'fork' : 'in_place')}
              style={{
                padding: '4px 8px',
                borderRadius: 'var(--sg-radius-sm, 6px)',
                border: '1px solid var(--sg-border-subtle)',
                background: 'var(--sg-bg-surface)',
                color: 'inherit',
                fontSize: 12,
              }}
            >
              <option value="in_place">{tr.residents.queryModeInPlace}</option>
              <option value="fork">{tr.residents.queryModeFork}</option>
            </select>
          ) : null}
          {canReset ? (
            <Button size="sm" variant="outline" loading={resetting} onClick={handleResetSession}>
              {resetting ? tr.residents.queryResetting : tr.residents.queryResetSession}
            </Button>
          ) : null}
        </Row>
      </Row>

      {!sessionReady ? <Notice tone="info">{tr.residents.queryNotReady}</Notice> : null}
      {error ? <Notice tone="crit">{error}</Notice> : null}

      <div
        ref={transcriptRef}
        onScroll={handleScroll}
        style={{
          minHeight: 160,
          maxHeight: 460,
          overflowY: 'auto',
          padding: '12px',
          borderRadius: 'var(--sg-radius-sm, 6px)',
          border: '1px solid var(--sg-border-subtle)',
          background: 'var(--sg-bg-subtle, rgba(128,128,128,0.04))',
          display: 'grid',
          gap: 'var(--sg-s-3)',
          alignContent: chatItems.length === 0 ? 'center' : 'start',
        }}
      >
        {chatItems.length === 0 ? (
          <div style={{ ...muted, fontSize: 13, textAlign: 'center', padding: '16px 8px' }}>
            {tr.residents.queryChatEmpty}
          </div>
        ) : (
          chatItems.map((item) => {
            const detail = loaded[item.id];
            const status = detail?.status ?? item.status;
            const isActive = !TERMINAL.has(status);
            const secs = elapsedSec(item.created, detail?.completed ?? item.completed, nowMs);
            const callerLabel =
              item.caller === 'operator' ? tr.residents.queryCallerOperator : item.caller;
            const stageText = tr.residents.queryStageDetail[status];

            return (
              <div key={item.id} style={{ display: 'grid', gap: 'var(--sg-s-2)' }}>
                {/* Caller prompt bubble (right aligned) */}
                <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <div
                    style={{
                      maxWidth: '90%',
                      minWidth: 0,
                      padding: '8px 12px',
                      borderRadius: '10px 10px 2px 10px',
                      border: '1px solid var(--sg-border-subtle)',
                      background: 'var(--sg-bg-surface)',
                      display: 'grid',
                      gap: 4,
                    }}
                  >
                    <Row style={{ justifyContent: 'space-between', gap: 'var(--sg-s-2)', flexWrap: 'wrap' }}>
                      <Row gap="var(--sg-s-1)">
                        <strong style={{ fontSize: 12 }}>{callerLabel}</strong>
                        {item.mode === 'fork' ? (
                          <StatusChip tone="idle">{tr.residents.queryModeFork}</StatusChip>
                        ) : null}
                      </Row>
                      <span style={{ ...muted, fontSize: 11 }}>{fmtDateTime(item.created)}</span>
                    </Row>
                    {item.task ? (
                      <span style={{ ...mono, ...muted, fontSize: 11 }}>task: {item.task}</span>
                    ) : null}
                    <div
                      style={{
                        fontSize: 13,
                        whiteSpace: 'pre-wrap',
                        overflowWrap: 'anywhere',
                      }}
                    >
                      {detail?.prompt ?? '…'}
                    </div>
                  </div>
                </div>

                {/* Resident answer bubble (left aligned) */}
                <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
                  <div
                    style={{
                      maxWidth: '92%',
                      minWidth: 0,
                      padding: '8px 12px',
                      borderRadius: '10px 10px 10px 2px',
                      border: '1px solid var(--sg-border-subtle)',
                      background: 'var(--sg-bg-surface)',
                      display: 'grid',
                      gap: 6,
                    }}
                  >
                    <Row style={{ justifyContent: 'space-between', gap: 'var(--sg-s-2)', flexWrap: 'wrap' }}>
                      <Row gap="var(--sg-s-2)" style={{ flexWrap: 'wrap' }}>
                        <strong style={{ fontSize: 12 }}>{agent.name}</strong>
                        <StatusChip tone={statusTone(status)}>
                          {tr.residents.queryStatus[status] ?? status}
                        </StatusChip>
                        {secs !== null ? (
                          <span style={{ ...mono, ...muted, fontSize: 11 }}>
                            {isActive
                              ? tr.residents.queryElapsedOf(secs, item.timeout_sec)
                              : tr.residents.queryElapsed(secs)}
                          </span>
                        ) : null}
                      </Row>
                      <span style={{ ...mono, ...muted, fontSize: 11 }}>
                        {item.id} · {tr.residents.epoch(item.epoch)}
                        {detail?.turn_id ? ` · ${detail.turn_id}` : ''}
                      </span>
                    </Row>

                    {isActive ? (
                      <Row style={{ justifyContent: 'space-between', gap: 'var(--sg-s-2)', flexWrap: 'wrap' }}>
                        <span style={{ ...muted, fontSize: 12 }}>
                          {stageText ?? tr.residents.queryPolling}
                        </span>
                        {status === 'queued' || status === 'dispatched' ? (
                          <Button size="sm" variant="outline" onClick={() => handleCancel(item.id)}>
                            {tr.residents.queryCancel}
                          </Button>
                        ) : null}
                      </Row>
                    ) : null}

                    {detail?.answer_truncated ? (
                      <Notice tone="warn">{tr.residents.queryTruncated}</Notice>
                    ) : null}
                    {detail?.error ? <Notice tone="crit">{detail.error}</Notice> : null}
                    {detail && detail.answer !== null ? (
                      <pre
                        style={{
                          margin: 0,
                          whiteSpace: 'pre-wrap',
                          overflowWrap: 'anywhere',
                          fontSize: 13,
                          lineHeight: 1.5,
                          fontFamily: 'var(--sg-font-mono)',
                        }}
                      >
                        {detail.answer}
                      </pre>
                    ) : null}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      {showJumpBottom ? (
        <Row style={{ justifyContent: 'flex-end' }}>
          <Button size="sm" variant="outline" onClick={jumpToBottom}>
            {tr.residents.queryScrollBottom}
          </Button>
        </Row>
      ) : null}

      <textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
            e.preventDefault();
            void handleSubmit();
          }
        }}
        placeholder={tr.residents.queryPlaceholder}
        disabled={!sessionReady || !selected || submitting}
        rows={3}
        style={{
          width: '100%',
          boxSizing: 'border-box',
          padding: '8px 10px',
          borderRadius: 'var(--sg-radius-sm, 6px)',
          border: '1px solid var(--sg-border-subtle)',
          background: 'var(--sg-bg-surface)',
          color: 'inherit',
          fontFamily: 'inherit',
          fontSize: 13,
          resize: 'vertical',
        }}
      />

      <Row style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 'var(--sg-s-2)' }}>
        <Row gap="var(--sg-s-2)" style={{ flexWrap: 'wrap' }}>
          <Button size="sm" disabled={!canSubmit} loading={submitting} onClick={handleSubmit}>
            {submitting ? tr.residents.querySending : tr.residents.querySubmit}
          </Button>
          <span style={{ ...muted, fontSize: 12 }}>{tr.residents.queryShortcut}</span>
        </Row>
        {hasPending ? (
          <span style={{ ...muted, fontSize: 12 }}>{tr.residents.queryPolling}</span>
        ) : null}
      </Row>
    </Stack>
  );
}
