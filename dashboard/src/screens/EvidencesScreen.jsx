import { useEffect, useMemo, useState } from 'react';
import { Database, KeyRound, Loader2, Plus, RadioTower, ReceiptText } from 'lucide-react';
import { api } from '../lib/api';
import { useAgent } from '../context/AgentContext';
import Button from '../components/Button';
import Modal from '../components/Modal';
import TextField, { TextArea } from '../components/TextField';
import { timeAgo } from '../lib/timeAgo';
import { analysisLabel, answeredBy } from '../lib/modelChoice';

const SOURCE_TYPES = ['transcript', 'bank', 'browser', 'social', 'location', 'manual', 'device', 'other'];

function Section({ title, note, children }) {
  return (
    <section className="mb-8">
      <h2 className="mb-1 text-sm font-medium text-text">{title}</h2>
      {note && <p className="mb-3 text-xs text-muted">{note}</p>}
      <div className="overflow-hidden rounded-xl border border-border bg-surface">{children}</div>
    </section>
  );
}

function prettyJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return '{}';
  }
}

function SourceModal({ initial, onClose, onSaved }) {
  const [sourceKey, setSourceKey] = useState(initial?.source_key || '');
  const [sourceType, setSourceType] = useState(initial?.source_type || 'manual');
  const [label, setLabel] = useState(initial?.label || '');
  const [description, setDescription] = useState(initial?.description || '');
  const [enabled, setEnabled] = useState(initial?.enabled ?? true);
  const [configText, setConfigText] = useState(prettyJson(initial?.config || {}));
  const [secretsText, setSecretsText] = useState('{}');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  async function save(e) {
    e.preventDefault();
    setSaving(true);
    setError('');
    try {
      const config = configText.trim() ? JSON.parse(configText) : {};
      const secrets = secretsText.trim() ? JSON.parse(secretsText) : {};
      const res = await api.post('/evidence/sources', {
        source_key: sourceKey,
        source_type: sourceType,
        label,
        description,
        enabled,
        config,
        secrets,
      });
      onSaved(res);
      onClose();
    } catch (err) {
      setError(err.message || 'Unable to save source.');
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={initial ? 'Edit listener' : 'Add listener'} onClose={onClose} width="max-w-2xl">
      <form onSubmit={save} className="flex flex-col gap-4">
        <div className="grid gap-3 md:grid-cols-2">
          <TextField label="Source key" value={sourceKey} onChange={(e) => setSourceKey(e.target.value)} placeholder="bank-main" autoFocus />
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-muted">Source type</span>
            <select value={sourceType} onChange={(e) => setSourceType(e.target.value)}
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-text outline-none focus-visible:border-accent">
              {SOURCE_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
            </select>
          </label>
        </div>
        <TextField label="Label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Main bank account" />
        <TextArea label="Description" value={description} onChange={(e) => setDescription(e.target.value)} rows={3} placeholder="What this listener ingests and why it exists." />
        <label className="flex items-center gap-2 text-sm text-text">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="h-4 w-4 accent-accent" />
          Listener enabled
        </label>
        <TextArea label="Public config JSON" value={configText} onChange={(e) => setConfigText(e.target.value)} rows={6} />
        <TextArea label="Secret config JSON" value={secretsText} onChange={(e) => setSecretsText(e.target.value)} rows={5} placeholder='{"api_key":"..."}' />
        <p className="text-xs text-muted">Secrets are accepted by the backend but never returned to the dashboard. Only secret key names are shown later.</p>
        {error && <p className="text-sm text-red-400">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" type="button" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={saving || !sourceKey.trim() || !label.trim()}>
            {saving ? 'Saving...' : 'Save listener'}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function EvidenceModal({ sources, onClose, onSaved }) {
  const [sourceKey, setSourceKey] = useState(sources[0]?.source_key || '');
  const [title, setTitle] = useState('');
  const [summary, setSummary] = useState('');
  const [occurredAt, setOccurredAt] = useState('');
  const [tags, setTags] = useState('');
  const [integrity, setIntegrity] = useState('1');
  const [rawPayloadText, setRawPayloadText] = useState('{}');
  const [normalizedPayloadText, setNormalizedPayloadText] = useState('{}');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  async function save(e) {
    e.preventDefault();
    setSaving(true);
    setError('');
    try {
      const raw_payload = rawPayloadText.trim() ? JSON.parse(rawPayloadText) : {};
      const normalized_payload = normalizedPayloadText.trim() ? JSON.parse(normalizedPayloadText) : {};
      const res = await api.post('/evidence', {
        source_key: sourceKey,
        title,
        summary,
        occurred_at: occurredAt ? new Date(occurredAt).toISOString() : undefined,
        integrity_confidence: Number(integrity),
        tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
        raw_payload,
        normalized_payload,
      });
      onSaved(res);
      onClose();
    } catch (err) {
      setError(err.message || 'Unable to create evidence.');
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title="Add evidence object" onClose={onClose} width="max-w-2xl">
      <form onSubmit={save} className="flex flex-col gap-4">
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-muted">Source</span>
          <select value={sourceKey} onChange={(e) => setSourceKey(e.target.value)}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-text outline-none focus-visible:border-accent">
            {sources.map((source) => <option key={source.id} value={source.source_key}>{source.label} ({source.source_key})</option>)}
          </select>
        </label>
        <div className="grid gap-3 md:grid-cols-2">
          <TextField label="Title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Domino's charge observed" autoFocus />
          <TextField label="Occurred at" type="datetime-local" value={occurredAt} onChange={(e) => setOccurredAt(e.target.value)} />
        </div>
        <TextArea label="Summary" value={summary} onChange={(e) => setSummary(e.target.value)} rows={4} placeholder="Raw evidence summary as received from the source." />
        <div className="grid gap-3 md:grid-cols-2">
          <TextField label="Tags (comma separated)" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="pizza, bank, food" />
          <TextField label="Integrity confidence" type="number" min="0.01" max="1" step="0.01" value={integrity} onChange={(e) => setIntegrity(e.target.value)} />
        </div>
        <TextArea label="Raw payload JSON" value={rawPayloadText} onChange={(e) => setRawPayloadText(e.target.value)} rows={6} />
        <TextArea label="Normalized payload JSON" value={normalizedPayloadText} onChange={(e) => setNormalizedPayloadText(e.target.value)} rows={6} />
        {error && <p className="text-sm text-red-400">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" type="button" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={saving || !sourceKey || !summary.trim()}>
            {saving ? 'Saving...' : 'Save evidence'}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function ListenerKeyModal({ credential, onClose }) {
  const endpoint = `${window.location.protocol}//${window.location.hostname}:8020/runtime/listeners/${credential.source_key}`;
  return (
    <Modal title="Listener ingest key" onClose={onClose} width="max-w-2xl">
      <p className="mb-4 text-sm text-muted">Copy the new key now. It is shown once, and the previous listener key stopped working immediately.</p>
      <p className="mb-1 text-xs font-medium text-muted">Webhook endpoint</p>
      <code className="mb-4 block overflow-x-auto rounded-md bg-background px-3 py-2 text-xs text-text">POST {endpoint}</code>
      <p className="mb-1 text-xs font-medium text-muted">X-MemoryGate-Listener-Key</p>
      <code className="block overflow-x-auto rounded-md bg-background px-3 py-2 text-xs text-accent">{credential.ingest_key}</code>
      <div className="mt-5 flex justify-end gap-2"><Button variant="secondary" onClick={() => navigator.clipboard.writeText(credential.ingest_key)}><KeyRound size={14} /> Copy key</Button><Button onClick={onClose}>Done</Button></div>
    </Modal>
  );
}

export default function EvidencesScreen() {
  const { agentId } = useAgent();
  const [sources, setSources] = useState(null);
  const [evidence, setEvidence] = useState(null);
  const [analysis, setAnalysis] = useState(null);
  const [sourceFilter, setSourceFilter] = useState('all');
  const [modal, setModal] = useState(null);
  const [listenerCredential, setListenerCredential] = useState(null);

  async function load() {
    const [sourceRes, evidenceRes, analysisRes] = await Promise.all([
      api.get('/evidence/sources').then((r) => r.results),
      api.get('/evidence', undefined, agentId).then((r) => r.results),
      api.get('/evidence/analysis', undefined, agentId).then((r) => r.results),
    ]);
    setSources(sourceRes);
    setEvidence(evidenceRes);
    setAnalysis(analysisRes);
  }

  useEffect(() => {
    load();
  }, [agentId]);

  async function rotateListenerKey(sourceKey) {
    try {
      const credential = await api.post(`/evidence/sources/${sourceKey}/rotate-ingest-key`, {});
      setListenerCredential(credential);
      load();
    } catch (err) {
      window.alert(err.message || 'Unable to rotate the listener key.');
    }
  }

  const filteredEvidence = useMemo(() => {
    if (!evidence) return [];
    return sourceFilter === 'all' ? evidence : evidence.filter((item) => item.source_key === sourceFilter);
  }, [evidence, sourceFilter]);

  if (!sources || !evidence || !analysis) {
    return (
      <div className="p-5 md:p-8">
        <div className="flex items-center gap-2 text-sm text-muted"><Loader2 size={14} className="animate-spin" /> Loading…</div>
      </div>
    );
  }

  return (
    <div className="p-5 md:p-8">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium text-text">Sources & Evidence</h1>
          <p className="mt-1 text-sm text-muted">The raw ingestion side of MemoryGate: listeners, source connectors, and immutable evidence objects before deeper analysis.</p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => setModal({ type: 'source' })}>
            <RadioTower size={15} /> Add listener
          </Button>
          <Button onClick={() => setModal({ type: 'evidence' })} disabled={sources.length === 0}>
            <Plus size={15} /> Add evidence
          </Button>
        </div>
      </div>

      <Section title="Listener sources" note="Connector definitions for automated ingestion. Secrets stay hidden after save, same principle as ToolGate.">
        {sources.length === 0 ? (
          <p className="px-4 py-3 text-sm text-muted">No listeners yet.</p>
        ) : (
          sources.map((source, index) => (
            <div key={source.id} className={`flex flex-wrap items-center gap-3 px-4 py-3 ${index > 0 ? 'border-t border-border' : ''}`}>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-text">{source.label}</span>
                  <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">{source.source_type}</span>
                  {!source.enabled && <span className="rounded-full border border-amber-500/30 px-2 py-0.5 text-[11px] text-amber-300">disabled</span>}
                </div>
                <p className="mt-1 text-xs text-muted">{source.description || source.source_key}</p>
                <p className="mt-1 text-[11px] text-muted">
                  {source.has_secrets ? `Secret keys: ${source.secret_keys.join(', ')}` : 'No secrets stored'}
                  {source.last_ingested_at ? ` · last evidence ${timeAgo(source.last_ingested_at)}` : ''}
                </p>
              </div>
              <Button variant="secondary" className="!py-1.5 text-xs" onClick={() => setModal({ type: 'source', initial: source })}>
                <KeyRound size={13} /> Edit
              </Button>
              <Button variant="secondary" className="!py-1.5 text-xs" onClick={() => rotateListenerKey(source.source_key)}>
                <KeyRound size={13} /> Rotate key
              </Button>
            </div>
          ))
        )}
      </Section>

      <Section title="Evidence objects" note="Immutable source-captured items. This is the raw material layer that later feeds episodes, analysis, and long-term knowledge.">
        <div className="flex items-center gap-3 border-b border-border px-4 py-3">
          <Database size={15} className="text-muted" />
          <select value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)}
            className="rounded-lg border border-border bg-background px-2.5 py-1.5 text-xs text-text outline-none focus-visible:border-accent">
            <option value="all">All sources</option>
            {sources.map((source) => <option key={source.id} value={source.source_key}>{source.label}</option>)}
          </select>
          <span className="ml-auto text-xs text-muted">{filteredEvidence.length} shown</span>
        </div>
        {filteredEvidence.length === 0 ? (
          <p className="px-4 py-3 text-sm text-muted">No evidence objects yet.</p>
        ) : (
          filteredEvidence.map((item, index) => (
            <div key={item.id} className={`px-4 py-3 ${index > 0 ? 'border-t border-border' : ''}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm text-text">{item.title || item.summary}</span>
                <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">{item.source_key}</span>
                <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">{Math.round(item.integrity_confidence * 100)}%</span>
                <span className="ml-auto text-[11px] text-muted">{timeAgo(item.occurred_at)}</span>
              </div>
              {item.title && <p className="mt-1 text-sm text-text/85">{item.summary}</p>}
              <p className="mt-1 text-xs text-muted">Tags: {item.tags.length ? item.tags.join(', ') : 'none'}</p>
            </div>
          ))
        )}
      </Section>

      <Section title="Analysis objects" note="Reserved lineage layer for future analyzers. No deep processing yet, but the UX now reflects where that work will live.">
        {analysis.length === 0 ? (
          <p className="px-4 py-3 text-sm text-muted">No analysis objects yet.</p>
        ) : (
          analysis.map((item, index) => (
            <div key={item.id} className={`px-4 py-3 ${index > 0 ? 'border-t border-border' : ''}`}>
              <div className="flex items-center gap-2">
                <ReceiptText size={14} className="text-muted" />
                <span className="text-sm text-text">{analysisLabel(item.analysis_type)}</span>
                <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">{Math.round(item.confidence * 100)}%</span>
                {answeredBy(item.model, item.provider) && <span className="truncate text-[11px] text-muted">by {answeredBy(item.model, item.provider)}</span>}
                <span className="ml-auto text-[11px] text-muted">{timeAgo(item.created_at)}</span>
              </div>
              <p className="mt-1 text-sm text-text/85">{item.output_summary || item.input_summary || 'No summary yet.'}</p>
            </div>
          ))
        )}
      </Section>

      {modal?.type === 'source' && (
        <SourceModal
          initial={modal.initial}
          onClose={() => setModal(null)}
          onSaved={() => load()}
        />
      )}
      {modal?.type === 'evidence' && (
        <EvidenceModal
          sources={sources}
          onClose={() => setModal(null)}
          onSaved={() => load()}
        />
      )}
      {listenerCredential && <ListenerKeyModal credential={listenerCredential} onClose={() => setListenerCredential(null)} />}
    </div>
  );
}
